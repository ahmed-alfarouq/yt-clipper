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
except ImportError:
    # Pillow is optional: without it every thumbnail reports failed=True and the
    # UI shows its placeholder. Loading and clipping are unaffected (§11).
    Image = None

from yt_clipper.core import downloader
from yt_clipper.core.errors import DownloadCancelled
from yt_clipper.core.log import (
    describe_failure,
    get_logger,
    redact_secrets,
    safe_message,
)
from yt_clipper.core.utils import format_seconds, sanitize_filename

logger = get_logger(__name__)

# Playlist utilities live in their own focused module - the canonical home of
# the availability decision (Phase 4A). It needs only the standard library and
# core.log, so this import cannot fail in a supported installation.
from yt_clipper.core.playlist_utils import (
    sort_videos_by_publish_date,
    filter_available_videos,
    detect_youtube_url_type,
)

THUMBNAIL_SIZE = (120, 68)


class VideoLoaderController:
    """Handles fetching video metadata/thumbnail and updating related widgets.

    Critical flow for playlists (must never let raw entries reach UI/download):
      YouTube URL
          ↓
      downloader.expand_playlist()  -> the canonical availability decision,
                                       applied at the extraction boundary
          ↓
      validated entries
          ↓
      _apply_playlist_metadata()    -> filter_available_videos() applied once, at
                                       the shared-state boundary (fail-open, §4)
          ↓
      sort_videos_by_publish_date()
          ↓
      shared state: loaded_playlist_entries
          ↓
      Preview + Download (same validated dataset)
    """

    def __init__(self, app):
        self.app = app

    def start_load_video(self):
        app = self.app
        url = app.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return

        app._load_request_id += 1
        request_id = app._load_request_id
        app.load_btn.configure(state="disabled", text="Loading...")
        try:
            app.set_download_enabled(False)
        except Exception as exc:
            logger.debug("Download button could not be disabled while loading: %s", describe_failure(exc))

        url_type = detect_youtube_url_type(url)
        try:
            if url_type == "playlist":
                try:
                    app.show_playlist_preview()
                except Exception as exc:
                    logger.debug("Playlist preview could not be shown before loading: %s", describe_failure(exc))
                app.playlist_preview.show_loading("Connecting to YouTube... Reading playlist details...")
                app.video_info_label.configure(
                    text="Connecting to YouTube...\nReading playlist details...",
                    text_color="#4da6ff",
                )
            else:
                try:
                    app.show_single_preview()
                except Exception as exc:
                    logger.debug("Single-video preview could not be shown before loading: %s", describe_failure(exc))
                app.video_info_label.configure(
                    text="Connecting to YouTube...\nReading video details; this may take a few seconds.",
                    text_color="#4da6ff",
                )
                app.playlist_preview.show_loading("Connecting to YouTube... Reading video details...")
        except Exception as exc:
            logger.debug("Loading placeholder could not be displayed: %s", describe_failure(exc))

        app.set_status("Loading video information...", "#4da6ff")
        self._show_load_progress()

        threading.Thread(
            target=self._load_video_worker,
            args=(request_id, url),
            daemon=True,
        ).start()

    def _load_video_worker(self, request_id, url):
        """Background half of "Load Video".

        Terminal outcomes (§2, §16): a resolved video/playlist posts metadata,
        an empty-but-valid playlist posts metadata with no entries, a failure
        posts `video_error` with a human-readable message, and a cancellation
        posts `video_load_cancelled` - never an error (§3).
        """
        app = self.app
        try:
            def _notify_retry(attempt, max_attempts, delay, exc):
                app._post_ui_event(
                    "video_retry", request_id, attempt, max_attempts, delay,
                    safe_message(exc),
                )

            result = downloader.expand_playlist(url, on_retry=_notify_retry)

            if result["is_playlist"]:
                # The downloader applied the canonical decision at the extraction
                # boundary, and _apply_playlist_metadata applies it at the
                # shared-state boundary it guards - the one that feeds both the
                # preview and the queue. Filtering here too would run the same
                # list through the same decision twice with nothing in between
                # that could change the answer.
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
                entry,
            )

            try:
                thumbnail, thumbnail_failed = self._fetch_thumbnail(
                    entry.get("thumbnail")
                )
            except Exception as thumb_exc:
                # §11: the video itself loaded successfully, so an unexpected
                # thumbnail problem must not be reported as a failed load. The
                # UI still learns the thumbnail is missing (failed=True).
                logger.warning(
                    "Thumbnail step failed after a successful load of %s; "
                    "continuing without a thumbnail: %s",
                    redact_secrets(url), describe_failure(thumb_exc),
                )
                thumbnail, thumbnail_failed = None, True
            app._post_ui_event(
                "video_thumbnail",
                request_id,
                thumbnail,
                thumbnail_failed,
            )
        except DownloadCancelled:
            # Cancellation is its own outcome: reported as cancelled, never as a
            # failure, and no further work is started for this request (§3).
            logger.info("Video load cancelled: %s", redact_secrets(url))
            app._post_ui_event("video_load_cancelled", request_id)
        except Exception as exc:
            # The load failed: one ERROR record for diagnosis, and a redacted,
            # traceback-free message for the user (§16, §18).
            logger.error("Video load failed for %s: %s",
                         redact_secrets(url), describe_failure(exc))
            app._post_ui_event("video_error", request_id, safe_message(exc))

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
        except Exception as exc:
            # §11: a thumbnail is decorative. The failure is recoverable and
            # non-fatal - the caller reports "Thumbnail unavailable" in the UI,
            # so the log only needs the detail a developer would want.
            logger.debug("Thumbnail could not be fetched or decoded: %s",
                         describe_failure(exc))
            return None, True

    def _show_load_progress(self):
        app = self.app
        try:
            anchor = app._get_current_preview_anchor()
        except Exception as exc:
            logger.debug("Progress bar anchor could not be resolved; using the fallback widget: %s", describe_failure(exc))
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

    def _apply_video_retry(self, request_id, attempt, max_attempts, delay, error_text):
        app = self.app
        if request_id != app._load_request_id:
            return
        try:
            app.set_download_enabled(False)
        except Exception as exc:
            logger.debug("Download button could not be disabled while retrying: %s", describe_failure(exc))
        try:
            app.video_info_label.configure(
                text=(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) "
                    f"in {delay:.0f}s...\n{error_text}"
                ),
                text_color="#e6a817",
            )
        except Exception as exc:
            logger.debug("Retry notice could not be shown in the video info label: %s", describe_failure(exc))
        try:
            if hasattr(app, 'playlist_preview') and app.playlist_preview.winfo_manager():
                app.playlist_preview.show_loading(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) in {delay:.0f}s...\n{error_text}"
                )
            else:
                app.playlist_preview.show_loading(
                    f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) in {delay:.0f}s..."
                )
        except Exception as exc:
            logger.debug("Retry notice could not be shown in the playlist preview: %s", describe_failure(exc))
        app.set_status(f"Retrying video load ({attempt}/{max_attempts})...", "#e6a817")

    def _apply_playlist_metadata(self, request_id, url, playlist_title, entries):
        """Apply playlist metadata – filtering MUST happen before sorting and state.

        Flow:
          validated entries (the canonical decision was applied at the
          extraction boundary)
              ↓
          filter_available_videos() - this boundary's single application, before
          sorting and before shared state (fail-open, §4)
              ↓
          sort_videos_by_publish_date()
              ↓
          shared state loaded_playlist_entries (validated)
              ↓
          Preview + Download same dataset
        """
        app = self.app
        if request_id != app._load_request_id:
            return

        # CRITICAL: Filter at shared data boundary before sorting and before storing
        try:
            available_entries = filter_available_videos(entries)
        except Exception as exc:
            # Fail open (§4): the downloader already validated these entries, so
            # a broken re-filter must not empty the user's playlist.
            logger.warning(
                "Playlist filter failed while applying metadata; keeping all %d "
                "entries: %s", len(entries), describe_failure(exc),
            )
            available_entries = list(entries)

        # Sort oldest→newest, missing dates at end (missing date != unavailable)
        try:
            sorted_entries = sort_videos_by_publish_date(available_entries)
        except Exception as exc:
            # Sorting is presentation-only, so the playlist is kept in the
            # downloader's order; the broken promise ("oldest first") is logged
            # because the UI text still claims the ordering (§12, §21).
            logger.warning(
                "Playlist could not be sorted by publish date; keeping the "
                "original order of %d entries: %s",
                len(available_entries), describe_failure(exc),
            )
            sorted_entries = list(available_entries)

        # Shared application state – validated dataset used by both preview and download
        app.loaded_url = url
        app.loaded_title = playlist_title
        app.loaded_playlist_entries = sorted_entries
        app.video_duration = None

        self._set_thumbnail(None, False)
        app.set_time_range_visible(False)
        self._apply_suggested_filename(playlist_title)

        try:
            app.show_playlist_preview()
        except Exception as exc:
            logger.debug("Playlist preview could not be shown for the loaded playlist: %s", describe_failure(exc))

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
        except Exception as exc:
            logger.debug("Playlist summary could not be shown in the video info label: %s", describe_failure(exc))

        # Playlist preview receives VALIDATED data, no own availability logic
        try:
            if not sorted_entries:
                app.playlist_preview.clear()
                app.playlist_preview._show_empty_state(
                    f"📃 {playlist_title}\nNo available videos — all are private/deleted/unavailable"
                )
            else:
                app.playlist_preview.set_videos(sorted_entries)
        except Exception as exc:
            logger.debug("Playlist preview rows could not be rendered: %s", describe_failure(exc))

        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        try:
            app.update_download_button_state()
        except Exception as exc:
            logger.debug("Download button state could not be refreshed after loading a playlist: %s", describe_failure(exc))
            try:
                app.set_download_enabled(bool(sorted_entries))
            except Exception as exc:
                logger.debug("Download button could not be re-enabled from the fallback path: %s", describe_failure(exc))

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
        except Exception as exc:
            logger.debug("Playlist thumbnail note could not be shown in the video info label: %s", describe_failure(exc))

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

        try:
            app.show_single_preview()
        except Exception as exc:
            logger.debug("Single-video preview could not be shown for the loaded video: %s", describe_failure(exc))

        try:
            app.video_info_label.configure(
                text=(
                    f"🎞  {title}\n"
                    f"Duration: {format_seconds(duration)}\n"
                    "Video details loaded • Fetching thumbnail..."
                ),
                text_color="#4da6ff",
            )
        except Exception as exc:
            logger.debug("Video details could not be shown in the video info label: %s", describe_failure(exc))
        app.update_clip_length()

        try:
            app.playlist_preview.clear()
        except Exception as exc:
            logger.debug("Playlist preview could not be cleared for a single video: %s", describe_failure(exc))

        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        try:
            app.update_download_button_state()
        except Exception as exc:
            logger.debug("Download button state could not be refreshed after loading a video: %s", describe_failure(exc))
            try:
                app.set_download_enabled(True)
            except Exception as exc:
                logger.debug("Download button could not be re-enabled from the fallback path: %s", describe_failure(exc))
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
        except Exception as exc:
            logger.debug("Video details could not be refreshed after the thumbnail step: %s", describe_failure(exc))

        app.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        try:
            app.update_download_button_state()
        except Exception as exc:
            logger.debug("Download button state could not be refreshed after the thumbnail step: %s", describe_failure(exc))
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

        try:
            app.show_single_preview()
        except Exception as exc:
            logger.debug("Single-video preview could not be shown for a failed load: %s", describe_failure(exc))

        try:
            app.video_info_label.configure(
                text=f"❌ Couldn't load video: {error_message}",
                text_color="#e05252",
            )
        except Exception as exc:
            logger.debug("Failure message could not be shown in the video info label: %s", describe_failure(exc))
        try:
            app.playlist_preview.clear()
        except Exception as exc:
            logger.debug("Playlist preview could not be cleared after a failed load: %s", describe_failure(exc))

        app.load_btn.configure(state="normal", text="Load Video")
        try:
            app.set_download_enabled(False)
        except Exception as exc:
            logger.debug("Download button could not be disabled after a failed load: %s", describe_failure(exc))
        app.set_status("Video information could not be loaded.", "#e05252")

    def _apply_video_load_cancelled(self, request_id):
        """Restore the UI after a cancelled video load.

        §3/§16: cancellation is its own terminal state. The UI shows "Cancelled"
        in the neutral grey used elsewhere for cancels - never the red failure
        styling and never an error message - and no further work is started for
        the cancelled request.
        """
        app = self.app
        if request_id != app._load_request_id:
            # A newer load already replaced this one; touching the UI now would
            # report the old request's outcome over the new one.
            logger.debug("Ignoring cancellation of stale load request %s", request_id)
            return

        app.video_duration = None
        app.loaded_url = None
        app.loaded_title = None
        app.loaded_playlist_entries = None
        self._set_thumbnail(None, False)
        self._hide_load_progress()

        try:
            app.video_info_label.configure(
                text="⏹ Loading cancelled",
                text_color="gray",
            )
        except Exception as exc:
            logger.debug("Cancellation notice could not be shown in the video info "
                         "label: %s", describe_failure(exc))
        try:
            app.playlist_preview.clear()
        except Exception as exc:
            logger.debug("Playlist preview could not be cleared after a cancelled "
                         "load: %s", describe_failure(exc))

        app.load_btn.configure(state="normal", text="Load Video")
        try:
            app.set_download_enabled(False)
        except Exception as exc:
            logger.debug("Download button could not be disabled after a cancelled "
                         "load: %s", describe_failure(exc))
        app.set_status("Loading cancelled.", "gray")

    def _set_thumbnail(self, image, failed):
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
        except Exception as exc:
            logger.debug("Thumbnail could not be displayed; clearing the previous image: %s", describe_failure(exc))
            app._thumbnail_image = None
