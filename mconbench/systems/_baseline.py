"""Shared driver for the one-instance-per-tenant baselines (vsoc/gae/redroid/anbox).

Each tenant is a full instance addressed by its own adb serial. Concrete
subclasses implement only the lifecycle hooks (``_launch``/``_stop``/``_remove``
and, if the adb port stride differs, ``port_stride``); everything *measured* --
concurrent cold-boot provision timing, ``O(N)`` deployment, and per-serial FPS --
lives here and reuses scalebench's proven ``adb_utils`` / ``container_utils`` /
``fps_profiler`` helpers (the same ones evaluate.py drives), addressing each
instance as Android user 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from .base import Driver, ProvisionSummary, TenantResult


class BaselineDriver(Driver):
    name = "baseline"
    port_stride = 1                       # adb port gap between instances (avd/gae -> 2)
    launcher_process = "com.android.launcher3"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        base_dir = os.environ.get("BASE_DIR") or cfg.get("paths.base_dir")
        if not base_dir or "${" in str(base_dir):
            raise SystemExit("BASE_DIR is not set (export it or set paths.base_dir in the config)")
        self.base_dir = Path(base_dir)
        scalebench = cfg.get("paths.scalebench_dir") or str(self.base_dir / "scalebench")
        self.scalebench_dir = Path(scalebench)
        self.python = sys.executable or "python3"

        # Connection / readiness knobs (per-system overrides under systems.<name>).
        self.base_adb_port = int(cfg.get(f"systems.{self.name}.base_adb_port", cfg.get("adb.start_port", 5555)))
        self.base_monitor_port = int(cfg.get(f"systems.{self.name}.base_monitor_port", 60000))
        self.ready_interval = float(cfg.get(f"systems.{self.name}.ready_interval_s", 2.0))
        self.wait_launcher = bool(cfg.get(f"systems.{self.name}.wait_launcher", True))
        self.launcher_process = str(
            cfg.get(f"systems.{self.name}.launcher_process", self.launcher_process)
        )
        # A system may have several possible launcher packages (e.g. GAE images
        # ship Pixel/AOSP launchers); readiness accepts any of them.
        self.launcher_candidates = [c for c in re.split(r"[,\s]+", self.launcher_process) if c]

        # Env passed to the lifecycle shell scripts.
        self.env = dict(os.environ)
        self.env.setdefault("BASE_DIR", str(self.base_dir))

        self._current_n = 0                       # last provisioned count (for teardown)
        densities = self._densities()
        self._max_capacity = max(densities) if densities else 0

    def _densities(self) -> List[int]:
        return sorted(set(self.cfg.get("sweep.densities", []) or []))

    # -- lifecycle hooks (subclasses implement) -----------------------------
    def _launch(self, n: int) -> bool:
        """Start ``n`` instances (block until launched, not necessarily ready)."""
        raise NotImplementedError

    def _stop(self, n: int) -> None:
        """Stop up to ``n`` running instances."""
        raise NotImplementedError

    def _remove(self, n: int) -> None:
        """Stop and delete per-instance state (overlays/logs) for ``n`` instances."""
        raise NotImplementedError

    # -- addressing ---------------------------------------------------------
    def serial(self, idx: int) -> str:
        return f"localhost:{self.base_adb_port + idx * self.port_stride}"

    def serials(self, n: int) -> List[str]:
        return [self.serial(i) for i in range(n)]

    def _resolve_serials(self, n: int, t0: float, boot_timeout: float) -> List[str]:
        """Serials to wait on after launch.

        Deterministic systems (vsoc/gae/redroid) know their serials up front from
        the fixed port mapping. Discovery-based systems (anbox, whose adb ports
        are assigned dynamically by the gateway) override this to poll
        ``adb devices`` until the tenants appear.
        """
        return self.serials(n)

    # -- shell helper -------------------------------------------------------
    def _sh(self, script: Path, args: List[str], timeout: Optional[float] = None) -> int:
        proc = subprocess.run(["bash", str(script), *args], env=self.env, timeout=timeout)
        return proc.returncode

    # -- neutral contract ---------------------------------------------------
    def reset(self, capacity: Optional[int] = None) -> None:
        n = int(capacity) if capacity else self._max_capacity
        if n > 0:
            self._remove(n)

    def prepare_pool(self, n_tenants: int) -> None:
        # Baselines cannot pre-warm a shared framework: every tenant is a full
        # stack cold-booted at provision time. Intentionally a no-op.
        return None

    def provision(
        self,
        n: int,
        interval: float = 1.0,
        boot_timeout: float = 180.0,
        json_out: Optional[Path] = None,
    ) -> Optional[ProvisionSummary]:
        """Cold-boot ``n`` instances concurrently and wait for each to become ready.

        The scenario matches the paper's concurrent cold boot: prior per-instance
        state is removed first so this is a true cold boot, and the measured clock
        starts when the launch request is issued (not during overlay cleanup).
        """
        self._remove(n)                # ensure a cold boot (fresh per-instance state)
        t0 = time.time()
        if not self._launch(n):
            self._stop(n)
            return None
        self._current_n = n

        serials = self._resolve_serials(n, t0, boot_timeout)
        results = self._wait_ready(serials, t0, boot_timeout)
        tenants = [
            TenantResult(handle=s, ready=results[s]["ready"], duration_s=results[s]["duration_s"])
            for s in serials
        ]
        ready_durs = [t.duration_s for t in tenants if t.ready and t.duration_s is not None]
        total_s = max(ready_durs) if ready_durs else None
        summary = ProvisionSummary(total_s=total_s, tenants=tenants)
        if json_out:
            self._write_summary(json_out, n, interval, summary)
        return summary

    def teardown(self) -> None:
        if self._current_n:
            self._stop(self._current_n)
            self._current_n = 0

    # -- readiness ----------------------------------------------------------
    def _wait_ready(self, serials: List[str], t0: float, boot_timeout: float) -> Dict[str, Dict[str, Any]]:
        adb = self._adb()
        results: Dict[str, Dict[str, Any]] = {s: {"ready": False, "duration_s": None} for s in serials}
        pending = list(serials)
        deadline = t0 + boot_timeout
        while pending:
            for s in list(pending):
                subprocess.run(["adb", "connect", s], capture_output=True, text=True)
                if self._is_ready(adb, s):
                    dur = time.time() - t0
                    results[s] = {"ready": True, "duration_s": dur}
                    print(f"[{self.name}] {s} ready after {dur:.1f}s")
                    pending.remove(s)
            if not pending:
                break
            if time.time() >= deadline:
                print(f"[{self.name}] readiness timeout ({boot_timeout:.0f}s); pending: {pending}")
                break
            time.sleep(self.ready_interval)
        return results

    def _boot_completed(self, adb, serial: str) -> bool:
        for prop in ("sys.boot_completed", "dev.bootcomplete"):
            res = adb.adb_shell(serial, ["getprop", prop], print_output=False, timeout=10)
            if (res.get("stdout") or "").strip().strip("[]").strip() == "1":
                return True
        ps = adb.adb_shell(serial, ["ps", "-A"], print_output=False, timeout=10)
        return "system_server" in (ps.get("stdout") or "")

    def _is_ready(self, adb, serial: str) -> bool:
        if not self._boot_completed(adb, serial):
            return False
        if self.wait_launcher and self.launcher_candidates:
            ps = adb.adb_shell(serial, ["ps", "-A"], print_output=False, timeout=10)
            ps_out = ps.get("stdout") or ""
            if not any(cand in ps_out for cand in self.launcher_candidates):
                return False
        return True

    # -- deployment ---------------------------------------------------------
    def deploy(self, app_files: List[Path], handles: List[Any]) -> Dict[str, Any]:
        """Install every app on every instance's user 0 (the ``O(N)`` baseline path).

        Unlike MCon (install-once + logical map), per-tenant stacks must copy and
        install each package into each isolated instance. Instances install in
        parallel; within an instance the packages install sequentially (AOSP has
        no parallel install). The measured window spans the whole batch.
        """
        adb = self._adb()
        packages: Dict[Any, List[str]] = {h: [] for h in handles}
        errors: Dict[Any, str] = {}

        def _install(serial: Any) -> None:
            try:
                before = set(adb.get_user_installed_packages(serial, 0))
                for pkg_file in app_files:
                    adb.install_apk(serial, 0, str(pkg_file))
                after = set(adb.get_user_installed_packages(serial, 0))
                packages[serial] = sorted(after - before)
            except Exception as exc:  # pragma: no cover - defensive
                errors[serial] = str(exc)

        t0 = time.time()
        threads = [threading.Thread(target=_install, args=(h,), daemon=True) for h in handles]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        total = time.time() - t0

        # Corpus = apps present on every tenant (intersection); n_installed is the
        # per-tenant app count (min across tenants) so the deploy metric reports
        # apps-per-tenant rather than the aggregate.
        per_tenant = [set(v) for v in packages.values()] or [set()]
        corpus = sorted(set.intersection(*per_tenant)) if per_tenant else []
        n_installed = min((len(v) for v in packages.values()), default=0)
        return {
            "packages": corpus,
            "n_installed": n_installed,
            "n_attempted": len(app_files),
            "tenants": list(handles),
            "physical_s": total,          # baselines: all install work is physical
            "map_s": 0.0,                 # no logical-mapping phase
            "total_s": total,
            "errors": errors,
        }

    # -- fps ----------------------------------------------------------------
    def measure_fps_round(
        self,
        assignments: Dict[Any, str],
        startup_s: float = 8.0,
        window_s: float = 60.0,
        drive: bool = True,
        monkey_events: int = 100000,
        min_frames: int = 1,
    ) -> Dict[Any, Dict[str, Any]]:
        """Run one FPS round: one app per instance (user 0), driven with ``monkey``.

        Mirrors MCon's methodology (gfxinfo frame-time histogram via fps_profiler)
        but iterates over per-tenant *serials* instead of Android users on a shared
        serial. ``assignments`` maps serial -> base package.
        """
        adb = self._adb()
        cu, fps_profiler = self._container()
        debug = bool(os.environ.get("MCONBENCH_FPS_DEBUG"))
        user = 0

        # 1. clean slate, then start each app on its instance's default display.
        for serial, pkg in assignments.items():
            cu.clear_user_display_cache(serial)
            adb.stop_package(serial, pkg)
            adb.start_package(serial, user, pkg)

        # 2. startup phase, then reset gfxinfo so the histogram reflects the window.
        time.sleep(startup_s)
        for serial, pkg in assignments.items():
            adb.adb_shell(serial, ["dumpsys", "gfxinfo", pkg, "reset"], print_output=False)

        # 3. drive interaction for the measurement window.
        procs: Dict[Any, Any] = {}
        if drive:
            for serial, pkg in assignments.items():
                res = adb.adb_shell(
                    serial,
                    [
                        "monkey", "--user", str(user),
                        "--pct-touch", "50", "--pct-motion", "50",
                        "--throttle", "500", "-p", pkg, str(monkey_events),
                    ],
                    print_output=False,
                    async_=True,
                )
                procs[serial] = res.get("process")
        time.sleep(window_s)

        # 4. resolve each instance's pid (with retry) and read gfxinfo for it.
        out: Dict[Any, Dict[str, Any]] = {}
        for serial, pkg in assignments.items():
            pid: Optional[int] = None
            for _ in range(10):
                pid = cu.get_pid_by_package_and_user(serial, pkg, user)
                if pid is not None:
                    break
                time.sleep(0.5)
            fps = 0.0
            frames = 0
            if pid is not None:
                data = fps_profiler.measure_app_fps(serial, pid) or {}
                rec = data.get(str(pid)) or (next(iter(data.values())) if data else None)
                if rec:
                    fps = float(rec.get("fps") or 0.0)
                    frames = int(rec.get("total_frames") or 0)
            out[serial] = {"package": pkg, "pid": pid, "fps": fps, "frames": frames, "ok": frames >= min_frames}
            if debug:
                print(f"[fps-dbg] {serial} pkg={pkg} pid={pid} frames={frames} fps={fps:.1f}")

        # 5. cleanup: stop monkey workers + apps so the next round starts clean.
        for proc in procs.values():
            if proc and proc.poll() is None:
                proc.terminate()
        for serial, pkg in assignments.items():
            adb.adb_shell(serial, ["pkill", "-f", "monkey"], print_output=False)
            adb.stop_package(serial, pkg)
        return out

    # -- scalebench helper imports -----------------------------------------
    def _adb(self):
        sb = str(self.scalebench_dir)
        if sb not in sys.path:
            sys.path.insert(0, sb)
        import adb_utils  # type: ignore
        return adb_utils

    def _container(self):
        sb = str(self.scalebench_dir)
        if sb not in sys.path:
            sys.path.insert(0, sb)
        import container_utils  # type: ignore
        import fps_profiler  # type: ignore
        return container_utils, fps_profiler

    # -- misc ---------------------------------------------------------------
    def _write_summary(self, json_out: Path, n: int, interval: float, summary: ProvisionSummary) -> None:
        payload = {
            "system": self.name,
            "requested": n,
            "ready_count": summary.ready_count,
            "total_s": summary.total_s,
            "interval_s": interval,
            "tenants": [
                {"handle": t.handle, "ready": t.ready, "duration_s": t.duration_s}
                for t in summary.tenants
            ],
        }
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(payload, indent=2))
