"""Command line: python -m pypsa_pf config.yaml [options]   (or: pypsa-pf config.yaml [options])"""

import argparse
import logging

from . import run
from .config import load_config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pypsa-pf", description=__doc__)
    ap.add_argument("config", help="YAML configuration file")
    ap.add_argument("--network", help="override the .nc file of the config")
    ap.add_argument("--snapshot", help='export this timestamp instead of the minimum-inertia one, e.g. "2013-01-11 11:00"')
    ap.add_argument("--no-powerfactory", action="store_true", help="stop after the PyPSA power flow")
    ap.add_argument("--save", "--dump", dest="save", metavar="DIR",
                    help="also save the snapshot model as CSV tables in DIR (to inspect it)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    overrides = {}
    if args.network:
        overrides["network"] = args.network
    if args.snapshot:
        overrides["snapshot"] = {"method": "timestamp", "timestamp": args.snapshot}
    cfg = load_config(args.config, **overrides)
    run(cfg, to_powerfactory=not args.no_powerfactory, dump_dir=args.save)


if __name__ == "__main__":
    main()
