"""Step-by-step example: choose the snapshot to export, then build it in PowerFactory.

The same as ``python -m pypsa_pf config.yaml``, one function call at a time, so each step
can be inspected or changed. Step 3 selects the minimum-inertia snapshot: replace it with
your own criterion (any timestamp of the network) to export a different instant.

    python examples/snapshot_selection.py config.yaml
"""

import logging
import sys

import pypsa_pf

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
cfg = pypsa_pf.load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")

# 1. Read the PyPSA-Eur result and drop unusable lines/units
n = pypsa_pf.load_network(cfg["network"])

# 2. Equivalent inertia per bus and for the system
inertia = pypsa_pf.compute_inertia(n, cfg)

# 3. Pick the instant to export: the one with minimum system inertia (or snapshot.timestamp).
#    Your own criterion goes here, e.g. the hour with the highest solar share:
#    snapshot = n.generators_t.p.filter(like="solar").sum(axis=1).idxmax()
snapshot = pypsa_pf.select_snapshot(n, cfg, inertia)

# 4. Non-linear AC power flow of that instant (dispatch from the optimisation)
setup = pypsa_pf.prepare_power_flow(n, snapshot, cfg)
pypsa_pf.run_power_flow(n, setup, cfg)

# 5. Aggregate per (bus, carrier): the tables that are sent to PowerFactory
model = pypsa_pf.build_snapshot_model(n, setup, cfg)
# model.to_csv("debug_tables")  # optional, to inspect the tables

# 6. Build the PowerFactory grid (in a separate process, see powerfactory.separate_process)
#    and compare its load flow with PyPSA
if cfg["powerfactory"]["separate_process"]:
    pypsa_pf._build_in_separate_process(model, cfg)
else:
    from pypsa_pf.powerfactory_builder import export_to_powerfactory

    export_to_powerfactory(model, cfg)
