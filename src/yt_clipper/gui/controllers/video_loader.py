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
from yt_clipper.core.utils import (
    format_seconds,
    sanitize_filename,
    sort_videos_by_publish_date,
    filter_available_videos,
    detect_youtube_url_type,
)

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

        # Detect URL type for initial preview switching (heuristic before extraction)
        url_type = detect_youtube_url_type(url)
        try:
            if url_type == "playlist":
                # Show playlist preview loading, hide single preview
                try:
                    app.show_playlist_preview()
                except Exception:
                    pass
                app.playlist_preview.show_loading("Connecting to YouTube... Reading playlist details...")
                app.video_info_label.configure(
                    text="Connecting to YouTube...\nReading playlist details...",
                    text_color="#4da6ff",
                )
            else:
                # Default to single preview for video URLs and unknown
                try:
                    app.show_single_preview()
                except Exception:
                    pass
                app.video_info_label.configure(
                    text="Connecting to YouTube...\nReading video details; this may take a few seconds.",
                    text_color="#4da6ff",
                )
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
                # Filter unavailable before passing to preview (task requirement)
                # Downloader already filters, but filter again for safety and to handle
                # any edge cases where raw entries might still contain unavailable
                try:
                    available = filter_available_videos(entries)
                except Exception:
                    available = entries

                app._post_ui_event(
                    "playlist_metadata",
                    request_id,
                    url,
                    result.get("playlist_title") or "Playlist",
                    available,
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
            # Pass full entry so publish_date and thumbnail are available
            app._post_ui_event(
                "video_metadata",
                request_id,
                url,
                title,
                float(duration),
                entry,
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
        try:
            anchor = app._get_current_preview_anchor()
        except Exception:
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
            # Show retry in whichever preview is currently visible
            if hasattr(app, 'playlist_preview') and app.playlist_preview.winfo_manager():
                app.playlist_preview.show_loading(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) in {delay:.0f}s...\n{error_text}"
                )
            else:
                app.playlist_preview.show_loading(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) in {delay:.0f}s..."
                )
        except Exception:
            pass
        app.set_status(f"Retrying video load ({attempt}/{max_attempts})...", "#e6a817")

    def _apply_playlist_metadata(self, request_id, url, playlist_title, entries):
        app = self.app
        if request_id != app._load_request_id:
            return

        # Filter unavailable videos (task requirement) - before sorting and before widget
        try:
            available_entries = filter_available_videos(entries)
        except Exception:
            available_entries = list(entries)

        # Sort oldest→newest by publish date, missing dates at end
        try:
            sorted_entries = sort_videos_by_publish_date(available_entries)
        except Exception:
            sorted_entries = list(available_entries)

        app.loaded_url = url
        app.loaded_title = playlist_title
        app.loaded_playlist_entries = sorted_entries
        app.video_duration = None

        self._set_thumbnail(None, False)
        app.set_time_range_visible(False)
        self._apply_suggested_filename(playlist_title)

        # Switch to playlist preview, hide single preview
        try:
            app.show_playlist_preview()
        except Exception:
            pass

        # Legacy label (now hidden when playlist visible, but keep for compat)
        try:
            if not sorted_entries:
                app.video_info_label.configure(
                    text=(
                        f"📃  {playlist_title}\n"
                        f"No available videos in playlist\n"
                        f"All videos are unavailable/private/deleted"
                    ),
                    text_color="#e6a817",
                )
            else:
                app.video_info_label.configure(
                    text=(
                        f"📃  {playlist_title}\n"
                        f"{len(sorted_entries)} available videos (filtered, sorted oldest→newest)\n"
                        f"Each video will be downloaded in full"
                    ),
                    text_color="#4caf50",
                )
        except Exception:
            pass

        # Playlist preview widget - displays filtered & sorted videos
        try:
            if not sorted_entries:
                app.playlist_preview.clear()
                app.playlist_preview._show_empty_state(
                    f"📃 {playlist_title}\nNo available videos — all are private/deleted/unavailable"
                )
            else:
                app.playlist_preview.set_videos(sorted_entries)
        except Exception:
            pass

        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        if not sorted_entries:
            app.set_status(
                f"Playlist loaded but no available videos (all unavailable)",
                "#e6a817",
            )
        else:
            app.set_status(
                f"Playlist loaded ({len(sorted_entries)} available, sorted oldest→newest). Click Download to queue them all.",
                "#4caf50",
            )

    def _apply_playlist_thumbnail(self, request_id, thumbnail, thumbnail_failed):
        app = self.app
        if request_id != app._load_request_id:
            return

        # Legacy thumbnail handling (now hidden when playlist preview visible)
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
        # New UI: playlist_preview handles its own thumbnails per-video

    def _apply_video_metadata(self, request_id, url, title, duration, entry=None):
        app = self.app
        if request_id != app._load_request_id:
            return

        app.video_duration = duration
        app.loaded_url = url
        app.loaded_title = title
        app.loaded_playlist_entries = None
        app.set_time_range_visible(True)
        self._apply_suggested_filename(title)

        app.start_slider.configure(to=duration, state="normal")
        app.end_slider.configure(to=duration, state="normal")
        app.start_slider.set(0)
        app.end_slider.set(duration)
        app.start_input.set_seconds(0)
        app.end_input.set_seconds(duration)

        # Switch to single-video preview, hide playlist preview
        try:
            app.show_single_preview()
        except Exception:
            pass

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

        # Clear playlist preview to avoid stale data when switching from playlist→video
        try:
            app.playlist_preview.clear()
        except Exception:
            pass

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
        app.set_time_range_visible(True)

        # Show error in single preview (default) and clear playlist preview
        try:
            app.show_single_preview()
        except Exception:
            pass

        try:
            app.video_info_label.configure(
                text=f"❌ Couldn't load video: {error_message}",
                text_color="#e05252",
            )
        except Exception:
            pass
        try:
            app.playlist_preview.clear()
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
            app._thumbnail_image = None
