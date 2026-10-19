# pypsa-pf: details

Complements the [README](../README.md).

## How the PowerFactory part runs

By default (`powerfactory.separate_process: true`) the snapshot model is saved to `model.pkl` and a second Python process (`python -m pypsa_pf.build model.pkl`) builds the grid, so PyPSA and PowerFactory are never loaded in the same process (some endpoint-security tools block that). If the build fails, fix the cause and rerun only that step:

```bash
python -m pypsa_pf.build model.pkl
```

Before building, pypsa-pf clears the grid and, in the active study case, the simulation events and result variables, because they point to objects of the previous build (`powerfactory.clear_grid`, `clear_events`, `clear_result_variables`).

## What is printed (the checks)

| Step | Checks |
|---|---|
| Load | Components, snapshots, installed capacity by carrier; removed lines/units |
| Inertia | System H min/mean/max, kinetic energy, 5 lowest snapshots, H per bus at the chosen one |
| AC power flow | Convergence; offline units switched to PQ; slack per sub-network; dispatch and Q by carrier; P/Q balance and losses; voltage range; most loaded lines |
| Export | Dropped small groups; buses needing more Q than their machines can give; exported balance; kinetic energy of the exported model |
| PowerFactory | Objects created; templates and plant models used; load flow vs PyPSA (ΔV, Δangle, slack P, HVDC flows) |

## Modelling

| PyPSA | PowerFactory |
|---|---|
| AC bus | `ElmTerm`, placed by its coordinates |
| Line | `ElmLne` + `TypLne` π-model (r, x, b, length, rated current from the optimised capacity; r0, x0 = 4·r, 4·x) |
| Synchronous units (CCGT, nuclear, hydro, PHS, biomass, ror, ...) | one `ElmSym` per (bus, carrier); `TypSym` copied from the library template of the carrier; rating = capacity online; plant model (governor, AVR) copied from a reference composite |
| Converter units (solar, wind, battery) | one `ElmGenstat` per (bus, carrier): `cCategory`, `ctrlStruct` (GFL/GFM), `Kpf`, short-circuit model `syme`; GFM converters get a VSM composite |
| Load | `ElmLod` with P and Q from the power flow |
| Reactive compensation | `ElmShnt` capacitor or reactor next to each voltage-controlling generator (`reactive_compensation`) |
| Link between two AC buses | `export.links_as`: `hvdc` (VSC – DC line – VSC), `wecc` (two `ElmGenstat` + WECC VSC-HVDC model, optionally with frequency control), or `loads` |
| every connection | `StaCubic` + closed breaker |

Groups with no unit online are not created. P and Q are the AC power-flow results, not the linear optimisation values. Slack and PV/PQ come from the PyPSA power flow.

## Reactive power

PyPSA-Eur has no reactive power, so it is created in the AC power flow:

- Each carrier has `pf_control: PV|PQ`. PV units control voltage (`constv` in PowerFactory); PQ units have Q = 0. Offline units are always PQ with Q = 0.
- PyPSA decides PV buses only from `Generator` components and ignores `StorageUnit` control. For buses where only storage units (hydro, PHS) control voltage, a zero-power voltage-control proxy generator is added, so both tools solve the same problem.
- One slack per sub-network: the largest online synchronous generator.
- PowerFactory allows one voltage controller per busbar: the slack, or else the largest machine, keeps the bus Q.
- Loads: Q = P·tanφ (default 0.33), or a seeded random factor.
- Neither power flow limits Q. The export warns about buses whose machines would exceed √(S² − P²), and `reactive_compensation` adds shunts so the generators are not overloaded.

## Grid-following / grid-forming converters

```yaml
carriers:
  battery: {control: GFM}                     # default strategy per carrier
converter_control:
  strategies:
    GFL: {ctrlStruct: 0, plant_model: null}
    GFM: {ctrlStruct: 1, plant_model: VSM}    # VSM composite copied for every GFM converter
  droop: 0.05                                 # Kpf = rating/(droop·f) [MW/Hz]; VSM Dp = 1/droop
  overrides:
    - {carrier: onwind, bus: "ES0 14", control: GFM}
  gfm_if_no_sync: true                        # a converter that must be the slack becomes GFM
```

## HVDC links

| `links_as` | Built as | For |
|---|---|---|
| `wecc` (default) | `ElmGenstat` at each end + the WECC VSC-HVDC model (DIgSILENT library, `VHVDC1`) | RMS simulations |
| `hvdc` | `ElmVscmono` (P-Q) – DC terminal – DC line – DC terminal – `ElmVscmono` (Vdc-Q) | load flow; drawn as a real link. No control model in RMS: the DC voltage is not regulated in simulations |
| `loads` | a fixed-P load at each end | simple studies |

With `wecc`, the link holds the PyPSA flow (`hvdc.wecc.frequency_control: false`), or controls the frequency of one end (`true`: receiving end; `auto`: the end without synchronous machines, e.g. an island). Frequency control adds a PLL and a PI controller on the model's power order: `Pref = P0 + Kp·Δf + Ki·∫Δf`. The island still needs a grid-forming source (`gfm_if_no_sync`). The DC line resistance is `hvdc.r_ohm_per_km` × the PyPSA link length in both `hvdc` and `wecc`.

## PowerFactory library

The library provided in `powerfactory/pypsa-pf_template.pfd` (contents listed in the README) holds one reference composite per kind of model:

| Composite | Frame (slots) | Config |
|---|---|---|
| `SG plant` | Frame Synchronous Generator: 1 Generator, governor, AVR, voltage transducer | `plant_models` |
| `VSM` | Grid-forming Converter Frame: 1 Converter, 2 Grid-forming control, 3 Virtual impedance, 4 Voltage control, 5 Power calculation, 6 Output voltage calculation, 7 Voltage measurement, 8 Current measurement | `converter_control.strategies.GFM.plant_model`, `vsm_dsl` |
| `hvdc` | Frame WECC HVDC VSC: 1 Converter 1, 2 Converter 2, 3 VSC HVDC, 4/5 Voltage terminal 1/2 | `hvdc.wecc.composite` |
| `hvdc f-ctrl` | Frame WECC HVDC VSC f-ctrl: as above + 6 Frequency Measurement (PLL), 7 Frequency control (`hvdc_fctrl`, output `Pref` to the `Pref` input of VSC HVDC) | `hvdc.wecc.composite_fctrl` |

The frequency controller `hvdc_fctrl` (parameters `fnom, Kp, Ki, P0`, in this order) computes the power order of the WECC model from the PLL frequency `f`:

```
inc(Pref) = P0
inc(x) = Pref - Kp*(fnom-f)/fnom
x. = Ki*(fnom-f)/fnom
Pref = x + Kp*(fnom-f)/fnom
```

## Using your own models

The composites can be changed or replaced, as long as:

- **everything a composite uses is stored inside it** (its DSL models and measurements), and their types are in the project's `Dynamic Models` or in the DIgSILENT global library, not in another project;
- **everything that depends on the network is left empty**: generator/converter slots and the buses of measurements and PLLs. pypsa-pf fills them in every copy;
- current and voltage measurements use the rated values of the measured element, not a fixed base, since every copy measures a different converter.

Parameters in the reference are the defaults of every copy. pypsa-pf writes: the governor droop (`plant_models.gov_droop`); VSM `Ta` = 2·H and `Dp` = 1/droop; WECC `MW_base`, `Idmax`, `Rdc` and `hvdc.wecc.params`; `hvdc_fctrl` `fnom`, `Kp`, `Ki`, `P0`.

Expected initialisation messages: the WECC model reports `s9`, `x_Idi` and `I_term2_pu_r` as not correctly initialised (its DC voltage controller starts at its lower limit). They are harmless: the link stays flat without events.

## Tests

```bash
pip install -e .[test]
pytest
```

The tests use a mock of the PowerFactory API (`tests/mock_powerfactory.py`), so they run without PowerFactory or PyPSA.

## Limitations

- Transformers and shunt impedances are not exported (PyPSA-Eur clustered networks normally have none); a warning is printed.
- Single-period networks only (no multi-investment-period snapshots).
- Units are aggregated per (bus, carrier).
- No reactive-power limits in either power flow.
- With `links_as: hvdc` the VSCs have no control model in RMS simulations; use `wecc` for dynamics.
