"""Tests of the PowerFactory builder against a mock of the PowerFactory API (no PyPSA or PowerFactory needed)."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import mock_powerfactory as mpf  # noqa: E402

from pypsa_pf.config import load_config  # noqa: E402
from pypsa_pf.powerfactory_builder import build_grid, run_load_flow_and_compare  # noqa: E402
from pypsa_pf.snapshot import SnapshotModel, pf_name  # noqa: E402


def small_model() -> SnapshotModel:
    buses = pd.DataFrame(
        {"v_nom": [380.0, 380.0], "x": [-3.7, -0.4], "y": [40.4, 39.5], "v_mag_pu": [1.0, 0.99],
         "v_ang_deg": [0.0, -5.0], "sub_network": ["0", "0"], "q_charging": [50.0, 50.0],
         "x_pos": [80.0, 500.0], "y_pos": [500.0, 80.0]},
        index=["ES0 0", "ES0 1"])
    lines = pd.DataFrame({"bus0": ["ES0 0"], "bus1": ["ES0 1"], "s_nom": [2000.0], "r_ohm": [5.0], "x_ohm": [43.0],
                          "b_siemens": [0.0007], "length_km": [150.0], "v_nom": [380.0]}, index=["0"])
    rows = [
        # name, bus, carrier, type, p, q, control, in_service, s_rated, pf_type/category, converter_control
        ("CCGT ES0 0", "ES0 0", "CCGT", "synchronous", 400.0, 50.0, "Slack", True, 800.0, "CCGT", None),
        ("CCGT ES0 1", "ES0 1", "CCGT", "synchronous", 0.0, 0.0, "PQ", False, 900.0, "CCGT", None),
        ("ror ES0 1", "ES0 1", "ror", "synchronous", 30.0, 0.0, "PQ", True, 60.0, "ror", None),
        ("PHS ES0 0", "ES0 0", "PHS", "synchronous", -100.0, 0.0, "PQ", True, 100.0, "hydro", None),
        ("solar ES0 1", "ES0 1", "solar", "converter", 700.0, 0.0, "PQ", True, 1000.0, "pv", "GFL"),
        ("onwind ES0 0", "ES0 0", "onwind", "converter", 300.0, 0.0, "PQ", True, 500.0, "wgen", "GFM"),
    ]
    units = pd.DataFrame(
        [{"name": r[0], "bus": r[1], "carrier": r[2], "type": r[3], "p": r[4], "q": r[5], "control": r[6],
          "in_service": r[7], "s_rated": r[8], "p_installed": r[8], "p_online": r[8] if r[7] else 0.0,
          "pf_type": r[9], "pf_category": r[9], "converter_control": r[10], "sc_ratio": 1.5,
          "v_set": 1.0, "n_online": int(r[7]), "n_units": 1, "H": 4.0} for r in rows]).set_index("name")
    loads = pd.DataFrame({"bus": ["ES0 0", "ES0 1", "ES0 1"], "p": [800.0, 600.0, 10.0], "q": [260.0, 200.0, 0.0],
                          "kind": ["load", "load", "link"]}, index=["Load ES0 0", "Load ES0 1", "Link 5 @ES0 1"])
    return SnapshotModel(pd.Timestamp("2013-01-15 11:00"), buses, lines, pd.DataFrame(), units, loads)


@pytest.fixture
def built():
    cfg = load_config(plant_models={"gov_droop": 0.04})
    app = mpf.Application(templates={"CCGT": {"h": 6.0, "xd": 2.0}})
    model = small_model()
    return cfg, app, model, build_grid(app, model, cfg)


def by_class(grid, cls):
    return {o.loc_name: o for o in grid.walk() if o.GetClassName() == cls}


def test_topology(built):
    _, _, _, objs = built
    grid = objs["grid"]
    assert set(by_class(grid, "ElmTerm")) == {"ES0 0", "ES0 1"}
    lines = by_class(grid, "ElmLne")
    assert set(lines) == {"Line_0"}
    assert lines["Line_0"].typ_id.bline > 0  # line charging inside the pi-model
    shunts = by_class(grid, "ElmShnt")  # default compensation "all": slack CCGT ES0 0 has Q = 50 Mvar
    assert set(shunts) == {"Cap_ES0 0"} and shunts["Cap_ES0 0"].shtype == 2
    cubs = [o for o in grid.walk() if o.GetClassName() == "StaCubic"]
    assert all(len(c._children) == 1 and c._children[0].GetClassName() == "StaSwitch" for c in cubs)
    assert len(by_class(grid, "IntGrfnet")) == 1


def test_offline_group_not_created_and_slack(built):
    _, _, _, objs = built
    syms = by_class(objs["grid"], "ElmSym")
    assert "CCGT ES0 1" not in syms  # offline -> not created
    slack = syms["CCGT ES0 0"]
    assert slack.ip_ctrl == 1 and slack.av_mode == "constv"
    assert slack.typ_id.h == 6.0 and slack.typ_id.xd == 2.0  # copied from the library template
    assert slack.typ_id.sgn == 800.0
    assert syms["ror ES0 1"].av_mode == "constq"
    assert syms["ror ES0 1"].typ_id.h == 4.0  # no template -> default type with config H


def test_converter_control_and_kpf(built):
    cfg, _, _, objs = built
    gens = by_class(objs["grid"], "ElmGenstat")
    assert gens["solar ES0 1"].ctrlStruct == 0 and gens["onwind ES0 0"].ctrlStruct == 1
    assert gens["solar ES0 1"].cCategory == "pv"
    # 5 % droop on 1000 MVA at 50 Hz -> 400 MW/Hz
    assert gens["solar ES0 1"].Kpf == pytest.approx(1000 / (0.05 * 50))
    assert gens["solar ES0 1"].Skss == pytest.approx(1500)


def test_loads_and_rebuild(built):
    _, _, _, objs = built
    loads = by_class(objs["grid"], "ElmLod")
    assert loads["Load_ES0 0"].plini == 800.0 and loads["Load_ES0 0"].qlini == 260.0
    assert "Link 5 _ES0 1" in loads  # '@' is not allowed in PF names


def test_rebuild_clears_grid(built):
    cfg, app, model, _ = built
    objs2 = build_grid(app, model, cfg)
    assert len(by_class(objs2["grid"], "ElmTerm")) == 2  # not duplicated


def test_load_flow_comparison(built):
    _, app, model, objs = built
    cmp = run_load_flow_and_compare(app, model, objs)
    assert list(cmp.index) == ["ES0 0", "ES0 1"]
    assert cmp.loc["ES0 1", "dv"] == pytest.approx(0.01)  # mock returns 1.0 pu


def test_pf_name_unique():
    used = set()
    assert pf_name("a.b/c", used) == "a_b_c"
    assert pf_name("a.b/c", used) == "a_b_c_1"
    assert len(pf_name("x" * 60, used)) == 40


@pytest.mark.parametrize("mode,q_gen,q_shunt", [("all", 0.0, 50.0), ("margin", 50.0, 0.0), ("none", 50.0, None)])
def test_reactive_compensation_modes(mode, q_gen, q_shunt):
    from pypsa_pf.powerfactory_builder import reactive_compensation
    cfg = load_config(reactive_compensation={"mode": mode, "margin_pu": 0.1})
    model, shunts = reactive_compensation(small_model(), cfg)
    assert model.units.at["CCGT ES0 0", "q"] == pytest.approx(q_gen)
    if q_shunt is None or q_shunt == 0.0:
        assert shunts.empty  # margin: 0.1 * 800 = 80 Mvar > 50 Mvar, nothing to compensate
    else:
        assert shunts.at["Cap_ES0 0", "q_mvar"] == pytest.approx(q_shunt)


def test_plant_models(built):
    _, _, _, objs = built
    plants = {k: v for k, v in by_class(objs["grid"], "ElmComp").items() if k.startswith("Plant")}
    assert set(plants) == {"Plant CCGT ES0 0", "Plant ror ES0 1", "Plant PHS ES0 0"}  # one per machine in service
    p = plants["Plant CCGT ES0 0"]
    assert p.pelm[0] is by_class(objs["grid"], "ElmSym")["CCGT ES0 0"]
    assert p.pelm[1].loc_name == "gov" and p.pelm[1]._parent is p  # its own copy of the governor
    pump = plants["Plant PHS ES0 0"]  # pumping: governor off, AVR on
    assert {d.loc_name: d._attrs.get("outserv") for d in pump._children} == {"gov": 1, "avr": 0}
    assert p.pelm[1].params[1] == 0.04  # gov_droop written into R


def test_plant_models_enabled():
    cfg = load_config(plant_models={"enabled": {"gov": True, "avr": False}})
    app = mpf.Application()
    objs = build_grid(app, small_model(), cfg)
    p = by_class(objs["grid"], "ElmComp")["Plant CCGT ES0 0"]
    assert {d.loc_name: d._attrs.get("outserv") for d in p._children} == {"gov": 0, "avr": 1}


def test_converter_composite_model(built):
    _, _, model, objs = built
    plants = by_class(objs["grid"], "ElmComp")
    assert "VSM onwind ES0 0" in plants and "VSM solar ES0 1" not in plants  # GFM only
    vsm = plants["VSM onwind ES0 0"]
    gen = by_class(objs["grid"], "ElmGenstat")["onwind ES0 0"]
    assert vsm.pelm[0] is gen
    assert all(o is None or o._parent is vsm for o in vsm.pelm[1:])  # its own copies, not the template's
    vmea, imea = vsm.pelm[6], vsm.pelm[7]
    assert vmea.pbusbar is objs["terms"]["ES0 0"]
    assert imea.pcubic.obj_id is gen and imea.pcubic._parent is objs["terms"]["ES0 0"]
    assert vsm.pelm[1].params[1] == pytest.approx(1 / 0.05)  # Dp = 1/droop, same as Kpf in pu
    assert vsm.pelm[1].params[0] == pytest.approx(2 * 4.0)  # Ta = 2 H


def test_study_case_cleared(built):
    _, app, _, _ = built
    assert app.events._children == [] and app.results._children == []


def model_with_link() -> SnapshotModel:
    m = small_model()
    links = pd.DataFrame({"bus0": ["ES0 1"], "bus1": ["ES0 0"], "p0": [-500.0], "p1": [500.0],
                          "p_nom": [600.0], "length_km": [300.0]}, index=["5640"])
    return SnapshotModel(m.snapshot, m.buses, m.lines, m.transformers, m.units, m.loads, links)


def test_wecc_hvdc():
    cfg = load_config(export={"links_as": "wecc"})
    objs = build_grid(mpf.Application(), model_with_link(), cfg)
    grid = objs["grid"]
    assert not by_class(grid, "ElmVscmono")
    gens = by_class(grid, "ElmGenstat")
    g1, g2 = gens["HVDC 5640 ES0 0"], gens["HVDC 5640 ES0 1"]  # p1 > 0: ES0 0 draws -> sending end
    r = 0.0001 * 300  # r_ohm_per_km x length
    i = 500.0 / 500.0  # ~1 kA at Udref 500 kV
    assert g2.pgini == 500.0 and g1.pgini == pytest.approx(-(500.0 + r * i ** 2), rel=1e-4)
    assert g1.sgn == pytest.approx(660.0)
    comp = by_class(grid, "ElmComp")["WECC HVDC 5640"]
    assert comp.pelm[0] is g1 and comp.pelm[1] is g2 and comp.pelm[2]._parent is comp
    assert comp.pelm[3].pbusbar is objs["terms"]["ES0 0"] and comp.pelm[4].pbusbar is objs["terms"]["ES0 1"]
    par = comp.pelm[2].params
    assert par[18] == 600.0 and par[55] == pytest.approx(1.05 * 600 / 500)  # MW_base, Idmax
    assert par[7] == pytest.approx(0.03) and par[22] == 1.0 and par[38] == 0 and par[48] == 0  # Rdc, eff, Refflag_r/i


def test_hvdc_vsc_still_default():
    objs = build_grid(mpf.Application(), model_with_link(), load_config())
    assert len(by_class(objs["grid"], "ElmVscmono")) == 2


def test_dc_send_power():
    from pypsa_pf.powerfactory_builder import _dc_send_power
    p_send = _dc_send_power(822.8, 2.5, 500.0)
    i = p_send / 500.0
    assert p_send - 2.5 * i ** 2 == pytest.approx(822.8)
    assert _dc_send_power(822.8, 0.0, 500.0) == 822.8


def test_wecc_frequency_control():
    m = model_with_link()
    m.buses["sub_network"] = ["0", "1"]  # ES0 1 (receiving end) on its own: no synchronous machine there
    sel = m.units.bus == "ES0 1"
    m.units.loc[sel, "type"] = "converter"
    m.units.loc[sel & m.units.converter_control.isna(), "converter_control"] = "GFM"
    cfg = load_config(export={"links_as": "wecc"}, hvdc={"wecc": {"frequency_control": "auto"}})
    objs = build_grid(mpf.Application(), m, cfg)
    comp = by_class(objs["grid"], "ElmComp")["WECC HVDC 5640"]
    pll, ctrl = comp.pelm[5], comp.pelm[6]
    assert pll._parent is comp and pll.pbusbar is objs["terms"]["ES0 1"] and pll.loc_name == "PLL ES0 1"
    assert ctrl.params == [1.0, pytest.approx(600 / 0.05), pytest.approx(600 / 0.05 / 2.0), 500.0]
    # without frequency control the power-control composite is used
    objs = build_grid(mpf.Application(), model_with_link(), load_config(export={"links_as": "wecc"}))
    assert len(by_class(objs["grid"], "ElmComp")["WECC HVDC 5640"].pelm) == 5


def test_wecc_frequency_control_empty_template_slots():
    m = model_with_link()
    cfg = load_config(export={"links_as": "wecc"}, hvdc={"wecc": {"frequency_control": True}})
    app = mpf.Application()
    tpl = [c for c in app.templates_grid._children if c.loc_name == "hvdc f-ctrl"][0]
    tpl.pelm = tpl.pelm[:5] + [None, None]
    objs = build_grid(app, m, cfg)
    comp = by_class(objs["grid"], "ElmComp")["WECC HVDC 5640"]
    assert comp.pelm[5].GetClassName() == "StaPll" and comp.pelm[5]._parent is comp
    assert comp.pelm[6].loc_name == "ctrl" and comp.pelm[6]._parent is comp
