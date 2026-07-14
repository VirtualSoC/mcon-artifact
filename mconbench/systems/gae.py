"""GAE (Google Android Emulator) driver: wraps scalebench/platform/avd.sh.

Standard AVDs are launched with ``-ports CONSOLE,ADB``, which exposes the adb
port over TCP, so -- exactly like vSoC -- each instance is addressed as
``localhost:<adb_port>`` via ``adb connect`` (this is what avd_measure_boot.sh
does too). The emulator consumes two consecutive ports per instance
(console + adb), so the adb port stride is 2: instance ``i`` -> adb port
``base_adb_port + 2*i`` (and console ``base_console_port + 2*i``).

avd.sh argument order is ``run|stop|rm COUNT ADB_BASE CONSOLE_BASE PREFIX``.
Two lifecycle quirks to be aware of:

  * ``stop`` kills *all* emulator processes on the host (not just this batch);
    fine for an AE run where GAE is the only emulator system active.
  * ``rm`` deletes the AVD definitions for ``PREFIX-1..COUNT``. Because avd.sh's
    ``run`` reuses an existing AVD (and does not wipe userdata), the neutral
    cold-boot path removes the AVDs before launching so ``run`` recreates them
    fresh. Consequence: avdmanager (re)creation currently falls *inside* the
    measured provision window. For strict parity with the paper's "starting a
    VM" definition, pre-create the AVDs and switch to a wipe-data launch; left
    as a documented follow-up until the measurement is calibrated live.
"""

from __future__ import annotations

import os

from ..config import Config
from ._baseline import BaselineDriver


class GAEDriver(BaselineDriver):
    name = "gae"
    port_stride = 2

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self.script = self.scalebench_dir / "platform" / "avd.sh"
        if not self.script.exists():
            raise SystemExit(f"avd.sh not found at {self.script}")
        self.base_console_port = int(cfg.get("systems.gae.base_console_port", 55554))
        self.avd_prefix = str(cfg.get("systems.gae.avd_prefix", "avd-batch"))
        # Optional Android SDK location; avd.sh otherwise defaults to ~/Android/Sdk.
        sdk_root = os.environ.get("ANDROID_SDK_ROOT") or cfg.get("systems.gae.sdk_root")
        if sdk_root and "${" not in str(sdk_root):
            self.env.setdefault("ANDROID_SDK_ROOT", str(sdk_root))
        # Headless by default so the emulator renders offscreen (cloud setup).
        if bool(cfg.get("systems.gae.no_window", True)):
            self.env.setdefault("AVD_NO_WINDOW", "1")

    def _args(self):
        return [str(self.base_adb_port), str(self.base_console_port), self.avd_prefix]

    def _launch(self, n: int) -> bool:
        return self._sh(self.script, ["run", str(n), *self._args()]) == 0

    def _stop(self, n: int) -> None:
        self._sh(self.script, ["stop", str(n), *self._args()])

    def _remove(self, n: int) -> None:
        self._sh(self.script, ["rm", str(n), *self._args()])
