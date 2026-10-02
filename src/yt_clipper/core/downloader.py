import os
import random
import time
from collections import deque
from typing import Any, cast

import yt_dlp
import yt_dlp.utils  # explicit submodule import so `yt_dlp.utils.DownloadError` resolves
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from yt_clipper.core.errors import DownloadCancelled
from yt_clipper.core.ffmpeg_runner import run_ffmpeg_clip
from yt_clipper.core.js_runtime import build_ydl_js_runtime_option
from yt_clipper.core.log import describe_failure, get_logger, redact_secrets, safe_message

logger = get_logger(__name__)


FORMAT_MAP = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}

# The quality tokens offered to the user, in the order both surfaces present
# them: the CLI's -q/--quality choices and the GUI's quality dropdown. This is
# the single definition of that list - it must stay in step with FORMAT_MAP's
# keys, because a token offered here but unresolved there would silently
# degrade to "best" through the FORMAT_MAP.get() default below. The order
# differs from FORMAT_MAP's key order on purpose: this is display order.
QUALITY_CHOICES = ("best", "4k", "1080p", "720p")

# The quality used when a caller does not choose one.
DEFAULT_QUALITY = "best"


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

    Failure contract (§19): debug/info stay silent, expected "unavailable video"
    warnings stay silent, unexpected warnings are recorded at DEBUG, and
    error-level messages are forwarded to the real logging path - redacted and
    de-duplicated - so a genuine failure is observable even when the caller
    only sees a generic DownloadError. One instance is created per extraction
    (see `_get_ydl_logger_option`), so the de-duplication window is exactly one
    yt-dlp run and needs no locking.
    """

    #: How many distinct error messages one run remembers for de-duplication.
    _MAX_REMEMBERED_ERRORS = 16

    def __init__(self, error_logger=None):
        self._error_logger = error_logger or logger
        self._recent_errors: deque[str] = deque(maxlen=self._MAX_REMEMBERED_ERRORS)

    def debug(self, msg):
        # Per-chunk extractor chatter: intentionally not observable (§19).
        pass

    def info(self, msg):
        # Same as debug: quiet=True already suppresses the user-facing copy.
        pass

    def warning(self, msg):
        try:
            lower = str(msg).lower()
        except Exception as exc:
            # The filter itself must never turn a warning into a failure.
            logger.debug("yt-dlp warning could not be inspected: %s",
                         describe_failure(exc))
            return
        if "unavailable video" in lower and "hidden" in lower:
            return
        if "youtube said" in lower and "unavailable" in lower:
            return
        if "youtube said: info" in lower:
            return
        # Any other warning: expected noise for the user, useful for diagnosis.
        logger.debug("yt-dlp warning: %s", redact_secrets(str(msg)))

    def error(self, msg):
        """Forward yt-dlp error-level messages to the logging path (§19)."""
        text = redact_secrets(str(msg)).strip()
        if not text:
            return
        key = " ".join(text.split())
        if key in self._recent_errors:
            # yt-dlp can report one failure through several channels; keep the
            # log free of repeats without losing the first occurrence.
            logger.debug("yt-dlp repeated error (already logged): %s", text)
            return
        self._recent_errors.append(key)
        self._error_logger.error("yt-dlp: %s", text)


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
            # Cancellation is an expected outcome, never a failure: recorded at
            # INFO and propagated untouched (§3).
            logger.info("Cancelled before attempt %d of the current operation", attempt)
            raise DownloadCancelled("Download cancelled")
        try:
            return func()
        except DownloadCancelled:
            raise
        except Exception as exc:
            if not _is_transient_error(exc):
                # Not our retryable class: the caller decides how to report it.
                logger.debug("Not retrying: %s", describe_failure(exc))
                raise
            if attempt >= max_attempts:
                # Exhausted retries must be observable, not silent (§10).
                logger.warning(
                    "Giving up after %d attempt(s): %s", attempt, describe_failure(exc)
                )
                raise
            delay = base_delay * (2 ** (attempt - 1)) * random.uniform(0.85, 1.15)
            logger.debug(
                "Transient failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt, max_attempts, delay, describe_failure(exc),
            )
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
    """Optional metadata: any failure falls back to None, never aborts (§12)."""
    try:
        from yt_clipper.core.playlist_utils import extract_publish_date
        return extract_publish_date(info)
    except Exception as exc:
        logger.debug("playlist_utils.extract_publish_date unavailable (%s); "
                     "trying the legacy location", describe_failure(exc))
        try:
            from yt_clipper.core.utils import extract_publish_date
            return extract_publish_date(info)
        except Exception as fallback_exc:
            # A missing publish date only affects sorting/display, so the
            # operation continues with the documented fallback (§12).
            logger.warning(
                "Could not determine publish date; continuing without it: %s",
                describe_failure(fallback_exc),
            )
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
      classify_video_entry() on ORIGINAL fields (before the title fallback)
          ↓
      UNAVAILABLE → removed completely
      AVAILABLE   → kept
      UNKNOWN     → full extraction of that single video → kept unless the
                    full result is itself explicitly unavailable
          ↓
      VALIDATED entries (missing flat metadata filled in from the full check)
          ↓
      shared state -> Preview + Download (sorting happens in the GUI layer)
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
        from yt_clipper.core import playlist_utils

        def _resolve_ambiguous_entry(single_url: str):
            """Resolve one flat entry whose metadata proves nothing either way.

            Returns (keep, full_info). keep is False only when a full extraction
            of that single video *explicitly* reports it as private/deleted/
            unavailable. An available result - or an error we cannot interpret -
            keeps the entry, because silently discarding a valid video is worse
            than letting yt-dlp report a genuine failure at download time.
            full_info is returned so the caller can fill in the metadata the
            flat entry was missing (title, duration, thumbnail, dates) instead
            of showing the raw URL as the title.
            """
            try:
                # Reuses the shared single-video extraction, so this gets the
                # same retry / filtered-logger / js-runtime handling as the rest
                # of the module, and no download is performed.
                full_info = _extract_info(
                    single_url, cancel_event=cancel_event, on_retry=on_retry
                )
            except yt_dlp.utils.DownloadError as exc:
                # Explicit private/deleted/unavailable wording is recognised by
                # the canonical vocabulary in playlist_utils - this module no
                # longer keeps a second copy of those markers.
                if playlist_utils.is_unavailable_error_text(exc):
                    # Expected condition, not an error (§18): a private/deleted
                    # video is filtered out on purpose.
                    logger.info(
                        "Playlist entry %s reported unavailable (%s); skipped",
                        redact_secrets(single_url), safe_message(exc),
                    )
                    return False, None
                # Some other DownloadError (geo-blocked, age-restricted, ...):
                # keep it and let the download itself report the problem.
                logger.warning(
                    "Availability check for %s was inconclusive (%s); keeping the "
                    "entry so the download can report a real failure",
                    redact_secrets(single_url), safe_message(exc),
                )
                return True, None
            except DownloadCancelled:
                # Cancellation must keep propagating to the caller/UI (§3).
                raise
            except Exception as exc:
                # Network or unexpected failure while checking: keep (conservative).
                logger.warning(
                    "Availability check for %s failed (%s); keeping the entry "
                    "rather than dropping a possibly valid video",
                    redact_secrets(single_url), describe_failure(exc),
                )
                return True, None

            if not isinstance(full_info, dict):
                logger.debug(
                    "Full extraction for %s returned %s; keeping the entry "
                    "without extra metadata", single_url, type(full_info).__name__,
                )
                return True, None
            # The full result carries complete metadata, so the shared
            # classifier can now give a definitive answer. The URL is filled in
            # because we already know it: a full result that happens to omit
            # webpage_url must not be mistaken for "nothing to download".
            full_check_target = dict(full_info)
            full_check_target.setdefault("url", single_url)
            decision = playlist_utils.classify_video_entry(full_check_target)
            if decision is playlist_utils.AVAILABILITY_UNAVAILABLE:
                return False, None
            return True, full_info

        for raw_entry in info.get("entries") or []:
            if not raw_entry:
                # An empty entry carries nothing to clip; skipping it must still
                # be visible so a shrinking playlist is explainable (§4, §18).
                logger.warning("Playlist entry was empty; skipped")
                continue
            entry_url = raw_entry.get("url") or raw_entry.get("webpage_url") or raw_entry.get("id")
            if not entry_url:
                logger.warning(
                    "Playlist entry %r has no usable URL or id; skipped",
                    raw_entry.get("title") if isinstance(raw_entry, dict) else raw_entry,
                )
                continue
            if not str(entry_url).startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"

            # Classify from the ORIGINAL yt-dlp fields (before the title->url
            # fallback below), so placeholder titles such as "[Private video]"
            # are still detected. The whole raw entry is passed on purpose: its
            # channel/uploader/duration fields are positive evidence that the
            # video exists, and dropping them is what used to make thin-but-valid
            # entries look private.
            check_dict = dict(raw_entry) if isinstance(raw_entry, dict) else {}
            check_dict["url"] = entry_url
            decision = playlist_utils.classify_video_entry(check_dict)
            if decision is playlist_utils.AVAILABILITY_UNAVAILABLE:
                # Expected filtering, never an error (§18): recorded at INFO so
                # "why is this video missing from my list?" stays answerable.
                logger.info(
                    "Playlist entry %s is explicitly unavailable; skipped", entry_url
                )
                continue

            # UNKNOWN means the flat listing carried no explicit signal and no
            # positive evidence (lockupViewModel entries can omit title,
            # availability AND channel metadata). That is not proof of
            # unavailability, so verify this single video with a lightweight full
            # extraction - only for ambiguous entries, never for the whole
            # playlist, to avoid a per-item network round trip.
            full_info = None
            if decision is playlist_utils.AVAILABILITY_UNKNOWN:
                keep, full_info = _resolve_ambiguous_entry(entry_url)
                if not keep:
                    continue

            # Fill in only what the flat entry was missing; never overwrite data
            # yt-dlp already gave us for the playlist item.
            source_entry = raw_entry if isinstance(raw_entry, dict) else {}
            if full_info:
                source_entry = dict(source_entry)
                for key, value in full_info.items():
                    if value is not None and source_entry.get(key) is None:
                        source_entry[key] = value

            publish_date = _extract_publish_date_from_info(source_entry)
            raw_entries.append({
                "url": entry_url,
                "title": source_entry.get("title") or entry_url,
                "duration": source_entry.get("duration"),
                "thumbnail": _best_thumbnail_url(source_entry),
                "publish_date": publish_date,
                "upload_date": source_entry.get("upload_date"),
                "timestamp": source_entry.get("timestamp"),
                "release_timestamp": source_entry.get("release_timestamp"),
                "availability": source_entry.get("availability"),
            })

        if not raw_entries:
            original_count = len(list(info.get("entries") or []))
            if original_count > 0:
                # EXPECTED_EMPTY, not a failure: every entry was explicitly
                # unavailable, which the user is told about through the UI (§2).
                logger.info(
                    "Playlist %r listed %d entr(y/ies) but none are available",
                    info.get("title"), original_count,
                )
                return {
                    "is_playlist": True,
                    "playlist_title": info.get("title"),
                    "entries": [],
                }
            # yt-dlp gave us an empty listing: still a user-visible failure
            # (reported by the caller), so record it without duplicating the
            # message the CLI/GUI already shows (§17, §18).
            logger.info("Playlist %r returned no entries at all", redact_secrets(url))
            raise ValueError("This playlist has no videos, or they're all unavailable.")

        # The loop above is this boundary's single application of the canonical
        # decision: every entry it kept was classified AVAILABLE, or UNKNOWN and
        # then resolved by a full extraction whose result was classified again.
        # Re-filtering the normalized output cannot change that answer - a
        # normalized entry always carries a URL and a title, so it classifies
        # AVAILABLE - and running the same list through the filter twice only
        # obscures which pass made the decision.
        logger.info(
            "Playlist %r resolved to %d downloadable video(s)",
            info.get("title"), len(raw_entries),
        )
        return {
            "is_playlist": True,
            "playlist_title": info.get("title"),
            "entries": raw_entries,
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
    quality=DEFAULT_QUALITY,
    audio_only=False,
    format_id=None,
    progress_hook=None,
    cancel_event=None,
):
    if start_sec is not None and end_sec is not None and end_sec <= start_sec:
        raise ValueError("End time must be after start time")
    if cancel_event is not None and cancel_event.is_set():
        # Already cancelled before any work started: report CANCELLED, do not
        # begin a download the user asked to stop (§3.4).
        logger.info("Clip request cancelled before it started: %s", redact_secrets(url))
        raise DownloadCancelled("Download cancelled")
    logger.info(
        "Clip requested: %s range=%s-%s audio_only=%s -> %s",
        redact_secrets(url), start_sec, end_sec, audio_only, output_path,
    )

    # Defensive check: ensure url is valid http (should already be filtered)
    # This is safety net, primary filtering happens earlier
    from yt_clipper.core.playlist_utils import is_video_entry_available
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
        # A genuinely invalid target: this is the operation's FAILURE outcome
        # (§7), so it propagates to the caller that reports it.
        raise
    except Exception as exc:
        # The safety net itself broke (e.g. an entry shape it cannot inspect).
        # Fail open - the primary filtering already ran - but stay observable
        # instead of hiding a broken guard (§4, §21).
        logger.warning("Download URL safety check could not run; continuing: %s",
                       describe_failure(exc))

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
                # Redacted: this text is displayed to the user, and yt-dlp
                # messages can embed signed media URLs (§16, §18).
                "error": safe_message(exc),
            })

    def _attempt():
        with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as exc:
                if format_id and not _is_transient_error(exc):
                    # Translated into an actionable message for the user; the
                    # reason is recorded because the raw text alone is cryptic.
                    logger.warning(
                        "Requested format_id %r is not usable for %s: %s",
                        format_id, redact_secrets(url), safe_message(exc),
                    )
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

    logger.info("Clip written: %s", output_path)
    return output_path


__all__ = [
    "DownloadCancelled",
    "download_clip",
    "expand_playlist",
    "get_video_info",
    "list_formats",
]
