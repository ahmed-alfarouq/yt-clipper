import io
import math
import os
import threading
import urllib.request
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox

try:
    from PIL import Image
except ImportError:  # The rest of the application can still run without thumbnails.
    Image = None

from yt_clipper.core import downloader
from yt_clipper.core.utils import format_seconds, sanitize_filename

THUMBNAIL_SIZE = (120, 68)


class VideoLoaderController:
    """Handles fetching video metadata/thumbnail and updating the related widgets."""

    def __init__(self, app):
        self.app = app

    # ---------- Kickoff ----------

    def start_load_video(self):
        app = self.app
        url = app.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return

        app._load_request_id += 1
        request_id = app._load_request_id
        app.load_btn.configure(state="disabled", text="Loading...")
        app.video_info_label.configure(
            text="Connecting to YouTube...\nReading video details; this may take a few seconds.",
            text_color="#4da6ff",
        )
        app.set_status("Loading video information...", "#4da6ff")
        self._show_load_progress()

        threading.Thread(
            target=self._load_video_worker,
            args=(request_id, url),
            daemon=True,
        ).start()

    def _load_video_worker(self, request_id, url):
        app = self.app
        try:
            def _notify_retry(attempt, max_attempts, delay, exc):
                app._post_ui_event(
                    "video_retry", request_id, attempt, max_attempts, delay, str(exc)
                )

            result = downloader.expand_playlist(url, on_retry=_notify_retry)

            if result["is_playlist"]:
                app._post_ui_event(
                    "playlist_metadata",
                    request_id,
                    url,
                    result.get("playlist_title") or "Playlist",
                    result["entries"],
                )
                return

            entry = result["entries"][0]
            duration = entry.get("duration")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError(
                    "This video has no usable duration. Live streams are not supported."
                )

            title = str(entry.get("title") or "Unknown title")
            app._post_ui_event(
                "video_metadata",
                request_id,
                url,
                title,
                float(duration),
            )

            thumbnail, thumbnail_failed = self._fetch_thumbnail(entry.get("thumbnail"))
            app._post_ui_event(
                "video_thumbnail",
                request_id,
                thumbnail,
                thumbnail_failed,
            )
        except Exception as exc:
            app._post_ui_event("video_error", request_id, str(exc))

    @staticmethod
    def _fetch_thumbnail(thumbnail_url):
        if not thumbnail_url or Image is None:
            return None, bool(thumbnail_url)

        try:
            request = urllib.request.Request(
                thumbnail_url,
                headers={"User-Agent": "YT-Clipper/1.0"},
            )
            # The per-call timeout below fully bounds this request; no need
            # to touch the process-global socket timeout (which would be
            # unsafe to mutate while other threads may be using sockets too).
            with urllib.request.urlopen(request, timeout=6) as response:
                image_data = response.read()
            image = Image.open(io.BytesIO(image_data)).convert("RGB")
            image.thumbnail(THUMBNAIL_SIZE)
            image.load()
            return image, False
        except Exception:
            return None, True

    # ---------- Progress bar helpers ----------

    def _show_load_progress(self):
        app = self.app
        if not app.load_progress.winfo_manager():
            app.load_progress.pack(
                fill="x",
                padx=20,
                pady=(0, 12),
                after=app.info_row,
            )
        app.load_progress.start()

    def _hide_load_progress(self):
        app = self.app
        app.load_progress.stop()
        app.load_progress.pack_forget()

    # ---------- Event handlers (called from app._handle_ui_event) ----------

    def _apply_video_retry(self, request_id, attempt, max_attempts, delay, error_text):
        app = self.app
        if request_id != app._load_request_id:
            return
        app.video_info_label.configure(
            text=(
                f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) "
                f"in {delay:.0f}s...\n{error_text}"
            ),
            text_color="#e6a817",
        )
        app.set_status(f"Retrying video load ({attempt}/{max_attempts})...", "#e6a817")

    def _apply_playlist_metadata(self, request_id, url, playlist_title, entries):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.loaded_url = url
        app.loaded_title = playlist_title
        app.loaded_playlist_entries = entries
        app.video_duration = None  # no single duration; the same typed range
                                    # is applied to every video in the playlist

        self._set_thumbnail(None, False)
        app.start_slider.configure(to=100, state="disabled")
        app.end_slider.configure(to=100, state="disabled")
        app.start_slider.set(0)
        app.end_slider.set(100)
        # Deliberately leave the H/M/S steppers as the user set them - that
        # typed range is what gets applied to every video in the playlist.

        self._apply_suggested_filename(playlist_title)

        app.video_info_label.configure(
            text=(
                f"📃  {playlist_title}\n"
                f"{len(entries)} videos in playlist\n"
                "The same start/end time will be clipped from every video."
            ),
            text_color="#4caf50",
        )
        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        app.set_status(
            f"Playlist loaded ({len(entries)} videos). "
            "Choose a time range and add it to the queue.",
            "#4caf50",
        )

    def _apply_video_metadata(self, request_id, url, title, duration):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.video_duration = duration
        app.loaded_url = url
        app.loaded_title = title
        app.loaded_playlist_entries = None
        self._apply_suggested_filename(title)

        app.start_slider.configure(to=duration, state="normal")
        app.end_slider.configure(to=duration, state="normal")
        app.start_slider.set(0)
        app.end_slider.set(duration)
        app.start_input.set_seconds(0)
        app.end_input.set_seconds(duration)
        app.video_info_label.configure(
            text=(
                f"🎞  {title}\n"
                f"Duration: {format_seconds(duration)}\n"
                "Video details loaded • Fetching thumbnail..."
            ),
            text_color="#4da6ff",
        )
        app.update_clip_length()

        # Unlock the UI as soon as we have what's actually needed
        # (duration/URL). The thumbnail is a nice-to-have and must never be
        # able to block the user from downloading.
        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        app.set_status("Video loaded. Fetching thumbnail...", "#4da6ff")

    def _apply_suggested_filename(self, title):
        app = self.app
        current = app.output_entry.get().strip()
        directory = os.path.dirname(current) if current else ""
        if not directory:
            directory = str(app._default_output_path().parent)

        safe_name = sanitize_filename(title) or "clip"
        extension = ".mp3" if app.audio_only_var.get() else ".mp4"
        suggested = Path(directory) / f"{safe_name}{extension}"

        app.output_entry.delete(0, "end")
        app.output_entry.insert(0, str(suggested))

    def _apply_video_thumbnail(self, request_id, thumbnail, thumbnail_failed):
        app = self.app
        if request_id != app._load_request_id:
            return

        self._set_thumbnail(thumbnail, thumbnail_failed)
        thumbnail_note = " • Thumbnail unavailable" if thumbnail_failed else ""
        app.video_info_label.configure(
            text=(
                f"🎞  {app.loaded_title}\n"
                f"Duration: {format_seconds(app.video_duration)}\n"
                f"Ready to clip{thumbnail_note}"
            ),
            text_color="#4caf50",
        )
        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        app.set_status("Video loaded. Choose a time range and add it to the queue.", "#4caf50")

    def _apply_video_error(self, request_id, error_message):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.video_duration = None
        app.loaded_url = None
        app.loaded_title = None
        app.loaded_playlist_entries = None
        self._set_thumbnail(None, False)
        self._hide_load_progress()
        app.video_info_label.configure(
            text=f"❌ Couldn't load video: {error_message}",
            text_color="#e05252",
        )
        app.load_btn.configure(state="normal", text="Load Video")
        app.set_status("Video information could not be loaded.", "#e05252")

    def _set_thumbnail(self, image, failed):
        app = self.app
        if image is None:
            app._thumbnail_image = None
            app.thumbnail_label.configure(image=None, text="⚠" if failed else "")
            return

        ctk_image = ctk.CTkImage(
            light_image=image,
            dark_image=image,
            size=image.size,
        )
        app._thumbnail_image = ctk_image
        app.thumbnail_label.configure(image=ctk_image, text="")