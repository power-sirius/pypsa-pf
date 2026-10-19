"""pypsa-pf: turn one snapshot of a solved PyPSA(-Eur) network into a DIgSILENT PowerFactory model."""

from .config import load_config
from .inertia import compute_inertia, select_snapshot
from .network import load_network
from .powerflow import prepare_power_flow, run_power_flow
from .snapshot import SnapshotModel, build_snapshot_model

__version__ = "0.1.0"
__all__ = [
    "load_config", "load_network", "compute_inertia", "select_snapshot",
    "prepare_power_flow", "run_power_flow", "build_snapshot_model", "SnapshotModel", "run",
]


def run(cfg: dict, network=None, to_powerfactory: bool = True, dump_dir=None):
    """Full pipeline: .nc -> snapshot selection -> AC power flow -> PowerFactory.

    ``dump_dir`` saves the snapshot model: a folder name writes CSV tables (to inspect them),
    a ``*.pkl`` file name a pickle that ``python -m pypsa_pf.build`` can build later.
    """
    n = network if network is not None else load_network(cfg["network"])
    snapshot = select_snapshot(n, cfg)
    setup = prepare_power_flow(n, snapshot, cfg)
    run_power_flow(n, setup, cfg)
    model = build_snapshot_model(n, setup, cfg)
    if dump_dir:
        if str(dump_dir).endswith(".pkl"):
            import pickle

            with open(dump_dir, "wb") as fh:
                pickle.dump(model, fh)
        else:
            model.to_csv(dump_dir)
    if to_powerfactory:
        if cfg["powerfactory"]["separate_process"]:
            _build_in_separate_process(model, cfg)
        else:
            from .powerfactory_builder import export_to_powerfactory

            export_to_powerfactory(model, cfg)
    return model


def _build_in_separate_process(model, cfg: dict) -> None:
    """Save model + config and run ``python -m pypsa_pf.build`` in a new process.

    PyPSA and PowerFactory are never loaded in the same Python process: some endpoint
    security tools block that combination.
    """
    import logging
    import os
    import pickle
    import subprocess
    import sys

    path = os.path.abspath(cfg["powerfactory"]["model_file"])
    with open(path, "wb") as fh:
        pickle.dump({"model": model, "cfg": cfg}, fh)
    logging.getLogger(__name__).info("Model saved to %s; building PowerFactory in a separate process", path)
    result = subprocess.run([sys.executable, "-m", "pypsa_pf.build", path])
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerFactory build process failed (exit code {result.returncode}). The model is saved, "
            f"you can retry only this step with: python -m pypsa_pf.build {path}")
