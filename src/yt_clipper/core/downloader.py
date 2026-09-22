import os
import random
import time
from typing import Any, cast

import yt_dlp
import yt_dlp.utils  # explicit submodule import so `yt_dlp.utils.DownloadError` resolves
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from yt_clipper.core.ffmpeg_runner import DownloadCancelled, run_ffmpeg_clip
from yt_clipper.core.js_runtime import build_ydl_js_runtime_option


FORMAT_MAP = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}


class _FilteredYtDlpLogger:
    """Custom yt-dlp logger that suppresses expected unavailable-video INFO messages.

    yt-dlp's YoutubeTab extractor emits:
        WARNING: [youtube:tab] YouTube said: INFO - 1 unavailable video is hidden
    This is informational and already handled by our filtering logic (unavailable
    videos are intentionally ignored). Showing it as a WARNING confuses users.

    This logger filters only that specific pattern, preserving real errors:
    - invalid URL, auth failure, network failure, extraction failure etc.
      are still raised as DownloadError exceptions and surfaced via UI.
    - Other warnings are suppressed to keep UI clean (quiet=True already does),
      but the specific unavailable-video message is explicitly ignored.
    """

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        try:
            lower = str(msg).lower()
            if "unavailable video" in lower and "hidden" in lower:
                return
            if "youtube said" in lower and "unavailable" in lower:
                return
            if "youtube said: info" in lower:
                return
        except Exception:
            pass
        pass

    def error(self, msg):
        pass


def _get_ydl_logger_option():
    return {"logger": _FilteredYtDlpLogger()}

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 1.5

_TRANSIENT_ERROR_MARKERS = (
    "timed out", "timeout", "temporary failure", "connection reset",
    "connection aborted", "connection refused", "network is unreachable",
    "name or service not known", "getaddrinfo failed", "nodename nor servname",
    "http error 500", "http error 502", "http error 503", "http error 504",
    "http error 429", "too many requests", "urlopen error", "ssl",
    "eof occurred", "broken pipe", "server disconnected", "curl error",
    "end of file", "i/o error", "read error", "reset by peer",
)


def _is_transient_error(exc):
    return any(marker in str(exc).lower() for marker in _TRANSIENT_ERROR_MARKERS)


def _retry_call(func, cancel_event=None, on_retry=None,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, base_delay=DEFAULT_BASE_DELAY):
    attempt = 1
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Download cancelled")
        try:
            return func()
        except DownloadCancelled:
            raise
        except Exception as exc:
            if attempt >= max_attempts or not _is_transient_error(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1)) * random.uniform(0.85, 1.15)
            if on_retry:
                on_retry(attempt, max_attempts, delay, exc)
            _sleep_cancellable(delay, cancel_event)
            attempt += 1


def _sleep_cancellable(delay, cancel_event):
    if cancel_event is None:
        time.sleep(delay)
        return
    remaining = delay
    step = 0.1
    while remaining > 0:
        if cancel_event.is_set():
            return
        interval = min(step, remaining)
        time.sleep(interval)
        remaining -= interval


def _extract_info(url, options=None, cancel_event=None, on_retry=None):
    ydl_options: dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
    }
    ydl_options.update(_get_ydl_logger_option())
    ydl_options.update(build_ydl_js_runtime_option() or {})
    if options:
        ydl_options.update(options)

    def _do_extract():
        with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
            return ydl.extract_info(url, download=False)

    info = _retry_call(_do_extract, cancel_event=cancel_event, on_retry=on_retry)
    if info is None:
        raise ValueError(f"Could not fetch video info for: {url}")
    _reject_playlist(info)
    return info


def _reject_playlist(info):
    if info.get("_type") == "playlist" or "entries" in info:
        raise ValueError(
            "This looks like a playlist URL. Paste a link to a single video, "
            "or use expand_playlist to queue every video in it."
        )


def _extract_publish_date_from_info(info):
    try:
        from yt_clipper.core.playlist_utils import extract_publish_date
        return extract_publish_date(info)
    except Exception:
        try:
            from yt_clipper.core.utils import extract_publish_date
            return extract_publish_date(info)
        except Exception:
            return None


def get_video_info(url, cancel_event=None, on_retry=None):
    info = _extract_info(url, cancel_event=cancel_event, on_retry=on_retry)
    return {
        "title": info.get("title", "Unknown title"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
        "publish_date": _extract_publish_date_from_info(info),
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
    }


def expand_playlist(url, cancel_event=None, on_retry=None, max_videos=None):
    """Resolve a URL into one or more individual video entries.

    For a single video URL, yt-dlp always does a full extraction regardless
    of the "flat" option below, so the one entry returned already has
    duration and thumbnail populated - no second network round trip needed.

    For a playlist URL, this uses yt-dlp's fast "flat" listing (one request
    for the whole playlist, not one per video) and returns one lightweight
    entry per video.

    CRITICAL ARCHITECTURE:
      Raw yt-dlp entries must NEVER reach preview/download. Filtering happens
      immediately after extraction, before building shared dataset.

      YouTube playlist URL
          ↓
      yt-dlp extraction (extract_flat)
          ↓
      RAW entries
          ↓
      is_video_entry_available() on ORIGINAL title/availability (before fallback)
          ↓
      REMOVE unavailable completely
          ↓
      VALIDATED entries
          ↓
      sort by publish date
          ↓
      shared state -> Preview + Download
    """
    ydl_options: dict[str, Any] = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
    }
    ydl_options.update(_get_ydl_logger_option())
    ydl_options.update(build_ydl_js_runtime_option() or {})
    if max_videos:
        ydl_options["playlistend"] = max_videos

    def _do_extract():
        with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
            return ydl.extract_info(url, download=False)

    info = _retry_call(_do_extract, cancel_event=cancel_event, on_retry=on_retry)
    if info is None:
        raise ValueError(f"Could not fetch info for: {url}")

    if info.get("_type") == "playlist" or "entries" in info:
        raw_entries = []
        try:
            from yt_clipper.core.playlist_utils import is_video_entry_available
        except ImportError:
            from yt_clipper.core.utils import is_video_entry_available

        # For ambiguous entries where title=None and availability=None (new lockupViewModel
        # private videos), flat extraction does NOT contain enough info. We need to verify
        # via a lightweight full extraction of that single video (no download). This is
        # only done for ambiguous entries, not for every playlist item, to avoid perf regression.
        def _is_ambiguous_and_unavailable_via_full_check(
            raw_dict: dict, single_url: str
        ) -> bool:
            """
            Returns True if the entry is definitively unavailable based on full extraction,
            False if it is available or cannot be determined (conservative: keep).
            Only called when title is None/empty and availability is None/empty.
            """
            # Actual yt-dlp data for private video in flat mode (issue #318):
            # title=None, availability=None, duration=None, view_count=None,
            # channel_url=None, uploader_url=None, channel missing.
            # Available video with missing title would still have channel_url etc.
            # But to avoid heuristic, we do a real yt-dlp extraction for that URL.
            try:
                # Use existing _extract_info which has retry and logger handling
                # It will raise DownloadError for private/deleted videos
                full_info = _extract_info(
                    single_url, cancel_event=cancel_event, on_retry=on_retry
                )
            except yt_dlp.utils.DownloadError as exc:
                msg = str(exc).lower()
                # Explicit private/deleted signals from yt-dlp full extraction
                if (
                    "private video" in msg
                    or "deleted video" in msg
                    or "video unavailable" in msg
                    or "has been removed" in msg
                    or "private" in msg
                    and "video" in msg
                ):
                    return True
                # If it's a different DownloadError (e.g., not private), be conservative
                # and treat as unavailable only if message clearly indicates unavailability
                # Otherwise keep it (return False) to avoid false filtering
                if "unavailable" in msg or "removed" in msg or "deleted" in msg:
                    return True
                return False
            except Exception:
                # On any other exception (network etc.), be conservative: keep
                return False

            # Full extraction succeeded – check its explicit availability/title
            try:
                if not is_video_entry_available(full_info):
                    return True
            except Exception:
                pass

            # Also check availability field directly from full info
            avail = (full_info.get("availability") or "").strip().lower()
            # Import unavailable set for direct check
            try:
                from yt_clipper.core.playlist_utils import _UNAVAILABLE_AVAILABILITY as _UNAV_SET
            except ImportError:
                _UNAV_SET = {
                    "private",
                    "needs_auth",
                    "premium",
                    "premium_only",
                    "subscriber_only",
                    "unavailable",
                }
            if avail in _UNAV_SET:
                return True

            # If full info title is placeholder, unavailable
            t = (full_info.get("title") or "").strip().lower()
            if "[private video]" in t or "[deleted video]" in t or "video unavailable" in t:
                return True

            return False

        for raw_entry in info.get("entries") or []:
            if not raw_entry:
                continue
            entry_url = raw_entry.get("url") or raw_entry.get("webpage_url") or raw_entry.get("id")
            if not entry_url:
                continue
            if not str(entry_url).startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"

            original_title = raw_entry.get("title")
            raw_availability = raw_entry.get("availability")

            # Check availability using ORIGINAL yt-dlp fields, BEFORE fallback
            # This prevents hidden videos with title=None from passing as available
            # because we would otherwise fallback title to url and miss placeholder detection
            check_dict = {
                "url": entry_url,
                "title": original_title,
                "availability": raw_availability,
                "id": raw_entry.get("id"),
            }

            try:
                if not is_video_entry_available(check_dict):
                    continue
            except Exception:
                # Conservative: if check crashes, skip only if title clearly indicates unavailable
                low = (original_title or "").lower()
                if "[private video]" in low or "[deleted video]" in low or "private video" in low or "deleted video" in low:
                    continue

            # CRITICAL: If title is None/empty and availability is None/empty, flat extraction
            # does NOT contain enough info (lockupViewModel private videos). Verify via
            # lightweight full extraction (no download) – only for ambiguous entries.
            # Actual yt-dlp data for private flat entry (boul2gom/yt-dlp#318):
            #   title=None, availability=None, duration=None, view_count=None,
            #   channel_url=None, uploader_url=None, channel/channel_id/uploader missing
            # Available entry with same title=None would still have channel_url etc.
            # We use explicit yt-dlp fields (not heuristic on thumbnail) to fast-path,
            # then fall back to full extraction for absolute reliability.
            title_is_empty = not (original_title and str(original_title).strip())
            avail_is_empty = not (raw_availability and str(raw_availability).strip())
            if title_is_empty and avail_is_empty:
                # Fast path based on verified actual raw data – no guessing on thumbnail
                ch_url = raw_entry.get("channel_url")
                upl_url = raw_entry.get("uploader_url")
                ch = raw_entry.get("channel")
                ch_id = raw_entry.get("channel_id")
                # Private entries have no channel info at all
                if not ch_url and not upl_url and not ch and not ch_id:
                    # Strong explicit signal: YouTube does not provide channel for private
                    # This is not "missing thumbnail" heuristic – it's channel presence
                    # which is expected for any public video. Verified from actual JSON.
                    continue
                # Otherwise, do full extraction check for reliability
                if _is_ambiguous_and_unavailable_via_full_check(raw_entry, entry_url):
                    continue

            publish_date = _extract_publish_date_from_info(raw_entry)
            raw_entries.append({
                "url": entry_url,
                "title": original_title or entry_url,
                "duration": raw_entry.get("duration"),
                "thumbnail": _best_thumbnail_url(raw_entry),
                "publish_date": publish_date,
                "upload_date": raw_entry.get("upload_date"),
                "timestamp": raw_entry.get("timestamp"),
                "release_timestamp": raw_entry.get("release_timestamp"),
                "availability": raw_entry.get("availability"),
            })

        if not raw_entries:
            original_count = len(list(info.get("entries") or []))
            if original_count > 0:
                return {
                    "is_playlist": True,
                    "playlist_title": info.get("title"),
                    "entries": [],
                }
            raise ValueError("This playlist has no videos, or they're all unavailable.")

        # Defensive second filter using shared utility
        try:
            from yt_clipper.core.playlist_utils import filter_available_videos
        except ImportError:
            from yt_clipper.core.utils import filter_available_videos
        try:
            entries = filter_available_videos(raw_entries)
        except Exception:
            entries = raw_entries

        return {
            "is_playlist": True,
            "playlist_title": info.get("title"),
            "entries": entries,
        }

    return {
        "is_playlist": False,
        "playlist_title": None,
        "entries": [{
            "url": url,
            "title": info.get("title") or url,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "publish_date": _extract_publish_date_from_info(info),
            "upload_date": info.get("upload_date"),
            "timestamp": info.get("timestamp"),
            "release_timestamp": info.get("release_timestamp"),
        }],
    }


def _best_thumbnail_url(raw_entry):
    thumbnail = raw_entry.get("thumbnail")
    if thumbnail:
        return thumbnail
    thumbnails = raw_entry.get("thumbnails") or []
    if thumbnails:
        best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        if best.get("url"):
            return best["url"]
    video_id = raw_entry.get("id")
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def list_formats(url):
    info = _extract_info(url)
    formats = info.get("formats") or []
    listed = []
    for media_format in formats:
        vcodec = media_format.get("vcodec") or "none"
        acodec = media_format.get("acodec") or "none"
        has_video = vcodec != "none"
        has_audio = acodec != "none"
        if has_video and has_audio:
            kind = "video+audio"
        elif has_video:
            kind = "video only"
        elif has_audio:
            kind = "audio only"
        else:
            continue
        listed.append({
            "format_id": media_format.get("format_id"),
            "kind": kind,
            "height": media_format.get("height"),
            "ext": media_format.get("ext"),
            "vbr": media_format.get("vbr"),
            "abr": media_format.get("abr"),
        })
    return listed


def download_clip(
    url,
    start_sec,
    end_sec,
    output_path="clip.mp4",
    quality="best",
    audio_only=False,
    format_id=None,
    progress_hook=None,
    cancel_event=None,
):
    if start_sec is not None and end_sec is not None and end_sec <= start_sec:
        raise ValueError("End time must be after start time")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Download cancelled")

    # Defensive check: ensure url is valid http (should already be filtered)
    # This is safety net, primary filtering happens earlier
    try:
        from yt_clipper.core.playlist_utils import is_video_entry_available
    except ImportError:
        from yt_clipper.core.utils import is_video_entry_available
    try:
        # If someone passes a dict-like unavailable entry as url (should not happen),
        # we still guard. For normal url string, this check passes if http.
        if isinstance(url, dict):
            if not is_video_entry_available(url):
                raise ValueError("Attempted to download an unavailable video")
        else:
            if not isinstance(url, str) or not url.startswith("http"):
                raise ValueError(f"Invalid download URL: {url}")
    except ValueError:
        raise
    except Exception:
        pass

    if audio_only:
        base, _extension = os.path.splitext(output_path)
        output_path = base + ".mp3"

    if audio_only:
        format_selector = "bestaudio/best"
    elif format_id:
        format_selector = format_id
    else:
        format_selector = FORMAT_MAP.get(quality, FORMAT_MAP["best"])

    ydl_options: dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
        "format": format_selector,
    }
    ydl_options.update(_get_ydl_logger_option())
    ydl_options.update(build_ydl_js_runtime_option() or {})

    def _notify_retry(attempt, max_attempts, delay, exc):
        if progress_hook:
            progress_hook({
                "status": "retrying",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "delay": delay,
                "error": str(exc),
            })

    def _attempt():
        with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as exc:
                if format_id and not _is_transient_error(exc):
                    raise ValueError(
                        f"format_id {format_id!r} is not available for this video "
                        f"(it may be video-only and need an audio format merged "
                        f"in, e.g. \"{format_id}+140\"): {exc}"
                    ) from exc
                raise
            if info is None:
                raise ValueError(f"Could not fetch video info for: {url}")
            _reject_playlist(info)

            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelled("Download cancelled")

            resolved_start = 0.0 if start_sec is None else float(start_sec)
            if end_sec is None:
                duration = info.get("duration")
                if not isinstance(duration, (int, float)) or duration <= 0:
                    raise ValueError(
                        "Could not determine this video's duration to download it in full."
                    )
                resolved_end = float(duration)
            else:
                resolved_end = float(end_sec)
            if resolved_end <= resolved_start:
                raise ValueError("End time must be after start time")

            selected_formats = info.get("requested_formats") or [info]
            shared_headers = info.get("http_headers") or {}
            formats = []
            for selected_format in selected_formats:
                media_format = dict(selected_format)
                if shared_headers and not media_format.get("http_headers"):
                    media_format["http_headers"] = shared_headers
                formats.append(media_format)

            ffmpeg = FFmpegPostProcessor(downloader=ydl)
            if not ffmpeg.available:
                raise RuntimeError(
                    "FFmpeg was not found. Install FFmpeg and make it available on PATH."
                )
            ffmpeg.check_version()

            run_ffmpeg_clip(
                executable=ffmpeg.executable,
                formats=formats,
                output_path=output_path,
                start_sec=resolved_start,
                end_sec=resolved_end,
                audio_only=audio_only,
                progress_hook=progress_hook,
                cancel_event=cancel_event,
                cookiejar=ydl.cookiejar,
                proxy=ydl.params.get("proxy"),
            )

    _retry_call(_attempt, cancel_event=cancel_event, on_retry=_notify_retry)

    return output_path


__all__ = [
    "DownloadCancelled",
    "download_clip",
    "expand_playlist",
    "get_video_info",
    "list_formats",
]
