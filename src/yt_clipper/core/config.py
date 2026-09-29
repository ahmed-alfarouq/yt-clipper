import json
from pathlib import Path
import os

from yt_clipper.core.log import describe_failure, get_logger

logger = get_logger(__name__)


def _make_config_dir(base):
    """Create (and return) the config directory, tolerating a failure to do so.

    §15: a configuration problem must never terminate the application. Importing
    this module used to raise when the directory could not be created (read-only
    home, exotic XDG_CONFIG_HOME), which killed the app before it started; now
    the failure is recorded and every later read/write degrades to defaults.
    """
    config_dir = Path(base) / "YTClipper"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create the settings folder %s; settings will not be "
            "saved this run: %s", config_dir, describe_failure(exc),
        )
    return config_dir


def get_config_dir():
    if os.name == "nt":
        base = os.getenv("APPDATA", str(Path.home()))
    else:
        base = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return _make_config_dir(base)

CONFIG_PATH = get_config_dir() / "config.json"

DEFAULTS = {"last_output_dir": str(Path.home() / "Videos")}

def load_config():
    """Return the stored settings merged over the defaults.

    A missing, unreadable or corrupt file is a recoverable condition: the run
    continues with defaults and the reason is recorded (§15).
    """
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULTS, **json.load(f)}
        except json.JSONDecodeError as exc:
            logger.warning(
                "Settings file %s is corrupt; using defaults instead: %s",
                CONFIG_PATH, describe_failure(exc),
            )
        except OSError as exc:
            logger.warning(
                "Settings file %s could not be read; using defaults instead: %s",
                CONFIG_PATH, describe_failure(exc),
            )
    return dict(DEFAULTS)

def save_config(data):
    """Persist settings; return True on success, False if they could not be saved.

    Failure is recoverable and must not abort the caller's operation (§15): the
    user's clip still runs, they simply lose the remembered preference, so the
    problem is logged at WARNING and reported through the return value instead
    of an exception.
    """
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "Settings could not be written to %s; continuing without saving: %s",
            CONFIG_PATH, describe_failure(exc),
        )
        return False
    return True
