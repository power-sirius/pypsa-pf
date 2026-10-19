"""In-memory description of one solved snapshot: the tables sent to PowerFactory.

Units are aggregated per (bus, carrier). Each group keeps:

* ``p_installed``: installed capacity of all its units
* ``p_online``: capacity of the units online at the snapshot; used as the machine
  rating in PowerFactory, so that the kinetic energy H*S matches the inertia
  used to select the snapshot
* ``p`` / ``q``: AC power-flow results (not the linear optimisation values)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CONVERTER
from .network import UNIT_COMPONENTS, _tdict
from .powerflow import PowerFlowSetup, _exported_buses, _row

logger = logging.getLogger(__name__)


@dataclass
class SnapshotModel:
    snapshot: pd.Timestamp
    buses: pd.DataFrame  # incl. geographic position and line charging per bus (for the checks)
    lines: pd.DataFrame  # r, x [ohm], b [S], length, rating
    transformers: pd.DataFrame
    units: pd.DataFrame  # aggregated per (bus, carrier)
    loads: pd.DataFrame  # loads (and link terminals exported as fixed-P loads)
    links: pd.DataFrame = field(default_factory=pd.DataFrame)  # links exported as HVDC (export.links_as hvdc|wecc)

    _TABLES = ("buses", "lines", "transformers", "units", "loads", "links")
    _STR_COLS = {"bus": str, "bus0": str, "bus1": str, "sub_network": str, "carrier": str,
                 "control": str, "converter_control": str, "pf_type": str, "pf_category": str, "kind": str}

    def to_csv(self, folder: str | Path) -> None:
        """Save the model as CSV tables, to inspect them (read back with :meth:`from_csv`)."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        for name in self._TABLES:
            getattr(self, name).to_csv(folder / f"{name}.csv")
        (folder / "snapshot.txt").write_text(str(self.snapshot))
        logger.info("Snapshot model written to %s", folder.resolve())

    @classmethod
    def from_csv(cls, folder: str | Path) -> "SnapshotModel":
        folder = Path(folder)
        tables = {}
        for name in cls._TABLES:
            if not (folder / f"{name}.csv").exists():
                continue
            df = pd.read_csv(folder / f"{name}.csv", index_col=0, dtype=cls._STR_COLS, keep_default_na=False,
                             na_values=[""])
            df.index = df.index.astype(str)
            for col in ("in_service",):
                if col in df:
                    df[col] = df[col].astype(str).str.lower().eq("true")
            tables[name] = df
        return cls(pd.Timestamp((folder / "snapshot.txt").read_text().strip()), **tables)


def _converter_control(units: pd.DataFrame, cfg: dict) -> pd.Series:
    ctrl = units.carrier.map(lambda c: cfg["carriers"][c].get("control", "GFL"))
    for ov in cfg["converter_control"]["overrides"]:
        mask = pd.Series(True, index=units.index)
        if ov.get("carrier"):
            mask &= units.carrier == ov["carrier"]
        if ov.get("bus"):
            mask &= units.bus == ov["bus"]
        if not mask.any():
            logger.warning("Converter-control override %s matched no unit", ov)
        ctrl[mask] = ov["control"]
    return ctrl.where(units["type"] == CONVERTER)


def build_snapshot_model(n, setup: PowerFlowSetup, cfg: dict) -> SnapshotModel:
    s = setup.snapshot
    ecfg = cfg["export"]
    buses_i = _exported_buses(n, cfg)

    # --- buses -------------------------------------------------------------
    buses = pd.DataFrame({
        "v_nom": n.buses.v_nom.reindex(buses_i),
        "x": n.buses.x.reindex(buses_i),
        "y": n.buses.y.reindex(buses_i),
        "v_mag_pu": _row(n.buses_t["v_mag_pu"], s, buses_i),
        "v_ang_deg": np.degrees(_row(n.buses_t["v_ang"], s, buses_i)),
        "sub_network": n.buses.sub_network.reindex(buses_i),
    })

    # --- lines -------------------------------------------------------------
    ln = n.lines[n.lines.bus0.isin(buses_i) & n.lines.bus1.isin(buses_i)]
    lines = pd.DataFrame({
        "bus0": ln.bus0, "bus1": ln.bus1,
        "v_nom": n.buses.v_nom.reindex(ln.bus0).values,
        "r_ohm": ln.r, "x_ohm": ln.x, "b_siemens": ln.b,
        "s_nom": ln.s_nom_opt.where(ln.s_nom_extendable, ln.s_nom),  # optimised capacity
        "length_km": ln.length.where(ln.length > 0, 1.0),
    })
    # pi-model shunt: half of the line susceptance at each end [Mvar at nominal voltage]
    q_half = lines.b_siemens * lines.v_nom ** 2 / 2
    buses["q_charging"] = (q_half.groupby(lines.bus0).sum().reindex(buses_i, fill_value=0.0)
                           + q_half.groupby(lines.bus1).sum().reindex(buses_i, fill_value=0.0))
    if not ecfg["line_charging"]:
        buses["q_charging"] = 0.0
        logger.warning("Line charging disabled: PowerFactory will miss %.0f Mvar that PyPSA includes",
                       q_half.sum() * 2)
    else:
        logger.info("Line charging: %.0f Mvar (at 1 pu), included in the pi-lines", q_half.sum() * 2)

    # --- transformers ------------------------------------------------------
    tr = n.transformers[n.transformers.bus0.isin(buses_i) & n.transformers.bus1.isin(buses_i)]
    transformers = pd.DataFrame({
        "bus0": tr.bus0, "bus1": tr.bus1,
        "v_nom0": n.buses.v_nom.reindex(tr.bus0).values, "v_nom1": n.buses.v_nom.reindex(tr.bus1).values,
        "s_nom": tr.s_nom_opt.where(tr.s_nom_extendable, tr.s_nom) if len(tr) else pd.Series(dtype=float),
        "r_pu": tr.r, "x_pu": tr.x, "tap_ratio": tr.get("tap_ratio", 1.0),
    })
    if len(transformers) and (transformers.tap_ratio != 1).any():
        logger.warning("Transformer tap ratios != 1 are not exported (nominal ratio is used)")
    if len(getattr(n, "shunt_impedances", [])):
        logger.warning("%d shunt impedances are not exported", len(n.shunt_impedances))

    # --- generating units, aggregated per (bus, carrier) ---------------------
    u = setup.units[setup.units.bus.isin(buses_i)].copy()
    u["p"] = pd.concat([_row(_tdict(n, c)["p"], s, u.index[u.component == c]) for c in UNIT_COMPONENTS])
    u["q"] = pd.concat([_row(_tdict(n, c)["q"], s, u.index[u.component == c]) for c in UNIT_COMPONENTS])
    u["kinetic_energy"] = u.H * u.online_capacity
    u["is_slack"] = u.control == "Slack"
    u["is_pv"] = u.control.isin(["PV", "Slack"])
    u["n_online"] = ((u.status > 0) & ~u.is_proxy).astype(int)
    u["n_real"] = (~u.is_proxy).astype(int)

    g = u.groupby(["bus", "carrier"], sort=True)
    units = g.agg(
        type=("type", "first"), component=("component", "first"),
        n_units=("n_real", "sum"), n_online=("n_online", "sum"),
        p_installed=("capacity", "sum"), p_online=("online_capacity", "sum"),
        p=("p", "sum"), q=("q", "sum"), kinetic_energy=("kinetic_energy", "sum"),
        is_slack=("is_slack", "any"), is_pv=("is_pv", "any"),
    ).reset_index()
    units["H"] = units.carrier.map(lambda c: float(cfg["carriers"][c].get("H", 0.0)))
    units["control"] = np.select([units.is_slack, units.is_pv], ["Slack", "PV"], "PQ")
    units["in_service"] = units.p_online > 0
    units["s_rated"] = units.p_online.where(units.in_service, units.p_installed) / ecfg["rated_power_factor"]
    units["v_set"] = units.bus.map(buses.v_mag_pu)
    units["pf_category"] = units.carrier.map(lambda c: cfg["carriers"][c].get("pf_category", "stat"))
    units["pf_type"] = units.carrier.map(lambda c: cfg["carriers"][c].get("pf_type", c))
    units["sc_ratio"] = units.carrier.map(lambda c: float(cfg["carriers"][c].get("sc_ratio", 1.5)))
    units["converter_control"] = _converter_control(units, cfg)
    # a converter acting as slack means its sub-network has no synchronous machine online:
    # it must form the grid (needed for RMS simulations)
    if cfg["converter_control"].get("gfm_if_no_sync", True):
        conv_slack = (units.control == "Slack") & (units["type"] == CONVERTER)
        units.loc[conv_slack, "converter_control"] = "GFM"
        for _, u in units[conv_slack].iterrows():
            logger.info("No synchronous machine online in the sub-network of %s: %s %s set to GFM",
                        u.bus, u.carrier, u.bus)
    units["name"] = units.carrier + " " + units.bus
    units = units.drop(columns=["is_slack", "is_pv"]).set_index("name")

    small = (units.p_installed < ecfg["min_capacity_mw"]) & (units.control == "PQ")  # never drop PV/slack
    if small.any():
        logger.warning("Dropping %d groups below %.1f MW (P = %.1f MW, Q = %.1f Mvar will move to the slack)",
                       small.sum(), ecfg["min_capacity_mw"], units.p[small].sum(), units.q[small].sum())
        units = units[~small]
    off_with_power = units[~units.in_service & ((units.p.abs() > 1e-3) | (units.q.abs() > 1e-3))]
    if len(off_with_power):
        logger.warning("Out-of-service groups carrying power (check!):\n%s", off_with_power[["p", "q"]].to_string())
    _check_q_capability(units)

    # --- loads (+ links represented as fixed P injections) -------------------
    ld = n.loads[n.loads.bus.isin(buses_i)]
    loads = pd.DataFrame({
        "bus": ld.bus, "p": _row(n.loads_t["p"], s, ld.index), "q": _row(n.loads_t["q"], s, ld.index),
    }).groupby("bus", as_index=False).sum()
    loads.index = pd.Index("Load " + loads.bus, name="name")
    loads["kind"] = "load"

    link_rows, hvdc_rows = [], []
    links_as = ecfg.get("links_as", "hvdc")
    as_hvdc = links_as in ("hvdc", "wecc")
    for name, link in n.links.iterrows():
        p0 = float(_row(n.links_t["p0"], s).get(name, 0.0))
        p1 = float(_row(n.links_t["p1"], s).get(name, 0.0))
        if as_hvdc and link.bus0 in buses_i and link.bus1 in buses_i:
            p_nom = link.p_nom_opt if link.get("p_nom_extendable", False) else link.p_nom
            length = link.get("length", 0.0)
            hvdc_rows.append({"name": str(name), "bus0": link.bus0, "bus1": link.bus1, "p0": p0, "p1": p1,
                              "p_nom": float(max(p_nom, abs(p0), abs(p1), 1.0)),
                              "length_km": float(length) if pd.notna(length) and length > 0 else 1.0})
            continue
        for end, p_end in (("0", p0), ("1", p1)):
            bus = link[f"bus{end}"]
            if bus in buses_i:
                link_rows.append({"name": f"Link {name} @{bus}", "bus": bus, "p": p_end, "q": 0.0, "kind": "link"})
    links = pd.DataFrame(hvdc_rows, columns=["name", "bus0", "bus1", "p0", "p1", "p_nom", "length_km"]).set_index("name")
    if len(links):
        how = ("WECC VSC-HVDC model (static generator - model - static generator)" if links_as == "wecc"
               else "HVDC (converter - DC line - converter)")
        logger.info("%d links exported as %s: %s", len(links), how,
                    {k: f"{r.bus0} -> {r.bus1}, {r.p0:.1f} MW" for k, r in links.iterrows()})
    if link_rows:
        loads = pd.concat([loads, pd.DataFrame(link_rows).set_index("name")])
        logger.info("%d link terminals exported as fixed-P loads (P > 0 = withdrawal)", len(link_rows))

    model = SnapshotModel(s, buses, lines, transformers, units, loads, links)
    log_model_summary(model)
    return model


def _check_q_capability(units: pd.DataFrame) -> None:
    """Per bus: Q of voltage-controlling groups vs their capability sqrt(S^2 - P^2).

    PyPSA has no Q limits and gives the whole bus Q to one generator, while
    PowerFactory shares it between the constv machines of the bus, so the
    check is done on the bus totals.
    """
    pv = units[units.in_service & units.control.isin(["PV", "Slack"])]
    if pv.empty:
        return
    cap = np.sqrt(np.clip(pv.s_rated ** 2 - pv.p ** 2, 0, None))
    per_bus = pd.DataFrame({"Q [Mvar]": pv.q.groupby(pv.bus).sum(), "Q capability [Mvar]": cap.groupby(pv.bus).sum()})
    per_bus["ratio"] = per_bus["Q [Mvar]"].abs() / per_bus["Q capability [Mvar]"].where(lambda c: c > 0)
    bad = per_bus[(per_bus.ratio > 1.0) | per_bus.ratio.isna() & (per_bus["Q [Mvar]"].abs() > 1)]
    if len(bad):
        logger.warning(
            "%d buses need more Q than their voltage-controlling machines can give (the power flow has no "
            "Q limits; consider letting converters control voltage, carriers.<c>.pf_control: PV):\n%s",
            len(bad), bad.sort_values("ratio", ascending=False).round(1).to_string())


def log_model_summary(m: SnapshotModel) -> None:
    on = m.units[m.units.in_service]
    logger.info(
        "Snapshot model %s: %d buses, %d lines, %d transformers, %d unit groups (%d in service), %d loads/links",
        m.snapshot, len(m.buses), len(m.lines), len(m.transformers), len(m.units), len(on), len(m.loads),
    )
    p_gen, q_gen = m.units.p.sum(), m.units.q.sum()
    p_ld, q_ld = m.loads.p.sum(), m.loads.q.sum()
    if len(m.links):
        p_ld += m.links.p0.sum() + m.links.p1.sum()  # HVDC losses (0 for a lossless link)
    logger.info("Exported balance: units P = %.1f MW, Q = %.1f Mvar | loads+links P = %.1f MW, Q = %.1f Mvar | "
                "P mismatch (= line losses) %.1f MW | line charging %.0f Mvar",
                p_gen, q_gen, p_ld, q_ld, p_gen - p_ld, m.buses.q_charging.sum())
    if p_gen - p_ld < -1e-3 or (p_gen - p_ld) > 0.05 * max(p_ld, 1.0):
        logger.warning("Active-power mismatch of %.1f MW is not plausible as losses: check the dispatch",
                       p_gen - p_ld)
    e_kin = on.kinetic_energy.sum()
    s_sync = on[on.type != CONVERTER].p_online.sum()
    s_conv = on[on.type == CONVERTER].p.abs().sum()
    logger.info("Exported kinetic energy %.1f GWs, equivalent H = %.2f s", e_kin / 1e3,
                e_kin / (s_sync + s_conv) if s_sync + s_conv > 0 else float("nan"))
    tbl = m.units[["control", "converter_control", "in_service", "p_online", "p", "q"]]
    logger.info("Unit groups:\n%s", tbl.round(1).to_string())


_PF_BAD = re.compile(r"[^A-Za-z0-9 _\-()]")


def pf_name(name: str, used: set[str], maxlen: int = 40) -> str:
    """PowerFactory-safe, unique object name."""
    base = _PF_BAD.sub("_", str(name)).strip()[:maxlen] or "obj"
    out, k = base, 1
    while out in used:
        suffix = f"_{k}"
        out, k = base[: maxlen - len(suffix)] + suffix, k + 1
    used.add(out)
    return out
