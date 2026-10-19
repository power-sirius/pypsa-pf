"""Build the PowerFactory grid from a :class:`SnapshotModel`.

==========================  =================================================================
PyPSA                       PowerFactory
==========================  =================================================================
AC bus                      ElmTerm + TermStrip symbol, placed by its coordinates
Line                        ElmLne + TypLne (pi-model with line charging; r0/x0 = 4 r/x)
synchronous (bus, carrier)  ElmSym; TypSym copied from the library template of the carrier
                            (carriers.<c>.pf_type), sgn = capacity online; plant model
                            (governor, AVR, ...) copied from a reference composite
converter (bus, carrier)    ElmGenstat; ctrlStruct GFL 0 / GFM 1, Kpf = sgn/(droop*f) [MW/Hz],
                            short-circuit model 'syme' with Skss = sc_ratio*sgn; optional
                            composite model per strategy (e.g. a VSM for GFM)
Load                        ElmLod (P, Q from the AC power flow)
Link between AC buses       export.links_as: hvdc (ElmVscmono - DC line - ElmVscmono),
                            wecc (two ElmGenstat + WECC VSC-HVDC composite), loads (ElmLod)
voltage-controlling group   ElmShnt next to it taking over its Q (reactive_compensation)
every connection            StaCubic + closed circuit breaker (StaSwitch)
==========================  =================================================================

Slack (ip_ctrl) and PV/PQ (av_mode) come from the PyPSA power flow. After building, the
PowerFactory load flow is run and compared with PyPSA.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import logging
import sys
import time

import numpy as np
import pandas as pd

from .config import CONVERTER
from .snapshot import SnapshotModel, pf_name

logger = logging.getLogger(__name__)
_warned: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------
def connect(cfg: dict):
    pcfg = cfg["powerfactory"]
    if pcfg.get("python_path") and pcfg["python_path"] not in sys.path:
        sys.path.append(pcfg["python_path"])
    import powerfactory as pf  # noqa: PLC0415  (only available with PowerFactory installed)

    # Only ONE start attempt per process: a failed GetApplication() followed by
    # GetApplicationExt() can kill the Python process without any message.
    inside_pf = "powerfactory" in os.path.basename(sys.executable).lower()
    logger.info("Starting PowerFactory (%s)...", "inside PowerFactory" if inside_pf else "external Python")
    if inside_pf or not hasattr(pf, "GetApplicationExt"):
        app = pf.GetApplication()
    else:
        try:
            app = pf.GetApplicationExt()
        except pf.ExitError as exc:
            raise RuntimeError(f"PowerFactory refused to start (ExitError code {exc.code}). "
                               "Close any open PowerFactory window and try again.") from exc
    if app is None:
        raise RuntimeError("Failed to connect to PowerFactory.")
    if pcfg.get("show_window", False):  # showing the GUI makes the build much slower
        app.Show()
    app.ClearOutputWindow()

    if pcfg.get("project"):
        if app.ActivateProject(pcfg["project"]) != 0:
            raise RuntimeError(f"Could not activate PowerFactory project '{pcfg['project']}' "
                               "(use the name shown in the Data Manager, or its full path)")
    project = app.GetActiveProject()
    if project is None:
        raise RuntimeError("No active project: set powerfactory.project in the config")

    if app.GetActiveStudyCase() is None:  # started from outside: activate the first study case
        cases = app.GetProjectFolder("study").GetContents("*.IntCase", 1)
        if not cases:
            raise RuntimeError(f"Project '{project.loc_name}' has no study case; create one first")
        cases[0].Activate()
        logger.info("Activated study case '%s'", cases[0].loc_name)
    logger.info("Connected to PowerFactory, project '%s', study case '%s'",
                project.loc_name, app.GetActiveStudyCase().loc_name)
    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _set(obj, attr: str, value) -> None:
    """Set a PF attribute; warn once (instead of crashing) if this PF version lacks it."""
    try:
        setattr(obj, attr, value)
    except Exception as exc:  # AttributeError / TypeError from the PF API
        key = (obj.GetClassName(), attr)
        if key not in _warned:
            _warned.add(key)
            logger.warning("Could not set %s.%s = %r (%s)", *key, value, exc)


def _get_grid(app, cfg: dict):
    pcfg = cfg["powerfactory"]
    project = app.GetActiveProject()
    grids = [g for g in project.GetContents("*.ElmNet", 1) if g.loc_name == pcfg["grid_name"]]
    if grids:
        grid = grids[0]
        if pcfg["clear_grid"]:
            for obj in grid.GetContents():
                obj.Delete()
            logger.info("Cleared existing grid '%s'", grid.loc_name)
        elif grid.GetContents():
            raise RuntimeError(f"Grid '{grid.loc_name}' is not empty and powerfactory.clear_grid is false")
    else:
        grid = app.GetProjectFolder("netdat").CreateObject("ElmNet", pcfg["grid_name"])
        _set(grid, "frnom", pcfg["frequency"])
        logger.info("Created grid '%s'", grid.loc_name)
    grid.Activate()
    return grid


def _clear_study_case(app, cfg: dict) -> None:
    """Delete the simulation events and the result variables (IntMon) of the active study
    case: they point to the elements of the previous build, which are deleted."""
    pcfg, case = cfg["powerfactory"], app.GetActiveStudyCase()
    if case is None:
        return
    n_evt = n_mon = 0
    if pcfg.get("clear_events", True):
        for folder in case.GetContents("*.IntEvt", 1):
            for evt in folder.GetContents():
                evt.Delete()
                n_evt += 1
    if pcfg.get("clear_result_variables", True):
        for res in case.GetContents("*.ElmRes", 1):
            for mon in res.GetContents("*.IntMon"):
                mon.Delete()
                n_mon += 1
    logger.info("Study case '%s': deleted %d simulation events and %d result-variable sets",
                case.loc_name, n_evt, n_mon)


def _get_type_folder(app, cfg: dict):
    """Folder (inside the Equipment Type Library) holding the per-machine TypSym copies."""
    library = app.GetProjectFolder("equip")
    name = f"{cfg['powerfactory']['grid_name']} types"
    for old in library.GetContents(f"{name}.IntFolder"):
        old.Delete()
    return library, library.CreateObject("IntFolder", name)


def _find_template(library, name: str):
    found = library.GetContents(f"{name}.TypSym", 1)
    return found[0] if found else None


def _try(app, method: str, *args):
    """Call an optional Application method (not all PowerFactory versions have all of them)."""
    fn = getattr(app, method, None)
    if fn is None:
        return None
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s%s failed: %s", method, args, exc)
        return None


def _find_plant_model(app, name: str, cache: dict):
    """Reference composite model (ElmComp) by name: first in the templates folder/grid (cache["_templates"]),
    then anywhere in the active project (so a hand-made copy in another grid is not picked first)."""
    if name not in cache:
        project = app.GetActiveProject()
        tname = cache.get("_templates")  # a folder (e.g. Library\\Templates) or a grid with that name
        folders = [f for f in project.GetContents(f"{tname}.*", 1) if tname and f.GetClassName() != "ElmComp"]
        found = [c for f in folders for c in f.GetContents(f"{name}.ElmComp", 1)]
        found = found or project.GetContents(f"{name}.ElmComp", 1)
        cache[name] = found[0] if found else None
        if found:
            logger.info("Plant model '%s' found: %s", name, found[0].GetFullName())
        else:
            logger.warning("Composite model '%s' not found in the project: no plant model attached", name)
    return cache[name]


def _copy_composite(grid, ref, gen, prefix: str):
    """Copy a reference composite model, put ``gen`` in slot 1 and point the other slots to
    the copied objects (the reference's slots still point to the reference's own objects)."""
    comp = grid.AddCopy(ref, f"{prefix} {gen.loc_name}")
    _try(comp, "SlotUpdate")
    slots = list(comp.GetAttribute("pelm") or ref.GetAttribute("pelm") or [])
    if not slots:
        raise RuntimeError(f"Composite model {ref.loc_name} has no slots")
    copies = {(o.loc_name, o.GetClassName()): o for o in comp.GetContents("*", 1)}
    for i, obj in enumerate(slots):
        if obj is not None and i > 0:
            slots[i] = copies.get((obj.loc_name, obj.GetClassName()), obj)
    slots[0] = gen  # slot 1: Generator / Converter
    comp.SetAttribute("pelm", slots)
    return comp


def _attach_converter_model(grid, ref, gen, term, cub):
    """Copy a converter composite model (e.g. VSM): converter in slot 1, its voltage
    measurement at the converter's terminal and its current measurement at its cubicle."""
    comp = _copy_composite(grid, ref, gen, ref.loc_name)
    _set(comp, "outserv", 0)
    for vmea in comp.GetContents("*.StaVmea", 1):
        _set(vmea, "pbusbar", term)
    for imea in comp.GetContents("*.StaImea", 1):
        _set(imea, "pcubic", cub)
    return comp


def _attach_plant_model(grid, ref, gen, enabled: dict | None = None):
    """Copy the reference composite model into the grid and put the machine in slot 1."""
    comp = _copy_composite(grid, ref, gen, "Plant")
    # the composite model itself is always in service; its models (gov, avr, ...) keep the in-service
    # state they have in the reference (so they can be switched off there), unless plant_models.enabled says otherwise
    _set(comp, "outserv", 0)
    ref_state = {d.loc_name: d.GetAttribute("outserv") for d in ref.GetContents("*.ElmDsl", 1)}
    for dsl in comp.GetContents("*.ElmDsl", 1):
        if dsl.loc_name in (enabled or {}):  # config plant_models.enabled overrides the reference
            _set(dsl, "outserv", 0 if enabled[dsl.loc_name] else 1)
        else:
            _set(dsl, "outserv", int(ref_state.get(dsl.loc_name) or 0))
    return comp


# position of the permanent droop R in the parameter list of known governor models
_GOV_DROOP_INDEX = {"TGOV1": 1}  # gov_IEEE_TGOV1: Trate, R, T1, T2, T3, Dt, Vmin, Vmax
_warned_once: set[str] = set()


def _set_gov_droop(comp, droop):
    if droop is None:
        return
    for dsl in comp.GetContents("gov.ElmDsl", 1):
        model = dsl.typ_id.loc_name if getattr(dsl, "typ_id", None) is not None else ""
        idx = next((i for key, i in _GOV_DROOP_INDEX.items() if key in model), None)
        if idx is None:
            if model not in _warned_once:
                _warned_once.add(model)
                logger.warning("gov_droop not applied: unknown parameter order of governor model '%s'", model)
            continue
        params = list(dsl.GetAttribute("params"))
        params[idx] = float(droop)
        dsl.SetAttribute("params", params)


# gfc_virtual synchronous machine parameters: Ta, Dp, w_c, T_LPF_u, f_setpoint
_VSM_TA, _VSM_DP = 0, 1


def _set_vsm_params(comp, dsl_name: str, droop, H) -> bool:
    """VSM DSL (named ``dsl_name`` in the composite): Ta = 2 H [s] and Dp = 1/droop [pu]
    (Kpf = sgn/(droop*f_nom) [MW/Hz] in per unit). True if the DSL was found."""
    found = False
    for dsl in comp.GetContents(f"{dsl_name}.ElmDsl", 1):
        params = list(dsl.GetAttribute("params"))
        params[_VSM_TA] = 2.0 * float(H)
        params[_VSM_DP] = 1.0 / float(droop)
        dsl.SetAttribute("params", params)
        found = True
    return found


def _dc_send_power(p_recv: float, r_ohm: float, u_kv: float) -> float:
    """Sending-end DC power [MW] that delivers p_recv [MW] through r_ohm with the sending-end
    voltage u_kv: P_send = U*I, P_recv = U*I - R*I^2 (kA^2 * ohm = MW)."""
    if r_ohm <= 0 or p_recv <= 0:
        return p_recv
    disc = u_kv ** 2 - 4 * r_ohm * p_recv
    if disc < 0:
        raise ValueError(f"DC line resistance {r_ohm} ohm too high to transmit {p_recv} MW at {u_kv} kV")
    return u_kv * (u_kv - disc ** 0.5) / (2 * r_ohm)


def _attach_wecc_hvdc(grid, ref, conv1, conv2, term1, term2, params: dict, p_nom: float, fctrl: dict | None = None):
    """Copy the WECC VSC-HVDC composite (frame "Frame WECC HVDC VSC": 1 Converter 1, 2 Converter 2,
    3 VSC HVDC, 4/5 Voltage terminal 1/2), put the two static generators in slots 1/2, the voltage
    measurements at their terminals, and write ``params`` into the VSC HVDC model by name, with
    MW_base = p_nom and Idmax = Pmax * MW_base / Udref [kA].

    With ``fctrl`` (frame "Frame WECC HVDC VSC f-ctrl": 6 Frequency measurement, 7 Frequency control)
    the PLL is placed at fctrl["term"] and the hvdc_fctrl model gets fnom, Kp, Ki, P0."""
    comp = _copy_composite(grid, ref, conv1, "WECC")
    comp.loc_name = f"WECC {conv1.loc_name.rsplit(' ', 2)[0]}"  # e.g. "WECC HVDC 5640"
    slots = list(comp.GetAttribute("pelm"))
    slots[1] = conv2
    comp.SetAttribute("pelm", slots)
    _set(comp, "outserv", 0)
    for idx, term in ((3, term1), (4, term2)):  # Voltage terminal 1 / 2
        if len(slots) > idx and slots[idx] is not None:
            _set(slots[idx], "pbusbar", term)
    dsl = slots[2] if len(slots) > 2 else None
    if dsl is None or not any(o == dsl for o in comp.GetContents("*.ElmDsl", 1)):
        raise RuntimeError(f"WECC composite '{ref.loc_name}': slot 3 (VSC HVDC) must hold a DSL model stored "
                           "inside the composite (not the one in the library)")
    values = list(dsl.GetAttribute("params"))
    if len(values) != len(_WECC_PARAMS):
        raise RuntimeError(f"'{dsl.loc_name}' has {len(values)} parameters, expected {len(_WECC_PARAMS)} (VHVDC1)")
    get = lambda k: float(params.get(k, values[_WECC_PARAMS.index(k)]))  # noqa: E731
    params = {**params, "MW_base": float(p_nom), "Idmax": get("Pmax") * float(p_nom) / get("Udref")}
    for name, value in params.items():
        values[_WECC_PARAMS.index(name)] = float(value)
    dsl.SetAttribute("params", values)
    if fctrl is not None:
        # slots 6/7 may be empty in the template: then take the PLL and the controller stored inside the copy
        plls = comp.GetContents("*.StaPll", 1)
        ctrls = [d for d in comp.GetContents("*.ElmDsl", 1) if not d == dsl]
        while len(slots) < 7:
            slots.append(None)
        if slots[5] is None or not any(o == slots[5] for o in plls):
            slots[5] = plls[0] if len(plls) == 1 else None
        if slots[6] is None or not any(o == slots[6] for o in ctrls):
            slots[6] = ctrls[0] if len(ctrls) == 1 else None
        pll, ctrl = slots[5], slots[6]
        if pll is None or ctrl is None:
            raise RuntimeError(
                f"WECC composite '{ref.loc_name}': needs exactly one PLL (StaPll) and one hvdc_fctrl model stored "
                f"inside it (found {len(plls)} PLL, {len(ctrls)} other DSL: {[d.loc_name for d in ctrls]}), and its "
                f"frame must have 7 slots (Frame WECC HVDC VSC f-ctrl)")
        comp.SetAttribute("pelm", slots)
        _set(pll, "pbusbar", fctrl["term"])
        pll.loc_name = f"PLL {fctrl['term'].loc_name}"  # template name is generic; the copy says where it measures
        cvals = list(ctrl.GetAttribute("params"))
        if len(cvals) != len(_FCTRL_PARAMS):
            raise RuntimeError(f"'{ctrl.loc_name}' has {len(cvals)} parameters, expected {_FCTRL_PARAMS}")
        ctrl.SetAttribute("params", [float(fctrl[k]) for k in _FCTRL_PARAMS])
    return comp, params


# parameter order of the island frequency controller hvdc_fctrl (Pref = x + Kp*(fnom-f)/fnom, x' = Ki*(fnom-f)/fnom)
_FCTRL_PARAMS = ["fnom", "Kp", "Ki", "P0"]


def _wecc_fctrl_end(model: SnapshotModel, lk, send: str, recv: str, mode) -> str | None:
    """End of the link whose AC frequency the HVDC controls: none (mode false), the receiving end
    (true), or with "auto" the end whose sub-network has no synchronous machine online."""
    if not mode:
        return None
    if mode is True or str(mode).lower() == "true":
        return recv
    sync = model.units[(model.units["type"] != CONVERTER) & model.units.in_service]
    sync_subnets = set(model.buses.sub_network.reindex(sync.bus).astype(str))
    for end in (recv, send):
        if str(model.buses.at[lk[end], "sub_network"]) not in sync_subnets:
            return end
    return None


# parameter order of the WECC VSC-HVDC model VHVDC1 (DIgSILENT library), as listed in its dialog
_WECC_PARAMS = [
    "Tr", "pll_delay", "vblk_rec", "unblk", "vblk_inv", "L", "C", "Rdc",
    "v1", "v2", "v3", "v4", "v5", "v6", "p1", "p2", "p3", "p4",
    "MW_base", "Pmax", "Kpi", "Kii", "eff", "Kpu", "Kiu", "Pmin", "Udref", "Imax",
    "Ipmax1", "Ipmax2", "Ipmax3", "Ipmin1", "Ipmin2", "Ipmin3", "Iqmax2", "Iqmax3", "Iqmin2", "Iqmin3",
    "Refflag_r", "Tq", "Tp", "dbd1", "dbd2", "Kc", "Kpv", "Kiv", "Kiq", "Kpq", "Refflag_i",
    "I_lim_term1_pu", "I_lim_term2_pu", "Idmin", "Udmin", "dPmin", "dQmin", "Idmax", "Udmax", "dPmax", "dQmax",
]


def _load_hook(spec: str | None):
    if not spec:
        return None
    module, func = spec.split(":")
    return getattr(importlib.import_module(module), func)


class _Diagram:
    """Single-line diagram drawn with IntGrf objects (buses at their scaled coordinates)."""

    def __init__(self, app, grid, model: SnapshotModel, cfg: dict):
        dcfg = cfg["powerfactory"]["diagram"]
        self.offset = dcfg["element_offset"]
        self.drawing_format = dcfg["drawing_format"]
        size = dcfg["page_size"]
        page_w, page_h = (size, size) if isinstance(size, (int, float)) else size
        page = grid.CreateObject("IntGrfnet")
        page.loc_name = grid.loc_name
        page.pDataFolder = grid
        page.rURTopX = page_w
        page.rURTopY = page_h
        grid.Deactivate()  # PowerFactory creates the page settings on (re)activation
        grid.Activate()
        self.page = page
        self.set_format()

        # bus positions: geographic x/y scaled into the drawing area
        area_x = dcfg.get("area_x") or dcfg.get("area")
        area_y = dcfg.get("area_y") or dcfg.get("area")
        pos = {}
        for c, (lo, hi) in (("x", area_x), ("y", area_y)):
            v = model.buses[c].astype(float)
            span = v.max() - v.min()
            pos[c] = (v - v.min()) * (hi - lo) / (span if span > 0 else 1.0) + lo
        self.pos = pd.DataFrame(pos)
        # one horizontal slot per carrier (plus loads and links) around each bus
        slots = list(dict.fromkeys(model.units.carrier)) + ["Load", "Link", "Shunt"]
        spread = dcfg["element_spread"]
        step = 2 * spread / (len(slots) - 1) if len(slots) > 1 else 0
        self.slot_x = {s: -spread + i * step for i, s in enumerate(slots)}

    def set_format(self) -> bool:
        fmts = self.page.GetContents("*.SetGrfPage", 1)
        if not fmts:
            return False
        _set(fmts[0], "aDrwFrm", self.drawing_format)
        ok = fmts[0].GetAttribute("aDrwFrm") == self.drawing_format
        if not ok:
            logger.warning("Drawing format '%s' not available in this project; the page keeps its default size",
                           self.drawing_format)
        return ok

    def add_point(self, name, x, y):
        self.pos.loc[name] = [x, y]

    def bus(self, obj, bus):
        g = self.page.CreateObject("IntGrf")
        g.loc_name = obj.loc_name
        g.pDataObj = obj
        g.sSymNam = "TermStrip"
        g.rCenterX, g.rCenterY = (float(v) for v in self.pos.loc[bus])
        g.rSizeX = 2

    def branch(self, obj, bus0, bus1, symbol="d_lin"):
        (x0, y0), (x1, y1) = ((float(v) for v in self.pos.loc[b]) for b in (bus0, bus1))
        g = self.page.CreateObject("IntGrf")
        g.pDataObj = obj
        g.loc_name = obj.loc_name
        g.sSymNam = symbol
        g.rCenterX, g.rCenterY = (x0 + x1) / 2, (y0 + y1) / 2
        for k, (x, y) in enumerate(((x0, y0), (x1, y1))):
            con = g.CreateObject("IntGrfcon")
            con.iDatConNr = k
            con.SetAttribute("points", [[(x0 + x1) / 2, (y0 + y1) / 2], [x, y]])

    def element(self, obj, bus, slot, symbol):
        x0, y0 = (float(v) for v in self.pos.loc[bus])
        g = self.page.CreateObject("IntGrf")
        g.pDataObj = obj
        g.loc_name = obj.loc_name
        g.sSymNam = symbol
        g.rCenterX = x0 + self.slot_x.get(slot, 0.0)
        g.rCenterY = y0 - self.offset
        con = g.CreateObject("IntGrfcon")
        con.iDatConNr = 0
        con.SetAttribute("points", [[g.rCenterX, y0], [g.rCenterX, y0 - self.offset]])


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------
def single_voltage_controller(model: SnapshotModel) -> SnapshotModel:
    """PowerFactory allows only one voltage-controlling element per busbar (without a
    station controller). Keep the slack, or else the largest group (rating), as the bus
    controller and give it the bus Q; other PV groups of that bus become PQ with Q = 0.
    Voltages and flows are unchanged (PyPSA also puts the whole bus Q on one generator)."""
    units = model.units.copy()
    pv = units[units.in_service & units.control.isin(["PV", "Slack"])]
    changed = []
    for bus, grp in pv.groupby("bus"):
        if len(grp) < 2:
            continue
        slack = grp.index[grp.control == "Slack"]
        ctrl = slack[0] if len(slack) else grp.s_rated.idxmax()  # largest machine
        others = grp.index.drop(ctrl)
        units.loc[ctrl, "q"] = grp.q.sum()
        units.loc[others, ["control", "q"]] = ["PQ", 0.0]
        changed.append(f"{bus}: {ctrl}")
    if changed:
        logger.info("%d buses had several voltage controllers; kept one per bus: %s", len(changed), changed)
    return dataclasses.replace(model, units=units)


def reactive_compensation(model: SnapshotModel, cfg: dict) -> tuple[SnapshotModel, pd.DataFrame]:
    """Shunts that take over the reactive power of the voltage-controlling generators.

    mode "all":    the shunt takes all the generator's Q (generator at Q = 0)
    mode "excess": only the Q beyond the capability sqrt(S^2 - P^2)
    mode "margin": the generator keeps up to +/- margin_pu * S_rated, the shunt takes the rest
    mode "none":   no shunts
    Positive Q (generator injecting) -> capacitor; negative -> reactor. Bus voltages
    do not change: the generator still holds its set-point, only its Q changes.
    """
    rcfg = cfg["reactive_compensation"]
    mode, units = rcfg["mode"], model.units.copy()
    rows = []
    if mode != "none":
        ctrl = units[units.in_service & units.control.isin(["PV", "Slack"])]
        for name, u in ctrl.iterrows():
            if mode == "all":
                keep = 0.0
            elif mode == "excess":
                keep = np.sqrt(max(u.s_rated ** 2 - u.p ** 2, 0.0))
            elif mode == "margin":
                keep = rcfg["margin_pu"] * u.s_rated
            else:
                raise ValueError("reactive_compensation.mode must be all | excess | margin | none")
            q_shunt = np.sign(u.q) * max(abs(u.q) - keep, 0.0)
            if abs(q_shunt) < rcfg["min_mvar"]:
                continue
            units.loc[name, "q"] = u.q - q_shunt
            v = model.buses.at[u.bus, "v_mag_pu"]
            rows.append({"name": f"{'Cap' if q_shunt > 0 else 'Reac'}_{u.bus}", "bus": u.bus, "generator": name,
                         "q_mvar": q_shunt, "q_rated": abs(q_shunt) / v ** 2,  # rating at nominal voltage
                         "v_nom": model.buses.at[u.bus, "v_nom"]})
    shunts = pd.DataFrame(rows, columns=["name", "bus", "generator", "q_mvar", "q_rated", "v_nom"]).set_index("name")
    if len(shunts):
        logger.info("Reactive compensation (%s): %d shunts, capacitors %.0f Mvar, reactors %.0f Mvar",
                    mode, len(shunts), shunts.q_mvar.clip(lower=0).sum(), -shunts.q_mvar.clip(upper=0).sum())
    return dataclasses.replace(model, units=units), shunts


def build_grid(app, model: SnapshotModel, cfg: dict) -> dict:
    pcfg, ecfg, ccfg = cfg["powerfactory"], cfg["export"], cfg["converter_control"]
    f_nom = pcfg["frequency"]
    logger.debug("pypsa-pf builder: %s", os.path.abspath(__file__))
    model = single_voltage_controller(model)
    model, shunts = reactive_compensation(model, cfg)
    t0 = time.time()

    def step(msg):
        logger.info("[%5.1f s] %s", time.time() - t0, msg)

    app.EchoOff()  # much faster: no output-window messages while creating objects
    step("preparing grid and type folder")
    _clear_study_case(app, cfg)
    grid = _get_grid(app, cfg)
    library, type_folder = _get_type_folder(app, cfg)
    step("creating diagram page")
    diagram = _Diagram(app, grid, model, cfg)
    hook = _load_hook(ccfg.get("hook"))
    used: set[str] = set()
    cubicles, objs = [], {"terms": {}, "units": {}, "loads": {}, "lines": {}, "shunts": {}, "plants": {}}
    plant_cfg, pumping = cfg.get("plant_models") or {}, []
    plant_cache = {"_templates": cfg["powerfactory"].get("templates_grid")}

    def connect_to(bus, elm, side=0):
        cub = objs["terms"][bus].CreateObject("StaCubic")
        cub.loc_name = pf_name(f"{bus}-{elm.loc_name}", used)
        cub.obj_id = elm
        cub.obj_bus = side
        cubicles.append(cub)
        return cub

    # -- buses ---------------------------------------------------------------
    # speed: cache database writes and stop redrawing the GUI while creating ~1500 objects
    _try(app, "SetWriteCacheEnabled", 1)
    _try(app, "SetGraphicUpdate", 0)
    _try(app, "SetGuiUpdateEnabled", 0)
    step("creating terminals")
    for bus, b in model.buses.iterrows():
        term = grid.CreateObject("ElmTerm")
        term.loc_name = pf_name(bus, used)
        term.uknom = float(b.v_nom)
        if pd.notna(b.x) and pd.notna(b.y):
            _set(term, "GPSlon", float(b.x))
            _set(term, "GPSlat", float(b.y))
        objs["terms"][bus] = term
        diagram.bus(term, bus)

    # -- lines as pi-models (ElmLne + TypLne, one type per line) ----------------
    step("creating lines (ElmLne)")
    charging = cfg["export"]["line_charging"]
    for ln_name, ln in model.lines.iterrows():
        nm = pf_name(f"Line_{ln_name}", used)
        length = float(ln.length_km)
        typ = type_folder.CreateObject("TypLne")
        typ.loc_name = nm
        _set(typ, "uline", float(ln.v_nom))  # kV
        _set(typ, "sline", float(ln.s_nom) / (3 ** 0.5 * float(ln.v_nom)))  # rated current, kA
        _set(typ, "rline", float(ln.r_ohm) / length)  # ohm/km
        _set(typ, "xline", float(ln.x_ohm) / length)
        _set(typ, "bline", float(ln.b_siemens) * 1e6 / length if charging else 0.0)  # uS/km
        _set(typ, "rline0", 4 * float(ln.r_ohm) / length)  # zero sequence: 4 x positive sequence
        _set(typ, "xline0", 4 * float(ln.x_ohm) / length)
        _set(typ, "systp", 0)  # AC
        _set(typ, "nlnph", 3)
        _set(typ, "frnom", f_nom)
        line = grid.CreateObject("ElmLne")
        line.loc_name = nm
        line.typ_id = typ
        line.dline = length  # km
        connect_to(ln.bus0, line, 0)
        connect_to(ln.bus1, line, 1)
        objs["lines"][ln_name] = line
        diagram.branch(line, ln.bus0, ln.bus1, symbol="d_lin")

    if len(model.transformers):
        logger.warning("%d transformers are not exported (not supported yet)", len(model.transformers))

    # -- synchronous generators ----------------------------------------------
    step("creating synchronous generators")
    templates: dict[str, object] = {}
    for u_name, u in model.units[(model.units["type"] != CONVERTER) & model.units.in_service].iterrows():
        gen = grid.CreateObject("ElmSym")
        gen.loc_name = pf_name(u_name, used)
        if u.pf_type not in templates:
            templates[u.pf_type] = _find_template(library, u.pf_type)
            if templates[u.pf_type] is None:
                logger.warning("TypSym template '%s' not found in the Equipment Type Library: "
                               "creating a default type (only sgn, ugn, cosn, h set)", u.pf_type)
            else:
                h_tpl = templates[u.pf_type].GetAttribute("h")
                logger.info("Template '%s': H = %s s (config H for inertia selection: %s s)",
                            u.pf_type, h_tpl, u.H)
        typ = type_folder.CreateObject("TypSym")
        if templates[u.pf_type] is not None:
            typ.CopyData(templates[u.pf_type])
        else:
            _set(typ, "ugn", float(model.buses.at[u.bus, "v_nom"]))
            _set(typ, "cosn", float(ecfg["rated_power_factor"]))
            _set(typ, "h", float(u.H))
        typ.loc_name = gen.loc_name
        typ.sgn = float(u.s_rated)
        gen.typ_id = typ
        gen.pgini = float(u.p)
        gen.qgini = float(u.q)
        gen.av_mode = "constv" if u.control in ("PV", "Slack") else "constq"
        _set(gen, "usetp", float(u.v_set))
        gen.ip_ctrl = 1 if u.control == "Slack" else 0
        _set(gen, "desc", [f"pypsa-pf: {u.n_online}/{u.n_units} units online, installed {u.p_installed:.1f} MW"])
        connect_to(u.bus, gen)
        objs["units"][u_name] = gen
        diagram.element(gen, u.bus, u.carrier, "d_symg")
        pm_name = plant_cfg.get(u.carrier, plant_cfg.get("default"))
        if pm_name:
            ref = _find_plant_model(app, pm_name, plant_cache)
            if ref is not None:
                comp = _attach_plant_model(grid, ref, gen, plant_cfg.get("enabled"))
                objs["plants"][u_name] = comp
                _set_gov_droop(comp, plant_cfg.get("gov_droop"))
                if u.p < 0:  # pumping: fixed-speed pumps keep the AVR but have no governor action
                    for dsl in comp.GetContents("*.ElmDsl", 1):
                        if dsl.loc_name in (plant_cfg.get("off_when_pumping") or []):
                            _set(dsl, "outserv", 1)
                    pumping.append(u_name)

    # -- static generators (converters) --------------------------------------
    step("creating static generators")
    for u_name, u in model.units[(model.units["type"] == CONVERTER) & model.units.in_service].iterrows():
        strategy = ccfg["strategies"][u.converter_control]
        gen = grid.CreateObject("ElmGenstat")
        gen.loc_name = pf_name(u_name, used)
        gen.cCategory = u.pf_category  # wgen, pv, stor, ...
        gen.sgn = float(u.s_rated)
        gen.cosn = float(ecfg["rated_power_factor"])
        gen.pgini = float(u.p)
        gen.qgini = float(u.q)
        _set(gen, "av_mode", "constv" if u.control in ("PV", "Slack") else "constq")
        _set(gen, "usetp", float(u.v_set))
        if u.control == "Slack":
            _set(gen, "ip_ctrl", 1)
        # control and plant model
        gen.ctrlStruct = int(strategy["ctrlStruct"])  # GFL: 0, GFM: 1
        gen.Kpf = gen.sgn / (ccfg["droop"] * f_nom)  # MW/Hz
        # short circuit
        gen.psutype = "syme"  # syme: eq. synchronous machine; fsce: full-size converter; dfag: DFIG
        gen.Skss = gen.sgn * float(u.sc_ratio)
        for attr, value in (strategy.get("pf_attributes") or {}).items():
            _set(gen, attr, value)
        _set(gen, "desc", [f"pypsa-pf: {u.converter_control} ({strategy.get('description', '')}), "
                           f"installed {u.p_installed:.1f} MW"])
        if hook is not None:
            hook(app=app, grid=grid, element=gen, unit=u, strategy=strategy)
        cub = connect_to(u.bus, gen)
        objs["units"][u_name] = gen
        diagram.element(gen, u.bus, u.carrier, "d_genstat")
        pm_name = strategy.get("plant_model")
        if pm_name:
            ref = _find_plant_model(app, pm_name, plant_cache)
            if ref is not None:
                comp = _attach_converter_model(grid, ref, gen, objs["terms"][u.bus], cub)
                objs["plants"][u_name] = comp
                vsm_dsl = ccfg.get("vsm_dsl", "vsm")
                if float(u.H) <= 0 and u.carrier not in _warned_once:
                    _warned_once.add(u.carrier)
                    logger.warning("Carrier '%s' has H = 0 in the config: VSM Ta = 0", u.carrier)
                if not _set_vsm_params(comp, vsm_dsl, ccfg["droop"], u.H) and pm_name not in _warned_once:
                    _warned_once.add(pm_name)
                    logger.warning("'%s': no DSL '%s' found, Ta and Dp not set (converter_control.vsm_dsl)",
                                   pm_name, vsm_dsl)

    if pumping:
        logger.info("%d pumping machines with %s switched off in their plant model: %s",
                    len(pumping), plant_cfg.get("off_when_pumping"), pumping)
    skipped = model.units[~model.units.in_service]
    if len(skipped):
        logger.info("%d groups not created (no unit online): %s", len(skipped), list(skipped.index))

    # -- loads and link terminals --------------------------------------------
    step("creating loads")
    for l_name, ld in model.loads.iterrows():
        load = grid.CreateObject("ElmLod")
        load.loc_name = pf_name(l_name.replace("Load ", "Load_"), used)
        load.plini = float(ld.p)
        load.qlini = float(ld.q)
        connect_to(ld.bus, load)
        objs["loads"][l_name] = load
        diagram.element(load, ld.bus, "Link" if ld.kind == "link" else "Load", "d_load")

    # -- HVDC links -------------------------------------------------------------
    links_as = cfg["export"].get("links_as", "hvdc")
    hcfg = cfg["hvdc"]
    if len(model.links):
        step(f"creating HVDC links ({links_as})")
    if links_as == "wecc":
        wcfg = hcfg.get("wecc") or {}
        fmode = wcfg.get("frequency_control", False)
        ref = _find_plant_model(app, wcfg.get("composite", "hvdc"), plant_cache) if len(model.links) else None
        ref_f = (_find_plant_model(app, wcfg.get("composite_fctrl", "hvdc f-ctrl"), plant_cache)
                 if len(model.links) and fmode else None)
        tpl_dsl = ([d for d in ref.GetContents("*.ElmDsl", 1)] or [None])[0] if ref is not None else None
        tpl = list(tpl_dsl.GetAttribute("params")) if tpl_dsl is not None else []
        for ln_name, lk in model.links.iterrows():
            # Converter 1 = sending end (draws P from its AC bus, PyPSA p > 0); the model allows only 1 -> 2
            send, recv = ("bus0", "bus1") if lk.p0 >= 0 else ("bus1", "bus0")
            params = dict(wcfg.get("params") or {})
            # DC line resistance: same as links_as hvdc (r_ohm_per_km x length) unless Rdc is given
            r_dc = float(params.get("Rdc", float(hcfg["r_ohm_per_km"]) * float(lk.length_km)))
            params["Rdc"] = r_dc
            u_dc = float(params.get("Udref", tpl[_WECC_PARAMS.index("Udref")] if len(tpl) == len(_WECC_PARAMS)
                                    else 500.0))  # kV
            # the receiving end keeps the PyPSA flow (as the P-Q converter with links_as hvdc); the sending
            # end also supplies the DC losses R*Id^2, so the load flow matches the model at t = 0
            p_recv = abs(float(lk.p0))
            p_send = _dc_send_power(p_recv, r_dc, u_dc)
            p_flow = p_recv
            convs = {}
            for end, p_inj in ((send, -p_send), (recv, p_recv)):
                bus = lk[end]
                gen = grid.CreateObject("ElmGenstat")
                gen.loc_name = pf_name(f"HVDC {ln_name} {bus}", used)
                gen.sgn = float(lk.p_nom) * float(hcfg.get("rating_margin", 1.1))
                gen.cosn = 1.0
                gen.pgini = p_inj
                gen.qgini = 0.0
                _set(gen, "av_mode", "constq")
                _set(gen, "ctrlStruct", 0)
                _set(gen, "Kpf", 0.0)
                _set(gen, "desc", [f"pypsa-pf: link {ln_name} ({'sending' if end == send else 'receiving'} end), "
                                   f"WECC VSC-HVDC model"])
                connect_to(bus, gen)
                diagram.element(gen, bus, "Link", "d_genstat")
                convs[end] = gen
            objs.setdefault("hvdc_gens", {})[ln_name] = (convs[send], convs[recv])
            f_end = _wecc_fctrl_end(model, lk, send, recv, fmode)
            fctrl = None
            if f_end is not None and ref_f is not None:
                fc = wcfg.get("fctrl") or {}
                sign = 1.0 if f_end == recv else -1.0  # Pref = delivered power: island at the sending end -> send less
                kp = sign * float(lk.p_nom) / float(fc.get("droop", 0.05))
                fctrl = {"term": objs["terms"][lk[f_end]], "fnom": float(fc.get("fnom", 1.0)), "Kp": kp,
                         "Ki": kp / float(fc.get("Ti", 2.0)), "P0": p_recv}
                logger.info("HVDC %s (WECC): frequency control of %s (Kp %.0f MW/pu, Ki %.0f MW/pu/s, P0 %.3f MW)",
                            ln_name, lk[f_end], fctrl["Kp"], fctrl["Ki"], p_recv)
            elif fmode:
                logger.info("HVDC %s (WECC): no frequency control (%s)", ln_name,
                            "both ends have synchronous machines" if f_end is None else "composite not found")
            use_ref = ref_f if fctrl is not None else ref
            if use_ref is None:
                continue
            comp, params = _attach_wecc_hvdc(grid, use_ref, convs[send], convs[recv], objs["terms"][lk[send]],
                                             objs["terms"][lk[recv]], params, lk.p_nom, fctrl)
            objs["plants"][f"HVDC {ln_name}"] = comp
            logger.info("HVDC %s (WECC): %s -> %s, sent %.2f MW, received %.2f MW (PyPSA), DC losses %.3f MW "
                        "(Rdc %.4f ohm = %.3f km x %g ohm/km), MW_base %.1f MW, Idmax %.3f kA", ln_name, lk[send],
                        lk[recv], p_send, p_recv, p_send - p_recv, r_dc, lk.length_km, hcfg["r_ohm_per_km"],
                        params["MW_base"], params["Idmax"])

    # VSCs: AC bus - VSC - DC terminal = DC line = DC terminal - VSC - AC bus
    v_dc = float(hcfg["v_dc_kv"])
    for ln_name, lk in (model.links.iterrows() if links_as == "hvdc" else []):
        p_end = hcfg["p_control_end"]
        dc_terms = {}
        for k, end in enumerate(("bus0", "bus1")):
            ac_bus = lk[end]
            dc_name = f"DC {ln_name} {ac_bus}"
            term = grid.CreateObject("ElmTerm")
            term.loc_name = pf_name(dc_name, used)
            term.uknom = v_dc
            _set(term, "systype", 1)  # DC
            objs["terms"][dc_name] = term
            dc_terms[end] = dc_name
            (xa, ya), (xb, yb) = ((float(v) for v in diagram.pos.loc[b]) for b in (lk.bus0, lk.bus1))
            f = 1 / 3 if k == 0 else 2 / 3  # DC terminals at 1/3 and 2/3 of the way
            diagram.add_point(dc_name, xa + f * (xb - xa), ya + f * (yb - ya))
            diagram.bus(term, dc_name)

            vsc = grid.CreateObject("ElmVscmono")
            vsc.loc_name = pf_name(f"VSC {ln_name} {ac_bus}", used)
            _set(vsc, "Unom", float(model.buses.at[ac_bus, "v_nom"]))
            _set(vsc, "Unomdc", v_dc)
            _set(vsc, "Snom", float(lk.p_nom) * float(hcfg.get("rating_margin", 1.1)))
            _set(vsc, "ctrlStruct", int(hcfg["ctrlStruct"]))
            _set(vsc, "Kpf", 0.0)
            p_ac = float(lk.p0 if end == "bus0" else lk.p1)  # P drawn from the AC bus (PyPSA convention)
            if end == p_end:
                _set(vsc, "i_acdc", 5)  # P-Q
                _set(vsc, "psetp", hcfg["p_sign"] * p_ac)
            else:
                _set(vsc, "i_acdc", 3)  # Vdc-Q
                _set(vsc, "usetp", 1.0)
            _set(vsc, "qsetp", 0.0)
            _set(vsc, "desc", [f"pypsa-pf: link {ln_name}, PyPSA P drawn from {ac_bus} = {p_ac:.1f} MW"])
            cub_ac = objs["terms"][ac_bus].CreateObject("StaCubic")
            cub_ac.loc_name = pf_name(f"{ac_bus}-{vsc.loc_name}", used)
            cub_dc = term.CreateObject("StaCubic")
            cub_dc.loc_name = pf_name(f"{dc_name}-{vsc.loc_name}", used)
            vsc.busac = cub_ac
            vsc.busdc = cub_dc
            cubicles.extend([cub_ac, cub_dc])
            objs.setdefault("vsc", {})[(ln_name, end)] = vsc
            diagram.branch(vsc, ac_bus, dc_name, symbol="2_vsc")

        nm = pf_name(f"DC line {ln_name}", used)
        typ = type_folder.CreateObject("TypLne")
        typ.loc_name = nm
        _set(typ, "uline", v_dc)
        _set(typ, "sline", float(lk.p_nom) / v_dc)  # kA = MW / kV
        _set(typ, "rline", float(hcfg["r_ohm_per_km"]))
        _set(typ, "systp", 1)  # DC
        dcl = grid.CreateObject("ElmLne")
        dcl.loc_name = nm
        dcl.typ_id = typ
        dcl.dline = float(lk.length_km)
        connect_to(dc_terms["bus0"], dcl, 0)
        connect_to(dc_terms["bus1"], dcl, 1)
        diagram.branch(dcl, dc_terms["bus0"], dc_terms["bus1"], symbol="d_lin")

    # -- reactive compensation shunts ------------------------------------------
    step("creating compensation shunts")
    for sh_name, sh in shunts.iterrows():
        elm = grid.CreateObject("ElmShnt")
        elm.loc_name = pf_name(sh_name, used)
        _set(elm, "ushnm", float(sh.v_nom))  # kV
        if sh.q_mvar > 0:
            _set(elm, "shtype", 2)  # C
            _set(elm, "qcapn", float(sh.q_rated))  # Mvar at nominal voltage
        else:
            _set(elm, "shtype", 1)  # R-L
            _set(elm, "qrean", float(sh.q_rated))
        _set(elm, "ncapx", 1)
        _set(elm, "ncapa", 1)
        _set(elm, "desc", [f"pypsa-pf: compensates {sh.generator}"])
        connect_to(sh.bus, elm)
        objs["shunts"][sh_name] = elm
        diagram.element(elm, sh.bus, "Shunt", cfg["powerfactory"]["diagram"]["shunt_symbol"])

    # -- circuit breakers ----------------------------------------------------
    step("creating circuit breakers")
    for cub in cubicles:
        switch = cub.CreateObject("StaSwitch")
        switch.aUsage = "cbk"
        switch.loc_name = cub.loc_name
        switch.on_off = 1

    # -- refresh graphics ----------------------------------------------------
    step("writing to the database")
    _try(app, "WriteChangesToDb")
    _try(app, "SetWriteCacheEnabled", 0)
    _try(app, "SetGuiUpdateEnabled", 1)
    _try(app, "SetGraphicUpdate", 1)
    step("refreshing graphics")
    grid.Deactivate()
    grid.Activate()
    if not diagram.set_format():  # settings may only exist after the rebuild
        logger.warning("Could not find the diagram page settings to set the drawing format")
    app.Rebuild(1)
    app.Rebuild(2)
    try:
        dm = app.GetDataManager()
        if dm is not None:
            dm.CloseAllTabs()
    except Exception:  # not available in all versions / engine mode
        pass

    app.EchoOn()
    step("done")
    logger.info("PowerFactory grid '%s': %d terminals, %d lines, %d generators (%d with plant model), "
                "%d loads, %d shunts", grid.loc_name, len(objs["terms"]), len(objs["lines"]),
                len(objs["units"]), len(objs["plants"]), len(objs["loads"]), len(objs["shunts"]))
    objs["grid"] = grid
    return objs


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def run_load_flow_and_compare(app, model: SnapshotModel, objs: dict) -> pd.DataFrame | None:
    """Run the PowerFactory load flow and compare bus voltages with PyPSA."""
    ldf = app.GetFromStudyCase("ComLdf")
    _set(ldf, "iopt_net", 0)  # balanced AC load flow
    _set(ldf, "iopt_lim", 0)  # no reactive-power limits, as in PyPSA
    if ldf.Execute() != 0:
        logger.error("PowerFactory load flow did not converge")
        return None
    ac_terms = {b: t for b, t in objs["terms"].items() if b in model.buses.index}
    cmp = pd.DataFrame({
        "v_pypsa": model.buses.v_mag_pu,
        "v_pf": pd.Series({b: t.GetAttribute("m:u") for b, t in ac_terms.items()}),
        "ang_pypsa": model.buses.v_ang_deg,
        "ang_pf": pd.Series({b: t.GetAttribute("m:phiu") for b, t in ac_terms.items()}),
    })
    cmp["dv"], cmp["dang"] = cmp.v_pf - cmp.v_pypsa, cmp.ang_pf - cmp.ang_pypsa
    logger.info("PowerFactory vs PyPSA: max |dV| = %.4f pu (%s), max |d angle| = %.3f deg (%s)",
                cmp.dv.abs().max(), cmp.dv.abs().idxmax(), cmp.dang.abs().max(), cmp.dang.abs().idxmax())
    for u_name in model.units.index[model.units.control == "Slack"]:
        if u_name in objs["units"]:
            logger.info("Slack %s: P PyPSA = %.1f MW, P PowerFactory = %.1f MW", u_name,
                        model.units.at[u_name, "p"], objs["units"][u_name].GetAttribute("m:P:bus1"))
    for ln_name, (g1, g2) in objs.get("hvdc_gens", {}).items():
        p1, p2 = g1.GetAttribute("m:P:bus1"), g2.GetAttribute("m:P:bus1")
        logger.info("HVDC %s (WECC): P injected into AC  %s %.2f MW | %s %.2f MW | DC losses %.3f MW", ln_name,
                    g1.loc_name, p1, g2.loc_name, p2, -(p1 + p2))
    for ln_name, lk in model.links.iterrows():
        vscs = objs.get("vsc", {})
        if (ln_name, "bus0") not in vscs:
            continue
        # m:P:busac is the power injected into the AC grid: the sending end is negative and
        # the sum of both ends is minus the DC losses
        pf0, pf1 = (vscs[(ln_name, e)].GetAttribute("m:P:busac") for e in ("bus0", "bus1"))
        logger.info("HVDC %s: P injected into AC  %s: PyPSA %.1f / PF %.1f MW | %s: PyPSA %.1f / PF %.1f MW | "
                    "DC losses PF %.1f MW (PyPSA link is lossless)",
                    ln_name, lk.bus0, -lk.p0, pf0, lk.bus1, -lk.p1, pf1, -(pf0 + pf1))
        if (pf0 > 0) != (-lk.p0 > 0) and abs(lk.p0) > 1:
            logger.warning("HVDC %s flows in the opposite direction to PyPSA: change hvdc.p_sign in config.yaml",
                           ln_name)
    if cmp.dv.abs().max() > 1e-3:
        logger.warning("Buses with |dV| > 1e-3 pu:\n%s", cmp[cmp.dv.abs() > 1e-3].round(4).to_string())
    return cmp


def export_to_powerfactory(model: SnapshotModel, cfg: dict, app=None) -> dict:
    app = app or connect(cfg)
    objs = build_grid(app, model, cfg)
    if cfg["powerfactory"]["run_load_flow"]:
        objs["comparison"] = run_load_flow_and_compare(app, model, objs)
    # leave the grid deactivated: activating it in PowerFactory draws the diagram
    objs["grid"].Deactivate()
    logger.info("Grid '%s' left deactivated: activate it in PowerFactory to draw the diagram",
                objs["grid"].loc_name)
    return objs
