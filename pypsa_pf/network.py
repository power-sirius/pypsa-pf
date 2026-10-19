"""Loading and cleaning a solved PyPSA(-Eur) network, plus unit-level helpers.

"Units" are all generating devices: PyPSA ``Generator`` and ``StorageUnit``
components, handled together with one row per device.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import CONVERTER, SYNCHRONOUS

logger = logging.getLogger(__name__)

UNIT_COMPONENTS = {"Generator": "generators", "StorageUnit": "storage_units"}


# ---------------------------------------------------------------------------
# loading / cleaning
# ---------------------------------------------------------------------------
def load_network(path: str):
    """Read a PyPSA-Eur result file (``*.nc``) and remove unusable objects."""
    import pypsa  # imported lazily so the rest of the package works without it

    logger.info("Loading network %s", path)
    n = pypsa.Network(path)
    clean_network(n)
    log_network_summary(n)
    return n


def _remove(n, component: str, names) -> None:
    names = list(names)
    if not names:
        return
    if hasattr(n, "mremove"):  # PyPSA < 0.33
        n.mremove(component, names)
    else:
        n.remove(component, names)


def clean_network(n) -> None:
    """Drop lines with zero capacity or invalid impedance, and units without p_nom.

    Done with ``n.remove`` so the time-series tables stay consistent.
    """
    bad_lines = n.lines.index[(n.lines.s_nom == 0) & ~n.lines.s_nom_extendable
                              | ~np.isfinite(n.lines.x) | ~np.isfinite(n.lines.r)]
    if len(bad_lines):
        logger.warning("Removing %d lines with s_nom=0 or invalid r/x: %s", len(bad_lines), list(bad_lines))
    _remove(n, "Line", bad_lines)

    for comp, attr in UNIT_COMPONENTS.items():
        df = getattr(n, attr)
        bad = df.index[df.p_nom.isna()]
        if len(bad):
            logger.warning("Removing %d %s with NaN p_nom", len(bad), comp)
        _remove(n, comp, bad)


def log_network_summary(n) -> None:
    logger.info(
        "Network: %d buses, %d lines, %d transformers, %d links, %d loads, %d generators, "
        "%d storage units, %d snapshots (%s -> %s)",
        len(n.buses), len(n.lines), len(n.transformers), len(n.links), len(n.loads),
        len(n.generators), len(n.storage_units), len(n.snapshots), n.snapshots[0], n.snapshots[-1],
    )
    cap = pd.concat([
        capacity(n.generators).groupby(n.generators.carrier).sum(),
        capacity(n.storage_units).groupby(n.storage_units.carrier).sum(),
    ]).groupby(level=0).sum()
    logger.info("Installed capacity by carrier [GW]:\n%s", (cap / 1e3).round(2).to_string())


# ---------------------------------------------------------------------------
# unit helpers
# ---------------------------------------------------------------------------
def capacity(df: pd.DataFrame) -> pd.Series:
    """Installed power [MW]: optimised value for extendable units, p_nom otherwise.

    Extendable units (e.g. batteries in PyPSA-Eur) can have p_nom = 0: their real
    capacity is p_nom_opt.
    """
    if "p_nom_opt" not in df:
        return df.p_nom.astype(float)
    ext = df.get("p_nom_extendable", pd.Series(False, index=df.index)).astype(bool)
    opt = df.p_nom_opt.where(df.p_nom_opt.notna() & (df.p_nom_opt > 0), df.p_nom)
    return opt.where(ext, df.p_nom).astype(float)


def carrier_attrs(carrier: str, cfg: dict) -> dict:
    try:
        return cfg["carriers"][carrier]
    except KeyError:
        raise KeyError(
            f"Carrier '{carrier}' is not defined in the config. Add it under 'carriers:' "
            f"(type: synchronous|converter, H, pf_control) or list it in 'ignore_carriers:'."
        ) from None


def unit_table(n, cfg: dict) -> pd.DataFrame:
    """Static table of all generating units (generators + storage units)."""
    frames = []
    for comp, attr in UNIT_COMPONENTS.items():
        df = getattr(n, attr)
        frames.append(pd.DataFrame({
            "component": comp,
            "bus": df.bus,
            "carrier": df.carrier,
            "capacity": capacity(df),
            "committable": df["committable"].astype(bool) if "committable" in df else False,
        }, index=df.index))
    units = pd.concat(frames)
    if units.index.duplicated().any():
        raise ValueError(f"Duplicated unit names: {list(units.index[units.index.duplicated()])}")

    units = units[~units.carrier.isin(cfg["ignore_carriers"])].copy()
    unknown = sorted(set(units.carrier) - set(cfg["carriers"]))
    if unknown:
        raise KeyError(f"Carriers missing from config 'carriers:' section: {unknown}")

    units["type"] = units.carrier.map(lambda c: cfg["carriers"][c]["type"])
    units["H"] = units.carrier.map(lambda c: float(cfg["carriers"][c].get("H", 0.0)))
    units["pf_control"] = units.carrier.map(lambda c: cfg["carriers"][c].get("pf_control", "PQ"))
    return units


def _tdict(n, comp: str):
    return getattr(n, UNIT_COMPONENTS[comp] + "_t")


def unit_series(n, attr: str, units: pd.DataFrame, snapshots=None, fill: float = 0.0) -> pd.DataFrame:
    """Time series ``attr`` (p, q, status, ...) for all units, snapshots x units."""
    snapshots = n.snapshots if snapshots is None else snapshots
    frames = []
    for comp in UNIT_COMPONENTS:
        idx = units.index[units.component == comp]
        df = _tdict(n, comp).get(attr)
        if df is None:
            df = pd.DataFrame(index=snapshots)
        frames.append(df.reindex(index=snapshots, columns=idx).astype(float).fillna(fill))
    return pd.concat(frames, axis=1)


def unit_status(n, units: pd.DataFrame, snapshots=None, cfg: dict | None = None) -> pd.DataFrame:
    """On/off state u_{c,t}: unit-commitment status for committable units.

    Non-committable units have no status in PyPSA: they are online (1), unless
    ``units_status.idle_offline`` is set, then they are offline whenever their
    dispatch is below ``idle_tol_mw``.
    """
    status = unit_series(n, "status", units, snapshots, fill=1.0)
    noncommit = ~units.committable.reindex(status.columns).values
    status.loc[:, noncommit] = 1.0
    scfg = (cfg or {}).get("units_status", {})
    if scfg.get("idle_offline", False):
        p = unit_series(n, "p", units, status.index)
        idle = (p.abs() < scfg.get("idle_tol_mw", 0.1)) & noncommit
        status = status.mask(idle, 0.0)
    return status.clip(0, 1)


def is_synchronous(units: pd.DataFrame) -> pd.Series:
    return units["type"] == SYNCHRONOUS


def is_converter(units: pd.DataFrame) -> pd.Series:
    return units["type"] == CONVERTER
