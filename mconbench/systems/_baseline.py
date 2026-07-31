"""Shared driver for the one-instance-per-tenant baselines (vsoc/gae/redroid/anbox).

Each tenant is a full instance addressed by its own adb serial. Concrete
subclasses implement only the lifecycle hooks (``_launch``/``_stop``/``_remove``
and, if the adb port stride differs, ``port_stride``); everything *measured* --
concurrent cold-boot provision timing, ``O(N)`` deployment, and per-serial FPS --
lives here and reuses mcon-artifact's proven ``adb_utils`` / ``container_utils`` /
``fps_profiler`` helpers (the same ones evaluate.py drives), addressing each
instance as Android user 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from ..config import Config
from .base import Driver, IssueStamp, ProvisionSummary, TenantResult


def _clock() -> float:
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(clock_id) if clock_id is not None else time.monotonic()


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
        artifact = cfg.get("paths.artifact_dir") or str(self.base_dir / "mcon-artifact")
        self.artifact_dir = Path(artifact)
        self.python = sys.executable or "python3"

        # Connection / readiness knobs (per-system overrides under systems.<name>).
        self.base_adb_port = int(cfg.get(f"systems.{self.name}.base_adb_port", cfg.get("adb.start_port", 5555)))
        self.base_monitor_port = int(cfg.get(f"systems.{self.name}.base_monitor_port", 60000))
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
    def _launch(
        self,
        n: int,
        interval: float,
        ready_poll_interval: float,
    ) -> Optional[List[IssueStamp]]:
        """Issue ``n`` starts and return their host timestamps in request order."""
        raise NotImplementedError

    def _prepare_launch(self, n: int) -> bool:
        """Perform unmeasured setup required before issue timestamps begin."""
        return True

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

    def _resolve_serials(
        self,
        n: int,
        t0: float,
        boot_timeout: float,
        poll_interval: float,
    ) -> List[str]:
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

    def _launch_batch_script(
        self,
        script: Path,
        args: List[str],
        n: int,
        interval: float,
        ready_poll_interval: float,
    ) -> Optional[List[IssueStamp]]:
        """Run a batch launcher that writes ``index wall_time`` issue records."""
        issue_path: Optional[Path] = None
        endpoint_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f"{self.name}-issues-", delete=False) as issue_file:
                issue_path = Path(issue_file.name)
            with tempfile.NamedTemporaryFile(prefix=f"{self.name}-endpoints-", delete=False) as endpoint_file:
                endpoint_path = Path(endpoint_file.name)
            env = dict(self.env)
            env["MCONBENCH_ISSUE_INTERVAL_S"] = str(max(0.0, interval))
            env["MCONBENCH_READY_POLL_INTERVAL_S"] = str(max(0.001, ready_poll_interval))
            env["MCONBENCH_ISSUE_LOG"] = str(issue_path)
            env["MCONBENCH_ENDPOINT_LOG"] = str(endpoint_path)
            proc = subprocess.run(["bash", str(script), *args], env=env)
            if proc.returncode != 0:
                return None
            stamps: List[IssueStamp] = []
            endpoints: Dict[int, str] = {}
            for line in endpoint_path.read_text().splitlines():
                index_text, endpoint = line.split(maxsplit=1)
                endpoints[int(index_text)] = endpoint
            for line in issue_path.read_text().splitlines():
                index_text, wall_text, monotonic_text = line.split(maxsplit=2)
                index = int(index_text)
                stamps.append(
                    IssueStamp(
                        index=index,
                        wall_time_s=float(wall_text),
                        monotonic_s=float(monotonic_text),
                        handle_hint=endpoints.get(index),
                    )
                )
            stamps.sort(key=lambda stamp: stamp.index)
            if len(stamps) != n:
                print(f"[{self.name}] launcher recorded {len(stamps)}/{n} issue timestamps")
                return None
            return stamps
        finally:
            if issue_path is not None:
                issue_path.unlink(missing_ok=True)
            if endpoint_path is not None:
                endpoint_path.unlink(missing_ok=True)

    def _schedule_launches(
        self,
        n: int,
        interval: float,
        launch_one: Callable[[int], Union[bool, str]],
    ) -> Optional[List[IssueStamp]]:
        """Start one worker per request, paced against absolute target times."""
        stamps: List[Optional[IssueStamp]] = [None] * n
        succeeded = [False] * n
        handle_hints: List[Optional[str]] = [None] * n
        first_target = _clock()

        def _worker(index: int) -> None:
            target = first_target + index * max(0.0, interval)
            delay = target - _clock()
            if delay > 0:
                time.sleep(delay)
            stamps[index] = IssueStamp(index, time.time(), _clock())
            result = launch_one(index)
            succeeded[index] = bool(result)
            if isinstance(result, str):
                handle_hints[index] = result

        threads = [threading.Thread(target=_worker, args=(index,), daemon=True) for index in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        complete = [
            IssueStamp(
                index=stamp.index,
                wall_time_s=stamp.wall_time_s,
                monotonic_s=stamp.monotonic_s,
                handle_hint=handle_hints[stamp.index],
            )
            for stamp in stamps
            if stamp is not None
        ]
        return complete if len(complete) == n and all(succeeded) else None

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
        ready_poll_interval: float = 0.1,
        boot_timeout: float = 180.0,
        ready_timeout: Optional[float] = None,
        json_out: Optional[Path] = None,
    ) -> Optional[ProvisionSummary]:
        """Cold-boot ``n`` instances concurrently and wait for each to become ready.

        The scenario matches the paper's concurrent cold boot: prior per-instance
        state is removed first so this is a true cold boot, and the measured clock
        starts when the launch request is issued (not during overlay cleanup).
        """
        self._remove(n)                # ensure a cold boot (fresh per-instance state)
        if not self._prepare_launch(n):
            return None
        tenant_timeout = float(ready_timeout if ready_timeout is not None else boot_timeout)
        self.env["MCONBENCH_BOOT_TIMEOUT_S"] = str(boot_timeout)
        self.env["MCONBENCH_READY_TIMEOUT_S"] = str(tenant_timeout)
        issues = self._launch(n, interval, ready_poll_interval)
        if not issues:
            self._stop(n)
            return None
        self._current_n = n

        first_issue = min(issues, key=lambda stamp: stamp.monotonic_s)
        hinted_serials = [issue.handle_hint for issue in issues]
        if len(hinted_serials) == n and all(hinted_serials):
            serials = [str(serial) for serial in hinted_serials]
        else:
            serials = self._resolve_serials(
                n,
                first_issue.wall_time_s,
                boot_timeout,
                ready_poll_interval,
            )
        results = self._wait_ready(serials, issues, tenant_timeout, ready_poll_interval)
        tenants = [
            TenantResult(
                handle=serial,
                ready=results[serial]["ready"],
                duration_s=results[serial]["duration_s"],
                issued_at_s=results[serial]["issued_at_s"],
                ready_at_s=results[serial]["ready_at_s"],
                issued_monotonic_s=results[serial]["issued_monotonic_s"],
                ready_monotonic_s=results[serial]["ready_monotonic_s"],
                boot_completed=results[serial]["boot_completed"],
                launcher_started=results[serial]["launcher_started"],
            )
            for serial in serials
        ]
        ready_times = [t.ready_monotonic_s for t in tenants if t.ready and t.ready_monotonic_s is not None]
        total_s = max(ready_times) - first_issue.monotonic_s if ready_times else None
        summary = ProvisionSummary(
            total_s=total_s,
            tenants=tenants,
            issue_interval_s=interval,
            ready_poll_interval_s=ready_poll_interval,
            ready_timeout_s=tenant_timeout,
        )
        if json_out:
            self._write_summary(json_out, n, interval, summary)
        return summary

    def teardown(self) -> None:
        if self._current_n:
            self._stop(self._current_n)
            self._current_n = 0

    # -- readiness ----------------------------------------------------------
    def _wait_ready(
        self,
        serials: List[str],
        issues: List[IssueStamp],
        boot_timeout: float,
        poll_interval: float,
    ) -> Dict[str, Dict[str, Any]]:
        adb = self._adb()
        results: Dict[str, Dict[str, Any]] = {}
        result_lock = threading.Lock()

        def _worker(serial: str, issue: IssueStamp) -> None:
            deadline = issue.monotonic_s + boot_timeout
            next_poll = _clock()
            boot_completed = False
            launcher_started = False
            subprocess.run(["adb", "connect", serial], capture_output=True, text=True)
            while _clock() < deadline:
                boot_completed, launcher_started = self._readiness_state(adb, serial)
                if boot_completed and launcher_started:
                    ready_wall = time.time()
                    ready_monotonic = _clock()
                    duration = ready_monotonic - issue.monotonic_s
                    with result_lock:
                        results[serial] = {
                            "ready": True,
                            "duration_s": duration,
                            "issued_at_s": issue.wall_time_s,
                            "ready_at_s": ready_wall,
                            "issued_monotonic_s": issue.monotonic_s,
                            "ready_monotonic_s": ready_monotonic,
                            "boot_completed": True,
                            "launcher_started": True,
                        }
                    print(f"[{self.name}] {serial} ready after {duration:.3f}s")
                    return
                next_poll += max(0.001, poll_interval)
                delay = next_poll - _clock()
                if delay > 0:
                    time.sleep(delay)
            with result_lock:
                results[serial] = {
                    "ready": False,
                    "duration_s": None,
                    "issued_at_s": issue.wall_time_s,
                    "ready_at_s": None,
                    "issued_monotonic_s": issue.monotonic_s,
                    "ready_monotonic_s": None,
                    "boot_completed": boot_completed,
                    "launcher_started": launcher_started,
                }
            print(f"[{self.name}] {serial} readiness timeout after {boot_timeout:.0f}s")

        threads = [
            threading.Thread(target=_worker, args=(serial, issue), daemon=True)
            for serial, issue in zip(serials, issues)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def _boot_completed(self, adb, serial: str) -> bool:
        res = adb.adb_shell(serial, ["getprop", "sys.boot_completed"], print_output=False, timeout=10)
        return (res.get("stdout") or "").strip().strip("[]").strip() == "1"

    def _launcher_started(self, adb, serial: str) -> bool:
        candidates = list(self.launcher_candidates)
        if not candidates:
            resolved = adb.adb_shell(
                serial,
                [
                    "cmd", "package", "resolve-activity", "--brief", "--user", "0",
                    "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME",
                ],
                print_output=False,
                timeout=10,
            )
            component = (resolved.get("stdout") or "").strip().splitlines()
            if component:
                candidates = [component[-1].split("/", 1)[0]]
        if not candidates:
            return False
        ps = adb.adb_shell(serial, ["ps", "-A"], print_output=False, timeout=10)
        ps_out = ps.get("stdout") or ""
        return any(candidate in ps_out for candidate in candidates)

    def _readiness_state(self, adb, serial: str) -> tuple[bool, bool]:
        boot_completed = self._boot_completed(adb, serial)
        if not boot_completed:
            return False, False
        launcher_started = self._launcher_started(adb, serial) if self.wait_launcher else True
        return boot_completed, launcher_started

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

    # -- artifact helper imports -------------------------------------------
    def _adb(self):
        artifact = str(self.artifact_dir)
        if artifact not in sys.path:
            sys.path.insert(0, artifact)
        import adb_utils  # type: ignore
        return adb_utils

    def _container(self):
        artifact = str(self.artifact_dir)
        if artifact not in sys.path:
            sys.path.insert(0, artifact)
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
            "ready_poll_interval_s": summary.ready_poll_interval_s,
            "ready_timeout_s": summary.ready_timeout_s,
            "tenants": [
                {
                    "handle": t.handle,
                    "ready": t.ready,
                    "duration_s": t.duration_s,
                    "issued_at_s": t.issued_at_s,
                    "ready_at_s": t.ready_at_s,
                    "issued_monotonic_s": t.issued_monotonic_s,
                    "ready_monotonic_s": t.ready_monotonic_s,
                    "boot_completed": t.boot_completed,
                    "launcher_started": t.launcher_started,
                }
                for t in summary.tenants
            ],
        }
        Path(json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(json_out).write_text(json.dumps(payload, indent=2))
