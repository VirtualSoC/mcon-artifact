"""mconbench CLI.

    python -m mconbench run provision_concurrent --system mcon --config config/default.yaml
    python -m mconbench plot all --data data/reference/
    python -m mconbench plot deploy --data data/runs/<timestamp>/
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .config import Config
from .experiments import provision, deploy, fps
from .plots import PLOTTERS, load_records, plot_all, plot_experiment
from .systems.mcon import MConDriver
from .systems.vsoc import VSoCDriver
from .systems.gae import GAEDriver
from .systems.redroid import RedroidDriver
from .systems.anbox import AnboxDriver

SYSTEMS = {
    "mcon": MConDriver,
    "vsoc": VSoCDriver,
    "gae": GAEDriver,
    "redroid": RedroidDriver,
    "anbox": AnboxDriver,
}

EXPERIMENTS = {
    "provision_concurrent": provision.run,
    "deploy": deploy.run,
    "fps": fps.run,
}


def _make_driver(system: str, cfg: Config):
    if system not in SYSTEMS:
        raise SystemExit(f"unknown/unsupported system {system!r}; have {sorted(SYSTEMS)}")
    return SYSTEMS[system](cfg)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mconbench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run an experiment and write canonical CSV")
    run_p.add_argument("experiment", choices=sorted(EXPERIMENTS))
    run_p.add_argument("--system", default="mcon")
    run_p.add_argument("--config", default="config/default.yaml")
    run_p.add_argument("--out", default=None, help="output dir (default: <runs_dir>/<timestamp>)")

    plot_p = sub.add_parser("plot", help="regenerate a figure (PDF) from canonical CSV data")
    plot_p.add_argument("experiment", choices=["all", *sorted(PLOTTERS)])
    plot_p.add_argument("--data", required=True, help="canonical CSV file or directory (searched recursively)")
    plot_p.add_argument("--out", default=None, help="output dir (default: output.figures_dir from config)")
    plot_p.add_argument("--config", default="config/default.yaml")

    args = parser.parse_args()

    if args.cmd == "run":
        cfg = Config.load(args.config)
        runs_dir = cfg.get("output.runs_dir", "data/runs")
        out = Path(args.out or Path(runs_dir) / datetime.now().strftime("%Y%m%d-%H%M%S"))
        driver = _make_driver(args.system, cfg)
        EXPERIMENTS[args.experiment](cfg, driver, out)
    elif args.cmd == "plot":
        records = load_records(args.data)
        if not records:
            raise SystemExit(f"no canonical records found under {args.data}")
        out_dir = args.out
        if out_dir is None:
            out_dir = Config.load(args.config).get("output.figures_dir", "data/figures")
        if args.experiment == "all":
            outputs = plot_all(records, out_dir)
        else:
            single = plot_experiment(records, args.experiment, out_dir)
            outputs = [single] if single else []
        for path in outputs:
            print(f"[plot] wrote {path}")
        if not outputs:
            print("[plot] no figures written (no matching data for the requested experiment)")


if __name__ == "__main__":
    main()
