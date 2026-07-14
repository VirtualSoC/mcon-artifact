#!/usr/bin/env python3
"""mcon control utility rewritten in Python."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from container_utils import (  # type: ignore
    get_user_id_by_instance,
    user_id_to_display_id,
    clear_user_display_cache,
)
from adb_utils import adb_shell, detect_adb, get_adb_devices  # type: ignore

try:
    import resource  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Windows fallback
    resource = None  # type: ignore


def log(level: str, message: str) -> None:
    timestamp = datetime.now().strftime("%a %b %d %I:%M:%S %p %Z %Y")
    print(f"{timestamp} {level}: {message}")


def format_timestamp(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        log("ERROR", f"{name} must be set (e.g. export {name}=/path/to/vsoc)")
        sys.exit(1)
    return value


def to_int(value: Optional[str], default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        log("WARN", f"invalid integer '{value}', falling back to {default}")
        return default


@dataclass
class DisplayProfile:
    """A per-tenant screen profile bound to a device slice at request time."""

    width: int
    height: int
    refresh: Optional[int] = None
    dpi: Optional[int] = None

    @property
    def monitor_arg(self) -> str:
        arg = f"{self.width}x{self.height}"
        if self.refresh:
            arg += f"@{self.refresh}"
        return arg


def parse_profile(token: str) -> DisplayProfile:
    """Parse a profile token 'WxH[@R][:DPI]' (e.g. '1080x1920@60:420')."""
    token = token.strip()
    res_ref, _, dpi_str = token.partition(":")
    res, _, ref = res_ref.partition("@")
    w_str, _, h_str = res.partition("x")
    try:
        width, height = int(w_str), int(h_str)
    except ValueError as exc:
        raise ValueError(f"invalid profile {token!r}; expected WxH[@R][:DPI]") from exc
    return DisplayProfile(
        width=width,
        height=height,
        refresh=int(ref) if ref else None,
        dpi=int(dpi_str) if dpi_str else None,
    )


def load_profiles(tokens: Optional[List[str]], profiles_file: Optional[str]) -> List[DisplayProfile]:
    """Collect profiles from repeated --profile tokens and/or a --profiles-file."""
    raw: List[str] = []
    if profiles_file:
        with open(profiles_file) as fh:
            raw.extend(s for s in (line.strip() for line in fh) if s and not s.startswith("#"))
    if tokens:
        raw.extend(tokens)
    return [parse_profile(t) for t in raw]


def raise_nofile_limit(target: int = 1_048_576) -> None:
    if not resource:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired_soft = min(target, hard) if hard > 0 else target
        if soft >= desired_soft:
            return
        resource.setrlimit(resource.RLIMIT_NOFILE, (desired_soft, hard))
    except (ValueError, OSError) as exc:
        log("WARN", f"unable to raise RLIMIT_NOFILE: {exc}")


@dataclass
class Config:
    base_dir: Path
    bliss_img_path: Path
    log_dir: Path
    log_file: Path
    pid_file: Path
    serial_log: Path
    monitor_port: int
    monitor_timeout: int
    default_count: int
    cpu_threads: int
    userdata_img: Path
    userdata_bkp: Path
    bridge_port: int
    adb_target: str
    launcher_process: str
    launcher_timeout: int
    adb_connect_timeout: float
    adb_connect_retries: int

    @classmethod
    def from_env(cls) -> "Config":
        base_dir = Path(require_env("BASE_DIR"))
        bliss_default = base_dir / "img" / "bliss"
        bliss_img_path = Path(os.environ.get("GUEST_IMG_PATH", str(bliss_default)))
        log_dir = base_dir / "log"
        # Add a time suffix to the logfile so each run gets a distinct file.
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = log_dir / f"mcon-{ts}.log"
        pid_file = log_dir / "mcon.pid"
        serial_log = log_dir / "mcon_kernel.log"
        monitor_port = to_int(os.environ.get("MONITOR_PORT"), 55_555)
        monitor_timeout = to_int(os.environ.get("MONITOR_TIMEOUT"), 10)
        default_count = to_int(os.environ.get("COUNT"), 1)
        cpu_threads = to_int(os.environ.get("CPU_THREADS"), _detect_cpu_threads())
        userdata_img = Path(os.environ.get("USERDATA_IMG", str(bliss_img_path / "userdata.qcow2")))
        userdata_bkp = Path(os.environ.get("USERDATA_BKP", str(bliss_img_path / "userdata_bkp.qcow2")))
        bridge_port = to_int(os.environ.get("BRIDGE_PORT"), 5_555)
        adb_target = os.environ.get("ADB_TARGET", "localhost:5555")
        launcher_process = os.environ.get("LAUNCHER_PROCESS", "com.android.launcher3")
        launcher_timeout = to_int(os.environ.get("LAUNCHER_TIMEOUT"), 600)
        adb_connect_timeout = float(os.environ.get("ADB_CONNECT_TIMEOUT", "1.0"))
        adb_connect_retries = to_int(os.environ.get("ADB_CONNECT_RETRIES"), 5)
        return cls(
            base_dir=base_dir,
            bliss_img_path=bliss_img_path,
            log_dir=log_dir,
            log_file=log_file,
            pid_file=pid_file,
            serial_log=serial_log,
            monitor_port=monitor_port,
            monitor_timeout=monitor_timeout,
            default_count=default_count,
            cpu_threads=cpu_threads,
            userdata_img=userdata_img,
            userdata_bkp=userdata_bkp,
            bridge_port=bridge_port,
            adb_target=adb_target,
            launcher_process=launcher_process,
            launcher_timeout=launcher_timeout,
            adb_connect_timeout=adb_connect_timeout,
            adb_connect_retries=adb_connect_retries,
        )


def _detect_cpu_threads() -> int:
    for cmd in ("nproc",):
        try:
            result = subprocess.run([cmd], capture_output=True, check=True, text=True)
            value = int(result.stdout.strip())
            if value > 0:
                return value
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
            continue
    try:
        import multiprocessing

        value = multiprocessing.cpu_count()
        if value > 0:
            return value
    except (ImportError, NotImplementedError):
        pass
    return 12  # sensible fallback


def ensure_log_dir(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)


def start_instance(cfg: Config, display_count: int) -> None:
    ensure_log_dir(cfg)
    raise_nofile_limit()

    qemu_cmd = [
        "bin/qemu-system-x86_64",
        "-pidfile",
        str(cfg.pid_file),
        "-accel",
        "kvm",
        "-cpu",
        "max",
        "-smp",
        str(cfg.cpu_threads - 4),
        "-object",
        "memory-backend-memfd,id=mem,size=24G,share=on",
        "-machine",
        "memory-backend=mem",
        "-m",
        "24G",
        "-device",
        "intel-hda",
        "-device",
        "hda-duplex",
        "-kernel",
        str(cfg.bliss_img_path / "kernel"),
        "-append",
        "nokaslr no_timer_check syscall_hardening=off root=/dev/ram0 androidboot.hardware=redroid androidboot.fstab_suffix=redroid androidboot.selinux=permissive console=ttyS0",
        "-initrd",
        str(cfg.bliss_img_path / "ramdisk.img"),
        "-drive",
        f"index=0,if=virtio,id=system,file={cfg.bliss_img_path / 'system.img'},format=raw,readonly=on",
        "-drive",
        f"index=1,if=virtio,id=vendor,file={cfg.bliss_img_path / 'vendor.img'},format=raw,readonly=on",
        "-drive",
        f"index=2,if=virtio,id=userdata,file={cfg.userdata_img},format=qcow2",
        "-display",
        "none",
        "-device",
        (
            "teleport,gl_debug=off,gl_log_level=0,gl_log_to_host=off,display_width=1080,"  # noqa: E501
            "display_height=1920,window_width=540,window_height=960,refresh_rate=60,"  # noqa: E501
            f"display_count={display_count},headless_mode=on,multi_process=on,bridge_port={cfg.bridge_port}"
        ),
        "-serial",
        f"file:{cfg.serial_log}",
        "-netdev",
        "user,id=wlan",
        "-device",
        "virtio-net-pci,netdev=wlan",
        "-netdev",
        "user,id=cell",
        "-device",
        "virtio-net-pci,netdev=cell",
        "-name",
        "debug-threads=on",
        "-monitor",
        f"telnet:127.0.0.1:{cfg.monitor_port},server,nowait",
    ]

    if cfg.pid_file.exists():
        cfg.pid_file.unlink(missing_ok=True)

    log("INFO", f"starting mcon instance (monitor port {cfg.monitor_port})")
    # Launch QEMU without -daemonize and redirect stdout/stderr to the logfile so
    # the guest logs continue to be written even after this Python process exits.
    try:
        # Ensure the parent log directory exists
        ensure_log_dir(cfg)
        # Open logfile in append mode and hand the FD to the child process.
        fh = open(str(cfg.log_file), "ab")
        proc = subprocess.Popen(
            qemu_cmd,
            stdout=fh,
            stderr=fh,
            cwd=str(cfg.base_dir),
            start_new_session=True,
        )
        # We do not wait here; QEMU will create the pidfile when ready.
        time.sleep(0.1)
    except OSError as exc:
        log("ERROR", f"failed to launch QEMU: {exc}")
        sys.exit(1)


def _read_pid(pid_file: Path) -> Optional[int]:
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def stop_instance(cfg: Config) -> None:
    pid = _read_pid(cfg.pid_file)
    if not pid:
        log("INFO", f"no running mcon instance (missing pidfile {cfg.pid_file})")
        return

    def _pid_alive(proc: int) -> bool:
        try:
            os.kill(proc, 0)
            return True
        except ProcessLookupError:
            return False

    if not _pid_alive(pid):
        log("WARN", f"pid {pid} from {cfg.pid_file} not running; cleaning up pidfile")
        cfg.pid_file.unlink(missing_ok=True)
        return

    log("INFO", f"stopping mcon instance (pid={pid})")
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        if not _pid_alive(pid):
            cfg.pid_file.unlink(missing_ok=True)
            return
        time.sleep(1)

    log("WARN", f"force killing pid {pid}")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    cfg.pid_file.unlink(missing_ok=True)


def reset_userdata(cfg: Config) -> None:
    if not cfg.userdata_bkp.exists():
        log("ERROR", f"backup userdata not found at {cfg.userdata_bkp}")
        return
    log("INFO", "restoring userdata from backup")
    shutil.copy2(cfg.userdata_bkp, cfg.userdata_img)


def warm_containers(cfg: Config, count: int) -> None:
    if count <= 1:
        log("INFO", "nothing to warm (COUNT <= 1)")
        return
    created: List[str] = []
    for i in range(1, count):
        name = f"CON-{i + 1}"
        log("INFO", f"warming container {name}")
        result = subprocess.run(
            ["cmd", "-d", "localhost:5555", "shell", "cmd", "container", "create", name],
            check=False,
        )
        if result.returncode == 0:
            created.append(name)
        else:
            log("WARN", f"failed to create container {name}: returncode={result.returncode}")

    if not created:
        return

    if not ensure_adb_target(cfg):
        log("WARN", "unable to verify container initialization (adb unavailable)")
        return

    _wait_for_container_initialization(cfg, created)


def _wait_for_container_initialization(cfg: Config, containers: List[str]) -> None:
    pending = {name for name in containers}
    if not pending:
        return

    deadline = time.time() + cfg.launcher_timeout
    log("INFO", f"waiting for {len(pending)} containers to initialize")
    while pending and time.time() < deadline:
        res = adb_shell(cfg.adb_target, ["cmd", "container", "list"], print_output=False, timeout=5)
        if res.get("returncode", 1) == 0:
            status = _parse_container_initialization(res.get("stdout", ""))
            initialized = [name for name in pending if status.get(name)]
            for name in initialized:
                pending.discard(name)
                log("INFO", f"container {name} initialized")
        else:
            stderr = res.get("stderr", "")
            log("DEBUG", f"cmd container list failed (ret={res.get('returncode')}): {stderr.strip()}")
        if pending:
            time.sleep(1)

    if pending:
        for name in pending:
            log("WARN", f"container {name} not initialized within {cfg.launcher_timeout}s")
    else:
        log("INFO", "all warmed containers initialized")


def _parse_container_initialization(output: str) -> Dict[str, bool]:
    status: Dict[str, bool] = {}
    for match in _CONTAINER_INFO_RE.finditer(output):
        name = match.group("name").strip()
        flags = match.group("flags").replace(" ", "")
        status[name] = "INITIALIZED" in flags.split("|")
    return status


_PROMPT = b"(qemu) "


def _read_until_prompt(sock: socket.socket, timeout: int) -> str:
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if _PROMPT in data:
                break
    except socket.timeout:
        pass
    return data.decode(errors="ignore")


_USER_FIELD_RE = re.compile(r"^u(\d+)_")
_CONTAINER_INFO_RE = re.compile(
    r"UserInfo\[id=(?P<id>\d+), name=(?P<name>[^,]+), [^\]]*flags=(?P<flags>[^\]]+)\]"
)


def _launcher_running_for_user(output: str, target_pkg: str, user_id: int) -> bool:
    for line in output.splitlines():
        if target_pkg not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        match = _USER_FIELD_RE.match(parts[0])
        if match and int(match.group(1)) == user_id:
            return True
    return False


def wait_for_launcher(cfg: Config, user_id: Optional[int]) -> Tuple[bool, float]:
    if user_id is None:
        return False, time.time()

    deadline = time.time() + cfg.launcher_timeout
    while time.time() < deadline:
        res = adb_shell(
            cfg.adb_target,
            ["ps", "-A"],
            print_output=False,
            timeout=5,
        )
        if res.get("returncode", 1) == 0:
            if _launcher_running_for_user(res.get("stdout", ""), cfg.launcher_process, user_id):
                return True, time.time()
        else:
            stderr = res.get("stderr", "")
            log("DEBUG", f"adb ps failed (ret={res.get('returncode')}): {stderr.strip()}")
        time.sleep(0.01)

    return False, time.time()


def ensure_adb_target(cfg: Config) -> bool:
    connected = get_adb_devices(return_first=False)
    if connected and cfg.adb_target in connected:
        return True

    try:
        detect_adb(targets=[cfg.adb_target], timeout=cfg.adb_connect_timeout)
    except Exception as exc:  # pragma: no cover - defensive
        log("WARN", f"adb connect failed: {exc}")
    time.sleep(1)
    connected = get_adb_devices(return_first=False)
    if connected and cfg.adb_target in connected:
        log("INFO", f"connected to {cfg.adb_target}")
        return True

    log("WARN", f"unable to connect to adb target {cfg.adb_target}")
    return False


def wait_for_boot(
    cfg: Config,
    timeout: Optional[float] = None,
    poll_interval: float = 2.0,
    require_launcher: bool = False,
) -> bool:
    """Block until the root instance reports sys.boot_completed=1.

    Replaces the fixed warm-up sleep used by older sweep scripts so drivers can
    proceed as soon as the framework is actually ready (typically ~10s).
    """
    if timeout is None:
        timeout = float(cfg.launcher_timeout)
    deadline = time.time() + timeout
    booted = False
    while time.time() < deadline:
        ensure_adb_target(cfg)
        res = adb_shell(cfg.adb_target, ["getprop", "sys.boot_completed"], print_output=False, timeout=5)
        if res.get("returncode", 1) == 0 and (res.get("stdout", "") or "").strip() == "1":
            booted = True
            break
        time.sleep(poll_interval)
    if not booted:
        log("WARN", f"root did not report boot_completed within {timeout:.0f}s")
        return False
    if require_launcher:
        ok, _ = wait_for_launcher(cfg, 0)
        if not ok:
            log("WARN", "root launcher not detected after boot_completed")
            return False
    log("INFO", "root instance booted")
    return True


def _hotplug_summary(tasks: List[HotplugTask], count: int, interval: float) -> Dict[str, Any]:
    """Reduce per-tenant HotplugTask state into a JSON-serializable summary."""
    start_times = [t.start_time for t in tasks if t.start_time > 0]
    ready_times = [t.end_time for t in tasks if t.ready and t.end_time is not None]
    first_start = min(start_times) if start_times else None
    last_ready = max(ready_times) if ready_times else None
    total = (last_ready - first_start) if (first_start is not None and last_ready is not None) else None
    tenants = []
    for t in tasks:
        duration = (t.end_time - t.start_time) if (t.end_time and t.start_time > 0) else None
        tenants.append(
            {
                "index": t.index,
                "instance_id": t.instance_id,
                "user_id": t.user_id,
                "profile": t.profile.monitor_arg if t.profile else None,
                "dpi": t.profile.dpi if t.profile else None,
                "start": t.start_time or None,
                "end": t.end_time,
                "duration_s": duration,
                "ready": t.ready,
                "status": "ready" if t.ready else ("failed" if t.start_time <= 0 else "timeout"),
            }
        )
    return {
        "count": count,
        "interval_s": interval,
        "first_start": first_start,
        "last_ready": last_ready,
        "total_s": total,
        "ready_count": len(ready_times),
        "tenants": tenants,
    }


def hotplug_containers(
    cfg: Config,
    count: int,
    profiles: Optional[List[DisplayProfile]] = None,
    interval: float = 1.0,
) -> Dict[str, Any]:
    adb_ready = ensure_adb_target(cfg)
    tasks = [HotplugTask(index=i + 1) for i in range(count)]
    if profiles:
        for i, task in enumerate(tasks):
            task.profile = profiles[i % len(profiles)]

    def launcher_monitor() -> None:
        _monitor_launchers(cfg, tasks, adb_ready)

    monitor_thread = threading.Thread(target=launcher_monitor, name="launcher-monitor", daemon=True)
    if adb_ready:
        monitor_thread.start()

    for task in tasks:
        _launch_hotplug(cfg, task.index, task)  # issue provision request (serial, fast)
        time.sleep(interval)  # pace successive requests (issuance-rate knob)

    if adb_ready:
        monitor_thread.join(timeout=cfg.launcher_timeout + 5)

    for task in tasks:
        if not task.reported:
            if task.end_time is None:
                task.end_time = time.time()
            _log_task_completion(cfg, task)

    # Stamp guest-side DPI after readiness is measured (QEMU already set the
    # resolution/refresh at slice creation). Done post-measurement so the extra
    # adb calls do not perturb provision-latency timing.
    for task in tasks:
        if task.ready and task.profile is not None and task.profile.dpi is not None:
            _apply_display_profile(cfg, task)

    summary = _hotplug_summary(tasks, count, interval)
    if summary["total_s"] is not None:
        log(
            "INFO",
            "hotplugged {} containers! start={} end={} total={:.3f}s".format(
                summary["ready_count"],
                format_timestamp(summary["first_start"]),
                format_timestamp(summary["last_ready"]),
                summary["total_s"],
            ),
        )
    return summary

def _extract_container_id(monitor_output: str) -> str:
    container_id = "unknown"
    for line in monitor_output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("(qemu)"):
            continue
        if line.isdigit():
            container_id = line
    return container_id


class HotplugTask:
    def __init__(self, index: int) -> None:
        self.index = index
        self.profile: Optional[DisplayProfile] = None
        self.start_time: float = 0.0
        self.instance_id: Optional[int] = None
        self.user_id: Optional[int] = None
        self.waiting = False
        self.ready = False
        self.end_time: Optional[float] = None
        self.reported = False


def _launch_hotplug(cfg: Config, idx: int, task: HotplugTask) -> None:
    host = "127.0.0.1"
    try:
        with socket.create_connection((host, cfg.monitor_port), timeout=cfg.monitor_timeout) as sock:
            greeting = _read_until_prompt(sock, cfg.monitor_timeout)
            # if greeting and greeting.strip():
            #     log("DEBUG", f"monitor greeting -> {greeting.strip()}")
            command = "vsoc container new"
            if task.profile is not None:
                command += f" {task.profile.monitor_arg}"
            sock.sendall((command + "\n").encode())
            response = _read_until_prompt(sock, cfg.monitor_timeout)
    except OSError as exc:
        log("WARN", f"hotplug {idx}/{task.index} failed to start: {exc}")
        task.end_time = time.time()
        return

    task.start_time = time.time()
    container_id = _extract_container_id(response)
    task.instance_id = int(container_id) if container_id.isdigit() else None
    if task.instance_id is not None:
        try:
            task.user_id = get_user_id_by_instance(task.instance_id)
        except ValueError:
            task.user_id = None


def _log_task_completion(cfg: Config, task: HotplugTask) -> None:
    if task.reported:
        return
    container_id = task.instance_id if task.instance_id is not None else "unknown"
    if task.start_time <= 0:
        # The provision request never got a start timestamp (monitor connect failed
        # or the request was rejected before issuing). Avoid computing a bogus
        # epoch-based duration (previously surfaced as duration=1.78e9s).
        log(
            "WARN",
            f"hotplug container {container_id} never started (request failed); status=failed",
        )
        task.reported = True
        return
    end_time = task.end_time or time.time()
    duration = end_time - task.start_time
    status = "ready" if task.ready else "timeout"
    start_fmt = format_timestamp(task.start_time)
    end_fmt = format_timestamp(end_time)

    log(
        "INFO",
        f"hotplugged container {container_id} start={start_fmt} end={end_fmt} duration={duration:.3f}s status={status}",
    )
    if not task.ready and task.user_id is not None:
        log(
            "WARN",
            f"launcher for container {container_id} user {task.user_id} not detected within {cfg.launcher_timeout}s",
        )
    task.reported = True


def _apply_display_profile(cfg: Config, task: HotplugTask) -> None:
    """Stamp guest-side display attributes (currently DPI via `wm density`).

    QEMU binds resolution/refresh to the device slice at `vsoc container new`
    time; DPI is an Android-side setting, so we apply it once the tenant is up.
    """
    profile = task.profile
    if profile is None or profile.dpi is None or task.user_id is None:
        return
    # The user->display map may have grown since it was last cached.
    clear_user_display_cache(cfg.adb_target)
    display_id = user_id_to_display_id(cfg.adb_target, task.user_id, physical=False)
    if display_id is None:
        log("WARN", f"no display for user {task.user_id}; skipping density")
        return
    res = adb_shell(
        cfg.adb_target,
        ["wm", "density", str(profile.dpi), "-d", str(display_id)],
        print_output=False,
        timeout=5,
    )
    if res.get("returncode", 1) == 0:
        log("INFO", f"set density {profile.dpi} on display {display_id} (user {task.user_id})")
    else:
        log("WARN", f"wm density failed on display {display_id}: {res.get('stderr', '').strip()}")


def _monitor_launchers(cfg: Config, tasks: List[HotplugTask], adb_ready: bool) -> None:
    if not adb_ready:
        return

    deadline = time.time() + cfg.launcher_timeout
    pending: Dict[int, HotplugTask] = {}
    consecutive_adb_failures = 0

    while time.time() < deadline:
        for task in tasks:
            if task.user_id is None or task.ready:
                continue
            if task.user_id not in pending:
                pending[task.user_id] = task
                if not task.waiting:
                    log(
                        "INFO",
                        f"hotplug {task.index}: waiting for {cfg.launcher_process} (id {task.instance_id} user {task.user_id})",
                    )
                    task.waiting = True

        if not pending:
            launching_pending = any(task.instance_id is None and not task.reported for task in tasks)
            if not launching_pending:
                break
            time.sleep(0.01)
            continue

        res = adb_shell(cfg.adb_target, ["ps", "-A"], print_output=False, timeout=5)
        if res.get("returncode", 1) == 0:
            consecutive_adb_failures = 0
            output = res.get("stdout", "")
            for line in output.splitlines():
                if cfg.launcher_process not in line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                match = _USER_FIELD_RE.match(parts[0])
                if not match:
                    continue
                uid = int(match.group(1))
                task = pending.get(uid)
                if task and not task.ready:
                    task.ready = True
                    task.end_time = time.time()
                    pending.pop(uid, None)
                    _log_task_completion(cfg, task)
        else:
            consecutive_adb_failures += 1
            stderr = res.get("stderr", "")
            # Back off instead of spinning at ~100 Hz when the adb device vanishes
            # (a dead instance previously flooded the log with identical errors).
            if consecutive_adb_failures <= 3 or consecutive_adb_failures % 30 == 0:
                log(
                    "DEBUG",
                    f"adb ps failed (ret={res.get('returncode')}, x{consecutive_adb_failures}): {stderr.strip()}",
                )
            # If the target has been unreachable for a sustained period the instance
            # is gone; stop waiting rather than burning the full launcher timeout.
            if consecutive_adb_failures >= 30:
                log(
                    "WARN",
                    "adb target unreachable for 30 consecutive polls; instance appears dead, aborting launcher monitor",
                )
                break
            time.sleep(1.0)
            continue

        if all(t.reported for t in tasks):
            return

        time.sleep(0.01)

    now = time.time()
    for task in tasks:
        if task.reported or task.user_id is None or task.ready:
            continue
        if task.end_time is None:
            task.end_time = now
        _log_task_completion(cfg, task)



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the mcon QEMU instance")
    parser.add_argument(
        "action",
        choices=["run", "warm", "hotplug", "stop", "rm", "remove", "delete"],
        help="operation to perform",
    )
    parser.add_argument(
        "count",
        nargs="?",
        type=int,
        help="optional COUNT argument for run/warm/hotplug",
    )
    parser.add_argument(
        "--profile",
        action="append",
        metavar="WxH[@R][:DPI]",
        help="per-tenant display profile for hotplug (repeatable; cycled across tenants)",
    )
    parser.add_argument(
        "--profiles-file",
        metavar="PATH",
        help="file with one profile token per line, applied to hotplugged tenants",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="[run] block until the root instance reports boot_completed",
    )
    parser.add_argument(
        "--boot-timeout",
        type=float,
        default=None,
        help="[run --wait] max seconds to wait for boot (default: launcher timeout)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="[hotplug] seconds between successive provision requests (issuance rate)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="[hotplug] write a structured JSON summary of provision timings",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = Config.from_env()

    def resolved_count() -> int:
        value = args.count if args.count is not None else cfg.default_count
        return value if value > 0 else 1

    if args.action == "run":
        start_instance(cfg, resolved_count())
        if args.wait:
            if not wait_for_boot(cfg, timeout=args.boot_timeout):
                sys.exit(2)
    elif args.action == "warm":
        warm_containers(cfg, resolved_count())
    elif args.action == "hotplug":
        profiles = load_profiles(args.profile, args.profiles_file)
        summary = hotplug_containers(
            cfg, resolved_count(), profiles=profiles or None, interval=args.interval
        )
        if args.json:
            out = Path(args.json)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(summary, indent=2))
            log("INFO", f"wrote hotplug summary -> {out}")
    elif args.action == "stop":
        stop_instance(cfg)
    elif args.action in {"rm", "remove", "delete"}:
        stop_instance(cfg)
        reset_userdata(cfg)
    else:  # pragma: no cover
        log("ERROR", f"unsupported action {args.action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
