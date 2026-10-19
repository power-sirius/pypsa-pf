"""Configuration: defaults, YAML loading and validation.

Every setting has a default here; a YAML file (see ``config.example.yaml``, where
each option is documented) only needs the values that differ.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

SYNCHRONOUS = "synchronous"
CONVERTER = "converter"

# Inertia constants of Rios-Penaloza et al., "Dynamic Stability Considerations During
# Capacity Expansion Planning". Extra PyPSA-Eur carriers are included so that other
# networks work out of the box.
DEFAULT_CARRIERS: dict[str, dict[str, Any]] = {
    # --- synchronous machines -------------------------------------------
    # pf_type: name of the TypSym template in the PowerFactory "Equipment Type
    #          Library" that is copied for every machine of this carrier
    "CCGT": {"type": SYNCHRONOUS, "H": 6.0, "pf_control": "PV", "pf_type": "CCGT"},
    "OCGT": {"type": SYNCHRONOUS, "H": 6.0, "pf_control": "PV", "pf_type": "CCGT"},
    "nuclear": {"type": SYNCHRONOUS, "H": 5.0, "pf_control": "PV", "pf_type": "nuclear"},
    "coal": {"type": SYNCHRONOUS, "H": 5.0, "pf_control": "PV", "pf_type": "nuclear"},
    "lignite": {"type": SYNCHRONOUS, "H": 5.0, "pf_control": "PV", "pf_type": "nuclear"},
    "oil": {"type": SYNCHRONOUS, "H": 5.0, "pf_control": "PV", "pf_type": "CCGT"},
    "geothermal": {"type": SYNCHRONOUS, "H": 4.0, "pf_control": "PQ", "pf_type": "hydro"},
    "biomass": {"type": SYNCHRONOUS, "H": 4.0, "pf_control": "PQ", "pf_type": "hydro"},
    "hydro": {"type": SYNCHRONOUS, "H": 4.0, "pf_control": "PV", "pf_type": "hydro"},
    "PHS": {"type": SYNCHRONOUS, "H": 4.0, "pf_control": "PQ", "pf_type": "hydro"},
    "ror": {"type": SYNCHRONOUS, "H": 3.0, "pf_control": "PQ", "pf_type": "ror"},
    # --- converter-interfaced sources ------------------------------------
    # pf_category: ElmGenstat.cCategory; sc_ratio: Skss = sc_ratio * sgn
    "onwind": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "wgen", "control": "GFL", "sc_ratio": 1.5},
    "offwind-ac": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "wgen", "control": "GFL", "sc_ratio": 1.5},
    "offwind-dc": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "wgen", "control": "GFL", "sc_ratio": 1.5},
    "offwind-float": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "wgen", "control": "GFL", "sc_ratio": 1.5},
    "solar": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "pv", "control": "GFL", "sc_ratio": 1.5},
    "solar-hsat": {"type": CONVERTER, "H": 0.5, "pf_control": "PQ", "pf_category": "pv", "control": "GFL", "sc_ratio": 1.5},
    "battery": {"type": CONVERTER, "H": 3.0, "pf_control": "PQ", "pf_category": "stor", "control": "GFM", "sc_ratio": 1.5},
}

DEFAULTS: dict[str, Any] = {
    "network": None,
    "snapshot": {
        "method": "min_inertia",  # "min_inertia" | "timestamp"
        "timestamp": None,
        "start": None,  # optional search window for min_inertia
        "end": None,
        "hours": None,  # e.g. [10, 18] -> only snapshots between 10:00 and 18:00
    },
    "carriers": DEFAULT_CARRIERS,
    "ignore_carriers": ["load"],  # PyPSA-Eur load-shedding generators
    "units_status": {
        # non-committable units (no on/off status in PyPSA) dispatched below idle_tol_mw are
        # treated as switched off: no inertia in the snapshot selection, not created in PowerFactory
        "idle_offline": True,
        "idle_tol_mw": 0.1,
    },
    "export": {
        "bus_carriers": ["AC"],
        "min_capacity_mw": 10.0,  # PQ (bus, carrier) groups below this are not exported
        "min_pv_capacity_mw": 10.0,  # groups below this do not control voltage
        "rated_power_factor": 1.0,  # S_rated = P_online / rated_power_factor (cosn)
        "line_charging": True,  # include the lines' shunt susceptance (pi-model)
        "links_as": "wecc",  # wecc: WECC VSC-HVDC model on two static generators | hvdc: VSC + DC line + VSC | loads
    },
    "power_flow": {
        "distribute_slack": True,
        "x_tol": 1.0e-6,
        "load_reactive": {"method": "tan_phi", "tan_phi": 0.33, "range": [0.2, 0.48], "seed": 42},
        "voltage_limits": [0.9, 1.1],
        "max_line_loading": 1.0,
    },
    "reactive_compensation": {
        # shunts that take over the Q of the voltage-controlling generators
        "mode": "all",  # all | excess | margin | none
        "margin_pu": 0.1,  # mode "margin": generators keep up to +/- margin_pu * S_rated
        "min_mvar": 1.0,  # smaller shunts are not created
    },
    "hvdc": {
        # links between two exported AC buses; export.links_as decides how they are built
        "v_dc_kv": 700.0,  # DC voltage (PyPSA has none); >= AC kV / 0.61 so the PWM modulation index stays below 1
        "rating_margin": 1.1,  # converter rating = rating_margin * link capacity (hvdc and wecc)
        "r_ohm_per_km": 0.0001,  # DC line resistance x PyPSA link length (hvdc and wecc)
        # links_as hvdc only (ElmVscmono - DC line - ElmVscmono, no control model in RMS):
        "p_control_end": "bus0",  # this converter controls P (P-Q); the other controls Vdc (Vdc-Q)
        "p_sign": -1,  # psetp = p_sign * P drawn from the AC bus (PowerFactory: psetp > 0 = injection into AC)
        "ctrlStruct": 0,  # 0 grid-following, 1 grid-forming
        "wecc": {
            # links_as wecc: one static generator per end and a copy of a reference composite with the WECC
            # VSC-HVDC model (VHVDC1): Converter 1 = sending end, Converter 2 = receiving end, voltage
            # measurements at their buses
            "composite": "hvdc",  # power control: the link holds the PyPSA flow
            # island frequency control: copy of "composite" with frame "Frame WECC HVDC VSC f-ctrl" and, stored
            # inside it, a PLL (StaPll, slot 6) and the hvdc_fctrl model (slot 7) driving the model's Pref input:
            # Pref = P0 + Kp*df + Ki*int(df), df = (fnom - f)/fnom, P0 = received PyPSA flow
            "composite_fctrl": "hvdc f-ctrl",
            "frequency_control": False,  # false | true (receiving end) | auto (the end without synchronous machines)
            "fctrl": {"droop": 0.05, "Ti": 2.0, "fnom": 1.0},  # Kp = MW_base / droop, Ki = Kp / Ti; fnom 1: PLL fmeas in pu
            # written into the VSC HVDC model (DSL VHVDC1) by name. Always set from the link: MW_base = link
            # capacity, Idmax = Pmax * MW_base / Udref, and Rdc = r_ohm_per_km * length (unless Rdc is given here).
            # The sending end supplies the DC losses; the receiving end keeps the PyPSA flow.
            "params": {"eff": 1.0, "Refflag_r": 0, "Refflag_i": 0},
        },
    },
    "plant_models": {
        # reference composite model (ElmComp) copied for every synchronous machine, which goes into
        # slot 1 (Generator). "default" for all machines; add "<carrier>: <name>" to override.
        "default": "SG plant",
        "enabled": {"gov": True, "avr": True, "vco": True},  # switch the models of every plant on/off
        "gov_droop": 0.05,  # permanent droop R written into every copied governor (TGOV1)
        "off_when_pumping": ["gov"],  # models switched off for machines with P < 0 (PHS pumping: AVR on, no governor)
    },
    "converter_control": {
        # per strategy: ElmGenstat.ctrlStruct (0 = grid-following, 1 = grid-forming), extra ElmGenstat
        # attributes, and the reference composite copied for every converter with that strategy
        # (converter in slot 1, its StaVmea/StaImea set to the converter's terminal/cubicle)
        "strategies": {
            "GFL": {"description": "Grid-following", "ctrlStruct": 0, "pf_attributes": {}, "plant_model": None},
            "GFM": {"description": "Grid-forming", "ctrlStruct": 1, "pf_attributes": {}, "plant_model": "VSM"},
        },
        "droop": 0.05,  # P-f droop in pu -> Kpf [MW/Hz] = sgn / (droop * f_nom); VSM damping Dp = 1/droop [pu]
        "vsm_dsl": "vsm_control",  # name of the VSM DSL in the composite: Ta = 2 H (carrier H), Dp = 1/droop
        "overrides": [],  # [{carrier: onwind, bus: "ES0 14", control: GFM}, ...]
        "gfm_if_no_sync": True,  # a converter that must act as slack (no synchronous machine) becomes GFM
        "hook": None,  # optional "module:function" called for every converter
    },
    "powerfactory": {
        "python_path": None,  # e.g. C:/Program Files/DIgSILENT/PowerFactory 2025/Python/3.12
        "project": None,  # None = use the active project
        "grid_name": "PyPSA",  # created if it does not exist
        "clear_grid": True,  # delete the grid's previous content before building
        "templates_grid": "Templates",  # folder (Library\\Templates) or grid holding the reference composites
        "clear_events": True,  # delete the study case's simulation events (they point to the previous build)
        "clear_result_variables": True,  # delete the result variables (IntMon) of the study case's results
        "frequency": 50.0,
        "run_load_flow": True,
        "show_window": False,  # show PowerFactory while building (slower)
        "separate_process": True,  # build PowerFactory in its own Python process (no PyPSA loaded there)
        "model_file": "model.pkl",  # where the snapshot model is saved for that process
        "diagram": {
            "drawing_format": "A0",  # standard PowerFactory format (landscape 1189 x 841 mm)
            "page_size": [1189, 841],
            "area_x": [60, 1130],  # bus coordinates are scaled into this area [mm]
            "area_y": [60, 790],
            "element_offset": 25,  # distance from bus to generators/loads
            "element_spread": 26,  # generators/loads spread over +/- this around the bus
            "shunt_symbol": "d_shunt",  # PowerFactory symbol name for ElmShnt
        },
    },
}


def _deep_update(base: dict, new: dict) -> dict:
    for key, value in (new or {}).items():
        if key == "carriers":
            # carrier entries are merged one by one so partial overrides work
            for carrier, attrs in (value or {}).items():
                base["carriers"][carrier] = {**base["carriers"].get(carrier, {}), **(attrs or {})}
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: str | Path | None = None, **overrides: Any) -> dict[str, Any]:
    """Load a YAML config on top of the defaults. Keyword overrides win."""
    cfg = copy.deepcopy(DEFAULTS)
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            _deep_update(cfg, yaml.safe_load(fh) or {})
        if cfg["network"] is not None and not Path(cfg["network"]).is_absolute():
            cfg["network"] = str((Path(path).parent / cfg["network"]).resolve())
    _deep_update(cfg, overrides)
    _validate(cfg)
    return cfg


def _validate(cfg: dict[str, Any]) -> None:
    # empty YAML keys (e.g. "overrides:" with only comments) load as None
    cfg["converter_control"]["overrides"] = cfg["converter_control"].get("overrides") or []
    cfg["ignore_carriers"] = cfg.get("ignore_carriers") or []
    for carrier, attrs in cfg["carriers"].items():
        if attrs.get("type") not in (SYNCHRONOUS, CONVERTER):
            raise ValueError(f"carrier '{carrier}': type must be '{SYNCHRONOUS}' or '{CONVERTER}'")
        if attrs.get("pf_control", "PQ") not in ("PV", "PQ"):
            raise ValueError(f"carrier '{carrier}': pf_control must be 'PV' or 'PQ'")
        if attrs["type"] == CONVERTER and attrs.get("control", "GFL") not in cfg["converter_control"]["strategies"]:
            raise ValueError(f"carrier '{carrier}': control '{attrs.get('control')}' is not a defined strategy")
    if (cfg["hvdc"].get("wecc") or {}).get("frequency_control", False) not in (False, True, "auto"):
        raise ValueError("hvdc.wecc.frequency_control must be false, true or auto")
    if cfg["export"].get("links_as", "hvdc") not in ("hvdc", "wecc", "loads"):
        raise ValueError("export.links_as must be 'hvdc', 'wecc' or 'loads'")
    if cfg["snapshot"]["method"] not in ("min_inertia", "timestamp"):
        raise ValueError("snapshot.method must be 'min_inertia' or 'timestamp'")
    for ov in cfg["converter_control"]["overrides"]:
        if ov.get("control") not in cfg["converter_control"]["strategies"]:
            raise ValueError(f"override {ov}: unknown control strategy")
