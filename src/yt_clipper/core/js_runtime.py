"""Best-effort detection of a JS runtime yt-dlp needs for full YouTube
support (the EJS challenge solver: https://github.com/yt-dlp/yt-dlp/wiki/EJS).

This deliberately does NOT set yt-dlp's internal `js_runtimes` YoutubeDL
option, since that option's exact shape is not part of yt-dlp's documented,
stable API and could change between releases. Instead this relies on
yt-dlp's own built-in PATH-based auto-detection: if a supported runtime's
executable is reachable on PATH, yt-dlp finds and uses it automatically with
zero extra config from us. All this module does is:

  1. Optionally widen PATH to include a `runtimes/` folder shipped next to
     the app (or next to the frozen .exe), so a bundled/portable runtime is
     picked up the exact same way a system-installed one would be. A no-op
     if that folder doesn't exist.
  2. Report whether *something* usable was already found, so the GUI can
     surface a one-time hint - important because yt-dlp's own warning goes
     to a console window the packaged GUI .exe doesn't even show, so today
     a user of the packaged app would never see it at all.
"""

import os
import shutil
import sys
from pathlib import Path

# In recommendation order per the EJS wiki - matches what yt-dlp itself
# looks for by default (only "deno" is enabled by default upstream; the
# others still count here since they mean the user solved this another way).
KNOWN_RUNTIME_EXECUTABLES = ["deno", "node", "bun", "qjs"]

EJS_WIKI_URL = "https://github.com/yt-dlp/yt-dlp/wiki/EJS"
DENO_INSTALL_URL = "https://github.com/denoland/deno/releases"


def ensure_bundled_runtime_on_path():
    """Prepend a `runtimes/` folder next to the app/exe to PATH, if present."""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        # .../src/yt_clipper/core/js_runtime.py -> project root
        base_dir = Path(__file__).resolve().parents[3]

    runtimes_dir = base_dir / "runtimes"
    if runtimes_dir.is_dir():
        os.environ["PATH"] = str(runtimes_dir) + os.pathsep + os.environ.get("PATH", "")


def find_available_runtime():
    """Return the name of the first known JS runtime found on PATH, else None."""
    for name in KNOWN_RUNTIME_EXECUTABLES:
        if shutil.which(name):
            return name
    return None
