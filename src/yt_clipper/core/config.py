import json
from pathlib import Path
import os

def get_config_dir():
    if os.name == "nt":
        base = os.getenv("APPDATA", str(Path.home()))
    else:
        base = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    config_dir = Path(base) / "YTClipper"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

CONFIG_PATH = get_config_dir() / "config.json"

DEFAULTS = {"last_output_dir": str(Path.home() / "Videos")}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULTS, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)

def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass