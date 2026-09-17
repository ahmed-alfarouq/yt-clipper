import os
import queue
import re
import subprocess
import threading
from collections import deque
from pathlib import Path


class DownloadCancelled(Exception):
    """Raised after the user cancels an active clip download."""


def run_ffmpeg_clip(
    executable,
    formats,
    output_path,
    start_sec,
    end_sec,
    audio_only=False,
    progress_hook=None,
    cancel_event=None,
    cookiejar=None,
    proxy=None,
):
    """Stream a selected clip through FFmpeg with progress and cancellation."""
    duration = float(end_sec) - float(start_sec)
    if duration <= 0:
        raise ValueError("End time must be after start time")
    if not formats:
        raise ValueError("No downloadable media format was selected")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(
        f"{output_path.stem}.part{output_path.suffix}"
    )
    _remove_if_present(partial_path)

    command = [
        str(executable),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    for media_format in formats:
        media_url = media_format.get("url")
        if not media_url:
            raise ValueError("The selected media format has no download URL")

        headers = media_format.get("http_headers") or {}
        if headers:
            header_text = "".join(
                f"{key}: {value}\r\n" for key, value in headers.items()
            )
            command.extend(["-headers", header_text])

        if cookiejar is not None and re.match(r"https?://", media_url):
            cookies = cookiejar.get_cookies_for_url(media_url)
            if cookies:
                cookie_text = "".join(
                    f"{cookie.name}={cookie.value}; "
                    f"path={cookie.path}; domain={cookie.domain};\r\n"
                    for cookie in cookies
                )
                command.extend(["-cookies", cookie_text])

        command.extend(
            [
                "-ss",
                _format_seconds(start_sec),
                "-t",
                _format_seconds(duration),
                "-i",
                media_url,
            ]
        )

    if audio_only:
        audio_index = _find_stream_index(formats, "acodec")
        command.extend(
            [
                "-map",
                f"{audio_index}:a:0",
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
            ]
        )
    else:
        video_index = _find_stream_index(formats, "vcodec")
        audio_index = _find_stream_index(formats, "acodec", required=False)
        command.extend(["-map", f"{video_index}:v:0"])
        if audio_index is not None:
            command.extend(["-map", f"{audio_index}:a:0"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
            ]
        )

    command.extend(
        [
            "-progress",
            "pipe:1",
            "-nostats",
            str(partial_path),
        ]
    )

    environment = None
    if proxy:
        environment = os.environ.copy()
        environment["HTTP_PROXY"] = proxy
        environment["http_proxy"] = proxy

    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
        creationflags=creation_flags,
    )

    stdout_lines = queue.Queue()
    stderr_tail = deque(maxlen=40)
    stdout_thread = threading.Thread(
        target=_read_lines,
        args=(process.stdout, stdout_lines),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stderr,
        args=(process.stderr, stderr_tail),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    progress_state = {}
    _emit_progress(progress_hook, 0.0, partial_path, duration, progress_state)

    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process(process)
                raise DownloadCancelled("Download cancelled")

            try:
                line = stdout_lines.get(timeout=0.1)
            except queue.Empty:
                continue
            _consume_progress_line(
                line,
                progress_state,
                duration,
                partial_path,
                progress_hook,
            )

        while True:
            try:
                line = stdout_lines.get_nowait()
            except queue.Empty:
                break
            _consume_progress_line(
                line,
                progress_state,
                duration,
                partial_path,
                progress_hook,
            )

        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("Download cancelled")
        if process.returncode != 0:
            details = "\n".join(stderr_tail).strip()
            raise RuntimeError(details or f"FFmpeg exited with code {process.returncode}")

        os.replace(partial_path, output_path)
        if progress_hook:
            progress_hook(
                {
                    "status": "finished",
                    "filename": str(output_path),
                    "downloaded_bytes": output_path.stat().st_size,
                    "_percent_str": "100.0%",
                }
            )
    except BaseException:
        if process.poll() is None:
            _stop_process(process)
        _remove_if_present(partial_path)
        raise


def _find_stream_index(formats, codec_key, required=True):
    for index, media_format in enumerate(formats):
        codec = media_format.get(codec_key)
        if codec and codec != "none":
            return index
    if required:
        stream_name = "audio" if codec_key == "acodec" else "video"
        raise ValueError(f"The selected formats contain no {stream_name} stream")
    return None


def _read_lines(stream, destination):
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        destination.put(line.rstrip("\r\n"))
    stream.close()


def _read_stderr(stream, destination):
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        cleaned = line.rstrip("\r\n")
        if cleaned:
            destination.append(cleaned)
    stream.close()


def _consume_progress_line(line, state, duration, partial_path, progress_hook):
    key, separator, value = line.partition("=")
    if not separator:
        return
    state[key.strip()] = value.strip()
    if key.strip() != "progress":
        return

    elapsed = _elapsed_seconds(state)
    fraction = max(0.0, min(1.0, elapsed / duration))
    if value.strip() != "end":
        fraction = min(fraction, 0.999)
    _emit_progress(progress_hook, fraction, partial_path, duration, state)


def _elapsed_seconds(state):
    raw_microseconds = state.get("out_time_us") or state.get("out_time_ms")
    if raw_microseconds:
        try:
            return max(0.0, float(raw_microseconds) / 1_000_000.0)
        except ValueError:
            pass

    time_text = state.get("out_time", "")
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", time_text)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _emit_progress(progress_hook, fraction, partial_path, duration, state):
    if progress_hook is None:
        return

    speed_text = state.get("speed", "").strip()
    eta_text = ""
    speed_match = re.fullmatch(r"([0-9.]+)x", speed_text)
    if speed_match:
        speed = float(speed_match.group(1))
        if speed > 0:
            remaining_media_seconds = max(0.0, duration * (1.0 - fraction))
            eta_text = _format_eta(remaining_media_seconds / speed)

    downloaded_bytes = 0
    try:
        downloaded_bytes = partial_path.stat().st_size
    except OSError:
        pass

    progress_hook(
        {
            "status": "downloading",
            "downloaded_bytes": downloaded_bytes,
            "_percent_str": f"{fraction * 100:.1f}%",
            "_speed_str": speed_text,
            "_eta_str": eta_text,
            "elapsed_media_seconds": duration * fraction,
            "clip_duration": duration,
        }
    )


def _format_seconds(value):
    return f"{float(value):.3f}"


def _format_eta(seconds):
    seconds = max(0, int(round(seconds)))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _remove_if_present(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
