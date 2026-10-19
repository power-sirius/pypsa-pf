"""Build the PowerFactory grid from a saved snapshot model, in its own Python process.

    python -m pypsa_pf.build model.pkl [config.yaml]

``python -m pypsa_pf config.yaml`` runs this automatically after the PyPSA part, so
PyPSA and PowerFactory never share one Python process. Run it by hand to retry only the
PowerFactory step. With a second argument, that config replaces the one saved in model.pkl
(PowerFactory settings only: the snapshot model itself is not recomputed).
"""

import logging
import pickle
import sys

from .config import load_config
from .powerfactory_builder import export_to_powerfactory


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit("usage: python -m pypsa_pf.build model.pkl [config.yaml]")
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    with open(argv[0], "rb") as fh:
        saved = pickle.load(fh)
    if isinstance(saved, dict):  # written by pypsa_pf.run: model + the config used
        model, cfg = saved["model"], saved["cfg"]
        if len(argv) > 1:
            cfg = load_config(argv[1])
    else:  # a bare SnapshotModel
        if len(argv) < 2:
            sys.exit("this model.pkl has no config inside: pass config.yaml as second argument")
        model, cfg = saved, load_config(argv[1])
    export_to_powerfactory(model, cfg)


if __name__ == "__main__":
    main()
