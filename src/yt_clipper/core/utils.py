import re
import os
import sys
import subprocess
 
_WINDOWS_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

def sanitize_filename(name, fallback="clip", max_length=150):
    """Turn an arbitrary string (e.g. a video title) into a filename stem
    that is safe on Windows (and harmless on other platforms).
 
    Strips characters Windows forbids in filenames, collapses whitespace,
    trims trailing dots/spaces (also disallowed by Windows), guards against
    reserved device names (CON, PRN, COM1, ...), and caps the length.
    Does not include a file extension; callers append their own.
    """
    name = (name or "").strip()
    name = _WINDOWS_INVALID_CHARS_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(" .")  # Windows disallows trailing dots/spaces.
 
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


# ---------------------------------------------------------------------------
# Playlist publish-date helpers (for sorted playlist preview)
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math as _math


def _parse_upload_date_str(value: str) -> Optional[datetime]:
    """Parse YYYYMMDD or YYYY-MM-DD into a datetime (UTC, midnight)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    # YYYYMMDD
    if len(v) == 8 and v.isdigit():
        try:
            return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    # YYYY-MM-DD or YYYY/MM/DD
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(v[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_publish_date(value: Any) -> Optional[datetime]:
    """Normalize various publish-date representations to datetime or None.

    Accepts:
    - datetime (returned as-is, forced to UTC if naive)
    - int/float unix timestamp (seconds)
    - string YYYYMMDD, YYYY-MM-DD, or numeric timestamp string
    - None / invalid -> None
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic: timestamps > 1e12 are likely milliseconds
        try:
            if _math.isnan(float(value)) or _math.isinf(float(value)):
                return None
        except Exception:
            return None
        ts = float(value)
        if ts > 1e12:  # ms
            ts = ts / 1000.0
        if ts < 0 or ts > 4102444800:  # year 2100 sanity check
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Try upload_date string first
        dt = _parse_upload_date_str(s)
        if dt:
            return dt
        # Try numeric timestamp string
        try:
            num = float(s)
            return parse_publish_date(num)
        except ValueError:
            pass
        # Try ISO-ish datetime string
        # e.g. "2024-01-15T..." or "January 15, 2024" is not parsed here on purpose;
        # we only handle machine formats. Human formatting is done in format_publish_date.
        # Attempt fromisoformat as last resort
        try:
            # fromisoformat doesn't handle Z, so replace
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def extract_publish_date(video: Dict[str, Any]) -> Optional[datetime]:
    """Extract publish date from a video dict using common yt-dlp keys.

    Looks for (in priority order):
    - publish_date (already datetime or parseable)
    - timestamp / release_timestamp
    - upload_date / release_date
    - upload_timestamp (some extractors)
    Returns datetime or None.
    """
    if not isinstance(video, dict):
        return None

    # Direct datetime field (our own normalized field)
    for key in ("publish_date", "publish_datetime"):
        if key in video and video[key] is not None:
            dt = parse_publish_date(video[key])
            if dt:
                return dt

    # Numeric timestamps
    for key in ("timestamp", "release_timestamp", "upload_timestamp", "creation_timestamp"):
        if key in video and video[key] is not None:
            dt = parse_publish_date(video[key])
            if dt:
                return dt

    # String dates like YYYYMMDD
    for key in ("upload_date", "release_date", "creation_date"):
        if key in video and video[key]:
            dt = parse_publish_date(video[key])
            if dt:
                return dt

    return None


def sort_videos_by_publish_date(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a new list sorted oldest→newest by publish date.

    - Does NOT mutate the original list.
    - Videos with no usable publish date are placed at the end, preserving
      their relative order (stable sort).
    - Never crashes on missing/malformed metadata.
    """
    if not videos:
        return []

    # Build list of (original_index, video, sort_key)
    decorated = []
    for idx, v in enumerate(videos):
        try:
            dt = extract_publish_date(v)
        except Exception:
            dt = None
        # Use (has_date? 0:1, dt, idx) so missing dates go to end, stable
        # For missing, use max datetime placeholder but sort key ensures they are last
        if dt is None:
            # Put at end, keep original order via idx
            sort_key = (1, datetime.max.replace(tzinfo=timezone.utc), idx)
        else:
            sort_key = (0, dt, idx)
        decorated.append((sort_key, v))

    decorated.sort(key=lambda x: x[0])
    return [v for _, v in decorated]


def format_publish_date(value: Any) -> str:
    """Format a publish date value to human readable, e.g. 'January 15, 2024'.

    Accepts datetime, timestamp, YYYYMMDD string, or dict containing date.
    Returns 'Unknown date' if not parseable.
    """
    dt: Optional[datetime] = None
    if isinstance(value, dict):
        dt = extract_publish_date(value)
    else:
        dt = parse_publish_date(value)

    if dt is None:
        return "Unknown date"
    try:
        # Use UTC date, format as "January 15, 2024"
        # Avoid locale-dependent %B issues by using strftime which is okay for English
        return dt.strftime("%B %d, %Y")
    except Exception:
        return "Unknown date"


# ---------------------------------------------------------------------------
# Availability filtering & URL type detection (for dual preview modes)
# ---------------------------------------------------------------------------

# yt-dlp flat playlist entries (extract_flat: in_playlist) come from
# YoutubeTabIE._extract_video / _extract_lockup_view_model:
#   - id: videoId
#   - url: https://www.youtube.com/watch?v=ID
#   - title: may be "[Private video]" / "[Deleted video]" for unavailable
#   - availability: "public", "unlisted", "private", "needs_auth", etc.
#     derived from badges AVAILABILITY_PRIVATE/PREMIUM/SUBSCRIPTION
#   - thumbnails, duration, timestamp may be missing in flat mode
#
# Unavailable videos are signaled explicitly by yt-dlp via:
#   - availability field (private, needs_auth, premium, subscriber_only, unavailable...)
#   - title placeholders like "[Private video]", "[Deleted video]"
# We must NOT use weak checks like title presence, thumbnail presence, id presence
# or publish date presence alone as availability test.

_UNAVAILABLE_TITLE_MARKERS = (
    "[private video]",
    "[deleted video]",
    "[unavailable]",
    "private video",
    "deleted video",
    "unavailable video",
)

_UNAVAILABLE_AVAILABILITY = {
    "private",
    "needs_auth",
    "premium",
    "subscriber_only",
    "unavailable",
    "needs_premium",
    "needs_subscription",
    "requires_auth",
    "auth_required",
    "removed",
    "deleted",
}


def is_video_entry_available(video: Dict[str, Any]) -> bool:
    """Determine if a yt-dlp flat playlist entry is usable by download pipeline.

    Uses metadata already obtained by playlist extraction, no extra network.

    Available even if:
      - publish_date is None (sorted to end)
      - thumbnail is None (fallback used)
      - title is missing/empty (fallback elsewhere, but still downloadable)
      - duration is missing (flat entries don't have duration)

    Unavailable when yt-dlp explicitly indicates inaccessibility:
      - availability in _UNAVAILABLE_AVAILABILITY
      - title is "[Private video]", "[Deleted video]", "[Unavailable]" etc.
      - url missing or not http (cannot construct valid download)
      - entry is None/empty

    This is the single source of truth for availability; preview and download
    must share its filtered result.
    """
    if not video or not isinstance(video, dict):
        return False

    # URL is required for download pipeline to resolve formats.
    # This is not a weak check: without http url, yt-dlp cannot download.
    url = video.get("url") or video.get("webpage_url") or ""
    if not isinstance(url, str) or not url.startswith("http"):
        return False

    # Explicit availability signal from yt-dlp badges
    availability = (video.get("availability") or "").strip().lower()
    if availability in _UNAVAILABLE_AVAILABILITY:
        return False

    # Title placeholders yt-dlp uses for hidden/deleted/private
    title = (video.get("title") or "").strip()
    lower_title = title.lower()

    if lower_title in _UNAVAILABLE_TITLE_MARKERS:
        return False
    for marker in _UNAVAILABLE_TITLE_MARKERS:
        if lower_title.startswith(marker):
            return False
    if "[private video]" in lower_title or "[deleted video]" in lower_title:
        return False
    if "video unavailable" in lower_title or "video has been removed" in lower_title:
        return False

    # Otherwise, consider available – even if title empty, thumbnail None,
    # publish_date None, duration None – those are handled elsewhere.
    return True


def is_video_available(video: Dict[str, Any]) -> bool:
    """Alias for is_video_entry_available, preferred short name."""
    return is_video_entry_available(video)


def filter_available_videos(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return new list containing only available videos.

    Single primary filtering implementation:
      raw entries -> filter_available_videos -> sort -> preview + download

    Does NOT mutate original list. Never crashes on malformed entries.
    If every video is unavailable, returns empty list (caller shows empty state).
    Does NOT filter based on missing publish_date/thumbnail/title alone.
    """
    if not videos:
        return []
    available: List[Dict[str, Any]] = []
    for v in videos:
        try:
            if is_video_entry_available(v):
                available.append(v)
        except Exception:
            continue
    return available


# URL type detection

from urllib.parse import urlparse, parse_qs


def detect_youtube_url_type(url: str) -> str:
    """Detect whether a YouTube URL is a video or playlist.

    Returns:
        "playlist" - YouTube playlist URL
        "video"    - YouTube video URL (including video URL that contains list= param)
        "unknown"  - not a recognizable YouTube URL or empty

    Logic:
    - Uses urllib parsing, not just substring search.
    - Handles normal cases:
        https://www.youtube.com/playlist?list=PLAYLIST_ID → playlist
        https://www.youtube.com/watch?v=VIDEO_ID → video
        https://youtu.be/VIDEO_ID → video
        https://www.youtube.com/shorts/VIDEO_ID → video
        https://www.youtube.com/embed/VIDEO_ID → video
    - Important edge: https://www.youtube.com/watch?v=ID&list=PL... → video
      (video URL containing playlist param should NOT be classified as playlist)

    This mirrors project's existing behavior where expand_playlist's is_playlist
    flag is the source of truth after extraction, but this heuristic is used for
    UI preview switching before extraction completes.
    """
    if not url or not isinstance(url, str):
        return "unknown"
    u = url.strip()
    if not u:
        return "unknown"

    try:
        parsed = urlparse(u)
        netloc = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        query = parse_qs(parsed.query)

        # Only handle youtube domains; otherwise unknown (caller will default to video)
        is_youtube = "youtube.com" in netloc or "youtu.be" in netloc
        if not is_youtube:
            # Could still be youtube URL without www, check
            if "youtube" not in netloc and "youtu.be" not in netloc:
                return "unknown"

        # youtu.be is always video
        if "youtu.be" in netloc:
            return "video"

        # Path checks
        if "/playlist" in path:
            # https://www.youtube.com/playlist?list=...
            # Even if it also has v param, playlist path takes precedence
            return "playlist"

        if "/shorts/" in path or "/embed/" in path or "/watch" in path:
            # Video URL, even if it contains list= param (video in playlist context)
            return "video"

        # Query param checks
        # If list param present and no v param, likely playlist
        has_list = "list" in query and any(v for v in query.get("list", []) if v)
        has_v = "v" in query and any(v for v in query.get("v", []) if v)

        if has_list and not has_v:
            # e.g., ?list=PL... without v
            return "playlist"

        if has_v:
            # Any URL with v param is video, even if list also present
            return "video"

        # Fallback: if only list param present in raw string and path is root or empty
        if has_list:
            return "playlist"

        # If URL contains watch?v= pattern but urlparse missed (e.g., no netloc)
        if "watch?v=" in u.lower() or "youtube.com/watch" in u.lower():
            return "video"

        # Default to video for youtube.com URLs that look like video
        # (e.g., youtube.com/v/ID)
        return "video"

    except Exception:
        return "unknown"


def is_youtube_playlist_url(url: str) -> bool:
    """Convenience wrapper: True if URL is detected as playlist."""
    return detect_youtube_url_type(url) == "playlist"


def is_youtube_video_url(url: str) -> bool:
    """Convenience wrapper: True if URL is detected as video."""
    return detect_youtube_url_type(url) == "video"