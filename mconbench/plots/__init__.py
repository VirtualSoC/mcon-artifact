"""Figure regeneration from canonical result CSVs.

    from mconbench.plots import load_records, plot_all, plot_experiment, PLOTTERS

The matplotlib backend is forced to the headless ``Agg`` here (before any
pyplot import) so figures render on servers without a display.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from .loader import load_records
from .figures import PLOTTERS, plot_all, plot_experiment

__all__ = ["load_records", "PLOTTERS", "plot_all", "plot_experiment"]
