"""Equivalent inertia and selection of the snapshot to export.

Equation (1) of Rios-Penaloza et al., "Dynamic Stability Considerations During
Capacity Expansion Planning":

    H_{b,t} = sum_c H_c * S_{c,t} / sum_c S_{c,t}

with S_{c,t} = installed power * u_{c,t} for synchronous units (u = on/off
status) and S_{c,t} = |p_{c,t}| for converter-interfaced units, so that idle
PV at night does not artificially lower the equivalent inertia.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .network import is_synchronous, unit_series, unit_status, unit_table

logger = logging.getLogger(__name__)


@dataclass
class InertiaResult:
    bus_H: pd.DataFrame  # snapshots x buses, equivalent inertia constant [s]
    system_H: pd.Series  # snapshots, system equivalent inertia constant [s]
    kinetic_energy: pd.Series  # snapshots, sum H*S of all units [MWs]


def compute_inertia(n, cfg: dict) -> InertiaResult:
    units = unit_table(n, cfg)
    status = unit_status(n, units, cfg=cfg)
    p = unit_series(n, "p", units)

    sync = is_synchronous(units)
    weight = p.abs()  # converter units: |p|
    weight.loc[:, sync.values] = status.loc[:, sync.values].mul(units.capacity[sync], axis=1)

    num = weight.mul(units.H, axis=1)
    bus_of = units.bus.reindex(weight.columns)
    num_bus = num.T.groupby(bus_of).sum().T
    den_bus = weight.T.groupby(bus_of).sum().T

    bus_H = (num_bus / den_bus.where(den_bus > 0)).reindex(columns=n.buses.index)
    system_H = num.sum(axis=1) / weight.sum(axis=1).where(lambda s: s > 0)
    return InertiaResult(bus_H=bus_H, system_H=system_H, kinetic_energy=num.sum(axis=1))


def select_snapshot(n, cfg: dict, inertia: InertiaResult | None = None) -> pd.Timestamp:
    """Return the snapshot to export, either fixed or the one with minimum inertia."""
    scfg = cfg["snapshot"]
    if scfg["method"] == "timestamp":
        ts = pd.Timestamp(scfg["timestamp"])
        if ts not in n.snapshots:
            near = n.snapshots[n.snapshots.get_indexer([ts], method="nearest")[0]]
            raise ValueError(f"Snapshot {ts} not in network (closest: {near})")
        logger.info("Using fixed snapshot %s", ts)
        return ts

    inertia = inertia or compute_inertia(n, cfg)
    H = inertia.system_H.dropna()
    if scfg.get("start") or scfg.get("end"):
        H = H.loc[scfg.get("start"):scfg.get("end")]
    if scfg.get("hours"):
        h0, h1 = scfg["hours"]
        H = H[(H.index.hour >= h0) & (H.index.hour <= h1)]
    if H.empty:
        raise ValueError("No snapshot left after applying the snapshot search window")

    logger.info(
        "System equivalent inertia: min %.2f s, mean %.2f s, max %.2f s "
        "(kinetic energy: min %.1f GWs, mean %.1f GWs)",
        H.min(), H.mean(), H.max(),
        inertia.kinetic_energy.min() / 1e3, inertia.kinetic_energy.mean() / 1e3,
    )
    lowest = pd.DataFrame({"H [s]": H.nsmallest(5),
                           "E_kin [GWs]": inertia.kinetic_energy.reindex(H.nsmallest(5).index) / 1e3})
    logger.info("Five lowest-inertia snapshots:\n%s", lowest.round(2).to_string())
    ts = H.idxmin()
    logger.info("Selected snapshot %s (H = %.2f s)", ts, H.min())
    logger.info("Bus inertia at selected snapshot [s]:\n%s",
                inertia.bus_H.loc[ts].round(2).sort_values().to_string())
    return ts
