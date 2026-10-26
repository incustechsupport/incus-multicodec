"""User settings and state files under %LOCALAPPDATA%\\windows-bluetooth-multicodec."""

from __future__ import annotations

import json
import os
import pathlib
import threading
from dataclasses import asdict, dataclass, field

STATE_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "windows-bluetooth-multicodec"
SETTINGS_FILE = STATE_DIR / "settings.json"
KEYS_FILE = STATE_DIR / "keys.json"
LOG_FILE = STATE_DIR / "multicodec.log"


@dataclass
class Settings:
    codec: str = "auto"  # auto | ldac | aptx-hd | aptx | aptx-ll | sbc
    ldac_quality: str = "auto"  # auto | hq | sq | mq
    source: str = "system"  # "system" (loopback of the default output) or a recording device name
    mute_speakers: bool = True
    last_device: str | None = None
    devices: dict[str, str] = field(default_factory=dict)  # address -> name, for paired devices
    system_adapter: str | None = None  # instance ID, when we took the PC's own Bluetooth (no dongle)
    # address -> {"name", "le": bool, "descriptor": hex (classic), "keyboard": bool (classic)}
    hid_devices: dict[str, dict] = field(default_factory=dict)

    _lock = threading.Lock()

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        with self._lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
