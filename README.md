# pypsa-pf

Turn any instant of a solved **PyPSA-Eur** network (`*.nc`) into a **DIgSILENT PowerFactory** model, ready for load-flow and RMS simulation.

pypsa-pf links capacity expansion planning (PyPSA-Eur) with dynamic stability analysis (PowerFactory). It was developed for *"Dynamic Stability Considerations During Capacity Expansion Planning"* (J. D. Rios-Peñaloza, A. Mortera-Canga, M. Prodanovic, IMDEA Energy, IEEE 2025).

```
 solved .nc ──► equivalent inertia ──► snapshot ──► AC power flow ──► per (bus, carrier) ──► PowerFactory grid
 (PyPSA-Eur)    per bus / system      (min H or    (PyPSA, one       aggregation           + plant models
                                       fixed time)  snapshot)                               + load flow check vs PyPSA
```

Everything is plain Python and driven by one YAML file. Each step logs the checks needed to follow the process (no figures).

## Installation

PowerFactory's Python API only works with the Python versions your PowerFactory release ships (see the folders in `...\DIgSILENT\PowerFactory 20xx\Python\`). Create the environment with one of them:

```bash
conda create -n pypsa-pf python=3.12
conda activate pypsa-pf
pip install -e .            # installs pypsa, pandas, numpy, pyyaml
```

## Quick start

1. Copy `config.example.yaml` to `config.yaml`. Set `network:` (your `.nc`) and `powerfactory.python_path:` (the PowerFactory `Python\3.xx` folder matching your Python).
2. Import the PowerFactory project with the dynamic models (see [PowerFactory project](#powerfactory-project)) and set `powerfactory.project`. PowerFactory must be **closed** when you run pypsa-pf: it starts PowerFactory in the background.
3. Run:

```bash
python -m pypsa_pf config.yaml                         # minimum-inertia snapshot -> PowerFactory
python -m pypsa_pf config.yaml --snapshot "2013-01-11 23:00"
python -m pypsa_pf config.yaml --no-powerfactory       # PyPSA part only (any OS, no PowerFactory needed)
python -m pypsa_pf config.yaml --save debug_tables     # also save the exported tables as CSV
```

`pypsa-pf config.yaml` works the same after installation. [`examples/min_inertia_to_powerfactory.py`](examples/min_inertia_to_powerfactory.py) runs the same steps one function call at a time.

4. Open PowerFactory and activate the grid (`grid_name`, default `PyPSA`): the diagram is drawn on activation.

### How the PowerFactory part runs

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

### Reactive power

PyPSA-Eur has no reactive power, so it is created in the AC power flow:

- Each carrier has `pf_control: PV|PQ`. PV units control voltage (`constv` in PowerFactory); PQ units have Q = 0. Offline units are always PQ with Q = 0.
- PyPSA decides PV buses only from `Generator` components and ignores `StorageUnit` control. For buses where only storage units (hydro, PHS) control voltage, a zero-power voltage-control proxy generator is added, so both tools solve the same problem.
- One slack per sub-network: the largest online synchronous generator.
- PowerFactory allows one voltage controller per busbar: the slack, or else the largest machine, keeps the bus Q.
- Loads: Q = P·tanφ (default 0.33), or a seeded random factor.
- Neither power flow limits Q. The export warns about buses whose machines would exceed √(S² − P²), and `reactive_compensation` adds shunts so the generators are not overloaded.

### Grid-following / grid-forming converters

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

### HVDC links

| `links_as` | Built as | For |
|---|---|---|
| `hvdc` | `ElmVscmono` (P-Q) – DC terminal – DC line – DC terminal – `ElmVscmono` (Vdc-Q) | load flow; drawn as a real link. No control model in RMS: the DC voltage is not regulated in simulations |
| `wecc` | `ElmGenstat` at each end + the WECC VSC-HVDC model (DIgSILENT library, `VHVDC1`) | RMS simulations |
| `loads` | a fixed-P load at each end | simple studies |

With `wecc`, the link holds the PyPSA flow (`hvdc.wecc.frequency_control: false`), or controls the frequency of one end (`true`: receiving end; `auto`: the end without synchronous machines, e.g. an island). Frequency control adds a PLL and a PI controller on the model's power order: `Pref = P0 + Kp·Δf + Ki·∫Δf`. The island still needs a grid-forming source (`gfm_if_no_sync`). The DC line resistance is `hvdc.r_ohm_per_km` × the PyPSA link length in both `hvdc` and `wecc`.

## PowerFactory project

pypsa-pf creates the network elements itself, but the **dynamic models are copied from reference models that must be in the library of your PowerFactory project**. They are provided in [`powerfactory/pypsa-pf_template.pfd`](powerfactory/pypsa-pf_template.pfd) (exported with PowerFactory 2025; it can be imported in that version or newer).

1. In PowerFactory, import the file: *File → Import → Data (\*.pfd)*. It creates the project `pypsa-pf_template` with the library below and a study case.
2. Either use that project directly (`powerfactory.project: pypsa-pf_template` in `config.yaml`), or copy its `Library\Dynamic Models`, `Library\Templates` and the `TypSym` templates of `Library\Equipment Type Library` into the same folders of your own project.
3. Close PowerFactory and run pypsa-pf.

Without these library elements the grid is still built, ready for load flow, and a warning is printed for every missing template or composite model.

### Library contents

```
<project>\Library
├─ Dynamic Models                    block definitions (types)
│  ├─ HVDC                           Composite Model Frames: Frame WECC HVDC VSC, Frame WECC HVDC VSC f-ctrl
│  │                                 VHVDC1 (WECC VSC-HVDC model), hvdc_fctrl (frequency controller)
│  ├─ SG                             Composite Model Frames: Frame Synchronous Generator
│  │                                 exc_IEEE_ST1A, gov_IEEE_TGOV1, vtc_IEEE_1
│  └─ VSM                            Composite Model Frames: Grid-forming Converter Frame
│                                    gfc_virtual synchronous machine, vim_controlled virtual impedance,
│                                    vco_P / vco_PI Voltage Controller, pq_power calculation positive sequence,
│                                    ovc_output voltage calculation, ovc_output voltage limiter
├─ Equipment Type Library            TypSym templates CCGT, nuclear, hydro, ror (carriers.<c>.pf_type)
└─ Templates                         reference composites copied by pypsa-pf (powerfactory.templates_grid)
   ├─ SG plant                       avr, gov, vco                                   -> every synchronous machine
   ├─ VSM                            vsm_control, vi, vco, pq, ovc + 2 measurements  -> every GFM converter
   ├─ hvdc                           VSC HVDC Model + 2 voltage measurements         -> links_as: wecc
   └─ hvdc f-ctrl                    as hvdc + PLL + ctrl                            -> wecc with frequency control
```

| Composite | Frame (slots) | Config |
|---|---|---|
| `SG plant` | Frame Synchronous Generator: 1 Generator, governor, AVR, voltage transducer | `plant_models` |
| `VSM` | Grid-forming Converter Frame: 1 Converter, 2 Grid-forming control, 3 Virtual impedance, 4 Voltage control, 5 Power calculation, 6 Output voltage calculation, 7 Voltage measurement, 8 Current measurement | `converter_control.strategies.GFM.plant_model`, `vsm_dsl` |
| `hvdc` | Frame WECC HVDC VSC: 1 Converter 1, 2 Converter 2, 3 VSC HVDC, 4/5 Voltage terminal 1/2 | `hvdc.wecc.composite` |
| `hvdc f-ctrl` | Frame WECC HVDC VSC f-ctrl: as above + 6 Frequency Measurement (PLL), 7 Frequency control (`hvdc_fctrl`, output `Pref` to the `Pref` input of VSC HVDC) | `hvdc.wecc.composite_fctrl` |

### Using your own models

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

## Citation

If you use this tool, please cite:

> J. D. Rios-Peñaloza, A. Mortera-Canga, M. Prodanovic, "Dynamic Stability Considerations During Capacity Expansion Planning", IEEE, 2025.

## Acknowledgements

Developed in the framework of the REDESFUERTES project (PID2022-142416OB-I00), funded by MICIU/AEI/10.13039/501100011033 and FSE+.

The code was refined with the assistance of Claude (Anthropic), an AI assistant. All modelling choices and results were reviewed and validated by the authors.
