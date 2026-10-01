import os
import re
import subprocess
import sys
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox

from yt_clipper.core import config as app_config
from yt_clipper.core.log import describe_failure, get_logger
from yt_clipper.core.utils import format_seconds, sanitize_filename

logger = get_logger(__name__)

from yt_clipper.core.playlist_utils import filter_available_videos
from yt_clipper.gui.models import DownloadJob

STATUS_COLORS = {
    "Queued": "gray",
    "Waiting": "#b0bec5",
    "Downloading": "#4da6ff",
    "Retrying": "#e6a817",
    "Processing": "#e6a817",
    "Done": "#4caf50",
    "Error": "#e05252",
    "Cancelling": "#e6a817",
    "Cancelled": "gray",
}


class QueueController:
    """Owns the download queue: enqueueing jobs, rendering rows, and
    reacting to job lifecycle events posted by the background worker.

    Playlist handling:
      - Receives validated playlist entries from shared state
      - _enqueue_playlist() applies the canonical availability filter once, at
        the point where download jobs are created, so no caller can turn an
        explicitly unavailable entry into a job
      - Preview and Download share the same validated dataset
    """

    def __init__(self, app):
        self.app = app
        self.last_output_path = None

    def download_clip(self):
        app = self.app
        url = app.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Paste a YouTube URL first.")
            return

        audio_only = app.audio_only_var.get()
        raw_output = app.output_entry.get().strip()
        if not raw_output:
            raw_output = str(app._default_output_path())

        requested_path = app._path_with_expected_extension(raw_output, audio_only)
        try:
            requested_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Nothing was queued, so this is a clean refusal rather than a
            # failed job: the user is told why and can pick another folder.
            logger.warning("Output folder %s could not be created; nothing queued: %s",
                           requested_path.parent, describe_failure(exc))
            messagebox.showerror(
                "Invalid output folder",
                f"The output folder could not be created:\n{exc}",
            )
            return

        if app.loaded_url == url and app.loaded_playlist_entries:
            # Shared validated dataset. _enqueue_playlist applies the canonical
            # availability filter where the jobs are created, so the same list is
            # not filtered twice on the way there.
            self._enqueue_playlist(app.loaded_playlist_entries, audio_only,
                                   requested_path)
            self.reset_fields()
            return

        start_sec = app.start_input.get_seconds()
        end_sec = app.end_input.get_seconds()
        if end_sec <= start_sec:
            messagebox.showerror(
                "Invalid range", "End time must be after start time."
            )
            return

        if app.loaded_url == url and app.video_duration is not None:
            if end_sec > app.video_duration:
                messagebox.showerror(
                    "Invalid range",
                    "End time cannot be later than the loaded video's duration.",
                )
                return
            label = app.loaded_title or url
        else:
            label = url

        output_path = self._unique_output_path(requested_path)
        job = DownloadJob(
            id=next(app._job_id_counter),
            url=url,
            label=label,
            start_sec=start_sec,
            end_sec=end_sec,
            quality=app.quality_var.get(),
            audio_only=audio_only,
            output_path=str(output_path),
        )
        app.queue_jobs.append(job)
        queued_behind_another = app.download_queue.enqueue(job)
        job.status = "Waiting" if queued_behind_another else "Queued"
        self.render_queue()
        if queued_behind_another:
            waiting_count = sum(
                queued_job.status in ("Queued", "Waiting")
                for queued_job in app.queue_jobs
            )
            app.set_status(
                f"Queued: {output_path.name} • {waiting_count} waiting",
                "#4da6ff",
            )
        else:
            app.progress.set(0)
            app.set_status(f"Starting download: {output_path.name}", "#4da6ff")

        app.app_config["last_output_dir"] = str(output_path.parent)
        self._remember_output_dir(app, output_path.parent)
        self.reset_fields()

    @staticmethod
    def _remember_output_dir(app, directory):
        """Persist the last output folder; a failure here is never fatal (§15).

        save_config already logs the reason at WARNING, so the UI only needs to
        say that the preference was not remembered - the queued downloads are
        unaffected either way.
        """
        if app_config.save_config(app.app_config) is False:
            app.set_status(
                "Settings could not be saved; they apply to this session only.",
                "#e6a817",
            )

    def _enqueue_playlist(self, entries, audio_only, requested_path):
        """Queue one job per playlist video, each downloaded in full.

        This is the queue boundary's single application of the canonical
        availability decision: whatever list reaches job creation is filtered
        here (fail-open, §4), so no caller - present or future - can turn an
        explicitly unavailable entry into a download job.
        """
        app = self.app
        directory = requested_path.parent
        extension = requested_path.suffix or (".mp3" if audio_only else ".mp4")

        # Canonical filter at the job-creation gate: the only pass on this path.
        try:
            validated_entries = filter_available_videos(entries)
        except Exception as exc:
            # Fail open (§4): dropping every video because a safety-net filter
            # raised would silently turn a valid playlist into an empty queue.
            logger.warning(
                "Defensive playlist filter failed while queueing; using the %d "
                "entries as given: %s", len(entries), describe_failure(exc),
            )
            validated_entries = entries

        queued_count = 0
        for index, entry in enumerate(validated_entries, start=1):
            safe_title = sanitize_filename(entry.get("title") or f"video_{index}")
            candidate = directory / f"{index:02d} - {safe_title}{extension}"
            output_path = self._unique_output_path(candidate)

            job = DownloadJob(
                id=next(app._job_id_counter),
                url=entry["url"],
                label=entry.get("title") or entry["url"],
                start_sec=None,
                end_sec=None,
                quality=app.quality_var.get(),
                audio_only=audio_only,
                output_path=str(output_path),
            )
            app.queue_jobs.append(job)
            queued_behind_another = app.download_queue.enqueue(job)
            job.status = "Waiting" if (queued_behind_another or index > 1) else "Queued"
            queued_count += 1

        self.render_queue()
        app.app_config["last_output_dir"] = str(directory)
        self._remember_output_dir(app, directory)
        app.set_status(f"Queued {queued_count} full videos from playlist", "#4da6ff")

    def _unique_output_path(self, requested_path):
        app = self.app
        reserved = {
            self._normalized_path_key(job.output_path) for job in app.queue_jobs
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
        app = self.app
        for widget in app.queue_frame.winfo_children():
            widget.destroy()
        app._queue_status_labels.clear()

        if not app.queue_jobs:
            ctk.CTkLabel(
                app.queue_frame,
                text="No downloads yet",
                text_color="gray",
            ).pack(pady=14)
            return

        for job in app.queue_jobs:
            row = ctk.CTkFrame(
                app.queue_frame,
                fg_color="gray20",
                corner_radius=8,
            )
            row.pack(fill="x", padx=8, pady=4)
            time_range = (
                "Full video"
                if job.start_sec is None or job.end_sec is None
                else f"{format_seconds(job.start_sec)}–{format_seconds(job.end_sec)}"
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
                text_color=STATUS_COLORS.get(job.status, "gray"),
                width=90,
            )
            status_label.pack(side="left", padx=5)
            app._queue_status_labels[job.id] = status_label

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
        app = self.app
        if job.status in ("Done", "Error", "Cancelled"):
            if job in app.queue_jobs:
                app.queue_jobs.remove(job)
                self.render_queue()
            return

        queue_state = app.download_queue.cancel(job.id)
        if queue_state == "active":
            job.status = "Cancelling"
            app.set_status(f"Cancelling: {Path(job.output_path).name}", "#e6a817")
            self.render_queue()
        elif queue_state == "pending":
            app.set_status(f"Cancelled queued download: {Path(job.output_path).name}", "gray")

    def _find_job(self, job_id):
        return next((job for job in self.app.queue_jobs if job.id == job_id), None)

    def _apply_job_started(self, job_id):
        app = self.app
        job = self._find_job(job_id)
        if job is None:
            return
        app._active_job_id = job_id
        job.status = "Downloading"
        job.progress = 0.0
        job.error = None
        app.progress.set(0)
        waiting_count = sum(
            queued_job.status in ("Queued", "Waiting")
            for queued_job in app.queue_jobs
            if queued_job.id != job_id
        )
        waiting_text = f" • {waiting_count} waiting" if waiting_count else ""
        app.set_status(
            f"Downloading: {Path(job.output_path).name}{waiting_text}",
            "#4da6ff",
        )
        self.render_queue()

    def _apply_download_progress(self, job_id, data):
        if self._find_job(job_id) is None:
            return
        status = data.get("status")
        if status == "downloading":
            job = self._find_job(job_id)
            if job is not None and job.status != "Downloading":
                job.status = "Downloading"
                self.render_queue()

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
        elif status == "retrying":
            job = self._find_job(job_id)
            if job is not None:
                job.status = "Retrying"
                self.render_queue()
            attempt = data.get("attempt")
            max_attempts = data.get("max_attempts")
            delay = data.get("delay") or 0
            error_text = data.get("error") or ""
            self._apply_job_progress(
                job_id,
                None,
                f"⚠ Network hiccup, retrying ({attempt}/{max_attempts}) "
                f"in {delay:.0f}s: {error_text}",
                "#e6a817",
            )

    @staticmethod
    def _progress_fraction(data):
        downloaded = data.get("downloaded_bytes")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)):
            if total > 0:
                return max(0.0, min(1.0, downloaded / total))

        percent_text = QueueController._clean_percent_text(data.get("_percent_str", ""))
        match = re.search(r"(\d+(?:\.\d+)?)", percent_text)
        if match:
            return max(0.0, min(1.0, float(match.group(1)) / 100.0))
        return None

    @staticmethod
    def _clean_percent_text(value):
        text = re.sub(r"\x1b\[[0-9;]*m", "", str(value or ""))
        return text.strip()

    def _apply_job_progress(self, job_id, progress, text, color):
        app = self.app
        if job_id != app._active_job_id:
            return
        if progress is not None:
            job = self._find_job(job_id)
            if job is not None:
                job.progress = progress
            app.progress.set(progress)
            status_label = app._queue_status_labels.get(job_id)
            if status_label is not None:
                status_label.configure(text=f"{progress * 100:.0f}%")
        app.set_status(text, color)

    def _apply_job_done(self, job_id):
        app = self.app
        job = self._find_job(job_id)
        if job is None:
            return
        job.status = "Done"
        job.error = None
        job.progress = 1.0
        app._active_job_id = None
        app.progress.set(1)
        self.last_output_path = job.output_path
        app.set_status(f"✅ Saved to {job.output_path}", "#4caf50")
        self.render_queue()

    def _apply_job_error(self, job_id, error_message):
        app = self.app
        job = self._find_job(job_id)
        if job is None:
            return
        job.status = "Error"
        job.error = error_message
        app._active_job_id = None
        app.set_status(f"❌ Error: {error_message}", "#e05252")
        self.render_queue()

    def _apply_job_cancelled(self, job_id):
        app = self.app
        job = self._find_job(job_id)
        if job is None:
            return
        output_name = Path(job.output_path).name if getattr(job, "output_path", None) else ""
        try:
            if job in app.queue_jobs:
                app.queue_jobs.remove(job)
        except ValueError:
            # The row was already removed (e.g. cancelled while pending); the
            # desired end state - no row for this job - is already true.
            pass
        if app._active_job_id == job_id:
            app._active_job_id = None
            try:
                app.progress.set(0)
            except Exception as exc:
                logger.debug("Progress bar could not be reset after cancel: %s",
                             describe_failure(exc))
        try:
            app._queue_status_labels.pop(job_id, None)
        except Exception as exc:
            logger.debug("Stale status label for job %s could not be dropped: %s",
                         job_id, describe_failure(exc))
        self.render_queue()
        try:
            if output_name:
                app.set_status(f"Cancelled: {output_name} removed", "gray")
            else:
                app.set_status("Download cancelled", "gray")
        except Exception as exc:
            # The cancellation itself already happened; only the status text
            # failed, so this stays a presentation-level note (§16).
            logger.debug("Cancellation status text could not be shown: %s",
                         describe_failure(exc))

    def _queue_idle(self):
        app = self.app
        app._active_job_id = None
        self.render_queue()

        done_count = sum(job.status == "Done" for job in app.queue_jobs)
        error_count = sum(job.status == "Error" for job in app.queue_jobs)

        if self.last_output_path and app.open_folder_var.get():
            self._open_containing_folder(self.last_output_path)
        self.last_output_path = None

        if error_count:
            app.set_status(
                f"Downloads finished: {done_count} completed, {error_count} failed",
                "#e6a817",
            )
        else:
            if done_count:
                app.progress.set(1)
                app.set_status(
                    f"Downloads finished: {done_count} completed", "#4caf50"
                )

    @staticmethod
    def _open_containing_folder(file_path):
        """Open the folder holding a finished download.

        §11: a convenience that must never undo a successful download. Failure
        (no file manager, headless session, sandbox) is recorded at WARNING so
        "the folder did not open" is explainable, and the job stays Done.
        """
        folder = os.path.dirname(os.path.abspath(file_path))
        try:
            if sys.platform == "win32":
                os.startfile(folder)
                return
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            result = subprocess.run([opener, folder], check=False)
            if result.returncode != 0:
                logger.warning("%s could not open %s (exit %s); the download itself "
                               "succeeded", opener, folder, result.returncode)
        except Exception as exc:
            logger.warning("Containing folder %s could not be opened; the download "
                           "itself succeeded: %s", folder, describe_failure(exc))

    def reset_fields(self):
        app = self.app
        app._load_request_id += 1
        app.url_entry.delete(0, "end")
        app.loaded_url = None
        app.loaded_title = None
        app.loaded_playlist_entries = None
        app.video_duration = None
        app.video_loader._set_thumbnail(None, False)
        try:
            app.playlist_preview.clear()
        except Exception as exc:
            logger.debug("Playlist preview could not be cleared on reset: %s",
                         describe_failure(exc))
        try:
            app.show_single_preview()
        except Exception as exc:
            logger.debug("Single preview could not be restored on reset: %s",
                         describe_failure(exc))
        try:
            app.video_info_label.configure(
                text="No video loaded yet",
                text_color="gray",
            )
        except Exception as exc:
            logger.debug("Video info label could not be reset: %s",
                         describe_failure(exc))
        try:
            app.set_download_enabled(False)
        except Exception as exc:
            logger.debug("Download button could not be disabled on reset: %s",
                         describe_failure(exc))
        app.set_time_range_visible(True)
