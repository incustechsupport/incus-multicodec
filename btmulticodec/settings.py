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
# The previous settings.json, kept so a crash or power cut mid-save can't lose the paired devices
SETTINGS_BACKUP = STATE_DIR / "settings.json.bak"
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

    @staticmethod
    def _read(path: pathlib.Path) -> dict | None:
        """The settings object in `path`, or None if it's missing, unreadable or not an object."""
        try:
            # utf-8-sig: Windows tools (PowerShell 5's Set-Content, Notepad) may add a BOM
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            logger.warning(f"{path} can't be read ({error})")
            return None
        if not isinstance(data, dict):
            logger.warning(f"{path} doesn't hold settings")
            return None
        return data

    @classmethod
    def load(cls) -> Settings:
        data = cls._read(SETTINGS_FILE)
        if data is None:
            data = cls._read(SETTINGS_BACKUP)  # a save interrupted by a crash or power cut
            if data is not None:
                logger.warning(f"using the backup settings ({SETTINGS_BACKUP})")
        if data is None:
            return cls()
        defaults = cls()
        known = {}
        for name in cls.__dataclass_fields__:
            if name not in data:
                continue
            value, default = data[name], getattr(defaults, name)
            # Keep a value only if it has the right shape (a hand-edited file mustn't crash the app)
            # (None only where None is the default, e.g. last_device; that one is a string otherwise)
            if (value is None or isinstance(value, str)) if default is None else isinstance(value, type(default)):
                known[name] = value
            else:
                logger.warning(f"ignoring setting {name}={value!r}")
        if known.get("codec") == "aptx-ll":  # dropped in 0.1.6; aptX LL headphones also offer plain aptX
            known["codec"] = "auto"
        return cls(**known)

    def save(self) -> None:
        with self._lock:
            # The engine thread may add a device while we copy: retry rather than fail the save
            for attempt in range(5):
                try:
                    text = json.dumps(asdict(self), indent=2)
                    break
                except RuntimeError:  # dictionary changed size during iteration
                    if attempt == 4:
                        raise
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())  # on disk before it replaces anything
            # At every step a complete copy exists: settings.json, settings.json.bak or the .tmp
            if SETTINGS_FILE.exists():
                os.replace(SETTINGS_FILE, SETTINGS_BACKUP)
            os.replace(tmp, SETTINGS_FILE)
