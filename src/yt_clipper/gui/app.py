import os
import queue
import threading
import traceback
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog

from yt_clipper.core import config as app_config
from yt_clipper.core.utils import format_seconds
from yt_clipper.gui.controllers import VideoLoaderController, QueueController, UpdateChecker
from yt_clipper.gui.controllers.video_loader import THUMBNAIL_SIZE
from yt_clipper.gui.services import SequentialDownloadQueue
from yt_clipper.gui.widgets.time_input import TimeInput


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

UI_POLL_INTERVAL_MS = 50


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
        self.loaded_playlist_entries = None
        self._syncing = False
        self._closing = False
        self._load_request_id = 0
        self._thumbnail_image = None

        self.app_config = app_config.load_config()
        if not isinstance(self.app_config, dict):
            self.app_config = {}

        self.queue_jobs = []
        self._queue_status_labels = {}
        self._job_id_counter = _make_id_counter()
        self._active_job_id = None

        # Workers only place plain data in this queue. Tk widgets are updated
        # exclusively by _poll_ui_events on the main thread.
        self._ui_events = queue.Queue()
        self._shutdown_event = threading.Event()
        self.download_queue = SequentialDownloadQueue(self._post_ui_event)

        # Controllers own the behavior; app.py owns layout and shared state.
        self.video_loader = VideoLoaderController(self)
        self.queue_controller = QueueController(self)
        self.update_checker = UpdateChecker(self)

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
            command=self.video_loader.start_load_video,
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
            command=self.queue_controller.download_clip,
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
        self.queue_controller.render_queue()

        self.progress = ctk.CTkProgressBar(card, height=14)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(0, 10))

        self.status_label = ctk.CTkLabel(card, text="Ready", text_color="gray")
        self.status_label.pack(pady=(0, 20))

        self.after(UI_POLL_INTERVAL_MS, self._poll_ui_events)
        self.after(500, self.update_checker.check_update_async)

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
            try:
                self._handle_ui_event(event_name, payload)
            except Exception:
                # A bug in one event handler must never take down all future
                # UI updates (progress bars, queue rows, thumbnails, etc.).
                # Print for visibility during development instead of failing
                # silently and freezing the app.
                traceback.print_exc()

        if not self._closing:
            self.after(UI_POLL_INTERVAL_MS, self._poll_ui_events)

    def _handle_ui_event(self, event_name, payload):
        if event_name == "video_metadata":
            self.video_loader._apply_video_metadata(*payload)
        elif event_name == "video_thumbnail":
            self.video_loader._apply_video_thumbnail(*payload)
        elif event_name == "video_error":
            self.video_loader._apply_video_error(*payload)
        elif event_name == "video_retry":
            self.video_loader._apply_video_retry(*payload)
        elif event_name == "playlist_metadata":
            self.video_loader._apply_playlist_metadata(*payload)
        elif event_name == "job_started":
            self.queue_controller._apply_job_started(*payload)
        elif event_name == "download_progress":
            self.queue_controller._apply_download_progress(*payload)
        elif event_name == "job_done":
            self.queue_controller._apply_job_done(*payload)
        elif event_name == "job_error":
            self.queue_controller._apply_job_error(*payload)
        elif event_name == "job_cancelled":
            self.queue_controller._apply_job_cancelled(*payload)
        elif event_name == "queue_idle":
            self.queue_controller._queue_idle()
        elif event_name == "update_available":
            self.update_checker._show_update_banner(*payload)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._shutdown_event.set()
        self.download_queue.shutdown()
        self.destroy()

    # ---------- Shared helpers (used by more than one controller) ----------

    def set_status(self, text, color="gray"):
        self.status_label.configure(text=text, text_color=color)

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


def _make_id_counter():
    import itertools
    return itertools.count(1)


def main():
    app = ClipperApp()
    app.mainloop()


if __name__ == "__main__":
    main()