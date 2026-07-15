"""Anbox Cloud driver: amc Android containers through Anbox Cloud.

Two deployment modes are supported:

    * ``local`` -- a bare-metal Anbox Cloud Appliance on this host. Container
        control goes through the user's trusted ``amc`` client.
    * ``multipass`` -- the VM setup, where control goes through
        ``multipass exec <vm> -- sudo amc ...``.

In both modes Android containers are created by ``amc``; adb reaches them via
    ``anbox-connect`` (the gateway), which assigns **dynamic** local ports. So
    tenants are *discovered* from ``adb devices`` (127.0.0.1:*), not computed
    from a fixed port map (``_resolve_serials`` override).

Launch reuses the existing control script (``platform/anbox_test.sh start N``),
which creates the containers and runs ``anbox-connect``. Teardown is done
directly via ``amc`` (list -> stop -> delete) plus killing the host-side
``anbox-connect``/tmux sessions, which is more robust than the script's
timestamped-file ``stop-*`` verbs.

CAVEATS:
    * Local mode requires ``amc`` to be trusted for the current user.
    * Multipass mode requires ``multipass``, ``anbox-connect``, ``tmux`` on the
        host and a working Anbox appliance in the VM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import List

from ..config import Config
from ._baseline import BaselineDriver


class AnboxDriver(BaselineDriver):
    name = "anbox"
    port_stride = 1  # unused (serials are discovered), kept for the base API

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self.backend = str(cfg.get("systems.anbox.backend", "multipass")).strip().lower()
        if self.backend not in {"local", "multipass"}:
            raise SystemExit("anbox: systems.anbox.backend must be 'local' or 'multipass'")
        self.vm_name = str(cfg.get("systems.anbox.vm_name", "anbox"))
        control = cfg.get("systems.anbox.control_script") or ""
        self.control_script = (
            os.fspath(control) if control and "${" not in str(control)
            else str(self.scalebench_dir / "platform" / "anbox_test.sh")
        )
        self.manage_vm = bool(cfg.get("systems.anbox.manage_vm", False))
        self.vm_boot_timeout = float(cfg.get("systems.anbox.vm_boot_timeout_s", 300.0))
        # On a bare-metal appliance the trusted admin interface is the root UNIX
        # socket: amc's per-user TLS identity is denied instance create/view by
        # the appliance's OpenFGA model (even inside the admin group), while root
        # (client_id 0 via the socket) has full access. So local mode drives amc
        # through passwordless `sudo` by default. Override with
        # systems.anbox.amc_sudo (or $ANBOX_AMC_SUDO) when a user's amc TLS
        # identity really can create instances.
        self.amc_sudo = bool(cfg.get("systems.anbox.amc_sudo", self.backend == "local"))
        # GPU slots per instance. amc launch defaults to the instance type's slot
        # count (0 for the container type), so a real GPU is NOT injected unless
        # we pass --gpu-slots; without it graphics init hangs and boot never
        # completes on a bare-metal appliance with a real GPU.
        self.gpu_slots = int(cfg.get("systems.anbox.gpu_slots", 1))
        self.env.setdefault("ANBOX_BACKEND", self.backend)
        self.env.setdefault("ANBOX_VM", self.vm_name)
        self.env.setdefault("ANBOX_AMC_SUDO", "1" if self.amc_sudo else "0")
        self.env.setdefault("ANBOX_GPU_SLOTS", str(self.gpu_slots))
        self._have_multipass = self.backend == "multipass" and shutil.which("multipass") is not None

    @property
    def _local_amc(self) -> str:
        """The local amc invocation, optionally via passwordless sudo."""
        return "sudo -n amc" if self.amc_sudo else "amc"

    # -- appliance access ---------------------------------------------------
    def _vm_running(self) -> bool:
        if self.backend == "local":
            return True
        if not self._have_multipass:
            return False
        res = subprocess.run(["multipass", "info", self.vm_name], capture_output=True, text=True)
        return res.returncode == 0 and re.search(r"State:\s*Running", res.stdout) is not None

    def _ensure_vm(self) -> None:
        if self.backend == "local":
            if shutil.which("amc") is None:
                raise SystemExit("anbox: `amc` not found; install the Anbox Cloud Appliance client tooling")
            if shutil.which("anbox-cloud-appliance") is None:
                raise SystemExit("anbox: `anbox-cloud-appliance` not found")
            status = subprocess.run(["anbox-cloud-appliance", "status"], capture_output=True, text=True)
            if status.returncode != 0 or "status: ready" not in status.stdout:
                raise SystemExit("anbox: local Anbox Cloud Appliance is not ready")
            auth = subprocess.run(
                ["bash", "-lc", f"{self._local_amc} node ls"],
                capture_output=True, text=True, env=self.env,
            )
            if auth.returncode != 0:
                msg = (auth.stderr or auth.stdout or "").strip()
                hint = (
                    "anbox: local `sudo amc` failed. Grant passwordless sudo for "
                    "`amc` (see docs/setup.md, Anbox bare-metal section), or set "
                    "systems.anbox.amc_sudo=false if this user's amc TLS identity "
                    "can create instances."
                    if self.amc_sudo else
                    "anbox: local `amc` is not authorized for this user. Trust the "
                    "user's AMC client certificate, or set systems.anbox.amc_sudo=true "
                    "to use the root unix-socket admin path via sudo."
                )
                raise SystemExit(hint + (f"\n{msg}" if msg else ""))
            return
        if not self._have_multipass:
            raise SystemExit("anbox: `multipass` not found; the Anbox appliance VM is required")
        if self._vm_running():
            return
        if not self.manage_vm:
            raise SystemExit(
                f"anbox: multipass VM '{self.vm_name}' is not running. Start it "
                f"(`multipass start {self.vm_name}`) or set systems.anbox.manage_vm=true."
            )
        print(f"[anbox] starting multipass VM {self.vm_name}")
        subprocess.run(["multipass", "start", self.vm_name], env=self.env)
        deadline = time.time() + self.vm_boot_timeout
        while time.time() < deadline:
            if self._vm_running():
                print(f"[anbox] multipass VM {self.vm_name} is running")
                return
            time.sleep(3)
        raise SystemExit(f"anbox: VM '{self.vm_name}' did not reach Running within {self.vm_boot_timeout:.0f}s")

    def _amc(self, args: str) -> subprocess.CompletedProcess:
        if self.backend == "local":
            return subprocess.run(
                ["bash", "-lc", f"{self._local_amc} {args}"],
                capture_output=True,
                text=True,
                env=self.env,
            )
        return subprocess.run(
            ["multipass", "exec", self.vm_name, "--", "bash", "-lc", f"sudo amc {args}"],
            capture_output=True,
            text=True,
            env=self.env,
        )

    # -- teardown (direct amc, robust) -------------------------------------
    def _list_containers(self) -> List[str]:
        res = self._amc("ls")
        if res.returncode != 0:
            return []
        # amc ls is a pipe-delimited table; the container id is a 20+ char cell.
        ids = re.findall(r"\|\s*([a-z0-9]{20,})\s*\|", res.stdout)
        seen, out = set(), []
        for cid in ids:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    def _cleanup(self) -> None:
        # Inner: stop + delete every container (only if the VM is up). Use
        # --force/--yes so crash-looping or errored instances are still removed
        # non-interactively; a plain `delete --yes` leaves stuck containers.
        if self._vm_running():
            for cid in self._list_containers():
                self._amc(f"stop {cid} --force")
                self._amc(f"delete {cid} --force --yes")
        # Host: kill anbox-connect sessions and drop stale adb targets.
        subprocess.run(["pkill", "-f", "anbox-connect"], capture_output=True)
        ls = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True)
        for name in (ls.stdout or "").split():
            if name.startswith("anbox_"):
                subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        for serial in (self._adb().get_adb_devices() or []):
            subprocess.run(["adb", "disconnect", serial], capture_output=True)

    # -- neutral contract ---------------------------------------------------
    def prepare_pool(self, n_tenants: int) -> None:
        # Un-measured: bring the outer VM up before the measured provision window.
        self._ensure_vm()

    def _launch(self, n: int) -> bool:
        self._ensure_vm()
        return self._sh(self.control_script, ["start", str(n)]) == 0

    def _stop(self, n: int) -> None:
        self._cleanup()

    def _remove(self, n: int) -> None:
        # Ensure the VM is up *before* the measured window (t0 in provision is set
        # after _remove), then clear any prior containers/adb so this is a cold boot.
        self._ensure_vm()
        self._cleanup()

    def _resolve_serials(self, n: int, t0: float, boot_timeout: float) -> List[str]:
        """Discover the dynamically-assigned adb serials from `adb devices`."""
        adb = self._adb()
        deadline = t0 + boot_timeout
        while True:
            serials = adb.get_adb_devices() or []
            if len(serials) >= n or time.time() >= deadline:
                return serials[:n]
            time.sleep(self.ready_interval)

