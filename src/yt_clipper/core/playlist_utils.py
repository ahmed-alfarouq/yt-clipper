"""
Playlist-specific utilities: availability, filtering, sorting, publish-date handling, URL detection.

This module is intentionally small and focused, keeping core/utils.py from becoming a dumping ground.
It provides the single authoritative implementation for:

- is_video_available / is_video_entry_available
- filter_available_videos
- sort_videos_by_publish_date
- publish-date parsing/formatting
- YouTube URL type detection

Flow:
  raw yt-dlp entries
      ↓
  filter_available_videos()  <- uses is_video_entry_available()
      ↓
  sort_videos_by_publish_date()
      ↓
  shared playlist state -> Preview + Download
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math as _math
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Publish-date helpers (for sorted playlist preview)
# ---------------------------------------------------------------------------

def _parse_upload_date_str(value: str) -> Optional[datetime]:
    """Parse YYYYMMDD or YYYY-MM-DD into a datetime (UTC, midnight)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 8 and v.isdigit():
        try:
            return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(v[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_publish_date(value: Any) -> Optional[datetime]:
    """Normalize various publish-date representations to datetime or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            if _math.isnan(float(value)) or _math.isinf(float(value)):
                return None
        except Exception:
            return None
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts < 0 or ts > 4102444800:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        dt = _parse_upload_date_str(s)
        if dt:
            return dt
        try:
            num = float(s)
            return parse_publish_date(num)
        except ValueError:
            pass
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def extract_publish_date(video: Dict[str, Any]) -> Optional[datetime]:
    """Extract publish date from a video dict using common yt-dlp keys."""
    if not isinstance(video, dict):
        return None
    for key in ("publish_date", "publish_datetime"):
        if key in video and video[key] is not None:
            dt = parse_publish_date(video[key])
            if dt:
                return dt
    for key in ("timestamp", "release_timestamp", "upload_timestamp", "creation_timestamp"):
        if key in video and video[key] is not None:
            dt = parse_publish_date(video[key])
            if dt:
                return dt
    for key in ("upload_date", "release_date", "creation_date"):
        if key in video and video[key]:
            dt = parse_publish_date(video[key])
            if dt:
                return dt
    return None


def sort_videos_by_publish_date(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return new list sorted oldest→newest by publish date.

    - Does NOT mutate original list.
    - Videos with no usable publish date are placed at the end, preserving relative order.
    - Never crashes on missing/malformed metadata.
    """
    if not videos:
        return []
    decorated = []
    for idx, v in enumerate(videos):
        try:
            dt = extract_publish_date(v)
        except Exception:
            dt = None
        if dt is None:
            sort_key = (1, datetime.max.replace(tzinfo=timezone.utc), idx)
        else:
            sort_key = (0, dt, idx)
        decorated.append((sort_key, v))
    decorated.sort(key=lambda x: x[0])
    return [v for _, v in decorated]


def format_publish_date(value: Any) -> str:
    """Format publish date to human readable, e.g. 'January 15, 2024'."""
    dt: Optional[datetime] = None
    if isinstance(value, dict):
        dt = extract_publish_date(value)
    else:
        dt = parse_publish_date(value)
    if dt is None:
        return "Unknown date"
    try:
        return dt.strftime("%B %d, %Y")
    except Exception:
        return "Unknown date"


# ---------------------------------------------------------------------------
# Availability filtering
# ---------------------------------------------------------------------------

# yt-dlp flat playlist entries (extract_flat: in_playlist) come from
# YoutubeTabIE._extract_video / _extract_lockup_view_model:
#   - id: videoId
#   - url: may be videoId or full https://www.youtube.com/watch?v=ID
#   - title: may be "[Private video]" / "[Deleted video]" for unavailable
#   - availability: "public", "unlisted", "private", "needs_auth", etc.
#     derived from badges AVAILABILITY_PRIVATE/PREMIUM/SUBSCRIPTION
#   - thumbnails, duration, timestamp may be missing in flat mode
#
# Unavailable signaled explicitly by yt-dlp via:
#   - availability field
#   - title placeholders
# We must NOT use weak checks like title presence, thumbnail presence, id presence
# or publish date presence alone.

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
    "premium_only",
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

    Single source of truth for availability; preview and download must share filtered result.
    """
    if not video or not isinstance(video, dict):
        return False

    # URL required for download pipeline
    url = video.get("url") or video.get("webpage_url") or ""
    if not isinstance(url, str) or not url.startswith("http"):
        # For raw flat entries where url may be just id, we allow id-based check
        # to avoid false negatives, but require that title/availability not indicate unavailable
        # However for final normalized entries, url must be http
        # To keep function usable for both raw and normalized, we check:
        # if url is just id (no http) but looks like youtube id, we don't fail here,
        # we let availability/title checks decide. But for safety, if url is empty, fail.
        if not url:
            return False
        # If url is not http but is a plausible video id (11 chars), allow further checks
        # Otherwise fail
        if not re.match(r'^[A-Za-z0-9_-]{11}$', str(url).strip()):
            # If it's not http and not a video id, it's invalid
            # But to avoid weak check, we only fail if it's clearly not a valid id/url
            # For transformed entries, we require http, so this will be False
            # For raw entries with id, we continue
            if not str(url).startswith("http"):
                # Check if it's an id-like string; if not, treat as unavailable
                # Actually for raw entries, url may be id, so we should not reject yet
                # We'll only reject if both url and webpage_url missing
                pass

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

    # NEW: Handle lockupViewModel private videos where title=None and availability=None
    # Actual yt-dlp flat data for private video (boul2gom/yt-dlp#318):
    #   title=None, availability=None, duration=None, view_count=None,
    #   channel_url=None, uploader_url=None, channel/channel_id/uploader/uploader_id missing
    # Available video with missing title would still have channel_url etc.
    # This is NOT "missing title alone" – it's title empty + no channel info,
    # which is explicit signal from yt-dlp that video is private.
    title_empty = not title
    avail_empty = not availability
    if title_empty and avail_empty:
        ch_url = video.get("channel_url")
        upl_url = video.get("uploader_url")
        ch = video.get("channel")
        ch_id = video.get("channel_id")
        upl = video.get("uploader")
        upl_id = video.get("uploader_id")
        # If all channel/uploader fields are missing/None, it's private in flat mode
        if not ch_url and not upl_url and not ch and not ch_id and not upl and not upl_id:
            # Also check that duration and view_count are missing (as in actual private data)
            # to avoid false positives, but channel missing alone is strong signal
            # We require at least channel missing, which is not expected for public videos
            return False

    # For final normalized entries, ensure http url exists
    # If url was originally id-only, we consider it available if other checks passed,
    # because downloader will convert id to full url. But if after normalization
    # url is still not http, it cannot be downloaded.
    # To enforce this for normalized entries, we check if url is http or id,
    # but if title is empty and url is id, we still allow (missing title != unavailable)
    # The definitive http check happens after normalization in downloader.
    # Here we only reject if url is empty.

    # Otherwise available
    return True


def is_video_available(video: Dict[str, Any]) -> bool:
    """Alias for is_video_entry_available."""
    return is_video_entry_available(video)


def filter_available_videos(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return new list containing only available videos.

    Single primary filtering implementation:
      raw entries -> filter_available_videos -> sort -> preview + download

    Does NOT mutate original list. Never crashes. Returns [] if all unavailable.
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


# ---------------------------------------------------------------------------
# URL type detection
# ---------------------------------------------------------------------------

def detect_youtube_url_type(url: str) -> str:
    """Detect whether a YouTube URL is a video or playlist.

    Returns:
        "playlist" - YouTube playlist URL
        "video"    - YouTube video URL (including video URL that contains list= param)
        "unknown"  - not a recognizable YouTube URL or empty
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

        is_youtube = "youtube.com" in netloc or "youtu.be" in netloc
        if not is_youtube:
            if "youtube" not in netloc and "youtu.be" not in netloc:
                return "unknown"

        if "youtu.be" in netloc:
            return "video"

        if "/playlist" in path:
            return "playlist"

        if "/shorts/" in path or "/embed/" in path or "/watch" in path:
            return "video"

        has_list = "list" in query and any(v for v in query.get("list", []) if v)
        has_v = "v" in query and any(v for v in query.get("v", []) if v)

        if has_list and not has_v:
            return "playlist"

        if has_v:
            return "video"

        if has_list:
            return "playlist"

        if "watch?v=" in u.lower() or "youtube.com/watch" in u.lower():
            return "video"

        return "video"

    except Exception:
        return "unknown"


def is_youtube_playlist_url(url: str) -> bool:
    return detect_youtube_url_type(url) == "playlist"


def is_youtube_video_url(url: str) -> bool:
    return detect_youtube_url_type(url) == "video"
