"""Redroid driver: inner Docker containers inside an outer QEMU VM, over SSH.

Redroid has two layers:

  * Outer VM  -- a Linux guest (QEMU) reached over SSH at ``ssh_host:ssh_port``.
                 It runs Docker and forwards each container's adb port back to
                 the host, so from the host each tenant is a plain
                 ``localhost:<adb_port>`` target (stride 1), exactly like vSoC.
    * Inner     -- Docker containers started/stopped/removed via ``init.sh`` inside
                                 the VM: ``cd <remote_dir> && ./init.sh run <count>`` /
                                 ``./init.sh stop`` / ``./init.sh rm``.

Only the *inner* container lifecycle is the measured provisioning step (booting
N Android containers). The outer VM is un-measured infrastructure: it must be
reachable before provisioning. By default the driver requires it to be up
already (matching the existing sweep workflow) and errors clearly otherwise; set
``manage_vm=true`` + ``vm_launch_cmd`` to have the driver start it.

Everything host-side (adb connect, readiness polling, O(N) deploy, per-serial
FPS) is inherited unchanged from :class:`BaselineDriver`.

Security: the SSH password is read from ``$REDROID_SSH_PASS`` (never committed)
and passed to ``sshpass`` via the environment (``sshpass -e``), not argv, so it
does not leak into the process list. If no password is set, key-based auth is
used.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import time
from typing import Optional

from ..config import Config
from ._baseline import BaselineDriver


class RedroidDriver(BaselineDriver):
    name = "redroid"
    port_stride = 1

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        # SSH endpoint: environment first (see env.example), then config.
        self.ssh_host = str(os.environ.get("REDROID_SSH_HOST") or cfg.get("systems.redroid.ssh_host", "localhost"))
        self.ssh_port = int(os.environ.get("REDROID_SSH_PORT") or cfg.get("systems.redroid.ssh_port", 2222))
        self.ssh_user = str(os.environ.get("REDROID_SSH_USER") or cfg.get("systems.redroid.ssh_user", "redroid"))
        self.remote_dir = str(cfg.get("systems.redroid.remote_dir", "/home/redroid"))
        self.init_script = str(cfg.get("systems.redroid.init_script", "./init.sh"))
        self.ssh_timeout = int(cfg.get("systems.redroid.ssh_timeout_s", 30))
        # Secret: env only by default (config fallback exists but is discouraged).
        self.ssh_password = os.environ.get("REDROID_SSH_PASS") or str(cfg.get("systems.redroid.ssh_password", "") or "")
        self._have_sshpass = shutil.which("sshpass") is not None

        # Outer VM lifecycle (opt-in).
        self.manage_vm = bool(cfg.get("systems.redroid.manage_vm", False))
        self.vm_launch_cmd = str(cfg.get("systems.redroid.vm_launch_cmd", "") or "")
        self.vm_boot_timeout = float(cfg.get("systems.redroid.vm_boot_timeout_s", 300.0))
        self._vm_proc: Optional[subprocess.Popen] = None

    # -- outer VM -----------------------------------------------------------
    def _ssh_reachable(self) -> bool:
        try:
            with socket.create_connection((self.ssh_host, self.ssh_port), timeout=3):
                return True
        except OSError:
            return False

    def _ensure_vm(self) -> None:
        """Guarantee the outer VM's SSH is reachable before any remote command."""
        if self._ssh_reachable():
            return
        if not self.manage_vm or not self.vm_launch_cmd:
            raise SystemExit(
                f"redroid: outer VM not reachable at {self.ssh_host}:{self.ssh_port}. "
                "Start it first, or set systems.redroid.manage_vm=true and "
                "systems.redroid.vm_launch_cmd to a working launcher."
            )
        print(f"[redroid] starting outer VM: {self.vm_launch_cmd}")
        self._vm_proc = subprocess.Popen(
            self.vm_launch_cmd,
            shell=True,
            cwd=str(self.scalebench_dir),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + self.vm_boot_timeout
        while time.time() < deadline:
            if self._ssh_reachable():
                print("[redroid] outer VM SSH is up")
                return
            time.sleep(3)
        raise SystemExit(f"redroid: outer VM did not become reachable within {self.vm_boot_timeout:.0f}s")

    def stop_vm(self) -> None:
        """Stop the outer VM if this driver started it (not called automatically)."""
        if self._vm_proc and self._vm_proc.poll() is None:
            self._vm_proc.terminate()
            try:
                self._vm_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._vm_proc.kill()
        self._vm_proc = None

    # -- SSH ----------------------------------------------------------------
    def _ssh(self, remote_cmd: str) -> int:
        self._ensure_vm()
        base = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={self.ssh_timeout}",
            "-p", str(self.ssh_port),
            f"{self.ssh_user}@{self.ssh_host}",
            remote_cmd,
        ]
        env = dict(self.env)
        if self.ssh_password:
            if not self._have_sshpass:
                raise SystemExit("redroid: REDROID_SSH_PASS is set but `sshpass` is not installed")
            env["SSHPASS"] = self.ssh_password           # sshpass -e reads from env, not argv
            cmd = ["sshpass", "-e", *base]
        else:
            cmd = base
        return subprocess.run(cmd, env=env).returncode

    def _init(self, verb: str, *args: str) -> int:
        remote = f"cd {shlex.quote(self.remote_dir)} && {self.init_script} {verb}"
        if args:
            remote += " " + " ".join(shlex.quote(a) for a in args)
        return self._ssh(remote)

    # -- neutral contract ---------------------------------------------------
    def prepare_pool(self, n_tenants: int) -> None:
        # Un-measured: bring the outer VM up (baselines can't pre-warm containers).
        self._ensure_vm()

    def _launch(self, n: int) -> bool:
        return self._init("run", str(n)) == 0

    def _stop(self, n: int) -> None:
        self._init("stop")

    def _remove(self, n: int) -> None:
        # Remove containers and their anonymous volumes so app installs from a
        # previous density/trial cannot leak into the next measured run.
        self._init("rm")
