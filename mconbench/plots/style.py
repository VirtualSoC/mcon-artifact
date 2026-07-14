"""Shared styling + aggregation for the figure plotters.

All plotters consume only the canonical schema (see ``mconbench.schema``): rows
are aggregated per (system, x_value) into mean +/- standard error across trials,
then drawn with a consistent per-system color/marker/order so MCon and every
baseline are directly comparable across figures.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

from ..schema import Record

# Per-system display metadata. `z` puts MCon on top; order drives the legend.
SYSTEM_STYLE: Dict[str, dict] = {
    "mcon":    {"label": "MCon",        "color": "#FF5733", "marker": "o", "lw": 2.4, "z": 6},
    "redroid": {"label": "Redroid",     "color": "#1f77b4", "marker": "s", "lw": 1.6, "z": 3},
    "anbox":   {"label": "Anbox Cloud", "color": "#2ca02c", "marker": "^", "lw": 1.6, "z": 3},
    "vsoc":    {"label": "vSoC",        "color": "#9467bd", "marker": "D", "lw": 1.6, "z": 3},
    "gae":     {"label": "GAE",         "color": "#8c564b", "marker": "v", "lw": 1.6, "z": 3},
}
SYSTEM_ORDER: List[str] = ["mcon", "redroid", "anbox", "vsoc", "gae"]

Series = Dict[str, List[Tuple[float, float, float]]]  # system -> [(x, mean, sem)]


def apply_style() -> None:
    import matplotlib

    matplotlib.rcParams.update({
        "figure.figsize": (4.2, 3.0),
        "font.size": 10,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "grid.linestyle": "--",
        "legend.frameon": False,
        "legend.fontsize": 8,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
        "pdf.fonttype": 42,   # keep text editable in the PDF (no type-3 outlines)
        "ps.fonttype": 42,
    })


def _style_for(system: str) -> dict:
    return SYSTEM_STYLE.get(
        system, {"label": system, "color": None, "marker": "o", "lw": 1.6, "z": 3}
    )


def systems_in_order(present: Iterable[str]) -> List[str]:
    present = set(present)
    ordered = [s for s in SYSTEM_ORDER if s in present]
    ordered += sorted(s for s in present if s not in SYSTEM_ORDER)  # unknowns last
    return ordered


def aggregate(records: Iterable[Record], experiment: str, metric: str) -> Series:
    """Return {system: [(x, mean, sem), ...sorted by x]} for one experiment+metric."""
    buckets: Dict[str, Dict[float, List[float]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r.experiment == experiment and r.metric == metric:
            buckets[r.system][r.x_value].append(r.value)
    series: Series = {}
    for system, xmap in buckets.items():
        pts: List[Tuple[float, float, float]] = []
        for x in sorted(xmap):
            vals = xmap[x]
            mean = statistics.fmean(vals)
            sem = (statistics.stdev(vals) / (len(vals) ** 0.5)) if len(vals) > 1 else 0.0
            pts.append((x, mean, sem))
        series[system] = pts
    return series


def max_density(records: Iterable[Record], experiment: str) -> Dict[str, float]:
    """Return {system: max_density} from the max_density marker rows, if present."""
    out: Dict[str, float] = {}
    for r in records:
        if r.experiment == experiment and r.metric == "max_density":
            out[r.system] = max(out.get(r.system, 0.0), r.value)
    return out


def plot_series(ax, series: Series, xlabel: str, ylabel: str, xlog: bool = True, ylog: bool = False) -> None:
    from matplotlib.ticker import FuncFormatter

    all_x: set = set()
    for system in systems_in_order(series.keys()):
        pts = series.get(system) or []
        if not pts:
            continue
        st = _style_for(system)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        es = [p[2] for p in pts]
        all_x.update(xs)
        ax.errorbar(
            xs, ys, yerr=es, label=st["label"], color=st.get("color"),
            marker=st["marker"], markersize=4, linewidth=st["lw"],
            capsize=2, zorder=st.get("z", 3),
        )
    if xlog:
        ax.set_xscale("log", base=2)
    if ylog:
        ax.set_yscale("log")
    if all_x:
        ticks = sorted(all_x)
        ax.set_xticks(ticks)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{int(round(v))}"))
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.legend()
