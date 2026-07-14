"""Load canonical result CSVs for plotting.

Accepts either a single CSV or a directory (searched recursively), so a fresh
run dir (``data/runs/<timestamp>/`` with one CSV per experiment) and the bundled
``data/reference/`` both work the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..schema import Record, read_records


def load_records(data_path: str | Path) -> List[Record]:
    path = Path(data_path)
    if path.is_file():
        return read_records(path)
    if not path.exists():
        raise SystemExit(f"data path not found: {path}")
    records: List[Record] = []
    for csv_path in sorted(path.rglob("*.csv")):
        try:
            records.extend(read_records(csv_path))
        except Exception as exc:  # pragma: no cover - skip malformed CSVs
            print(f"[plot] skipping {csv_path}: {exc}")
    return records
