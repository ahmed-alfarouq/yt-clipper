"""General utilities: filename sanitization, folder opening, time formatting.

Playlist-specific helpers (availability, sorting, publish-date, URL detection)
have been moved to playlist_utils.py to keep this file small and focused.
This file re-exports those helpers for backward compatibility.
"""

import re
import os
import sys
import subprocess

_WINDOWS_INVALID_CHARS_RE = re.compile(r'[<>:\"/\\|?*\x00-\\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

def sanitize_filename(name, fallback="clip", max_length=150):
    """Turn an arbitrary string into a filename stem safe on Windows."""
    name = (name or "").strip()
    name = _WINDOWS_INVALID_CHARS_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(" .")
    if not name:
        name = fallback
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .") or fallback
    return name


def open_containing_folder(file_path):
    folder = os.path.dirname(os.path.abspath(file_path))
    if sys.platform == "win32":
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.run(["open", folder])
    else:
        subprocess.run(["xdg-open", folder])

def format_seconds(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def time_to_seconds(time_str):
    """Accepts HH:MM:SS, MM:SS, or SS."""
    parts = [float(p) for p in time_str.split(':')]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    elif len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return parts[0]


# Re-export playlist utilities for backward compatibility
from yt_clipper.core.playlist_utils import (
    parse_publish_date,
    extract_publish_date,
    sort_videos_by_publish_date,
    format_publish_date,
    is_video_entry_available,
    is_video_available,
    filter_available_videos,
    detect_youtube_url_type,
    is_youtube_playlist_url,
    is_youtube_video_url,
)

__all__ = [
    "sanitize_filename",
    "open_containing_folder",
    "format_seconds",
    "time_to_seconds",
    "parse_publish_date",
    "extract_publish_date",
    "sort_videos_by_publish_date",
    "format_publish_date",
    "is_video_entry_available",
    "is_video_available",
    "filter_available_videos",
    "detect_youtube_url_type",
    "is_youtube_playlist_url",
    "is_youtube_video_url",
]
