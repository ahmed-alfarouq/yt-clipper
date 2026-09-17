import queue
import threading
import time
from pathlib import Path

from yt_clipper.core import downloader


class SequentialDownloadQueue:
    """Runs download jobs sequentially and starts queued work automatically.

    The event callback is invoked from this service's worker thread. Callers
    must marshal those events onto their UI thread before touching widgets.
    """

    def __init__(self, event_callback):
        self._event_callback = event_callback
        self._jobs = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending_ids = set()
        self._cancelled_pending_ids = set()
        self._active = False
        self._active_job_id = None
        self._active_cancel_event = None
        self._stopping = threading.Event()
        self._worker = threading.Thread(
            target=self._run,
            name="yt-clipper-download-queue",
            daemon=True,
        )
        self._worker.start()

    @property
    def is_busy(self):
        with self._state_lock:
            return self._active or bool(self._pending_ids)

    def enqueue(self, job):
        """Add a job and return True when it was placed behind existing work."""
        with self._state_lock:
            queued_behind_existing_work = self._active or bool(self._pending_ids)
            self._pending_ids.add(job.id)
        self._jobs.put(job)
        return queued_behind_existing_work

    def cancel(self, job_id):
        """Cancel an active/pending job and return its previous queue state."""
        with self._state_lock:
            if self._active_job_id == job_id and self._active_cancel_event:
                self._active_cancel_event.set()
                return "active"
            if job_id in self._pending_ids:
                self._cancelled_pending_ids.add(job_id)
                self._event_callback("job_cancelled", job_id)
                return "pending"
        return None

    def shutdown(self):
        self._stopping.set()
        with self._state_lock:
            if self._active_cancel_event:
                self._active_cancel_event.set()
        self._jobs.put(None)

    def _run(self):
        while not self._stopping.is_set():
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                return

            with self._state_lock:
                self._pending_ids.discard(job.id)
                was_cancelled = job.id in self._cancelled_pending_ids
                self._cancelled_pending_ids.discard(job.id)
                if not was_cancelled:
                    self._active = True
                    self._active_job_id = job.id
                    self._active_cancel_event = threading.Event()
                    cancel_event = self._active_cancel_event

            if was_cancelled:
                self._jobs.task_done()
                self._notify_if_idle()
                continue

            self._event_callback("job_started", job.id)
            try:
                Path(job.output_path).parent.mkdir(parents=True, exist_ok=True)
                last_progress_emit = 0.0

                def progress_hook(data, job_id=job.id):
                    nonlocal last_progress_emit
                    status = data.get("status")
                    now = time.monotonic()
                    if status == "downloading" and now - last_progress_emit < 0.1:
                        return
                    last_progress_emit = now
                    self._event_callback("download_progress", job_id, dict(data))

                downloader.download_clip(
                    job.url,
                    job.start_sec,
                    job.end_sec,
                    job.output_path,
                    quality=job.quality,
                    audio_only=job.audio_only,
                    progress_hook=progress_hook,
                    cancel_event=cancel_event,
                )
                self._event_callback("job_done", job.id)
            except downloader.DownloadCancelled:
                self._event_callback("job_cancelled", job.id)
            except Exception as exc:
                self._event_callback("job_error", job.id, str(exc))
            finally:
                self._jobs.task_done()
                with self._state_lock:
                    self._active = False
                    self._active_job_id = None
                    self._active_cancel_event = None

            self._notify_if_idle()

    def _notify_if_idle(self):
        with self._state_lock:
            is_idle = not self._active and not self._pending_ids
        if is_idle:
            self._event_callback("queue_idle")
