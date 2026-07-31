"""vSoC driver: wraps mcon-artifact/platform/vsoc.sh (one QEMU instance per tenant).

vsoc.sh exposes ``run|stop|rm COUNT BASE_MONITOR_PORT BASE_ADB_PORT`` and maps
instance ``i`` to adb port ``BASE_ADB_PORT + i`` (stride 1). It reads BASE_DIR and
GUEST_IMG_PATH from the environment, which this driver forwards.
"""

from __future__ import annotations

import os

from ..config import Config
from ._baseline import BaselineDriver


class VSoCDriver(BaselineDriver):
    name = "vsoc"
    port_stride = 1

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self.script = self.artifact_dir / "platform" / "vsoc.sh"
        if not self.script.exists():
            raise SystemExit(f"vsoc.sh not found at {self.script}")
        vsoc = os.environ.get("GUEST_IMG_PATH") or cfg.get("paths.vsoc_img_path")
        if vsoc and "${" not in str(vsoc):
            self.env.setdefault("GUEST_IMG_PATH", str(vsoc))

    def _ports(self):
        return [str(self.base_monitor_port), str(self.base_adb_port)]

    def _launch(self, n: int, interval: float, ready_poll_interval: float):
        return self._launch_batch_script(
            self.script,
            ["run", str(n), *self._ports()],
            n,
            interval,
            ready_poll_interval,
        )

    def _stop(self, n: int) -> None:
        self._sh(self.script, ["stop", str(n), *self._ports()])

    def _remove(self, n: int) -> None:
        self._sh(self.script, ["rm", str(n), *self._ports()])
