"""Figure plotters, one per in-scope paper figure.

Each plotter reads canonical records, aggregates the relevant metric, and writes
a PDF. They degrade gracefully: if a data set has no rows for a figure (e.g. only
deploy was run), the plotter prints a note and returns ``None`` instead of
writing an empty figure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..schema import Record
from . import style


def _new_fig():
    import matplotlib.pyplot as plt

    style.apply_style()
    return plt.subplots()


def _save(fig, out_path: Path) -> Path:
    import matplotlib.pyplot as plt

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def _annotate_max_density(ax, records: List[Record], experiment: str, series: style.Series) -> None:
    """Mark each system's max sustained density (paper's circled numbers)."""
    maxd = style.max_density(records, experiment)
    for system in style.systems_in_order(series.keys()):
        n = maxd.get(system)
        pts = series.get(system) or []
        if not n or not pts:
            continue
        # place the marker at the curve point nearest the max density
        x, y, _ = min(pts, key=lambda p: abs(p[0] - n))
        st = style.SYSTEM_STYLE.get(system, {})
        ax.annotate(
            f"{int(round(n))}", xy=(x, y), xytext=(0, 6), textcoords="offset points",
            ha="center", fontsize=7, color=st.get("color"), zorder=st.get("z", 3),
        )


def plot_provision_concurrent(records: List[Record], out_path: Path) -> Optional[Path]:
    series = style.aggregate(records, "provision_concurrent", "total_latency_s")
    if not any(series.values()):
        print("[plot] no provision_concurrent/total_latency_s rows; skipping")
        return None
    fig, ax = _new_fig()
    style.plot_series(
        ax, series,
        xlabel="Number of tenants (N)", ylabel="Provision time (s)",
        xlog=True, ylog=True,
    )
    _annotate_max_density(ax, records, "provision_concurrent", series)
    return _save(fig, out_path)


def plot_deploy(records: List[Record], out_path: Path) -> Optional[Path]:
    series = style.aggregate(records, "deploy", "deploy_total_s")
    if not any(series.values()):
        print("[plot] no deploy/deploy_total_s rows; skipping")
        return None
    fig, ax = _new_fig()
    style.plot_series(
        ax, series,
        xlabel="Number of tenants (N)", ylabel="Deployment time (s)",
        xlog=True, ylog=True,
    )
    return _save(fig, out_path)


def plot_fps(records: List[Record], out_path: Path) -> Optional[Path]:
    series = style.aggregate(records, "fps", "avg_fps")
    if not any(series.values()):
        print("[plot] no fps/avg_fps rows; skipping")
        return None
    fig, ax = _new_fig()
    style.plot_series(
        ax, series,
        xlabel="Number of tenants (N)", ylabel="Average FPS",
        xlog=True, ylog=False,
    )
    ax.axhline(60, ls=":", color="gray", lw=1, zorder=1)  # 60 Hz refresh ceiling
    ax.set_ylim(0, None)
    _annotate_max_density(ax, records, "fps", series)
    return _save(fig, out_path)


# experiment id -> (plotter, output filename matching osdi26-paper/fig/*.pdf)
PLOTTERS: Dict[str, Tuple[Callable[[List[Record], Path], Optional[Path]], str]] = {
    "provision_concurrent": (plot_provision_concurrent, "container_boot_time.pdf"),
    "deploy": (plot_deploy, "container_install_time.pdf"),
    "fps": (plot_fps, "fps.pdf"),
}


def plot_experiment(records: List[Record], experiment: str, out_dir: str | Path) -> Optional[Path]:
    if experiment not in PLOTTERS:
        raise SystemExit(f"no plotter for {experiment!r}; have {sorted(PLOTTERS)}")
    fn, fname = PLOTTERS[experiment]
    return fn(records, Path(out_dir) / fname)


def plot_all(records: List[Record], out_dir: str | Path) -> List[Path]:
    out: List[Path] = []
    for fn, fname in PLOTTERS.values():
        result = fn(records, Path(out_dir) / fname)
        if result is not None:
            out.append(result)
    return out
