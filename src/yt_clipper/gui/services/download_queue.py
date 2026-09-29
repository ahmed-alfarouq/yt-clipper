import queue
import threading
import time
from pathlib import Path

from yt_clipper.core import downloader
from yt_clipper.core.errors import DownloadCancelled
from yt_clipper.core.log import describe_failure, get_logger, safe_message

logger = get_logger(__name__)


class SequentialDownloadQueue:
    """Runs download jobs sequentially and starts queued work automatically.

    The event callback is invoked from this service's worker thread. Callers
    must marshal those events onto their UI thread before touching widgets.

    Failure contract (§9): every job reaches exactly one terminal state -
    COMPLETED (`job_done`), CANCELLED (`job_cancelled`) or FAILED (`job_error`)
    - and one job's failure never stops the jobs behind it. Cancellation is
    recognised as its own outcome and is never reported as an error (§3).
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

            self._emit("job_started", job.id)
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
                    try:
                        self._event_callback("download_progress", job_id, dict(data))
                    except Exception as exc:
                        # Progress display is presentation-only and fires many
                        # times a second: a broken listener must not abort the
                        # download (§11, §16), and must not flood the log.
                        logger.debug(
                            "Progress event for job %s could not be delivered: %s",
                            job_id, describe_failure(exc),
                        )

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
                logger.info("Download job %s completed: %s", job.id, job.output_path)
                self._emit("job_done", job.id)
            except DownloadCancelled:
                # CANCELLED, not FAILED: the user asked for this (§3, §9).
                logger.info("Download job %s was cancelled by the user", job.id)
                self._emit("job_cancelled", job.id)
            except Exception as exc:
                # The single failure record for this job (§18). The message sent
                # to the UI is redacted and free of tracebacks (§16).
                logger.error("Download job %s failed for %s: %s",
                             job.id, job.url, describe_failure(exc))
                self._emit("job_error", job.id, safe_message(exc))
            finally:
                self._jobs.task_done()
                with self._state_lock:
                    self._active = False
                    self._active_job_id = None
                    self._active_cancel_event = None

            self._notify_if_idle()

    def _emit(self, event_name, *args):
        """Deliver a terminal/lifecycle event to the listener.

        The job's outcome is already decided when this runs, so a listener that
        raises can neither turn a finished download into a failure nor stop the
        remaining jobs (§9, §21). Such a bug is logged with its traceback
        instead of being swallowed (§18).
        """
        try:
            self._event_callback(event_name, *args)
        except Exception:
            logger.exception("Queue event %r could not be delivered", event_name)

    def _notify_if_idle(self):
        with self._state_lock:
            is_idle = not self._active and not self._pending_ids
        if is_idle:
            self._emit("queue_idle")
