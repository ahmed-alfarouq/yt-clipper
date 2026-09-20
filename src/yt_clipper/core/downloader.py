import os
import random
import time
from typing import Any, cast

import yt_dlp
import yt_dlp.utils  # explicit submodule import so `yt_dlp.utils.DownloadError` resolves
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from yt_clipper.core.ffmpeg_runner import DownloadCancelled, run_ffmpeg_clip


FORMAT_MAP = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}

DEFAULT_MAX_ATTEMPTS = 4  # 1 initial try + up to 3 retries
DEFAULT_BASE_DELAY = 1.5  # seconds; roughly doubles each retry (1.5s, 3s, 6s...)

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
    """Best-effort check for a likely network/server hiccup worth retrying.

    Deliberately conservative: things like a bad URL, an invalid format_id,
    a rejected playlist, or missing FFmpeg are NOT retried, since retrying
    those only delays a useful error message without any chance of success.
    """
    return any(marker in str(exc).lower() for marker in _TRANSIENT_ERROR_MARKERS)


def _retry_call(func, cancel_event=None, on_retry=None,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, base_delay=DEFAULT_BASE_DELAY):
    """Call func() with exponential backoff on transient errors.

    Cancellation is checked before every attempt and in small increments
    during the backoff sleep, so a cancel request is never held up waiting
    out a retry delay. on_retry(attempt, max_attempts, delay, exc), when
    given, is called right before each backoff sleep so callers can surface
    the retry to the user instead of it happening silently.
    """
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
    """Extract one video's information without downloading it."""
    ydl_options: dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
    }
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


def get_video_info(url, cancel_event=None, on_retry=None):
    info = _extract_info(url, cancel_event=cancel_event, on_retry=on_retry)
    return {
        "title": info.get("title", "Unknown title"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
    }


def expand_playlist(url, cancel_event=None, on_retry=None, max_videos=None):
    """Resolve a URL into one or more individual video entries.

    For a single video URL, yt-dlp always does a full extraction regardless
    of the "flat" option below, so the one entry returned already has
    duration and thumbnail populated - no second network round trip needed.

    For a playlist URL, this uses yt-dlp's fast "flat" listing (one request
    for the whole playlist, not one per video) and returns one lightweight
    entry per video. Those entries do NOT have duration/thumbnail, since
    fetching that for every video up front would mean a full extraction per
    video before anything could be queued.
    """
    ydl_options: dict[str, Any] = {
        "quiet": True,
        "extract_flat": "in_playlist",
    }
    if max_videos:
        ydl_options["playlistend"] = max_videos

    def _do_extract():
        with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
            return ydl.extract_info(url, download=False)

    info = _retry_call(_do_extract, cancel_event=cancel_event, on_retry=on_retry)
    if info is None:
        raise ValueError(f"Could not fetch info for: {url}")

    if info.get("_type") == "playlist" or "entries" in info:
        entries = []
        for raw_entry in info.get("entries") or []:
            if not raw_entry:
                continue
            entry_url = raw_entry.get("url") or raw_entry.get("webpage_url") or raw_entry.get("id")
            if not entry_url:
                continue
            if not str(entry_url).startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry_url}"
            entries.append({
                "url": entry_url,
                "title": raw_entry.get("title") or entry_url,
                "duration": raw_entry.get("duration"),
                "thumbnail": _best_thumbnail_url(raw_entry),
            })
        if not entries:
            raise ValueError("This playlist has no videos, or they're all unavailable.")
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
        }],
    }


def _best_thumbnail_url(raw_entry):
    """Pick a thumbnail URL out of a yt-dlp entry.

    A flat (extract_flat) playlist entry almost never has the singular
    "thumbnail" field populated - that's only reliably set after a full,
    non-flat extraction. What it does have is a "thumbnails" list of
    {url, width, height} dicts, so we take the largest of those. As a last
    resort, YouTube's default thumbnail path is predictable from the video
    id alone, so that's used if nothing else is available.
    """
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
    """List every available format, video-only, audio-only, and muxed alike.

    Earlier this filtered out audio-only formats (vcodec == "none"), which
    made the listing useless for picking a format_id to pass to
    download_clip: video-only formats need an audio-only format merged in,
    but there was no way to see which audio format_ids existed.
    """
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
            continue  # neither video nor audio (e.g. storyboards); not downloadable

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
    """Download one time range with observable FFmpeg progress.

    yt-dlp selects and authorizes the source formats. The actual ranged transfer
    runs through our FFmpeg wrapper because yt-dlp's external FFmpeg downloader
    does not publish intermediate progress hooks or expose cancellation.

    start_sec/end_sec may be None to mean "the whole video": a None start_sec
    is treated as 0, and a None end_sec is resolved from the video's own
    duration once it's known (during the same extraction that already
    happens below). This is what full-video playlist downloads use, so no
    separate "download the whole thing" code path exists - it's just a clip
    whose range happens to be the entire video.

    format_id, when given, is passed straight through as a yt-dlp format
    selector (see `list_formats`) and takes priority over `quality`. It can
    name a single format_id (only valid if that format already has both
    video and audio) or an explicit merge like "137+140" (video-only id +
    audio-only id, as yt-dlp's own -f flag accepts). It is ignored when
    audio_only is True, since that always selects the best audio stream.

    On a transient network/server error (timeout, connection reset, 5xx,
    429, ...), the whole attempt - metadata fetch and FFmpeg transfer alike
    - is retried with exponential backoff, since the signed media URLs
    yt-dlp resolves can expire, so re-extracting on retry is the safe
    choice. progress_hook, if given, receives a {"status": "retrying", ...}
    event before each retry so the caller can surface it instead of the
    retry happening silently.
    """
    if start_sec is not None and end_sec is not None and end_sec <= start_sec:
        raise ValueError("End time must be after start time")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Download cancelled")

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

            # Resolve a full-video range using this video's own duration,
            # now that we actually have it - this is the only place that
            # needs to know about "None means full video".
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