"""User settings and state files under %LOCALAPPDATA%\\windows-bluetooth-multicodec."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

# Named after the product's first name ("Windows Bluetooth Multicodec"), kept so settings and pairings carry over
STATE_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "windows-bluetooth-multicodec"
SETTINGS_FILE = STATE_DIR / "settings.json"
KEYS_FILE = STATE_DIR / "keys.json"
LOG_FILE = STATE_DIR / "multicodec.log"
# Instance ID of the PC's own Bluetooth adapter while we hold it (the no-dongle fallback). Kept out of
# settings.json because the CLI's setup-adapter can write it while the tray app holds Settings in memory.
SYSTEM_ADAPTER_FILE = STATE_DIR / "system-adapter.txt"


def system_adapter() -> str | None:
    try:
        return SYSTEM_ADAPTER_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def set_system_adapter(instance_id: str | None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if instance_id:
        SYSTEM_ADAPTER_FILE.write_text(instance_id, encoding="utf-8")
    else:
        SYSTEM_ADAPTER_FILE.unlink(missing_ok=True)


@dataclass
class Settings:
    # Highest quality by default: the best codec the device has, and for LDAC 990 kbps whenever the link
    # holds it (libldac's adaptive mode steps down only under real congestion and climbs back up)
    codec: str = "auto"  # auto | ldac | aptx-hd | aptx | aac | sbc
    ldac_quality: str = "auto"  # auto (best, adaptive) | hq | sq | mq (fixed)
    source: str = "system"  # "system" (loopback of the default output) or a recording device name
    mute_speakers: bool = True
    check_for_updates: bool = True  # look for a newer version on software.incustech.app now and then
    run_as_admin: bool = False  # restart through elevation.TASK_NAME so input reaches admin windows
    last_device: str | None = None
    devices: dict[str, str] = field(default_factory=dict)  # address -> name, for paired devices
    # address -> {"name", "le": bool, "descriptor": hex (classic), "keyboard": bool (classic)}
    hid_devices: dict[str, dict] = field(default_factory=dict)

    _lock = threading.Lock()

    @classmethod
    def load(cls) -> Settings:
        try:
            # utf-8-sig: Windows tools (PowerShell 5's Set-Content, Notepad) may add a BOM
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        except OSError:
            return cls()
        except ValueError:
            logger.warning(f"{SETTINGS_FILE} is not valid JSON; starting with default settings")
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if known.get("codec") == "aptx-ll":  # dropped in 0.1.6; aptX LL headphones also offer plain aptX
            known["codec"] = "auto"
        return cls(**known)

    def save(self) -> None:
        with self._lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
