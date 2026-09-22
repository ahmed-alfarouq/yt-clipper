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
        # Legacy label (hidden) kept for backward compat
        try:
            app.video_info_label.configure(
                text="Connecting to YouTube...\nReading video details; this may take a few seconds.",
                text_color="#4da6ff",
            )
        except Exception:
            pass
        # New playlist preview shows loading state
        try:
            app.playlist_preview.show_loading("Connecting to YouTube... Reading video details...")
        except Exception:
            pass
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
                entries = result["entries"]
                app._post_ui_event(
                    "playlist_metadata",
                    request_id,
                    url,
                    result.get("playlist_title") or "Playlist",
                    entries,
                )
                # Thumbnail fetching is now handled by PlaylistPreviewWidget itself
                # for each video, with caching. No need to fetch first thumbnail here.
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
            # Pass full entry so publish_date and thumbnail are available for playlist preview
            app._post_ui_event(
                "video_metadata",
                request_id,
                url,
                title,
                float(duration),
                entry,
            )

            # Legacy thumbnail fetch kept for backward compat, but playlist_preview
            # will also fetch its own thumbnail with caching
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
        anchor = getattr(app, 'playlist_preview', None) or app.info_row
        if not app.load_progress.winfo_manager():
            app.load_progress.pack(
                fill="x",
                padx=20,
                pady=(0, 12),
                after=anchor,
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
        try:
            app.video_info_label.configure(
                text=(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) "
                    f"in {delay:.0f}s...\n{error_text}"
                ),
                text_color="#e6a817",
            )
        except Exception:
            pass
        try:
            app.playlist_preview.show_loading(
                f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) in {delay:.0f}s...\n{error_text}"
            )
        except Exception:
            pass
        app.set_status(f"Retrying video load ({attempt}/{max_attempts})...", "#e6a817")

    def _apply_playlist_metadata(self, request_id, url, playlist_title, entries):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.loaded_url = url
        app.loaded_title = playlist_title
        # Sort oldest→newest by publish date, missing dates at end
        try:
            from yt_clipper.core.utils import sort_videos_by_publish_date
            sorted_entries = sort_videos_by_publish_date(entries)
        except Exception:
            sorted_entries = list(entries)

        app.loaded_playlist_entries = sorted_entries
        app.video_duration = None  # no single duration; every video in the
                                    # playlist is downloaded in full instead

        self._set_thumbnail(None, False)
        # Start/End Time have no meaning for a full-playlist download, so
        # they're hidden entirely rather than disabled - there's nothing
        # for the user to set here.
        app.set_time_range_visible(False)

        self._apply_suggested_filename(playlist_title)

        # Legacy label kept for backward compat (hidden)
        try:
            app.video_info_label.configure(
                text=(
                    f"📃  {playlist_title}\n"
                    f"{len(sorted_entries)} videos in playlist\n"
                    "Each video will be downloaded in full • Sorted oldest→newest"
                ),
                text_color="#4caf50",
            )
        except Exception:
            pass

        # New playlist preview widget - displays all videos sorted
        try:
            app.playlist_preview.set_videos(sorted_entries)
        except Exception:
            pass

        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        app.set_status(
            f"Playlist loaded ({len(sorted_entries)} videos, sorted oldest→newest). Click Download to queue them all.",
            "#4caf50",
        )

    def _apply_playlist_thumbnail(self, request_id, thumbnail, thumbnail_failed):
        app = self.app
        if request_id != app._load_request_id:
            return

        # Legacy thumbnail handling (hidden widget)
        self._set_thumbnail(thumbnail, thumbnail_failed)
        entries = app.loaded_playlist_entries or []
        thumbnail_note = (
            " • Preview thumbnail unavailable" if thumbnail_failed
            else " • Showing the first video's thumbnail"
        )
        try:
            app.video_info_label.configure(
                text=(
                    f"📃  {app.loaded_title}\n"
                    f"{len(entries)} videos in playlist\n"
                    f"Each video will be downloaded in full{thumbnail_note}"
                ),
                text_color="#4caf50",
            )
        except Exception:
            pass
        # New UI: playlist_preview already handles its own thumbnails per-video,
        # so we don't need to update it here. The first thumbnail event is now
        # essentially a no-op for the new preview.

    def _apply_video_metadata(self, request_id, url, title, duration, entry=None):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.video_duration = duration
        app.loaded_url = url
        app.loaded_title = title
        app.loaded_playlist_entries = None
        # A single video always uses a time range, even if the previous
        # load was a playlist that hid these fields.
        app.set_time_range_visible(True)
        self._apply_suggested_filename(title)

        app.start_slider.configure(to=duration, state="normal")
        app.end_slider.configure(to=duration, state="normal")
        app.start_slider.set(0)
        app.end_slider.set(duration)
        app.start_input.set_seconds(0)
        app.end_input.set_seconds(duration)
        try:
            app.video_info_label.configure(
                text=(
                    f"🎞  {title}\n"
                    f"Duration: {format_seconds(duration)}\n"
                    "Video details loaded • Fetching thumbnail..."
                ),
                text_color="#4da6ff",
            )
        except Exception:
            pass
        app.update_clip_length()

        # New playlist preview: show single video as a one-item playlist
        # This replaces the old single-video preview per task requirements
        try:
            if entry is None:
                entry = {
                    "url": url,
                    "title": title,
                    "duration": duration,
                    "thumbnail": None,
                    "publish_date": None,
                }
            # Ensure entry has at least url and title
            if not entry.get("url"):
                entry["url"] = url
            if not entry.get("title"):
                entry["title"] = title
            app.playlist_preview.set_videos([entry])
        except Exception:
            pass

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
        try:
            app.video_info_label.configure(
                text=(
                    f"🎞  {app.loaded_title}\n"
                    f"Duration: {format_seconds(app.video_duration)}\n"
                    f"Ready to clip{thumbnail_note}"
                ),
                text_color="#4caf50",
            )
        except Exception:
            pass
        # New UI: playlist_preview already shows the video; its thumbnail
        # fetching is handled internally. We keep legacy thumbnail label update
        # for backward compat but don't need to refresh playlist_preview here
        # because it already has the thumbnail URL and will fetch it itself.
        # However, if the legacy fetch succeeded, we can try to update cache.
        try:
            if thumbnail is not None and app.loaded_url:
                # The widget's cache is keyed by thumbnail URL, not by image,
                # so we can't directly inject the PIL image here. The widget
                # will fetch its own thumbnail. This is intentional to avoid
                # coupling.
                pass
        except Exception:
            pass
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
        # A failed load might follow a previously loaded playlist that hid
        # these fields - restore them so the UI isn't left looking broken.
        app.set_time_range_visible(True)
        try:
            app.video_info_label.configure(
                text=f"❌ Couldn't load video: {error_message}",
                text_color="#e05252",
            )
        except Exception:
            pass
        try:
            app.playlist_preview.clear()
            app.playlist_preview._show_empty_state(f"❌ Couldn't load video: {error_message}")
        except Exception:
            pass
        app.load_btn.configure(state="normal", text="Load Video")
        app.set_status("Video information could not be loaded.", "#e05252")

    def _set_thumbnail(self, image, failed):
        """Update the thumbnail widget, or clear it back to empty/warning.

        Order matters here. CTkImage's underlying Tk PhotoImage objects are
        only kept alive by the Python reference in app._thumbnail_image. If
        that reference is dropped (set to None) BEFORE the widget is told to
        stop using it, CPython's refcounting GC destroys the PhotoImage
        immediately, which deletes the underlying Tk image by name (e.g.
        "pyimage1") - and the *next* widget redraw then fails with
        `_tkinter.TclError: image "pyimageN" does not exist`, since the
        widget's C-level config still points at that now-deleted name.
        Reconfiguring the widget first, then releasing the Python reference,
        avoids that race entirely.
        """
        app = self.app
        try:
            if image is None:
                app.thumbnail_label.configure(image=None, text="⚠" if failed else "")
                app._thumbnail_image = None
                return

            ctk_image = ctk.CTkImage(
                light_image=image,
                dark_image=image,
                size=image.size,
            )
            app.thumbnail_label.configure(image=ctk_image, text="")
            app._thumbnail_image = ctk_image
        except Exception:
            # The thumbnail is a nice-to-have. A Tk/CTk quirk here must never
            # abort the caller mid-way through applying video/playlist
            # metadata - that would leave the Load button and progress bar
            # stuck forever, which is worse than a missing thumbnail.
            app._thumbnail_image = None