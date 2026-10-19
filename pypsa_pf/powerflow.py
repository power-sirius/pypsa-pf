"""Non-linear (AC) power flow of one snapshot with PyPSA.

The dispatch of PyPSA-Eur comes from a linearised optimisation, so it is solved
again with a full AC power flow before being sent to PowerFactory. Set-up:

* Every unit gets the PV/PQ control of its carrier (``carriers.<c>.pf_control``).
* Units offline at the snapshot (unit-commitment status 0, or zero capacity)
  are PQ with Q = 0: they are out of service in PowerFactory too.
* (bus, carrier) groups below ``export.min_pv_capacity_mw`` do not control voltage.
* PyPSA decides PV buses only from ``Generator`` components and ignores the
  control of ``StorageUnit`` components (e.g. hydro). For every bus where only
  storage units control the voltage, a zero-power "voltage-control proxy"
  Generator is added, so PyPSA and PowerFactory solve the same problem; its Q
  is reported under the storage carrier.
* One slack per AC sub-network: the largest online synchronous generator
  (a converter only if the sub-network has no synchronous machine online).
* PQ units get Q = 0; loads get Q = P * tan(phi) (``power_flow.load_reactive``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .network import UNIT_COMPONENTS, _tdict, is_synchronous, unit_status, unit_table

logger = logging.getLogger(__name__)


@dataclass
class PowerFlowSetup:
    snapshot: pd.Timestamp
    units: pd.DataFrame  # unit_table + status/online_capacity/control at the snapshot
    slack: dict[str, str] = field(default_factory=dict)  # sub_network -> unit name


def _exported_buses(n, cfg) -> pd.Index:
    return n.buses.index[n.buses.carrier.isin(cfg["export"]["bus_carriers"])]


def prepare_power_flow(n, snapshot, cfg: dict) -> PowerFlowSetup:
    """Write controls and set-points for the AC power flow into ``n`` (in place)."""
    snapshot = pd.Timestamp(snapshot)
    units = unit_table(n, cfg)
    units["status"] = unit_status(n, units, pd.Index([snapshot]), cfg).iloc[0]
    units["online_capacity"] = units.capacity * units.status

    # --- controls ---------------------------------------------------------
    units["control"] = units.pf_control
    offline = units.online_capacity <= 0
    units.loc[offline, "control"] = "PQ"
    n_off_pv = int((offline & (units.pf_control == "PV")).sum())
    if n_off_pv:
        logger.info("%d offline voltage-controlling units switched to PQ (Q = 0)", n_off_pv)

    # small (bus, carrier) groups do not control voltage (a few MW cannot hold a transmission bus)
    min_pv = cfg["export"].get("min_pv_capacity_mw", 0.0)
    group_cap = units.groupby(["bus", "carrier"]).online_capacity.transform("sum")
    small_pv = (units.control == "PV") & (group_cap < min_pv)
    units.loc[small_pv, "control"] = "PQ"
    if small_pv.any():
        logger.info("%d units in groups below %.0f MW do not control voltage (PQ, Q = 0)", small_pv.sum(), min_pv)

    n.determine_network_topology()
    bus_sn = n.buses.sub_network
    slack = {}
    gens = units[(units.component == "Generator") & (units.online_capacity > 0)]
    for sn in sorted(set(bus_sn.reindex(_exported_buses(n, cfg)).dropna())):
        cand = gens[gens.bus.map(bus_sn) == sn]
        if cand.empty:
            logger.warning("Sub-network %s has no online generator; PyPSA will pick its own slack", sn)
            continue
        sync = cand[is_synchronous(cand)]
        if sync.empty:
            logger.warning("Sub-network %s has no online synchronous generator; "
                           "using a converter-interfaced unit as slack", sn)
        pick = (sync if not sync.empty else cand).online_capacity.idxmax()
        units.loc[pick, "control"] = "Slack"
        slack[sn] = pick
    logger.info("Slack per sub-network: %s",
                {sn: f"{u} ({units.at[u, 'carrier']}, {units.at[u, 'online_capacity']:.0f} MW)"
                 for sn, u in slack.items()})

    units["is_proxy"] = False
    proxies = _add_voltage_control_proxies(n, units)
    if proxies is not None:
        units = pd.concat([units, proxies])

    for comp, attr in UNIT_COMPONENTS.items():
        df = getattr(n, attr)
        df["control"] = units.control.reindex(df.index).fillna("PQ")  # ignored carriers -> PQ

    # --- set-points -------------------------------------------------------
    for comp, attr in UNIT_COMPONENTS.items():
        t = _tdict(n, comp)
        idx = getattr(n, attr).index
        p = t["p"].reindex(index=n.snapshots, columns=idx).fillna(0.0)
        if comp == "StorageUnit":  # storage without capacity must not dispatch
            no_cap = units.capacity.reindex(idx).fillna(0.0) <= 0
            p.loc[:, no_cap.values] = 0.0
        t["p_set"] = p
        t["q_set"] = pd.DataFrame(0.0, index=n.snapshots, columns=idx)

    p_load = n.loads_t["p"] if not n.loads_t["p"].empty else n.loads_t["p_set"]
    p_load = p_load.reindex(index=n.snapshots, columns=n.loads.index).fillna(0.0)
    n.loads_t["p_set"] = p_load
    n.loads_t["q_set"] = p_load * _load_tan_phi(p_load, cfg["power_flow"]["load_reactive"])

    if len(n.links):
        n.links_t["p_set"] = n.links_t["p0"].reindex(index=n.snapshots, columns=n.links.index).fillna(0.0)

    return PowerFlowSetup(snapshot=snapshot, units=units, slack=slack)


def _add_voltage_control_proxies(n, units: pd.DataFrame) -> pd.DataFrame:
    """Zero-power PV generators at buses whose only voltage control is a StorageUnit."""
    pv = units[units.control.isin(["PV", "Slack"])]
    gen_pv_buses = set(pv.bus[pv.component == "Generator"])
    sto_pv = pv[(pv.component == "StorageUnit") & ~pv.bus.isin(gen_pv_buses)]
    rows = []
    for bus, grp in sto_pv.groupby("bus"):
        carrier = grp.online_capacity.groupby(grp.carrier).sum().idxmax()
        name = f"{bus} {carrier} voltage control"
        n.add("Generator", name, bus=bus, carrier=carrier, control="PV", p_nom=0.0)
        ref = grp[grp.carrier == carrier].iloc[0]
        rows.append({**ref.to_dict(), "component": "Generator", "capacity": 0.0, "committable": False,
                     "status": 1.0, "online_capacity": 0.0, "control": "PV", "is_proxy": True, "_name": name})
    if rows:
        logger.info("Added %d voltage-control proxies for storage-only PV buses: %s",
                    len(rows), [r["bus"] for r in rows])
        return pd.DataFrame(rows).set_index("_name").rename_axis(None)
    return None


def _load_tan_phi(p_load: pd.DataFrame, lcfg: dict):
    if lcfg["method"] == "tan_phi":
        return float(lcfg["tan_phi"])
    if lcfg["method"] == "random":
        rng = np.random.default_rng(lcfg.get("seed"))
        lo, hi = lcfg["range"]
        return pd.DataFrame(rng.uniform(lo, hi, size=p_load.shape), index=p_load.index, columns=p_load.columns)
    raise ValueError("power_flow.load_reactive.method must be 'tan_phi' or 'random'")


def run_power_flow(n, setup: PowerFlowSetup, cfg: dict) -> None:
    """Solve the AC power flow for the selected snapshot and log a check report."""
    pcfg = cfg["power_flow"]
    for name in ("pypsa", "pypsa.pf", "pypsa.network.power_flow"):
        logging.getLogger(name).setLevel(logging.WARNING)

    info = n.pf(snapshots=[setup.snapshot], x_tol=pcfg["x_tol"], distribute_slack=pcfg["distribute_slack"])
    converged = info["converged"]
    conv = np.asarray(converged, dtype=bool)
    if not conv.all():
        raise RuntimeError(f"AC power flow did NOT converge at {setup.snapshot}:\n{converged}")
    n_iter = np.asarray(info["n_iter"]).max() if "n_iter" in info else "?"
    logger.info("AC power flow converged at %s (max %s iterations)", setup.snapshot, n_iter)
    report_power_flow(n, setup, cfg)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def _row(df, snapshot, columns=None):
    row = df.loc[snapshot] if snapshot in df.index else pd.Series(dtype=float)
    return row.reindex(columns).fillna(0.0) if columns is not None else row


def report_power_flow(n, setup: PowerFlowSetup, cfg: dict) -> dict:
    s, units = setup.snapshot, setup.units
    pcfg = cfg["power_flow"]

    p = pd.concat([_row(_tdict(n, c)["p"], s, units.index[units.component == c]) for c in UNIT_COMPONENTS])
    q = pd.concat([_row(_tdict(n, c)["q"], s, units.index[units.component == c]) for c in UNIT_COMPONENTS])
    p_set = pd.concat([_row(_tdict(n, c)["p_set"], s, units.index[units.component == c]) for c in UNIT_COMPONENTS])

    by_carrier = pd.DataFrame({
        "installed [MW]": units.capacity, "online [MW]": units.online_capacity,
        "P [MW]": p, "Q [Mvar]": q,
    }).groupby(units.carrier).sum()
    logger.info("Dispatch at %s by carrier:\n%s", s, by_carrier.round(1).to_string())

    p_load, q_load = _row(n.loads_t["p"], s).sum(), _row(n.loads_t["q"], s).sum()
    p_link = (_row(n.links_t["p0"], s).sum() + _row(n.links_t["p1"], s).sum()) if len(n.links) else 0.0
    p_loss = (_row(n.lines_t["p0"], s) + _row(n.lines_t["p1"], s)).sum()
    q_loss = (_row(n.lines_t["q0"], s) + _row(n.lines_t["q1"], s)).sum()
    if len(n.transformers):
        p_loss += (_row(n.transformers_t["p0"], s) + _row(n.transformers_t["p1"], s)).sum()
        q_loss += (_row(n.transformers_t["q0"], s) + _row(n.transformers_t["q1"], s)).sum()
    logger.info(
        "Balance: generation P = %.1f MW, Q = %.1f Mvar | load P = %.1f MW, Q = %.1f Mvar | "
        "link losses = %.1f MW | branch losses P = %.1f MW, Q = %.1f Mvar (negative = net charging)",
        p.sum(), q.sum(), p_load, q_load, p_link, p_loss, q_loss,
    )
    redispatch = (p - p_set)
    logger.info("Slack redispatch: total %.1f MW; largest changes:\n%s", redispatch.sum(),
                redispatch[redispatch.abs() > 1e-3].abs().nlargest(5).round(1).to_string())

    bad_q = units.index[(units.online_capacity <= 0) & ~units.is_proxy & (q.abs() > 1e-3)]
    if len(bad_q):
        logger.warning("Offline units with non-zero Q (should not happen): %s", list(bad_q))

    exported = _exported_buses(n, cfg)
    v = _row(n.buses_t["v_mag_pu"], s, exported)
    lo, hi = pcfg["voltage_limits"]
    logger.info("Bus voltages: min %.4f pu (%s), max %.4f pu (%s)", v.min(), v.idxmin(), v.max(), v.idxmax())
    out = v[(v < lo) | (v > hi)]
    if len(out):
        logger.warning("Buses outside [%s, %s] pu:\n%s", lo, hi, out.round(4).to_string())

    if len(n.lines):
        s_nom = n.lines.s_nom_opt.where(n.lines.s_nom_extendable, n.lines.s_nom)
        s0 = np.hypot(_row(n.lines_t["p0"], s), _row(n.lines_t["q0"], s))
        s1 = np.hypot(_row(n.lines_t["p1"], s), _row(n.lines_t["q1"], s))
        loading = np.maximum(s0, s1) / s_nom
        logger.info("Most loaded lines [%% of s_nom]:\n%s", (100 * loading.nlargest(5)).round(1).to_string())
        over = loading[loading > pcfg["max_line_loading"]]
        if len(over):
            logger.warning("%d lines above %.0f%% loading: %s", len(over), 100 * pcfg["max_line_loading"],
                           list(over.index))
    return {"p": p, "q": q, "v": v}
