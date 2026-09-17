import io
import itertools
import math
import os
import queue
import re
import threading
from turtle import title
import urllib.request
import webbrowser
from pathlib import Path
import subprocess
import sys
import customtkinter as ctk
from tkinter import filedialog, messagebox

try:
    from PIL import Image
except ImportError:  # The rest of the application can still run without thumbnails.
    Image = None

from yt_clipper.core import config as app_config
from yt_clipper.core import downloader, updater
from yt_clipper.core.utils import format_seconds, sanitize_filename
from yt_clipper.gui.models import DownloadJob
from yt_clipper.gui.services import SequentialDownloadQueue
from yt_clipper.gui.widgets.time_input import TimeInput


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CURRENT_VERSION = "1.1.0"
UPDATE_OWNER = "ahmed-alfarouq"
UPDATE_REPO = "yt-clipper"

UI_POLL_INTERVAL_MS = 50
THUMBNAIL_SIZE = (120, 68)


class ClipperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Clipper")
        self.update_idletasks()

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w, win_h = 600, min(800, max(500, screen_h - 80))
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.minsize(480, 500)
        self.resizable(True, True)

        self.video_duration = None
        self.loaded_url = None
        self.loaded_title = None
        self._syncing = False
        self._closing = False
        self._load_request_id = 0
        self._thumbnail_image = None

        self.app_config = app_config.load_config()
        if not isinstance(self.app_config, dict):
            self.app_config = {}

        self.queue_jobs = []
        self._queue_status_labels = {}
        self._job_id_counter = itertools.count(1)
        self._active_job_id = None

        # Workers only place plain data in this queue. Tk widgets are updated
        # exclusively by _poll_ui_events on the main thread.
        self._ui_events = queue.Queue()
        self._shutdown_event = threading.Event()
        self.download_queue = SequentialDownloadQueue(self._post_ui_event)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Create the optional update area without packing it. An empty CTkFrame
        # has a non-trivial default requested height, which otherwise appears
        # as a large blank area above the title.
        self.banner_container = ctk.CTkFrame(self, fg_color="transparent")

        self.header_label = ctk.CTkLabel(
            self,
            text="🎬 YouTube Clipper",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        self.header_label.pack(pady=(10, 5))
        ctk.CTkLabel(
            self,
            text="Download a high-quality clip from any YouTube video",
            font=ctk.CTkFont(size=13),
            text_color="gray",
        ).pack(pady=(0, 10))

        card = ctk.CTkScrollableFrame(self, corner_radius=16)
        card.pack(padx=25, pady=(0, 20), fill="both", expand=True)

        self.bind(
            "<F11>",
            lambda _event: self.attributes(
                "-fullscreen", not bool(self.attributes("-fullscreen"))
            ),
        )
        self.bind("<Escape>", lambda _event: self.attributes("-fullscreen", False))

        # URL and metadata.
        ctk.CTkLabel(card, text="Video URL", anchor="w").pack(
            fill="x", padx=20, pady=(20, 5)
        )
        url_row = ctk.CTkFrame(card, fg_color="transparent")
        url_row.pack(fill="x", padx=20)
        self.url_entry = ctk.CTkEntry(
            url_row,
            placeholder_text="https://youtube.com/watch?v=...",
            height=40,
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.load_btn = ctk.CTkButton(
            url_row,
            text="Load Video",
            width=110,
            height=40,
            command=self.start_load_video,
        )
        self.load_btn.pack(side="left")

        self.info_row = ctk.CTkFrame(card, fg_color="transparent")
        self.info_row.pack(fill="x", padx=20, pady=(8, 8))
        self.thumbnail_label = ctk.CTkLabel(
            self.info_row,
            text="",
            width=THUMBNAIL_SIZE[0],
            height=THUMBNAIL_SIZE[1],
            fg_color="gray20",
            corner_radius=6,
        )
        self.thumbnail_label.pack(side="left", padx=(0, 12))
        self.video_info_label = ctk.CTkLabel(
            self.info_row,
            text="No video loaded yet",
            text_color="gray",
            anchor="w",
            justify="left",
            wraplength=380,
        )
        self.video_info_label.pack(side="left", fill="x", expand=True)

        self.load_progress = ctk.CTkProgressBar(
            card,
            height=5,
            mode="indeterminate",
        )

        # Start time.
        ctk.CTkLabel(card, text="Start Time", anchor="w").pack(fill="x", padx=20)
        self.start_input = TimeInput(card, on_change=self.on_start_change)
        self.start_input.pack(padx=20, pady=(5, 5))
        self.start_slider = ctk.CTkSlider(
            card,
            from_=0,
            to=100,
            state="disabled",
            command=self.on_start_slide,
        )
        self.start_slider.set(0)
        self.start_slider.pack(fill="x", padx=20, pady=(0, 15))

        # End time.
        ctk.CTkLabel(card, text="End Time", anchor="w").pack(fill="x", padx=20)
        self.end_input = TimeInput(card, on_change=self.on_end_change)
        self.end_input.pack(padx=20, pady=(5, 5))
        self.end_slider = ctk.CTkSlider(
            card,
            from_=0,
            to=100,
            state="disabled",
            command=self.on_end_slide,
        )
        self.end_slider.set(100)
        self.end_slider.pack(fill="x", padx=20, pady=(0, 5))

        self.clip_length_label = ctk.CTkLabel(
            card,
            text="Clip length: —",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#4da6ff",
        )
        self.clip_length_label.pack(pady=(5, 15))

        # Quality and audio-only mode.
        quality_row = ctk.CTkFrame(card, fg_color="transparent")
        quality_row.pack(fill="x", padx=20)
        self.quality_var = ctk.StringVar(value="best")
        self.quality_menu = ctk.CTkOptionMenu(
            quality_row,
            variable=self.quality_var,
            values=["best", "4k", "1080p", "720p"],
            height=40,
        )
        self.quality_menu.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.audio_only_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            quality_row,
            text="🎵 Audio only (MP3)",
            variable=self.audio_only_var,
            command=self.on_audio_toggle,
        ).pack(side="left")

        # Output location.
        ctk.CTkLabel(card, text="Save To", anchor="w").pack(
            fill="x", padx=20, pady=(20, 5)
        )
        save_row = ctk.CTkFrame(card, fg_color="transparent")
        save_row.pack(fill="x", padx=20)
        self.output_entry = ctk.CTkEntry(save_row, height=40)
        self.output_entry.insert(0, str(self._default_output_path()))
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(
            save_row,
            text="Browse",
            width=90,
            height=40,
            command=self.browse_output,
            fg_color="gray30",
            hover_color="gray20",
        ).pack(side="left")

        self.open_folder_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            card,
            text="📂 Open folder after download",
            variable=self.open_folder_var,
        ).pack(anchor="w", padx=20, pady=(8, 0))
        
        download_row = ctk.CTkFrame(card, fg_color="transparent")
        download_row.pack(fill="x", padx=20, pady=(25, 10))
        self.download_button = ctk.CTkButton(
            download_row,
            text="⬇ Download Clip",
            height=44,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.download_clip,
            fg_color="#2e7d32",
            hover_color="#1b5e20",
        )
        self.download_button.pack(fill="x")

        ctk.CTkLabel(
            card,
            text="Downloads",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(fill="x", padx=20, pady=(10, 5))
        self.queue_frame = ctk.CTkFrame(card, fg_color="gray14", corner_radius=10)
        self.queue_frame.pack(fill="x", padx=20, pady=(0, 15))
        self.render_queue()

        self.progress = ctk.CTkProgressBar(card, height=14)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(0, 10))

        self.status_label = ctk.CTkLabel(card, text="Ready", text_color="gray")
        self.status_label.pack(pady=(0, 20))

        self.after(UI_POLL_INTERVAL_MS, self._poll_ui_events)
        self.after(500, self.check_update_async)

    # ---------- Main-thread event handling ----------

    def _post_ui_event(self, event_name, *payload):
        if not self._shutdown_event.is_set():
            self._ui_events.put((event_name, payload))

    def _poll_ui_events(self):
        if self._closing:
            return

        while True:
            try:
                event_name, payload = self._ui_events.get_nowait()
            except queue.Empty:
                break
            self._handle_ui_event(event_name, payload)

        if not self._closing:
            self.after(UI_POLL_INTERVAL_MS, self._poll_ui_events)

    def _handle_ui_event(self, event_name, payload):
        if event_name == "video_metadata":
            self._apply_video_metadata(*payload)
        elif event_name == "video_thumbnail":
            self._apply_video_thumbnail(*payload)
        elif event_name == "video_error":
            self._apply_video_error(*payload)
        elif event_name == "job_started":
            self._apply_job_started(*payload)
        elif event_name == "download_progress":
            self._apply_download_progress(*payload)
        elif event_name == "job_done":
            self._apply_job_done(*payload)
        elif event_name == "job_error":
            self._apply_job_error(*payload)
        elif event_name == "job_cancelled":
            self._apply_job_cancelled(*payload)
        elif event_name == "queue_idle":
            self._queue_idle()
        elif event_name == "update_available":
            self._show_update_banner(*payload)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._shutdown_event.set()
        self.download_queue.shutdown()
        self.destroy()

    # ---------- Video loading ----------

    def start_load_video(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return

        self._load_request_id += 1
        request_id = self._load_request_id
        self.load_btn.configure(state="disabled", text="Loading...")
        self.video_info_label.configure(
            text="Connecting to YouTube...\nReading video details; this may take a few seconds.",
            text_color="#4da6ff",
        )
        self.set_status("Loading video information...", "#4da6ff")
        self._show_load_progress()

        threading.Thread(
            target=self._load_video_worker,
            args=(request_id, url),
            daemon=True,
        ).start()

    def _load_video_worker(self, request_id, url):
        try:
            info = downloader.get_video_info(url)
            duration = info.get("duration")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise ValueError(
                    "This video has no usable duration. Live streams are not supported."
                )

            title = str(info.get("title") or "Unknown title")
            self._post_ui_event(
                "video_metadata",
                request_id,
                url,
                title,
                float(duration),
            )

            thumbnail, thumbnail_failed = self._fetch_thumbnail(info.get("thumbnail"))
            self._post_ui_event(
                "video_thumbnail",
                request_id,
                thumbnail,
                thumbnail_failed,
            )
        except Exception as exc:
            self._post_ui_event("video_error", request_id, str(exc))

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

    def _show_load_progress(self):
        if not self.load_progress.winfo_manager():
            self.load_progress.pack(
                fill="x",
                padx=20,
                pady=(0, 12),
                after=self.info_row,
            )
        self.load_progress.start()

    def _hide_load_progress(self):
        self.load_progress.stop()
        self.load_progress.pack_forget()

    def _apply_video_metadata(self, request_id, url, title, duration):
        if request_id != self._load_request_id:
            return

        self.video_duration = duration
        self.loaded_url = url
        self.loaded_title = title
        self._apply_suggested_filename(title)
        
        self.start_slider.configure(to=duration, state="normal")
        self.end_slider.configure(to=duration, state="normal")
        self.start_slider.set(0)
        self.end_slider.set(duration)
        self.start_input.set_seconds(0)
        self.end_input.set_seconds(duration)
        self.video_info_label.configure(
            text=(
                f"🎞  {title}\n"
                f"Duration: {format_seconds(duration)}\n"
                "Video details loaded • Fetching thumbnail..."
            ),
            text_color="#4da6ff",
        )
        self.update_clip_length()

    def _apply_suggested_filename(self, title):
        current = self.output_entry.get().strip()
        directory = os.path.dirname(current) if current else ""
        if not directory:
            directory = str(self._default_output_path().parent)

        safe_name = sanitize_filename(title) or "clip"
        extension = ".mp3" if self.audio_only_var.get() else ".mp4"
        suggested = Path(directory) / f"{safe_name}{extension}"

        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(suggested))

    def _apply_video_thumbnail(self, request_id, thumbnail, thumbnail_failed):
        if request_id != self._load_request_id:
            return

        self._set_thumbnail(thumbnail, thumbnail_failed)
        thumbnail_note = " • Thumbnail unavailable" if thumbnail_failed else ""
        self.video_info_label.configure(
            text=(
                f"🎞  {self.loaded_title}\n"
                f"Duration: {format_seconds(self.video_duration)}\n"
                f"Ready to clip{thumbnail_note}"
            ),
            text_color="#4caf50",
        )
        self.load_btn.configure(state="normal", text="Load Video")
        self._hide_load_progress()
        self.set_status("Video loaded. Choose a time range and add it to the queue.", "#4caf50")

    def _apply_video_error(self, request_id, error_message):
        if request_id != self._load_request_id:
            return

        self.video_duration = None
        self.loaded_url = None
        self.loaded_title = None
        self._set_thumbnail(None, False)
        self._hide_load_progress()
        self.video_info_label.configure(
            text=f"❌ Couldn't load video: {error_message}",
            text_color="#e05252",
        )
        self.load_btn.configure(state="normal", text="Load Video")
        self.set_status("Video information could not be loaded.", "#e05252")

    def _set_thumbnail(self, image, failed):
        if image is None:
            self._thumbnail_image = None
            self.thumbnail_label.configure(image=None, text="⚠" if failed else "")
            return

        ctk_image = ctk.CTkImage(
            light_image=image,
            dark_image=image,
            size=image.size,
        )
        self._thumbnail_image = ctk_image
        self.thumbnail_label.configure(image=ctk_image, text="")

    # ---------- Output mode ----------

    def _default_output_path(self):
        configured_dir = self.app_config.get("last_output_dir")
        if not isinstance(configured_dir, str) or not configured_dir.strip():
            configured_dir = str(Path.home() / "Videos")
        return Path(configured_dir).expanduser() / "clip.mp4"

    def on_audio_toggle(self):
        audio_only = self.audio_only_var.get()
        self.quality_menu.configure(state="disabled" if audio_only else "normal")

        current = self.output_entry.get().strip()
        if current:
            updated = self._path_with_expected_extension(current, audio_only)
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, str(updated))

    @staticmethod
    def _path_with_expected_extension(path_text, audio_only):
        expected_extension = ".mp3" if audio_only else ".mp4"
        path = Path(os.path.expandvars(os.path.expanduser(path_text)))
        if path.suffix.lower() != expected_extension:
            path = path.with_suffix(expected_extension)
        return path

    def browse_output(self):
        audio_only = self.audio_only_var.get()
        extension = ".mp3" if audio_only else ".mp4"
        filetypes = (
            [("MP3 audio", "*.mp3")]
            if audio_only
            else [("MP4 video", "*.mp4")]
        )

        initial_dir = self.app_config.get("last_output_dir", str(Path.home()))
        if not isinstance(initial_dir, str) or not Path(initial_dir).is_dir():
            initial_dir = str(Path.home())

        path = filedialog.asksaveasfilename(
            defaultextension=extension,
            filetypes=filetypes,
            initialdir=initial_dir,
        )
        if not path:
            return

        normalized = self._path_with_expected_extension(path, audio_only)
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(normalized))
        self.app_config["last_output_dir"] = str(normalized.parent)
        app_config.save_config(self.app_config)

    def _open_containing_folder(self, file_path):
        folder = os.path.dirname(os.path.abspath(file_path))
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.run(["open", folder], check=False)
            else:
                subprocess.run(["xdg-open", folder], check=False)
        except Exception:
            # Opening the folder is a convenience feature; failures here
            # should never interrupt or overshadow a successful download.
            pass
    # ---------- Time synchronization ----------

    def on_start_change(self, seconds):
        if self._syncing:
            return
        self._syncing = True
        try:
            seconds = max(0, float(seconds))
            if self.video_duration is not None:
                seconds = min(seconds, self.video_duration)
            self.start_slider.set(seconds)
            if seconds > self.end_slider.get():
                self.end_slider.set(seconds)
                self.end_input.set_seconds(seconds)
        finally:
            self._syncing = False
        self.update_clip_length()

    def on_end_change(self, seconds):
        if self._syncing:
            return
        self._syncing = True
        try:
            seconds = max(0, float(seconds))
            if self.video_duration is not None:
                seconds = min(seconds, self.video_duration)
            self.end_slider.set(seconds)
            if seconds < self.start_slider.get():
                self.start_slider.set(seconds)
                self.start_input.set_seconds(seconds)
        finally:
            self._syncing = False
        self.update_clip_length()

    def on_start_slide(self, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.start_input.set_seconds(value)
            if value > self.end_slider.get():
                self.end_slider.set(value)
                self.end_input.set_seconds(value)
        finally:
            self._syncing = False
        self.update_clip_length()

    def on_end_slide(self, value):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.end_input.set_seconds(value)
            if value < self.start_slider.get():
                self.start_slider.set(value)
                self.start_input.set_seconds(value)
        finally:
            self._syncing = False
        self.update_clip_length()

    def update_clip_length(self):
        length = self.end_slider.get() - self.start_slider.get()
        self.clip_length_label.configure(
            text=f"Clip length: {format_seconds(max(0, length))}"
        )

    # ---------- Queue ----------

    def set_status(self, text, color="gray"):
        self.status_label.configure(text=text, text_color=color)

    def download_clip(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return

        start_sec = self.start_input.get_seconds()
        end_sec = self.end_input.get_seconds()
        if end_sec <= start_sec:
            messagebox.showerror(
                "Invalid range", "End time must be after start time."
            )
            return

        if self.loaded_url == url and self.video_duration is not None:
            if end_sec > self.video_duration:
                messagebox.showerror(
                    "Invalid range",
                    "End time cannot be later than the loaded video's duration.",
                )
                return
            label = self.loaded_title or url
        else:
            # A manually entered URL remains supported, but metadata from a
            # previously loaded URL is never reused for it.
            label = url

        audio_only = self.audio_only_var.get()
        raw_output = self.output_entry.get().strip()
        if not raw_output:
            raw_output = str(self._default_output_path())

        requested_path = self._path_with_expected_extension(raw_output, audio_only)
        try:
            requested_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Invalid output folder",
                f"The output folder could not be created:\n{exc}",
            )
            return

        output_path = self._unique_output_path(requested_path)
        job = DownloadJob(
            id=next(self._job_id_counter),
            url=url,
            label=label,
            start_sec=start_sec,
            end_sec=end_sec,
            quality=self.quality_var.get(),
            audio_only=audio_only,
            output_path=str(output_path),
        )
        self.queue_jobs.append(job)
        queued_behind_another = self.download_queue.enqueue(job)
        job.status = "Waiting" if queued_behind_another else "Queued"
        self.render_queue()
        if queued_behind_another:
            waiting_count = sum(
                queued_job.status in ("Queued", "Waiting")
                for queued_job in self.queue_jobs
            )
            self.set_status(
                f"Queued: {output_path.name} • {waiting_count} waiting",
                "#4da6ff",
            )
        else:
            self.progress.set(0)
            self.set_status(f"Starting download: {output_path.name}", "#4da6ff")

        self.app_config["last_output_dir"] = str(output_path.parent)
        app_config.save_config(self.app_config)
        self.reset_fields()

    def _unique_output_path(self, requested_path):
        reserved = {
            self._normalized_path_key(job.output_path) for job in self.queue_jobs
        }
        candidate = requested_path
        suffix_number = 2
        while candidate.exists() or self._normalized_path_key(candidate) in reserved:
            candidate = requested_path.with_name(
                f"{requested_path.stem}_{suffix_number}{requested_path.suffix}"
            )
            suffix_number += 1
        return candidate

    @staticmethod
    def _normalized_path_key(path):
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def render_queue(self):
        for widget in self.queue_frame.winfo_children():
            widget.destroy()
        self._queue_status_labels.clear()

        if not self.queue_jobs:
            ctk.CTkLabel(
                self.queue_frame,
                text="No downloads yet",
                text_color="gray",
            ).pack(pady=14)
            return

        status_colors = {
            "Queued": "gray",
            "Waiting": "#b0bec5",
            "Downloading": "#4da6ff",
            "Processing": "#e6a817",
            "Done": "#4caf50",
            "Error": "#e05252",
            "Cancelling": "#e6a817",
            "Cancelled": "gray",
        }
        for job in self.queue_jobs:
            row = ctk.CTkFrame(
                self.queue_frame,
                fg_color="gray20",
                corner_radius=8,
            )
            row.pack(fill="x", padx=8, pady=4)
            time_range = (
                f"{format_seconds(job.start_sec)}–{format_seconds(job.end_sec)}"
            )
            kind = "🎵 MP3" if job.audio_only else job.quality
            ctk.CTkLabel(
                row,
                text=f"{job.label}  ({time_range}, {kind})",
                anchor="w",
                wraplength=300,
            ).pack(side="left", padx=10, pady=8, fill="x", expand=True)
            status_text = job.status
            if job.status == "Downloading":
                status_text = f"{job.progress * 100:.0f}%"
            status_label = ctk.CTkLabel(
                row,
                text=status_text,
                text_color=status_colors.get(job.status, "gray"),
                width=90,
            )
            status_label.pack(side="left", padx=5)
            self._queue_status_labels[job.id] = status_label

            ctk.CTkButton(
                row,
                text="✕",
                width=28,
                height=28,
                fg_color="transparent",
                hover_color="gray30",
                state="normal",
                command=lambda queued_job=job: self.cancel_or_remove(queued_job),
            ).pack(side="right", padx=8)

    def cancel_or_remove(self, job):
        if job.status in ("Done", "Error", "Cancelled"):
            if job in self.queue_jobs:
                self.queue_jobs.remove(job)
                self.render_queue()
            return

        queue_state = self.download_queue.cancel(job.id)
        if queue_state == "active":
            job.status = "Cancelling"
            self.set_status(f"Cancelling: {Path(job.output_path).name}", "#e6a817")
            self.render_queue()
        elif queue_state == "pending":
            self.set_status(f"Cancelled queued download: {Path(job.output_path).name}", "gray")

    def _find_job(self, job_id):
        return next((job for job in self.queue_jobs if job.id == job_id), None)

    def _apply_job_started(self, job_id):
        job = self._find_job(job_id)
        if job is None:
            return
        self._active_job_id = job_id
        job.status = "Downloading"
        job.progress = 0.0
        job.error = None
        self.progress.set(0)
        waiting_count = sum(
            queued_job.status in ("Queued", "Waiting")
            for queued_job in self.queue_jobs
            if queued_job.id != job_id
        )
        waiting_text = f" • {waiting_count} waiting" if waiting_count else ""
        self.set_status(
            f"Downloading: {Path(job.output_path).name}{waiting_text}",
            "#4da6ff",
        )
        self.render_queue()

    def _apply_download_progress(self, job_id, data):
        status = data.get("status")
        if status == "downloading":
            progress = self._progress_fraction(data)
            percent_text = self._clean_percent_text(data.get("_percent_str", ""))
            downloaded_text = self._clean_percent_text(
                data.get("_downloaded_bytes_str", "")
            )
            total_text = self._clean_percent_text(data.get("_total_bytes_str", ""))
            if not total_text:
                total_text = self._clean_percent_text(
                    data.get("_total_bytes_estimate_str", "")
                )
            speed_text = self._clean_percent_text(data.get("_speed_str", ""))
            eta_text = self._clean_percent_text(data.get("_eta_str", ""))

            details = []
            if not percent_text and progress is not None:
                percent_text = f"{progress * 100:.1f}%"
            if percent_text:
                details.append(percent_text)
            if downloaded_text and total_text:
                details.append(f"{downloaded_text} / {total_text}")
            elif downloaded_text:
                details.append(downloaded_text)
            if speed_text:
                details.append(speed_text)
            if eta_text:
                details.append(f"ETA {eta_text}")

            progress_text = "Downloading"
            if details:
                progress_text += "... " + " • ".join(details)
            self._apply_job_progress(
                job_id,
                progress,
                progress_text,
                "#4da6ff",
            )
        elif status == "finished":
            job = self._find_job(job_id)
            if job is not None:
                job.status = "Processing"
                self.render_queue()
            self._apply_job_progress(
                job_id,
                None,
                "Processing/merging...",
                "#e6a817",
            )

    @staticmethod
    def _progress_fraction(data):
        downloaded = data.get("downloaded_bytes")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)):
            if total > 0:
                return max(0.0, min(1.0, downloaded / total))

        percent_text = ClipperApp._clean_percent_text(data.get("_percent_str", ""))
        match = re.search(r"(\d+(?:\.\d+)?)", percent_text)
        if match:
            return max(0.0, min(1.0, float(match.group(1)) / 100.0))
        return None

    @staticmethod
    def _clean_percent_text(value):
        text = re.sub(r"\x1b\[[0-9;]*m", "", str(value or ""))
        return text.strip()

    def _apply_job_progress(self, job_id, progress, text, color):
        if job_id != self._active_job_id:
            return
        if progress is not None:
            job = self._find_job(job_id)
            if job is not None:
                job.progress = progress
            self.progress.set(progress)
            status_label = self._queue_status_labels.get(job_id)
            if status_label is not None:
                status_label.configure(text=f"{progress * 100:.0f}%")
        self.set_status(text, color)

    def _apply_job_done(self, job_id):
        job = self._find_job(job_id)
        if job is None:
            return
        job.status = "Done"
        job.error = None
        job.progress = 1.0
        self._active_job_id = None
        self.progress.set(1)
        
        if self.open_folder_var.get():                 
            self._open_containing_folder(job.output_path)
            
        self.set_status(f"✅ Saved to {job.output_path}", "#4caf50")
        self.render_queue()

    def _apply_job_error(self, job_id, error_message):
        job = self._find_job(job_id)
        if job is None:
            return
        job.status = "Error"
        job.error = error_message
        self._active_job_id = None
        self.set_status(f"❌ Error: {error_message}", "#e05252")
        self.render_queue()

    def _apply_job_cancelled(self, job_id):
        job = self._find_job(job_id)
        if job is None:
            return
        job.status = "Cancelled"
        job.error = None
        job.progress = 0.0
        if self._active_job_id == job_id:
            self._active_job_id = None
            self.progress.set(0)
        self.set_status(f"Cancelled: {Path(job.output_path).name}", "gray")
        self.render_queue()

    def reset_fields(self):
        self.url_entry.delete(0, "end")
        self.loaded_url = None
        self.loaded_title = None
        self.video_duration = None
        self._set_thumbnail(None, False)
        self.video_info_label.configure(text="No video loaded yet", text_color="gray")

        self.start_slider.configure(to=100, state="disabled")
        self.end_slider.configure(to=100, state="disabled")
        self.start_slider.set(0)
        self.end_slider.set(100)
        self.start_input.set_seconds(0)
        self.end_input.set_seconds(0)
        self.clip_length_label.configure(text="Clip length: —")

        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(self._default_output_path()))

    def _queue_idle(self):
        self._active_job_id = None
        self.render_queue()

        done_count = sum(job.status == "Done" for job in self.queue_jobs)
        error_count = sum(job.status == "Error" for job in self.queue_jobs)
        cancelled_count = sum(job.status == "Cancelled" for job in self.queue_jobs)
        if error_count:
            self.set_status(
                f"Downloads finished: {done_count} completed, {error_count} failed, "
                f"{cancelled_count} cancelled",
                "#e6a817",
            )
        elif cancelled_count:
            self.set_status(
                f"Downloads finished: {done_count} completed, "
                f"{cancelled_count} cancelled",
                "gray",
            )
        else:
            self.progress.set(1)
            self.set_status(
                f"Downloads finished: {done_count} completed", "#4caf50"
            )

    # ---------- Update notification ----------

    def check_update_async(self):
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            is_newer, latest, url = updater.check_for_update(
                UPDATE_OWNER,
                UPDATE_REPO,
                CURRENT_VERSION,
            )
            if is_newer and latest and url:
                self._post_ui_event("update_available", latest, url)
        except Exception:
            # Update checks are optional and must never interfere with downloads.
            return

    def _show_update_banner(self, latest, url):
        for widget in self.banner_container.winfo_children():
            widget.destroy()

        self.banner_container.pack(
            fill="x",
            side="top",
            before=self.header_label,
        )
        banner = ctk.CTkFrame(
            self.banner_container,
            fg_color="#2d5f8a",
            corner_radius=0,
        )
        banner.pack(fill="x")
        ctk.CTkLabel(
            banner,
            text=f"🔔 Version {latest} is available",
            text_color="white",
        ).pack(side="left", padx=15, pady=8)
        ctk.CTkButton(
            banner,
            text="View Release",
            width=110,
            height=28,
            command=lambda: webbrowser.open(url),
        ).pack(side="right", padx=(0, 10), pady=8)
        ctk.CTkButton(
            banner,
            text="✕",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color="#1e4566",
            command=self._dismiss_update_banner,
        ).pack(side="right", pady=8)

    def _dismiss_update_banner(self):
        for widget in self.banner_container.winfo_children():
            widget.destroy()
        self.banner_container.pack_forget()


def main():
    app = ClipperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
