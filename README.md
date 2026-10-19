# pypsa-pf

Turn any instant of a solved **PyPSA-Eur** network (`*.nc`) into a **DIgSILENT PowerFactory** model, ready for load-flow and RMS simulation.

```
 solved .nc ──► equivalent inertia ──► snapshot ──► AC power flow ──► per (bus, carrier) ──► PowerFactory grid
 (PyPSA-Eur)    per bus / system      (min H or    (PyPSA, one       aggregation           + plant models
                                       fixed time)  snapshot)                               + load flow check vs PyPSA
```

Everything is plain Python and driven by one YAML file ([`config.example.yaml`](config.example.yaml), every option commented). Each step logs the checks needed to follow the process. Modelling details, HVDC options and how to use your own dynamic models: [docs/details.md](docs/details.md).

## Installation

PowerFactory's Python API only works with the Python versions your PowerFactory release ships (see the folders in `...\DIgSILENT\PowerFactory 20xx\Python\`). Create the environment with one of them:

```bash
conda create -n pypsa-pf python=3.12
conda activate pypsa-pf
pip install -e .
```

## PowerFactory project

The dynamic models (plant models of synchronous machines, VSM for grid-forming converters, WECC HVDC model) are copied from reference models that **must be in the library of your PowerFactory project**. They are provided in [`powerfactory/pypsa-pf_template.pfd`](powerfactory/pypsa-pf_template.pfd) (PowerFactory 2025 or newer):

1. In PowerFactory: *File → Import → Data (\*.pfd)*. This creates the project `pypsa-pf_template`.
2. Use that project (`powerfactory.project: pypsa-pf_template`), or copy its `Library\Dynamic Models`, `Library\Templates` and the `TypSym` templates of `Library\Equipment Type Library` into your own project.

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


Without them the grid is still built, ready for load flow, with a warning for every missing model.

## Quick start

1. Make your own settings file: copy `config.example.yaml` and rename the copy `config.yaml`. Open it and change three lines:

   ```yaml
   network: networks/my_network.nc     # your solved PyPSA-Eur network

   powerfactory:
     python_path: "C:/Program Files/DIgSILENT/PowerFactory 2025/Python/3.12"   # same Python version as your environment
     project: pypsa-pf_template          # the PowerFactory project imported above
   ```

   Everything else can stay as it is.
2. Close PowerFactory: pypsa-pf starts it in the background.
3. Run either of:

```bash
python -m pypsa_pf config.yaml                         # minimum-inertia snapshot -> PowerFactory
python -m pypsa_pf config.yaml --snapshot "2013-01-11 23:00" # a chosen snapshot -> PowerFactory
```

4. Open PowerFactory and activate the grid (`powerfactory.grid_name`, default `PyPSA`): the diagram is drawn on activation.

If the PowerFactory step fails, fix the cause and rerun only that step with `python -m pypsa_pf.build model.pkl`.

## Citation

If you use this tool, please cite:

> J. D. Rios-Peñaloza, A. Mortera-Canga and M. Prodanovic, "Dynamic Stability Considerations During Capacity Expansion Planning," *2025 IEEE International Conference on Environment and Electrical Engineering and 2025 IEEE Industrial and Commercial Power Systems Europe (EEEIC / I&CPS Europe)*, Chania, Crete, Greece, 2025, pp. 1-6, doi: [10.1109/EEEIC/ICPSEurope64998.2025.11169185](https://doi.org/10.1109/EEEIC/ICPSEurope64998.2025.11169185).

```bibtex
@inproceedings{RiosPenaloza2025,
  author    = {Rios-Pe{\~n}aloza, J. D. and Mortera-Canga, A. and Prodanovic, M.},
  title     = {Dynamic Stability Considerations During Capacity Expansion Planning},
  booktitle = {2025 IEEE International Conference on Environment and Electrical Engineering and 2025 IEEE Industrial and Commercial Power Systems Europe (EEEIC / I\&CPS Europe)},
  address   = {Chania, Crete, Greece},
  year      = {2025},
  pages     = {1--6},
  doi       = {10.1109/EEEIC/ICPSEurope64998.2025.11169185}
}
```

## Acknowledgements

Developed in the framework of the REDESFUERTES project (PID2022-142416OB-I00), funded by MICIU/AEI/10.13039/501100011033 and FSE+.

The code was refined with the assistance of Claude (Anthropic), an AI assistant. All modelling choices and results were reviewed and validated by the authors.
