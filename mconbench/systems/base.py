"""Neutral driver contract shared by MCon and the per-tenant baselines.

The experiment runners (provision/deploy/fps) are written against this contract
only, so they never encode a system's execution model. The two models are:

  * MCon           : one instance, N Android *users* on a shared framework.
                     A tenant `handle` is an Android user id (int). Provisioning
                     hotplugs users onto a pre-warmed namespace pool.
  * Baselines      : one full instance *per tenant* (vsoc/gae/redroid/anbox).
                     A tenant `handle` is an adb serial (e.g. "localhost:5556").
                     Provisioning starts N instances and waits for each to boot.

`handle` is therefore opaque to the experiments: they receive it from
`provision(...)` and hand it straight back to `deploy(...)` / `measure_fps_round(...)`,
which each driver interprets in its own terms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TenantResult:
    """Outcome of provisioning a single tenant."""

    handle: Any                       # MCon: Android user id; baselines: adb serial
    ready: bool                       # did it reach operational (boot_completed + launcher)?
    duration_s: Optional[float] = None  # request-issued -> operational, seconds


@dataclass
class ProvisionSummary:
    """Result of provisioning N tenants concurrently."""

    total_s: Optional[float]          # first request issued -> last tenant operational
    tenants: List[TenantResult] = field(default_factory=list)

    @property
    def ready_count(self) -> int:
        return sum(1 for t in self.tenants if t.ready)

    def ready_handles(self) -> List[Any]:
        return [t.handle for t in self.tenants if t.ready]


class Driver(ABC):
    """Every system driver implements this neutral lifecycle.

    Provisioning is split into two phases so MCon's one-time framework cost is
    not re-timed per density:

      * ``prepare_pool(n)`` — one-time / per-density setup that is *not* part of
        the measured provision latency (MCon: boot + warm the namespace pool;
        baselines: no-op, since they cannot pre-warm a shared framework).
      * ``provision(n)``    — the *measured* step that brings N tenants up and
        leaves them running (MCon: hotplug onto the warm pool; baselines: start
        N instances and wait for readiness).

    ``teardown()`` stops the running tenants between trials/densities;
    ``reset(capacity)`` restores a clean state (MCon: fresh userdata; baselines:
    remove per-instance overlays for up to ``capacity`` tenants).
    """

    name: str = "driver"

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def reset(self, capacity: Optional[int] = None) -> None:
        """Restore a clean slate (remove any prior tenants/userdata)."""

    @abstractmethod
    def prepare_pool(self, n_tenants: int) -> None:
        """Un-measured setup for up to ``n_tenants`` tenants (may be a no-op)."""

    @abstractmethod
    def provision(
        self,
        n: int,
        interval: float = 1.0,
        boot_timeout: float = 180.0,
        json_out: Optional[Path] = None,
    ) -> Optional[ProvisionSummary]:
        """Bring up ``n`` tenants (measured) and leave them running.

        Returns ``None`` if the tenants could not be brought up at all (e.g. the
        root instance failed to boot), which the caller treats as a failed
        density.
        """

    @abstractmethod
    def teardown(self) -> None:
        """Stop the currently running tenants."""

    # -- workload -----------------------------------------------------------
    @abstractmethod
    def deploy(self, app_files: List[Path], handles: List[Any]) -> Dict[str, Any]:
        """Deploy ``app_files`` to the given tenant ``handles``.

        Returns a timing summary with at least: ``total_s``, ``physical_s``,
        ``map_s``, ``n_installed``, ``n_attempted``, ``packages``, ``errors``.
        """

    @abstractmethod
    def measure_fps_round(
        self,
        assignments: Dict[Any, str],
        startup_s: float = 8.0,
        window_s: float = 60.0,
        drive: bool = True,
        min_frames: int = 1,
    ) -> Dict[Any, Dict[str, Any]]:
        """Run one FPS round for ``{handle: package}`` and return per-tenant stats.

        Each value is ``{package, pid, fps, frames, ok}``.
        """
