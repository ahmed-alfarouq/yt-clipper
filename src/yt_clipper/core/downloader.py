import os
from typing import Any, cast

import yt_dlp
import yt_dlp.utils
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from yt_clipper.core.ffmpeg_runner import DownloadCancelled, run_ffmpeg_clip


FORMAT_MAP = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}


def _extract_info(url, options=None):
    """Extract one video's information without downloading it."""
    ydl_options: dict[str, Any] = {
        "quiet": True,
        "noplaylist": True,
    }
    if options:
        ydl_options.update(options)

    with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"Could not fetch video info for: {url}")
    _reject_playlist(info)
    return info


def _reject_playlist(info):
    if info.get("_type") == "playlist" or "entries" in info:
        raise ValueError(
            "This looks like a playlist URL. Paste a link to a single video."
        )


def get_video_info(url):
    info = _extract_info(url)
    return {
        "title": info.get("title", "Unknown title"),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
    }


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
    if end_sec <= start_sec:
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

    with yt_dlp.YoutubeDL(cast(Any, ydl_options)) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            if format_id:
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
            start_sec=start_sec,
            end_sec=end_sec,
            audio_only=audio_only,
            progress_hook=progress_hook,
            cancel_event=cancel_event,
            cookiejar=ydl.cookiejar,
            proxy=ydl.params.get("proxy"),
        )

    return output_path


__all__ = [
    "DownloadCancelled",
    "download_clip",
    "get_video_info",
    "list_formats",
]