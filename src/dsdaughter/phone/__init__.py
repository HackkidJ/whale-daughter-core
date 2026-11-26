"""手机设备代理：读屏、通知、电量，以及后续的动作能力。"""

from __future__ import annotations

import time
from pathlib import Path


class ScreenshotStore:
    """截图落盘（审计/给用户看）。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, png: bytes) -> Path:
        path = self.directory / f"{int(time.time() * 1000)}.png"
        path.write_bytes(png)
        return path
