import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import os

from yt_clipper.core import downloader
from yt_clipper.core.utils import format_seconds
from yt_clipper.gui.widgets.time_input import TimeInput

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class ClipperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Clipper")
        self.geometry("580x760")
        self.resizable(False, False)

        self.video_duration = None
        self._syncing = False

        ctk.CTkLabel(self, text="🎬 YouTube Clipper",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(25, 5))
        ctk.CTkLabel(self, text="Download a high-quality clip from any YouTube video",
                     font=ctk.CTkFont(size=13), text_color="gray").pack(pady=(0, 20))

        card = ctk.CTkFrame(self, corner_radius=16)
        card.pack(padx=25, pady=5, fill="both", expand=True)

        # URL + Load
        ctk.CTkLabel(card, text="Video URL", anchor="w").pack(fill="x", padx=20, pady=(20, 5))
        url_row = ctk.CTkFrame(card, fg_color="transparent")
        url_row.pack(fill="x", padx=20)
        self.url_entry = ctk.CTkEntry(url_row, placeholder_text="https://youtube.com/watch?v=...", height=40)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.load_btn = ctk.CTkButton(url_row, text="Load Video", width=110, height=40,
                                       command=self.start_load_video)
        self.load_btn.pack(side="left")

        self.video_info_label = ctk.CTkLabel(card, text="No video loaded yet", text_color="gray", anchor="w")
        self.video_info_label.pack(fill="x", padx=20, pady=(8, 20))

        # Start time
        ctk.CTkLabel(card, text="Start Time", anchor="w").pack(fill="x", padx=20)
        self.start_input = TimeInput(card, on_change=self.on_start_change)
        self.start_input.pack(padx=20, pady=(5, 5))
        self.start_slider = ctk.CTkSlider(card, from_=0, to=100, state="disabled",
                                           command=self.on_start_slide)
        self.start_slider.set(0)
        self.start_slider.pack(fill="x", padx=20, pady=(0, 15))

        # End time
        ctk.CTkLabel(card, text="End Time", anchor="w").pack(fill="x", padx=20)
        self.end_input = TimeInput(card, on_change=self.on_end_change)
        self.end_input.pack(padx=20, pady=(5, 5))
        self.end_slider = ctk.CTkSlider(card, from_=0, to=100, state="disabled",
                                         command=self.on_end_slide)
        self.end_slider.set(100)
        self.end_slider.pack(fill="x", padx=20, pady=(0, 5))

        self.clip_length_label = ctk.CTkLabel(card, text="Clip length: —",
                                               font=ctk.CTkFont(size=12, weight="bold"),
                                               text_color="#4da6ff")
        self.clip_length_label.pack(pady=(5, 15))

        # Quality
        ctk.CTkLabel(card, text="Quality", anchor="w").pack(fill="x", padx=20, pady=(0, 5))
        self.quality_var = ctk.StringVar(value="best")
        ctk.CTkOptionMenu(card, variable=self.quality_var,
                          values=["best", "4k", "1080p", "720p"], height=40).pack(fill="x", padx=20)

        # Save location
        ctk.CTkLabel(card, text="Save To", anchor="w").pack(fill="x", padx=20, pady=(20, 5))
        save_row = ctk.CTkFrame(card, fg_color="transparent")
        save_row.pack(fill="x", padx=20)
        self.output_entry = ctk.CTkEntry(save_row, height=40)
        self.output_entry.insert(0, os.path.join(os.getcwd(), "clip.mp4"))
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(save_row, text="Browse", width=90, height=40, command=self.browse_output,
                      fg_color="gray30", hover_color="gray20").pack(side="left")

        # Download
        self.download_btn = ctk.CTkButton(card, text="⬇  Download Clip", height=48,
                                           font=ctk.CTkFont(size=15, weight="bold"),
                                           command=self.start_download)
        self.download_btn.pack(fill="x", padx=20, pady=(25, 15))

        self.progress = ctk.CTkProgressBar(card, height=14)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=20, pady=(0, 10))

        self.status_label = ctk.CTkLabel(card, text="Ready", text_color="gray")
        self.status_label.pack(pady=(0, 20))

    # ---------- Video loading ----------

    def start_load_video(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return
        self.load_btn.configure(state="disabled", text="Loading...")
        self.video_info_label.configure(text="Fetching video info...", text_color="#4da6ff")
        threading.Thread(target=self.load_video, args=(url,), daemon=True).start()

    def load_video(self, url):
        try:
            info = downloader.get_video_info(url)
            duration = info["duration"]
            self.video_duration = duration

            self.start_slider.configure(to=duration, state="normal")
            self.end_slider.configure(to=duration, state="normal")
            self.start_slider.set(0)
            self.end_slider.set(duration)
            self.start_input.set_seconds(0)
            self.end_input.set_seconds(duration)

            self.video_info_label.configure(
                text=f"🎞  {info['title']}   •   Duration: {format_seconds(duration)}",
                text_color="#4caf50")
            self.update_clip_length()
        except Exception as e:
            self.video_info_label.configure(text=f"❌ Couldn't load video: {e}", text_color="#e05252")
        finally:
            self.load_btn.configure(state="normal", text="Load Video")

    # ---------- Sync: steppers <-> slider ----------

    def on_start_change(self, seconds):
        if self._syncing:
            return
        self._syncing = True
        if self.video_duration is not None:
            seconds = min(seconds, self.video_duration)
        self.start_slider.set(seconds)
        if seconds > self.end_slider.get():
            self.end_slider.set(seconds)
            self.end_input.set_seconds(seconds)
        self._syncing = False
        self.update_clip_length()

    def on_end_change(self, seconds):
        if self._syncing:
            return
        self._syncing = True
        if self.video_duration is not None:
            seconds = min(seconds, self.video_duration)
        self.end_slider.set(seconds)
        if seconds < self.start_slider.get():
            self.start_slider.set(seconds)
            self.start_input.set_seconds(seconds)
        self._syncing = False
        self.update_clip_length()

    def on_start_slide(self, value):
        if self._syncing:
            return
        self._syncing = True
        self.start_input.set_seconds(value)
        if value > self.end_slider.get():
            self.end_slider.set(value)
            self.end_input.set_seconds(value)
        self._syncing = False
        self.update_clip_length()

    def on_end_slide(self, value):
        if self._syncing:
            return
        self._syncing = True
        self.end_input.set_seconds(value)
        if value < self.start_slider.get():
            self.start_slider.set(value)
            self.start_input.set_seconds(value)
        self._syncing = False
        self.update_clip_length()

    def update_clip_length(self):
        length = self.end_slider.get() - self.start_slider.get()
        self.clip_length_label.configure(text=f"Clip length: {format_seconds(max(0, length))}")

    # ---------- Download ----------

    def browse_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4 files", "*.mp4")])
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def set_status(self, text, color="gray"):
        self.status_label.configure(text=text, text_color=color)

    def start_download(self):
        url = self.url_entry.get().strip()
        start_sec = self.start_input.get_seconds()
        end_sec = self.end_input.get_seconds()
        output = self.output_entry.get().strip()
        quality = self.quality_var.get()

        if not url:
            messagebox.showerror("Missing info", "Paste a URL and load the video first.")
            return

        self.download_btn.configure(state="disabled", text="Downloading...")
        self.progress.set(0)
        self.set_status("Starting...", "#4da6ff")
        threading.Thread(target=self.run_download,
                          args=(url, start_sec, end_sec, output, quality), daemon=True).start()

    def progress_hook(self, d):
        if d['status'] == 'downloading':
            pct_str = d.get('_percent_str', '0%').strip().replace('%', '')
            try:
                self.progress.set(float(pct_str) / 100)
            except ValueError:
                pass
            self.set_status(f"Downloading... {d.get('_percent_str', '').strip()}", "#4da6ff")
        elif d['status'] == 'finished':
            self.set_status("Processing/merging...", "#e6a817")

    def run_download(self, url, start_sec, end_sec, output, quality):
        try:
            downloader.download_clip(url, start_sec, end_sec, output, quality, self.progress_hook)
            self.progress.set(1)
            self.set_status(f"✅ Saved to {output}", "#4caf50")
        except Exception as e:
            self.set_status(f"❌ Error: {e}", "#e05252")
        finally:
            self.download_btn.configure(state="normal", text="⬇  Download Clip")


def main():
    app = ClipperApp()
    app.mainloop()