"""Canonical result schema for the MCon reproduction harness.

Every experiment driver emits rows in this single long-format schema, and every
plotter consumes *only* this schema. This decoupling is what lets figures be
regenerated from bundled reference data on any machine (no GPU / custom QEMU
required), while a fresh measurement run produces byte-compatible CSVs.

One row = one measured value.

Columns
-------
system       : one of SYSTEMS (mcon, redroid, anbox, vsoc, gae)
experiment   : one of EXPERIMENTS
x_name       : the swept independent variable name (e.g. "density", "profiles")
x_value      : numeric value of the independent variable
metric       : what `value` measures (see METRICS)
value        : the measured number
unit         : unit of `value` (e.g. "s", "fps", "count")
trial        : 0-based repetition index
timestamp    : ISO-8601 UTC of when the row was recorded
extra        : JSON blob for anything experiment-specific (never parsed by plots)
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

SYSTEMS = ("mcon", "redroid", "anbox", "vsoc", "gae")

# Experiment ids map 1:1 to the five in-scope paper figures.
EXPERIMENTS = (
    "provision_concurrent",   # -> fig/container_boot_time.pdf
    "provision_under_load",   # -> fig/container_provision.pdf
    "warmpool",               # -> fig/warmpool.pdf
    "deploy",                 # -> fig/container_install_time.pdf
    "fps",                    # -> fig/fps.pdf   (density = derived annotation)
)

# Canonical metric names + their units. Keep this list closed so plotters can
# rely on exact strings.
METRICS = {
    "total_latency_s": "s",     # provision_concurrent: first-start -> last-ready
    "tenant_latency_s": "s",    # per-tenant provision latency (for breakdown/CDF)
    "alloc_latency_s": "s",     # provision_under_load: single new allocation
    "provision_latency_s": "s", # warmpool: end-to-end provision under profile P
    "deploy_total_s": "s",      # deploy: all apps on all N tenants
    "avg_fps": "fps",           # fps: mean FPS across successful apps
    "max_density": "count",     # fps/warmpool: highest N that survives all trials
}

CSV_HEADER = [
    "system",
    "experiment",
    "x_name",
    "x_value",
    "metric",
    "value",
    "unit",
    "trial",
    "timestamp",
    "extra",
]


@dataclass
class Record:
    system: str
    experiment: str
    x_name: str
    x_value: float
    metric: str
    value: float
    unit: str = ""
    trial: int = 0
    timestamp: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.system not in SYSTEMS:
            raise ValueError(f"unknown system {self.system!r}; expected one of {SYSTEMS}")
        if self.experiment not in EXPERIMENTS:
            raise ValueError(f"unknown experiment {self.experiment!r}; expected one of {EXPERIMENTS}")
        if self.metric not in METRICS:
            raise ValueError(f"unknown metric {self.metric!r}; expected one of {sorted(METRICS)}")
        if not self.unit:
            self.unit = METRICS[self.metric]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_row(self) -> Dict[str, Any]:
        row = asdict(self)
        row["extra"] = json.dumps(self.extra, separators=(",", ":"), sort_keys=True)
        return row


def write_records(path: str | Path, records: Iterable[Record], append: bool = False) -> Path:
    """Write records to a canonical CSV, creating parent dirs and header as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not (append and path.exists())
    mode = "a" if append else "w"
    with path.open(mode, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        if write_header:
            writer.writeheader()
        for rec in records:
            writer.writerow(rec.to_row())
    return path


def read_records(path: str | Path) -> List[Record]:
    """Read a canonical CSV back into Record objects."""
    path = Path(path)
    out: List[Record] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            out.append(
                Record(
                    system=row["system"],
                    experiment=row["experiment"],
                    x_name=row["x_name"],
                    x_value=float(row["x_value"]),
                    metric=row["metric"],
                    value=float(row["value"]),
                    unit=row.get("unit", ""),
                    trial=int(row.get("trial", 0) or 0),
                    timestamp=row.get("timestamp", ""),
                    extra=json.loads(row["extra"]) if row.get("extra") else {},
                )
            )
    return out
