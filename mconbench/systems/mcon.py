"""MCon system driver: wraps the hardened scalebench/platform/mcon.py.

Every measurement primitive lives in mcon.py (boot, warm, hotplug with the
structured --json summary, stop, reset). This driver only orchestrates
subprocess calls and returns parsed results, so experiments never parse logs.
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


class MConDriver(Driver):
    name = "mcon"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        base_dir = os.environ.get("BASE_DIR") or cfg.get("paths.base_dir")
        if not base_dir or "${" in str(base_dir):
            raise SystemExit("BASE_DIR is not set (export it or set paths.base_dir in the config)")
        self.base_dir = Path(base_dir)
        scalebench = cfg.get("paths.scalebench_dir") or str(self.base_dir / "scalebench")
        self.scalebench_dir = Path(scalebench)
        self.mcon = self.scalebench_dir / "platform" / "mcon.py"
        if not self.mcon.exists():
            raise SystemExit(f"mcon.py not found at {self.mcon}")
        self.python = sys.executable or "python3"
        # adb bridge serial for the whole instance (all tenants share one serial).
        self.adb_target = os.environ.get("ADB_TARGET") or f"localhost:{cfg.get('adb.bridge_port', 5555)}"

        # mcon.py reads BASE_DIR / GUEST_IMG_PATH / ports from the environment.
        self.env = dict(os.environ)
        self.env.setdefault("BASE_DIR", str(self.base_dir))
        bliss = cfg.get("paths.bliss_img_path")
        if bliss and "${" not in str(bliss):
            self.env.setdefault("GUEST_IMG_PATH", str(bliss))
        self.env.setdefault("MONITOR_PORT", str(cfg.get("adb.monitor_port", 55555)))
        self.env.setdefault("BRIDGE_PORT", str(cfg.get("adb.bridge_port", 5555)))
        self.env.setdefault("LAUNCHER_TIMEOUT", str(cfg.get("mcon.launcher_timeout_s", 600)))

        # Cache of (user_id, package) whose runtime permissions we've pre-granted
        # (see _grant_permissions) so we grant once, not on every FPS round.
        self._perm_granted: set = set()

    # -- primitives ---------------------------------------------------------
    def _run(self, args: List[str], timeout: Optional[float] = None) -> int:
        proc = subprocess.run(
            [self.python, str(self.mcon), *args],
            env=self.env,
            timeout=timeout,
        )
        return proc.returncode

    def reset(self, capacity: Optional[int] = None) -> None:
        """Restore a clean userdata image (removes any prior tenants).

        ``capacity`` is part of the neutral contract (baselines use it to know
        how many per-instance overlays to remove) and is ignored here: MCon has
        a single userdata image regardless of tenant count.
        """
        self._run(["rm"])

    def boot(self, count: int = 1, boot_timeout: Optional[float] = 180.0) -> bool:
        """Start the root instance and block until boot_completed."""
        args = ["run", str(count), "--wait"]
        if boot_timeout:
            args += ["--boot-timeout", str(boot_timeout)]
        return self._run(args) == 0

    def warm(self, count: int) -> None:
        self._run(["warm", str(count)])

    def hotplug(
        self,
        count: int,
        interval: float = 1.0,
        profiles_file: Optional[Path] = None,
        json_out: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        """Provision `count` tenants; return the structured JSON summary."""
        args = ["hotplug", str(count), "--interval", str(interval)]
        if profiles_file:
            args += ["--profiles-file", str(profiles_file)]
        if json_out:
            args += ["--json", str(json_out)]
        self._run(args)
        if json_out and Path(json_out).exists():
            return json.loads(Path(json_out).read_text())
        return None

    def stop(self) -> None:
        self._run(["stop"])

    # -- neutral contract ---------------------------------------------------
    def prepare_pool(self, n_tenants: int) -> None:
        """Pre-create the framework namespace pool (un-measured, one-time).

        ``warm C`` creates accounts CON-2..CON-C, i.e. ``C - 1`` namespaces, so
        to back ``n_tenants`` tenants we warm ``n_tenants + 1``. The root
        instance is booted to warm the pool and stopped again; the pool persists
        in the userdata overlay for the subsequent measured ``provision``.
        """
        if not self.boot(count=1):
            raise SystemExit("prepare_pool: root instance failed to boot")
        self.warm(n_tenants + 1)
        self.stop()

    def provision(
        self,
        n: int,
        interval: float = 1.0,
        boot_timeout: float = 180.0,
        json_out: Optional[Path] = None,
    ) -> Optional[ProvisionSummary]:
        """Boot the root instance and hotplug ``n`` tenants onto the warm pool.

        Leaves the instance running for a subsequent deploy/fps step; the caller
        invokes ``teardown()`` when done.
        """
        if not self.boot(count=1, boot_timeout=boot_timeout):
            self.stop()
            return None
        summary = self.hotplug(n, interval=interval, json_out=json_out)
        if not summary:
            return None
        tenants = [
            TenantResult(
                handle=t.get("user_id"),
                ready=bool(t.get("ready")),
                duration_s=t.get("duration_s"),
            )
            for t in summary.get("tenants", [])
        ]
        return ProvisionSummary(total_s=summary.get("total_s"), tenants=tenants)

    def teardown(self) -> None:
        self.stop()

    # -- deployment ---------------------------------------------------------
    def _adb(self):
        """Lazy-import scalebench's adb/container helpers (they live outside this pkg)."""
        sb = str(self.scalebench_dir)
        if sb not in sys.path:
            sys.path.insert(0, sb)
        import adb_utils  # type: ignore
        return adb_utils

    def installed_packages(self, user_id: int) -> List[str]:
        """Third-party packages visible to `user_id` (base names, suffix stripped)."""
        adb = self._adb()
        return sorted(adb.get_user_installed_packages(self.adb_target, user_id))

    def deploy(self, app_files: List[Path], tenant_users: List[int]) -> Dict[str, Any]:
        """Deploy apps to `tenant_users` the MCon way and return a timing summary.

        Phase 1 (shared, sequential): physically install every app once on user 0.
        The package names are recovered by diffing user 0's package list, matching
        evaluate.py's proven flow. Phase 2 (per-tenant, serial by default): logically
        map each shared package into every tenant via `pm install-existing` -- no file
        copy. Concurrency is bounded by ``experiments.deploy.map_concurrency`` (default
        1 = serial, matching evaluate.py); unbounded parallelism convoys on the
        PackageManagerService install lock and breaks near-O(1) scaling.
        The measured window spans both phases (first install command -> last mapping).
        """
        adb = self._adb()
        before = set(adb.get_user_installed_packages(self.adb_target, 0))
        t0 = time.time()
        for pkg_file in app_files:
            adb.install_apk(self.adb_target, 0, str(pkg_file))
        after = set(adb.get_user_installed_packages(self.adb_target, 0))
        t_phys = time.time()
        packages = sorted(after - before)

        errors: Dict[int, List[str]] = {}
        mapped: Dict[int, int] = {u: 0 for u in tenant_users}

        # Bound per-tenant mapping concurrency. The reference (evaluate.py) maps
        # SERIALLY; one unbounded thread per tenant makes every `pm install-existing`
        # convoy on PackageManagerService's global install lock, so per-call latency
        # explodes at high N (measured: map_s ~63s@N=8 -> ~1500s@N=16), destroying the
        # near-O(1) deployment trend. Default to serial (=1) to match the reference.
        map_concurrency = max(1, int(self.cfg.get("experiments.deploy.map_concurrency", 1)))
        sem = threading.Semaphore(map_concurrency)

        def _map_tenant(user: int) -> None:
            with sem:
                for pkg in packages:
                    try:
                        if adb.install_existing_package(self.adb_target, user, pkg):
                            mapped[user] += 1
                    except Exception as exc:  # pragma: no cover - defensive
                        errors.setdefault(user, []).append(f"{pkg}: {exc}")

        threads = [threading.Thread(target=_map_tenant, args=(u,), daemon=True) for u in tenant_users]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        t_end = time.time()

        return {
            "packages": packages,
            "n_installed": len(packages),
            "n_attempted": len(app_files),
            "tenants": list(tenant_users),
            "mapped_per_tenant": mapped,
            "map_concurrency": map_concurrency,
            "physical_s": t_phys - t0,
            "map_s": t_end - t_phys,
            "total_s": t_end - t0,
            "errors": errors,
        }

    # -- fps ----------------------------------------------------------------
    def _container(self):
        """Lazy-import scalebench's container_utils + fps_profiler helpers."""
        sb = str(self.scalebench_dir)
        if sb not in sys.path:
            sys.path.insert(0, sb)
        import container_utils  # type: ignore
        import fps_profiler  # type: ignore
        return container_utils, fps_profiler

    def tenant_users(self) -> List[int]:
        """Android user ids of the live tenants (root user 0 excluded)."""
        cu, _ = self._container()
        cu.clear_user_display_cache(self.adb_target)
        return sorted(u for u in (cu.query_all_users(self.adb_target) or []) if u != 0)

    def _grant_permissions(self, adb, user_id: int, package: str) -> None:
        """Pre-grant an app's requested runtime permissions for a user.

        Some apps raise a first-run permission dialog (GrantPermissionsActivity)
        on launch that sits on top and blocks the app from rendering -- observed
        as frames=0 for those tenants during FPS measurement (esp. on secondary
        displays, where monkey's random taps do not reliably dismiss it). Granting
        the requested runtime permissions up front means no dialog appears and the
        app proceeds to its UI. `pm grant` harmlessly errors for non-runtime or
        non-requested permissions, so failures are ignored. Cached per (user, pkg).
        """
        if (user_id, package) in self._perm_granted:
            return
        self._perm_granted.add((user_id, package))  # add first: avoid retry storms on failure
        res = adb.adb_shell(self.adb_target, ["dumpsys", "package", package], print_output=False, timeout=10)
        if res.get("returncode", 1) != 0:
            return
        perms: List[str] = []
        in_requested = False
        for line in res.get("stdout", "").splitlines():
            s = line.strip()
            if s.startswith("requested permissions:"):
                in_requested = True
                continue
            if in_requested:
                if s.endswith("permissions:"):  # next section (install/runtime permissions:)
                    break
                m = re.match(r"([A-Za-z0-9_.]+\.permission\.[A-Za-z0-9_]+)", s)
                if m:
                    perms.append(m.group(1))
        for perm in perms:
            adb.adb_shell(
                self.adb_target,
                ["pm", "grant", "--user", str(user_id), package, perm],
                print_output=False,
                timeout=5,
            )

    def measure_fps_round(
        self,
        assignments: Dict[int, str],
        startup_s: float = 8.0,
        window_s: float = 60.0,
        drive: bool = True,
        monkey_events: int = 100000,
        min_frames: int = 1,
    ) -> Dict[int, Dict[str, Any]]:
        """Run one FPS round and return per-tenant frame statistics.

        This mirrors the reference driver (evaluate.py, invoked by
        mcon_measure_sweep.py): start one app per tenant on its own display,
        optionally drive it with `monkey` (touch/motion) for the measurement
        window, then read `dumpsys gfxinfo` and derive FPS from the frame-time
        histogram (via fps_profiler). FPS is attributed to a tenant by the pid.

        assignments : {user_id: base_package}
        returns     : {user_id: {package, pid, fps, frames, ok}}
        """
        adb = self._adb()
        cu, fps_profiler = self._container()
        # The user->display mapping is cached process-globally by container_utils
        # and is rebuilt from the *current* instance's running containers. Drop any
        # stale entries from a previous density so new tenants resolve correctly.
        cu.clear_user_display_cache(self.adb_target)
        debug = bool(os.environ.get("MCONBENCH_FPS_DEBUG"))

        # 1. clean slate, then start each app on its tenant's display. Pre-grant
        #    each app's runtime permissions first so a first-run permission dialog
        #    (GrantPermissionsActivity) does not sit on top and block rendering.
        for user, pkg in assignments.items():
            self._grant_permissions(adb, user, pkg)
            adb.stop_package(self.adb_target, pkg)
            adb.start_package(self.adb_target, user, pkg)

        # 2. startup phase: let every app come up, then reset gfxinfo so the
        #    histogram reflects (mostly) the measurement window, not launch jank.
        #    Packages are installed under their base name (pm install-existing does
        #    not rename), so reset by the base name.
        time.sleep(startup_s)
        for user, pkg in assignments.items():
            adb.adb_shell(
                self.adb_target,
                ["dumpsys", "gfxinfo", pkg, "reset"],
                print_output=False,
            )

        # 3. drive interaction for the measurement window.
        procs: Dict[int, Any] = {}
        if drive:
            for user, pkg in assignments.items():
                res = adb.adb_shell(
                    self.adb_target,
                    [
                        "monkey", "--user", str(user),
                        "--pct-touch", "50", "--pct-motion", "50",
                        "--throttle", "500", "-p", pkg, str(monkey_events),
                    ],
                    print_output=False,
                    async_=True,
                )
                procs[user] = res.get("process")
        time.sleep(window_s)

        # 4. resolve each tenant's pid (retry: heavy apps at high density spawn
        #    slowly, so a single early query would miss the later ones) and read
        #    gfxinfo for that pid.
        out: Dict[int, Dict[str, Any]] = {}
        for user, pkg in assignments.items():
            pid: Optional[int] = None
            for _ in range(10):
                pid = cu.get_pid_by_package_and_user(self.adb_target, pkg, user)
                if pid is not None:
                    break
                time.sleep(0.5)
            fps = 0.0
            frames = 0
            if pid is not None:
                data = fps_profiler.measure_app_fps(self.adb_target, pid) or {}
                rec = data.get(str(pid)) or (next(iter(data.values())) if data else None)
                if rec:
                    fps = float(rec.get("fps") or 0.0)
                    frames = int(rec.get("total_frames") or 0)
            # Require a minimum frame count: a handful of frames yields a
            # histogram-derived FPS that is statistically meaningless (e.g. 2
            # frames -> 50 fps), so treat such instances as not rendering.
            out[user] = {"package": pkg, "pid": pid, "fps": fps, "frames": frames, "ok": frames >= min_frames}
            if debug:
                print(f"[fps-dbg] user={user} pkg={pkg} pid={pid} frames={frames} fps={fps:.1f}")

        # 5. cleanup: stop monkey workers + apps so the next round starts clean.
        for proc in procs.values():
            if proc and proc.poll() is None:
                proc.terminate()
        adb.adb_shell(self.adb_target, ["pkill", "-f", "monkey"], print_output=False)
        for user, pkg in assignments.items():
            adb.stop_package(self.adb_target, pkg)
        return out
