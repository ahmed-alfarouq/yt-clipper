"""
Playlist-specific utilities: availability, filtering, sorting, publish-date handling, URL detection.

This module is intentionally small and focused, keeping core/utils.py from becoming a dumping ground.
It provides the single authoritative implementation for:

- classify_video_entry (tri-state: available / unavailable / unknown)
- is_video_available / is_video_entry_available
- filter_available_videos
- sort_videos_by_publish_date
- publish-date parsing/formatting
- YouTube URL type detection

Flow:
  raw yt-dlp entries
      ↓
  classify_video_entry()      <- explicit signals only
      ↓
  unavailable → dropped · available → kept · unknown → resolved by the caller
      ↓
  filter_available_videos()   <- uses is_video_entry_available()
      ↓
  sort_videos_by_publish_date()
      ↓
  shared playlist state -> Preview + Download
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import math as _math
from urllib.parse import urlparse, parse_qs

from yt_clipper.core.log import describe_failure, get_logger

logger = get_logger(__name__)


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
            # Format probe, not a failure (§12): an unrecognised upload date
            # simply means "no date", which sorts last and renders as
            # "Unknown date". Nothing is discarded and no operation fails.
            return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(v[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            # Next candidate format; the loop's outcome (None) is documented
            # above, so a per-format miss is not worth a log record.
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
            # A value that cannot even be floated is simply "no date" (§12).
            # Broad on purpose: the input comes from arbitrary yt-dlp metadata.
            return None
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        if ts < 0 or ts > 4102444800:
            return None
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            # Platform-specific timestamp limits: "no date", never a failure.
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
            # Format probe, not a failure: the ISO attempt below still runs.
            pass
        try:
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception as exc:
            # Optional display metadata: no recognised date simply sorts last
            # and renders as "Unknown date" (§12).
            logger.debug("Unrecognised publish date %r: %s", s, describe_failure(exc))
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
        except Exception as exc:
            # Sorting is presentation-only: an entry without a usable date keeps
            # its place at the end instead of failing the whole sort (§12).
            logger.debug("Publish date unavailable while sorting entry %r: %s",
                         v.get("id") if isinstance(v, dict) else v,
                         describe_failure(exc))
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
    except Exception as exc:
        # Display-only fallback (e.g. a year outside the platform's strftime
        # range): the row still renders, so this is not an operation failure.
        logger.debug("Could not format publish date %r: %s", dt, describe_failure(exc))
        return "Unknown date"


# ---------------------------------------------------------------------------
# Availability classification
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
# UNAVAILABLE is only ever decided from an explicit signal:
#   - availability field
#   - title placeholders
#   - no URL at all (nothing could be downloaded)
# We must NOT use weak checks like title presence, thumbnail presence, id presence
# or publish date presence alone. A flat entry can legitimately omit title,
# availability, duration and channel metadata, so "not enough data" is a third
# state (UNKNOWN) that the caller has to resolve - never a silent drop.

AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_UNAVAILABLE = "unavailable"
AVAILABILITY_UNKNOWN = "unknown"

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

# Presence of any of these means YouTube returned a real video object for the
# entry. Private/deleted lockupViewModel entries omit every one of them
# (verified from actual yt-dlp JSON, boul2gom/yt-dlp#318), so they are positive
# evidence of availability. Their *absence* is only "unknown", never
# "unavailable" - that distinction is the whole point of the third state.
_AVAILABILITY_EVIDENCE_KEYS = (
    "channel",
    "channel_id",
    "channel_url",
    "uploader",
    "uploader_id",
    "uploader_url",
    "duration",
    "view_count",
)


def _field_text(value: Any) -> str:
    """Best-effort stripped text of a metadata field.

    Keeps classification total: None, booleans, empty/zero values, containers
    and unexpected object types all mean "field absent" instead of raising
    AttributeError on .strip().
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not value:
        return ""
    return str(value)


def classify_video_entry(video: Dict[str, Any]) -> str:
    """Classify one yt-dlp entry (flat or full) into one of three states.

      AVAILABILITY_UNAVAILABLE - yt-dlp/YouTube explicitly marks it private,
                                 deleted, removed, premium/auth-only or
                                 unavailable, or it carries no URL to download.
      AVAILABILITY_AVAILABLE   - positive evidence of a real video: a usable
                                 title, an explicit availability value that is
                                 not one of the unavailable ones, or any
                                 channel/uploader/duration/view_count metadata.
      AVAILABILITY_UNKNOWN     - the entry proves nothing either way. The caller
                                 must resolve it (a single-video full
                                 extraction) instead of discarding it.

    Single source of truth for availability. Never raises.
    """
    if not isinstance(video, dict) or not video:
        return AVAILABILITY_UNAVAILABLE

    # Nothing can be downloaded without a URL. Raw flat entries may carry a bare
    # video id; callers expand it to a watch URL before classifying.
    if not (_field_text(video.get("url")) or _field_text(video.get("webpage_url"))):
        return AVAILABILITY_UNAVAILABLE

    # Explicit availability signal from yt-dlp badges.
    availability = _field_text(video.get("availability")).lower()
    if availability in _UNAVAILABLE_AVAILABILITY:
        return AVAILABILITY_UNAVAILABLE

    # Title placeholders yt-dlp uses for hidden/deleted/private entries.
    title = _field_text(video.get("title"))
    lower_title = title.lower()
    if lower_title in _UNAVAILABLE_TITLE_MARKERS:
        return AVAILABILITY_UNAVAILABLE
    for marker in _UNAVAILABLE_TITLE_MARKERS:
        if lower_title.startswith(marker):
            return AVAILABILITY_UNAVAILABLE
    if "[private video]" in lower_title or "[deleted video]" in lower_title:
        return AVAILABILITY_UNAVAILABLE
    if "video unavailable" in lower_title or "video has been removed" in lower_title:
        return AVAILABILITY_UNAVAILABLE

    # Positive evidence, cheapest and strongest first.
    if title:
        return AVAILABILITY_AVAILABLE
    if availability:
        # Any other explicit value ("public", "unlisted", ...) means YouTube
        # described a real, reachable video.
        return AVAILABILITY_AVAILABLE
    for key in _AVAILABILITY_EVIDENCE_KEYS:
        if _field_text(video.get(key)):
            return AVAILABILITY_AVAILABLE

    # No explicit signal and no positive evidence: insufficient metadata.
    return AVAILABILITY_UNKNOWN


def is_video_entry_available(video: Dict[str, Any]) -> bool:
    """True unless the entry is *explicitly* unavailable.

    Thin boolean view over classify_video_entry(), kept for the existing callers
    (filter_available_videos and the downloader's defensive checks). UNKNOWN
    counts as available here on purpose: silently discarding a video just
    because its flat metadata was thin is exactly the failure this guards
    against. expand_playlist() resolves UNKNOWN entries with a full extraction
    of that single video before they can reach the preview or the queue, and
    normalized entries always carry a title, so they classify as AVAILABLE.

    Single source of truth for availability; preview and download must share
    the filtered result.
    """
    return classify_video_entry(video) is not AVAILABILITY_UNAVAILABLE


def is_video_available(video: Dict[str, Any]) -> bool:
    """Alias for is_video_entry_available."""
    return is_video_entry_available(video)


def filter_available_videos(videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return new list containing only videos that are not explicitly unavailable.

    Single primary filtering implementation:
      raw entries -> filter_available_videos -> sort -> preview + download

    Drops AVAILABILITY_UNAVAILABLE entries only; AVAILABILITY_AVAILABLE and
    AVAILABILITY_UNKNOWN entries are kept (see classify_video_entry).

    Does NOT mutate original list. Never crashes. Returns [] if all unavailable.
    Does NOT filter based on missing publish_date/thumbnail/title alone.

    Failure contract (§4): an entry whose availability cannot be determined
    because the check itself raised is KEPT and reported at WARNING. Only an
    explicit "unavailable" answer removes a video - an internal error must never
    silently shrink the user's playlist.
    """
    if not videos:
        return []
    available: List[Dict[str, Any]] = []
    for v in videos:
        try:
            if is_video_entry_available(v):
                available.append(v)
        except Exception as exc:
            # Keep the entry FIRST and explain without touching it again: the
            # object that broke the check may break a second time, and a
            # diagnostic that raises would turn a recoverable warning into a
            # failure of the whole filter (§4, §21).
            available.append(v)
            logger.warning(
                "Availability check raised for a playlist entry; keeping it "
                "instead of dropping a possibly valid video: %s",
                describe_failure(exc),
            )
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

    except Exception as exc:
        # Fail safe: an URL we cannot parse is treated as "not a playlist" so a
        # single-video clip still works. Logged because a misrouted playlist URL
        # would otherwise be invisible (§4, §18).
        logger.warning("Could not classify YouTube URL %r; treating it as unknown: %s",
                       u, describe_failure(exc))
        return "unknown"


def is_youtube_playlist_url(url: str) -> bool:
    return detect_youtube_url_type(url) == "playlist"


def is_youtube_video_url(url: str) -> bool:
    return detect_youtube_url_type(url) == "video"
