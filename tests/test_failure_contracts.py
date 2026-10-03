"""Phase 3 — failure contracts.

Every test here proves a *contract*, not a line of code: each one asserts the
terminal outcome (SUCCESS / EXPECTED_EMPTY / NOT_AVAILABLE / CANCELLED /
RECOVERABLE_FAILURE / FATAL_FAILURE), that cancellation is never swallowed,
retried or misreported, that a failure never leaves a partial artefact behind,
and that the log says what happened without leaking secrets.

The suite reuses tests/helpers.py (stub-only-if-missing dependencies, CLI
runner, download recorder) and never touches the network.
"""

import io
import itertools
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import types
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # sets sys.path + a throwaway XDG_CONFIG_HOME on import

from helpers import (  # noqa: E402
    DownloadCallRecorder,
    FakeFFmpegPostProcessor,
    flat_playlist,
    playlist_entry,
    run_cli,
    single_video_result,
    wait_for,
)

from yt_clipper.core import config as config_module  # noqa: E402
from yt_clipper.core import downloader  # noqa: E402
from yt_clipper.core import ffmpeg_runner  # noqa: E402
from yt_clipper.core import js_runtime  # noqa: E402
from yt_clipper.core import playlist_utils  # noqa: E402
from yt_clipper.core import updater  # noqa: E402
from yt_clipper.core.downloader import _FilteredYtDlpLogger  # noqa: E402
from yt_clipper.core.errors import DownloadCancelled  # noqa: E402
from yt_clipper.core.log import (  # noqa: E402
    describe_failure,
    redact_secrets,
    safe_message,
)

gui = helpers.import_gui_modules()  # noqa: E402
ClipperApp = gui["ClipperApp"]
DownloadJob = gui["DownloadJob"]
QueueController = gui["QueueController"]
SequentialDownloadQueue = gui["SequentialDownloadQueue"]

from yt_clipper.gui.app import ClipperApp as _ClipperAppModuleRef  # noqa: E402,F401
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402
from yt_clipper.gui.controllers.update_checker import UpdateChecker  # noqa: E402
from yt_clipper.gui.services import download_queue as download_queue_module  # noqa: E402

import yt_dlp.utils  # noqa: E402  (real or stubbed DownloadError)

DownloadError = yt_dlp.utils.DownloadError

LOG_ROOT = "yt_clipper"

VIDEO_URL = "https://www.youtube.com/watch?v=contractvid1"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLCONTRACTS"
SECRET_COOKIE = "SAPISID=SuperSecretValue123"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class Permissive:
    """Presentation-only stand-in: any attribute, any call, no-op."""

    def __call__(self, *args, **kwargs):
        return Permissive()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Permissive()


class RecordingWidget:
    """Records configure() calls so tests can assert on label text."""

    def __init__(self):
        self.configured = []
        self.text = None

    def configure(self, **kwargs):
        self.configured.append(kwargs)
        if "text" in kwargs:
            self.text = kwargs["text"]

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


class FakeVar:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class FakeEntry:
    def __init__(self, text=""):
        self._text = text

    def get(self):
        return self._text

    def delete(self, *args):
        self._text = ""

    def insert(self, *args):
        self._text = args[-1] if args else ""


class FakeTimeInput:
    def __init__(self, seconds):
        self._seconds = seconds

    def get_seconds(self):
        return self._seconds

    def set_seconds(self, seconds):
        self._seconds = seconds


class QueueHarness:
    """The REAL SequentialDownloadQueue plus an event recorder."""

    def __init__(self, listener=None):
        self.events = []
        self._listener = listener
        self.queue = SequentialDownloadQueue(self._on_event)

    def _on_event(self, name, *payload):
        self.events.append((name, payload))
        if self._listener is not None:
            self._listener(name, payload)

    def event_names(self):
        return [name for name, _ in self.events]

    def payloads(self, name):
        return [payload for event, payload in self.events if event == name]

    def shutdown(self):
        self.queue.shutdown()


class FakeLoaderApp:
    """Stands in for ClipperApp on the video-loading path."""

    def __init__(self, request_id=1, output_path=None):
        self._load_request_id = request_id
        self.events = []
        self.statuses = []
        self.video_info_label = RecordingWidget()
        self.thumbnail_label = RecordingWidget()
        self.load_btn = RecordingWidget()
        self.playlist_preview = Permissive()
        self.output_entry = FakeEntry(str(output_path or Path(tempfile.gettempdir()) / "clip.mp4"))
        self.audio_only_var = FakeVar(False)
        self.loaded_url = None
        self.loaded_title = None
        self.loaded_playlist_entries = None
        self.video_duration = None
        self._thumbnail_image = None

    def _default_output_path(self):
        return Path(tempfile.gettempdir()) / "clip.mp4"

    def _post_ui_event(self, name, *payload):
        self.events.append((name, payload))

    def event_names(self):
        return [name for name, _ in self.events]

    def payload(self, name):
        for event_name, payload in self.events:
            if event_name == name:
                return payload
        return None

    def set_status(self, text, color="gray"):
        self.statuses.append((text, color))

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Permissive()


class FakeFFmpegProcess:
    """The slice of subprocess.Popen that run_ffmpeg_clip() actually uses."""

    def __init__(self, returncode=0, progress=(), stderr_lines=(),
                 spins=2, wait_timeouts=0):
        self._returncode = returncode
        self.returncode = None
        self.stdout = io.StringIO("".join(f"{key}={value}\n" for key, value in progress))
        self.stderr = io.StringIO("".join(line + "\n" for line in stderr_lines))
        self._spins = spins
        self._wait_timeouts = wait_timeouts
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._spins > 0:
            self._spins -= 1
            return None
        self.returncode = self._returncode
        return self._returncode

    def wait(self, timeout=None):
        if self._wait_timeouts > 0:
            self._wait_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        self.returncode = self._returncode
        return self._returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakeCookieJar:
    """Stands in for yt-dlp's cookiejar so the FFmpeg cookie path is exercised."""

    def __init__(self, cookie_text=SECRET_COOKIE):
        self.cookie_text = cookie_text

    def get_cookies_for_url(self, url):
        name, _, value = self.cookie_text.partition("=")
        return [types.SimpleNamespace(name=name, value=value, path="/",
                                      domain=".example")]


def write_partial(output_path, text="partial media"):
    """Create the .part file FFmpeg would be writing, so cleanup is provable."""
    partial = Path(output_path).with_name(
        f"{Path(output_path).stem}.part{Path(output_path).suffix}"
    )
    partial.write_text(text, encoding="utf-8")
    return partial


class LogCapture:
    """Capture records from the `yt_clipper` logger tree.

    Unlike unittest.assertLogs this tolerates *zero* records, which is itself a
    contract assertion here: expected conditions (a private video, an empty
    playlist) must be quiet rather than logged as errors. Propagation is turned
    off for the duration so logging.lastResort does not interleave text with the
    test runner's own output.
    """

    def __init__(self, level=logging.DEBUG):
        self.level = level
        self.records = []
        self._handler = None
        self._saved = None

    def __enter__(self):
        logger = logging.getLogger(LOG_ROOT)
        self._saved = (logger.level, logger.propagate)
        handler = logging.Handler()
        handler.emit = self.records.append
        handler.setLevel(self.level)
        logger.addHandler(handler)
        logger.setLevel(self.level)
        logger.propagate = False
        self._handler = handler
        return self

    def __exit__(self, *exc_info):
        logger = logging.getLogger(LOG_ROOT)
        logger.removeHandler(self._handler)
        logger.level, logger.propagate = self._saved
        return False

    @property
    def messages(self):
        return [record.getMessage() for record in self.records]

    @property
    def levels(self):
        return [record.levelname for record in self.records]

    @property
    def text(self):
        return "\n".join(self.messages)

    def count(self, level_name):
        return self.levels.count(level_name)


class ContractTestCase(unittest.TestCase):
    """Shared scaffolding: temp dir + log inspection helpers."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-contracts-"))

    # -- log helpers --------------------------------------------------------
    def capture(self, level=logging.DEBUG):
        return LogCapture(level)

    @staticmethod
    def messages(cm):
        return cm.messages

    @staticmethod
    def levels(cm):
        return cm.levels

    @staticmethod
    def joined(cm):
        return cm.text

    def assertNoLevel(self, cm, *level_names):
        for record in cm.records:
            self.assertNotIn(record.levelname, level_names,
                             f"unexpected {record.levelname}: {record.getMessage()}")

    # -- ffmpeg helpers -----------------------------------------------------
    def fake_ffmpeg_run(self, output_path, process=None, create_partial=True,
                        formats=None, **kwargs):
        """Run the real run_ffmpeg_clip() against a fake process.

        Returns (result_or_exception, process). The .part file is created when
        FFmpeg "starts" so that cleanup is provable.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = output_path.with_name(
            f"{output_path.stem}.part{output_path.suffix}"
        )
        process = process or FakeFFmpegProcess()
        formats = formats or [
            {"url": "https://media.example/clip?signature=abc123", "vcodec": "h264"}
        ]

        def popen(*args, **popen_kwargs):
            if create_partial:
                partial.write_bytes(b"partial media")
            return process

        with mock.patch.object(ffmpeg_runner.subprocess, "Popen", side_effect=popen):
            try:
                result = ffmpeg_runner.run_ffmpeg_clip(
                    executable="/usr/bin/ffmpeg",
                    formats=formats,
                    output_path=output_path,
                    start_sec=kwargs.pop("start_sec", 0),
                    end_sec=kwargs.pop("end_sec", 10),
                    **kwargs,
                )
            except BaseException as exc:  # the caller asserts on the outcome
                return exc, process
        return result, process

    def partial_for(self, output_path):
        output_path = Path(output_path)
        return output_path.with_name(
            f"{output_path.stem}.part{output_path.suffix}"
        )


# ---------------------------------------------------------------------------
# §1/§2 — one canonical cancellation type, one classification vocabulary
# ---------------------------------------------------------------------------

class TestCanonicalFailureTypes(ContractTestCase):
    def test_1_download_cancelled_is_a_single_class_across_layers(self):
        """isinstance() must work no matter which module the caller imported."""
        from yt_clipper.core import errors
        from yt_clipper.core import ffmpeg_runner as fr
        from yt_clipper.gui.services import download_queue as dq

        self.assertIs(errors.DownloadCancelled, fr.DownloadCancelled)
        self.assertIs(errors.DownloadCancelled, downloader.DownloadCancelled)
        self.assertIs(errors.DownloadCancelled, dq.DownloadCancelled)

    def test_2_cancellation_is_not_a_subclass_of_a_failure_type(self):
        """A cancel must not be catchable as "just another error" by accident."""
        self.assertTrue(issubclass(DownloadCancelled, Exception))
        self.assertNotIsInstance(DownloadCancelled("x"), ValueError)
        self.assertNotIsInstance(DownloadCancelled("x"), OSError)

    def test_3_playlist_tri_state_vocabulary_is_unchanged(self):
        """§4: AVAILABLE / UNAVAILABLE / UNKNOWN must stay three distinct states."""
        self.assertEqual(
            {playlist_utils.AVAILABILITY_AVAILABLE,
             playlist_utils.AVAILABILITY_UNAVAILABLE,
             playlist_utils.AVAILABILITY_UNKNOWN},
            {"available", "unavailable", "unknown"},
        )


# ---------------------------------------------------------------------------
# §3 — cancellation is never swallowed, retried, or misreported
# ---------------------------------------------------------------------------

class TestCancellationContract(ContractTestCase):
    def test_1_retry_wrapper_never_retries_a_cancellation(self):
        calls = []

        def func():
            calls.append(1)
            raise DownloadCancelled("Download cancelled")

        with self.assertRaises(DownloadCancelled), self.capture() as cm:
            downloader._retry_call(func, max_attempts=4, base_delay=0.001)
        self.assertEqual(len(calls), 1, "cancellation was retried")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_2_already_cancelled_operation_does_no_work(self):
        """§3.4: no new work may start after a cancel."""
        calls = []
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DownloadCancelled), self.capture() as cm:
            downloader._retry_call(lambda: calls.append(1), cancel_event=cancel)
        self.assertEqual(calls, [])
        self.assertIn("INFO", self.levels(cm), "cancellation was not recorded")

    def test_3_download_clip_refuses_to_start_when_already_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        patcher, calls = helpers.patch_youtube_dl({VIDEO_URL: {"duration": 10}})
        with patcher, self.assertRaises(DownloadCancelled), self.capture() as cm:
            downloader.download_clip(VIDEO_URL, 0, 5, str(self.tmp / "clip.mp4"),
                                     cancel_event=cancel)
        self.assertEqual(calls, [], "extraction started after cancellation")
        self.assertIn("INFO", self.levels(cm))
        self.assertNoLevel(cm, "ERROR")

    def test_4_cancellation_survives_the_playlist_availability_check(self):
        """§3.3: an ambiguous entry must not turn a cancel into "keep/drop"."""
        entries = [{"id": "ambiguousvid1", "title": None, "availability": None}]
        info = {"_type": "playlist", "title": "P", "entries": entries}

        def cancel_always():
            raise DownloadCancelled("Download cancelled")

        patcher, calls = helpers.patch_youtube_dl({
            PLAYLIST_URL: info,
            "https://www.youtube.com/watch?v=ambiguousvid1": cancel_always,
        })
        with patcher, self.assertRaises(DownloadCancelled):
            downloader.expand_playlist(PLAYLIST_URL)

    def test_5_ffmpeg_cancellation_reports_cancelled_and_leaves_no_partial(self):
        output = self.tmp / "clip.mp4"
        partial = write_partial(output)
        cancel = threading.Event()
        cancel.set()
        process = FakeFFmpegProcess(returncode=0, spins=5)

        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(output, process, cancel_event=cancel)

        self.assertIsInstance(result, DownloadCancelled)
        self.assertFalse(partial.exists(), ".part survived a cancellation")
        self.assertFalse(output.exists(), "a cancelled clip produced an output file")
        self.assertTrue(process.terminated, "FFmpeg was left running after cancel")
        self.assertIn("cancelled", self.joined(cm).lower())
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_6_queue_reports_cancelled_not_failed(self):
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)

        def cancel_download(*args, **kwargs):
            raise DownloadCancelled("Download cancelled")

        job = DownloadJob(id=1, url=VIDEO_URL, label="clip", start_sec=0, end_sec=5,
                          quality="best", audio_only=False,
                          output_path=str(self.tmp / "clip.mp4"))
        with mock.patch.object(downloader, "download_clip", side_effect=cancel_download), \
                self.capture() as cm:
            harness.queue.enqueue(job)
            self.assertTrue(wait_for(lambda: "job_cancelled" in harness.event_names()))

        self.assertNotIn("job_error", harness.event_names(),
                         "a cancellation was reported as a failure")
        self.assertIn("INFO", self.levels(cm))
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_7_cli_batch_stops_on_cancellation_and_does_not_count_it_as_failure(self):
        entries = [playlist_entry("vid1", "one"), playlist_entry("vid2", "two"),
                   playlist_entry("vid3", "three")]
        calls = []

        def download(url, start, end, output_path="clip.mp4", quality="best",
                     audio_only=False, format_id=None, progress_hook=None,
                     cancel_event=None):
            calls.append(url)
            if len(calls) == 2:
                raise DownloadCancelled("Download cancelled")
            return output_path

        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(entries)), \
                mock.patch.object(downloader, "download_clip", side_effect=download):
            code, out, err = run_cli([PLAYLIST_URL, "-o", str(self.tmp / "batch")])

        self.assertEqual(code, 130, f"unexpected exit code; stderr={err}")
        self.assertEqual(len(calls), 2, "the batch kept queueing after a cancel")
        self.assertIn("cancelled", (out + err).lower())
        self.assertNotIn("Traceback", err)
        self.assertIn("1 succeeded, 0 failed", out,
                      "a cancel was counted as a failed video")

    def test_8_cli_interrupt_is_reported_as_cancelled_without_a_traceback(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)), \
                mock.patch.object(downloader, "download_clip", side_effect=interrupt):
            code, out, err = run_cli([VIDEO_URL, "00:05", "00:10",
                                      "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", err)
        self.assertIn("Cancelled", err)

    def test_9_loader_reports_a_cancelled_load_as_cancelled(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)

        with mock.patch.object(downloader, "expand_playlist",
                               side_effect=DownloadCancelled("Download cancelled")), \
                self.capture() as cm:
            loader._load_video_worker(app._load_request_id, VIDEO_URL)

        self.assertIn("video_load_cancelled", app.event_names())
        self.assertNotIn("video_error", app.event_names(),
                         "a cancelled load was reported as an error")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_10_cancelled_load_restores_the_ui_without_error_styling(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_video_load_cancelled(app._load_request_id)

        self.assertEqual(app.statuses[-1][0], "Loading cancelled.")
        self.assertEqual(app.statuses[-1][1], "gray", "cancel shown in a failure colour")
        self.assertIn("cancelled", app.video_info_label.text.lower())
        self.assertEqual(app.video_info_label.configured[-1]["text_color"], "gray")
        self.assertIsNone(app.loaded_playlist_entries)

    def test_11_stale_cancellation_is_ignored(self):
        """A cancel for an older request must not overwrite a newer load."""
        app = FakeLoaderApp(request_id=7)
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_video_load_cancelled(3)
        self.assertEqual(app.statuses, [])

    def test_12_cancellation_during_a_sleep_stops_the_retry_loop(self):
        cancel = threading.Event()
        attempts = []

        def transient():
            attempts.append(1)
            raise RuntimeError("HTTP Error 503: Service Unavailable")

        threading.Timer(0.05, cancel.set).start()

        def fast_sleep(delay, cancel_event):
            # Stand-in for the real backoff: wait for the cancel instead of
            # sleeping it out, so the test stays fast and deterministic.
            cancel_event.wait(5.0)

        with mock.patch.object(downloader, "_sleep_cancellable", fast_sleep):
            with self.assertRaises(DownloadCancelled):
                downloader._retry_call(transient, cancel_event=cancel,
                                       max_attempts=9, base_delay=0.2)
        self.assertLess(len(attempts), 9, "retries continued after cancellation")


# ---------------------------------------------------------------------------
# §4/§5/§6 — playlist availability tri-state, extraction failure, empty result
# ---------------------------------------------------------------------------

class TestPlaylistAvailabilityContracts(ContractTestCase):
    def test_1_entry_is_kept_when_the_availability_check_itself_fails(self):
        """§4: an uninterpretable error must never shrink the playlist."""
        class Exploding(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("classifier exploded")

        entries = [Exploding(id="boom"), playlist_entry("goodvid1", "good")]
        with self.capture() as cm:
            kept = playlist_utils.filter_available_videos(entries)

        self.assertEqual(len(kept), 2, "a raising entry was silently dropped")
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("classifier exploded", self.joined(cm))
        self.assertNoLevel(cm, "ERROR")

    def test_2_explicitly_unavailable_entries_are_dropped_without_an_error_log(self):
        """§18: expected private/deleted videos are not errors."""
        entries = [
            playlist_entry("publicvid1", "public"),
            {"id": "privatevid1", "url": "https://www.youtube.com/watch?v=privatevid1",
             "title": "[Private video]", "availability": "private"},
        ]
        with self.capture() as cm:
            kept = playlist_utils.filter_available_videos(entries)

        self.assertEqual([e["url"] for e in kept],
                         ["https://www.youtube.com/watch?v=publicvid1"])
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_3_unknown_availability_is_never_collapsed_into_unavailable(self):
        thin = {"id": "thinvid1", "url": "https://www.youtube.com/watch?v=thinvid1"}
        self.assertEqual(playlist_utils.classify_video_entry(thin),
                         playlist_utils.AVAILABILITY_UNKNOWN)
        kept = playlist_utils.filter_available_videos([thin])
        self.assertEqual(len(kept), 1, "UNKNOWN was collapsed into a drop")

    def test_4_inconclusive_full_check_keeps_the_entry_and_is_observable(self):
        entries = [{"id": "geovid1", "title": None, "availability": None}]
        info = {"_type": "playlist", "title": "P", "entries": entries}
        geo_url = "https://www.youtube.com/watch?v=geovid1"
        patcher, calls = helpers.patch_youtube_dl({
            PLAYLIST_URL: info,
            geo_url: DownloadError("The uploader has not made this video available "
                                   "in your country"),
        })
        with patcher, self.capture() as cm:
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual([e["url"] for e in result["entries"]], [geo_url])
        self.assertIn("WARNING", self.levels(cm))
        self.assertNoLevel(cm, "ERROR")

    def test_5_unexpected_error_during_full_check_keeps_the_entry(self):
        entries = [{"id": "oddvid1", "title": None, "availability": None}]
        info = {"_type": "playlist", "title": "P", "entries": entries}
        patcher, _ = helpers.patch_youtube_dl({
            PLAYLIST_URL: info,
            "https://www.youtube.com/watch?v=oddvid1": RuntimeError("socket went away"),
        })
        with patcher, self.capture() as cm:
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual(len(result["entries"]), 1)
        self.assertIn("keeping the entry", self.joined(cm))
        self.assertIn("WARNING", self.levels(cm))

    def test_6_extraction_failure_raises_instead_of_returning_an_empty_list(self):
        """§5: [] would be indistinguishable from "playlist has no videos"."""
        patcher, _ = helpers.patch_youtube_dl({
            PLAYLIST_URL: DownloadError("Unable to extract playlist data"),
        })
        with patcher, self.assertRaises(DownloadError):
            downloader.expand_playlist(PLAYLIST_URL)

    def test_7_extraction_failure_becomes_a_user_visible_failure_in_the_cli(self):
        patcher, _ = helpers.patch_youtube_dl({
            PLAYLIST_URL: DownloadError("Unable to extract playlist data"),
        })
        with patcher:
            code, out, err = run_cli([PLAYLIST_URL, "-o", str(self.tmp / "batch")])
        self.assertEqual(code, 1)
        self.assertIn("Unable to extract playlist data", err)
        self.assertNotIn("Traceback", err)

    def test_8_extraction_failure_becomes_video_error_in_the_loader(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        with mock.patch.object(downloader, "expand_playlist",
                               side_effect=DownloadError("Unable to extract")), \
                self.capture() as cm:
            loader._load_video_worker(app._load_request_id, PLAYLIST_URL)

        self.assertIn("video_error", app.event_names())
        self.assertNotIn("playlist_metadata", app.event_names())
        self.assertIn("ERROR", self.levels(cm), "a failed load was not logged")
        message = app.payload("video_error")[1]
        self.assertIn("Unable to extract", message)
        self.assertNotIn("Traceback", message, "a traceback reached the user")

    def test_9_fully_unavailable_playlist_is_expected_empty_not_fatal(self):
        """§2/§6: EXPECTED_EMPTY is a valid terminal outcome."""
        entries = [{"id": "p1", "url": "https://www.youtube.com/watch?v=p1",
                    "title": "[Private video]", "availability": "private"}]
        info = {"_type": "playlist", "title": "Secrets", "entries": entries}
        patcher, _ = helpers.patch_youtube_dl({PLAYLIST_URL: info})
        with patcher, self.capture() as cm:
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual(result["entries"], [])
        self.assertTrue(result["is_playlist"])
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_10_expected_empty_playlist_is_explained_not_reported_as_an_error(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        result = flat_playlist([], title="All private")
        with mock.patch.object(downloader, "expand_playlist", return_value=result), \
                self.capture() as cm:
            loader._load_video_worker(app._load_request_id, PLAYLIST_URL)

        self.assertIn("playlist_metadata", app.event_names())
        self.assertNotIn("video_error", app.event_names())
        self.assertNoLevel(cm, "ERROR")

    def test_11_expected_empty_playlist_applies_a_clear_ui_state(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_playlist_metadata(app._load_request_id, PLAYLIST_URL,
                                       "All private", [])
        self.assertIn("No available videos", app.video_info_label.text)
        self.assertEqual(app.loaded_playlist_entries, [])
        self.assertEqual(app.statuses[-1][1], "#e6a817",
                         "an empty playlist was styled as a hard failure")

    def test_12_truly_empty_listing_is_still_a_failure(self):
        """A playlist that lists nothing at all is not the same as "all private"."""
        info = {"_type": "playlist", "title": "Empty", "entries": []}
        patcher, _ = helpers.patch_youtube_dl({PLAYLIST_URL: info})
        with patcher, self.assertRaises(ValueError):
            downloader.expand_playlist(PLAYLIST_URL)

    def test_13_malformed_entries_are_skipped_observably(self):
        info = {"_type": "playlist", "title": "P", "entries": [
            None,
            {"title": "no id at all"},
            {"id": "okvid1", "title": "ok", "availability": "public"},
        ]}
        patcher, _ = helpers.patch_youtube_dl({PLAYLIST_URL: info})
        with patcher, self.capture() as cm:
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual(len(result["entries"]), 1, "a malformed entry vanished silently")
        self.assertEqual(self.levels(cm).count("WARNING"), 2)

    def test_14_secondary_filter_failure_fails_open_observably(self):
        """§4/§21: a broken safety-net filter must not empty a valid playlist."""
        entries = [playlist_entry("a1", "alpha"), playlist_entry("b2", "beta")]
        with mock.patch.object(video_loader_module, "filter_available_videos",
                               side_effect=RuntimeError("filter exploded")), \
                self.capture() as cm:
            app = FakeLoaderApp()
            loader = video_loader_module.VideoLoaderController(app)
            loader._apply_playlist_metadata(app._load_request_id, PLAYLIST_URL,
                                            "P", entries)

        self.assertEqual(len(app.loaded_playlist_entries), 2,
                         "the fail-open fallback dropped videos")
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("filter exploded", self.joined(cm))
# ---------------------------------------------------------------------------
# §8 — FFmpeg: exit code, stderr diagnostics, timeout, cancellation, no secrets
# ---------------------------------------------------------------------------

class TestFFmpegContracts(ContractTestCase):
    def test_1_exit_zero_is_success_and_the_partial_becomes_the_output(self):
        output = self.tmp / "clip.mp4"
        progress = []
        process = FakeFFmpegProcess(returncode=0,
                                    progress=[("out_time_us", "5000000"),
                                              ("progress", "end")])
        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(
                output, process,
                progress_hook=lambda data: progress.append(data),
            )

        self.assertIsNone(result, "a successful run must not raise")
        self.assertTrue(output.exists(), "SUCCESS did not produce the output file")
        self.assertFalse(self.partial_for(output).exists(), ".part was left behind")
        self.assertEqual(progress[-1]["status"], "finished")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_2_non_zero_exit_is_a_failure_carrying_the_stderr_tail(self):
        output = self.tmp / "clip.mp4"
        process = FakeFFmpegProcess(returncode=1,
                                    stderr_lines=["Conversion failed!",
                                                  "Invalid data found when processing input"])
        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(output, process)

        self.assertIsInstance(result, RuntimeError)
        self.assertIn("Invalid data found", str(result), "the stderr tail was lost")
        self.assertFalse(output.exists(), "a failed run produced an output file")
        self.assertFalse(self.partial_for(output).exists(),
                         "a failed run left a .part behind")
        self.assertIn("ERROR", self.levels(cm))
        self.assertIn("exit code 1", self.joined(cm))

    def test_3_failure_diagnostics_never_carry_secrets(self):
        output = self.tmp / "clip.mp4"
        process = FakeFFmpegProcess(
            returncode=1,
            stderr_lines=[f"Server returned 403 for url?signature=SECRET_SIG&sapisid=x",
                          f"Cookie: {SECRET_COOKIE}",
                          "HTTP error 403 Forbidden"],
        )
        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(output, process)

        self.assertIsInstance(result, RuntimeError)
        for text in (str(result), self.joined(cm)):
            self.assertNotIn("SuperSecretValue123", text)
            self.assertNotIn("SECRET_SIG", text)
        self.assertIn("403 Forbidden", str(result), "useful diagnostics were lost")

    def test_4_the_command_line_is_never_logged(self):
        """§8/§18: headers, cookies and signed URLs stay out of the log."""
        output = self.tmp / "clip.mp4"
        formats = [{
            "url": "https://media.example/clip?signature=TOPSECRET&expire=1",
            "vcodec": "h264",
            "http_headers": {"Cookie": SECRET_COOKIE, "User-Agent": "AgentX/9.9"},
        }]
        process = FakeFFmpegProcess(returncode=0, progress=[("progress", "end")])
        with self.capture() as cm:
            self.fake_ffmpeg_run(output, process, formats=formats,
                                 cookiejar=FakeCookieJar())

        logged = self.joined(cm)
        self.assertNotIn("TOPSECRET", logged)
        self.assertNotIn("SuperSecretValue123", logged)
        self.assertNotIn("-headers", logged)
        self.assertNotIn("-cookies", logged)
        self.assertIn("FFmpeg started", logged, "the run itself was not recorded")

    def test_5_cancellation_mid_run_stops_ffmpeg_and_discards_the_partial(self):
        output = self.tmp / "clip.mp4"
        cancel = threading.Event()
        process = FakeFFmpegProcess(returncode=0, spins=50)
        threading.Timer(0.05, cancel.set).start()

        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(output, process, cancel_event=cancel)

        self.assertIsInstance(result, DownloadCancelled)
        self.assertTrue(process.terminated, "FFmpeg kept running after a cancel")
        self.assertFalse(self.partial_for(output).exists())
        self.assertFalse(output.exists())
        self.assertNoLevel(cm, "ERROR")

    def test_6_stop_process_escalates_from_terminate_to_kill(self):
        process = FakeFFmpegProcess(returncode=0, wait_timeouts=1)
        with self.capture() as cm:
            ffmpeg_runner._stop_process(process)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed, "a process ignoring terminate was not killed")
        self.assertIn("WARNING", self.levels(cm))

    def test_7_a_stuck_process_never_replaces_the_real_outcome(self):
        """§8: cleanup trouble must not mask CANCELLED with TimeoutExpired."""
        output = self.tmp / "clip.mp4"
        cancel = threading.Event()
        cancel.set()
        process = FakeFFmpegProcess(returncode=0, spins=5, wait_timeouts=99)

        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(output, process, cancel_event=cancel)

        self.assertIsInstance(result, DownloadCancelled,
                              "cleanup replaced the cancellation outcome")
        self.assertTrue(process.killed)
        self.assertIn("ERROR", self.levels(cm), "the stuck process went unreported")
        self.assertIn("still running after kill", self.joined(cm))

    def test_8_unparsable_progress_timestamps_do_not_fail_the_clip(self):
        output = self.tmp / "clip.mp4"
        process = FakeFFmpegProcess(returncode=0,
                                    progress=[("out_time_us", "not-a-number"),
                                              ("out_time", "00:00:03.50"),
                                              ("progress", "end")])
        seen = []
        with self.capture() as cm:
            result, process = self.fake_ffmpeg_run(
                output, process, progress_hook=lambda data: seen.append(data))

        self.assertIsNone(result)
        self.assertTrue(output.exists())
        self.assertIn("DEBUG", self.levels(cm), "the odd timestamp went unreported")
        self.assertNoLevel(cm, "ERROR", "WARNING")
        self.assertTrue(any(item["status"] == "downloading" for item in seen))

    def test_9_an_unreadable_partial_does_not_break_progress_reporting(self):
        """A missing byte count is presentation-only: the hook still fires."""
        class UnreadablePath:
            name = "clip.part.mp4"

            def stat(self):
                raise OSError("device gone")

        seen = []
        with self.capture() as cm:
            ffmpeg_runner._emit_progress(
                lambda data: seen.append(data), 0.5, UnreadablePath(), 10.0,
                {"speed": "2.0x"},
            )

        self.assertEqual(len(seen), 1, "progress stopped because stat failed")
        self.assertEqual(seen[0]["downloaded_bytes"], 0)
        self.assertIn("DEBUG", self.levels(cm), "the stat failure went unreported")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_10_preflight_failures_never_start_a_process(self):
        output = self.tmp / "clip.mp4"
        with mock.patch.object(ffmpeg_runner.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                ffmpeg_runner.run_ffmpeg_clip("/usr/bin/ffmpeg", [{"url": "https://x/y"}],
                                              output, 10, 10)
            with self.assertRaises(ValueError):
                ffmpeg_runner.run_ffmpeg_clip("/usr/bin/ffmpeg", [], output, 0, 10)
            with self.assertRaises(ValueError):
                ffmpeg_runner.run_ffmpeg_clip("/usr/bin/ffmpeg", [{"url": None}],
                                              output, 0, 10)
        popen.assert_not_called()
        self.assertFalse(self.partial_for(output).exists())


# ---------------------------------------------------------------------------
# §7 — download_clip(): SUCCESS / CANCELLED / FAILURE, nothing else
# ---------------------------------------------------------------------------

class TestDownloadClipContracts(ContractTestCase):
    def info(self, duration=100.0, **extra):
        payload = {
            "id": "contractvid1",
            "title": "Contract video",
            "duration": duration,
            "http_headers": {"Cookie": SECRET_COOKIE},
            "requested_formats": [
                {"url": "https://media.example/v?signature=SIGSECRET", "vcodec": "h264"},
            ],
        }
        payload.update(extra)
        return payload

    def run_download(self, script, ffmpeg_outcome=None, **kwargs):
        """Patch the yt-dlp + FFmpeg boundaries and call the real download_clip."""
        recorded = {}

        def fake_run_ffmpeg_clip(executable, formats, output_path, start_sec, end_sec,
                                 audio_only=False, progress_hook=None,
                                 cancel_event=None, cookiejar=None, proxy=None):
            recorded.update({"formats": formats, "start": start_sec, "end": end_sec,
                             "output": output_path, "cookiejar": cookiejar})
            if isinstance(ffmpeg_outcome, BaseException):
                raise ffmpeg_outcome
            Path(output_path).write_bytes(b"media")
            return None

        patcher, calls = helpers.patch_youtube_dl(script)
        with patcher, \
                mock.patch.object(downloader, "FFmpegPostProcessor", FakeFFmpegPostProcessor), \
                mock.patch.object(downloader, "run_ffmpeg_clip", side_effect=fake_run_ffmpeg_clip):
            try:
                result = downloader.download_clip(**kwargs)
            except BaseException as exc:
                return exc, calls, recorded
        return result, calls, recorded

    def test_1_success_returns_the_output_path(self):
        output = self.tmp / "clip.mp4"
        with self.capture() as cm:
            result, calls, recorded = self.run_download(
                {VIDEO_URL: self.info()},
                url=VIDEO_URL, start_sec=10, end_sec=40, output_path=str(output),
            )

        self.assertEqual(result, str(output))
        self.assertEqual((recorded["start"], recorded["end"]), (10.0, 40.0))
        self.assertTrue(output.exists())
        self.assertIn("INFO", self.levels(cm), "a successful clip was not recorded")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_2_invalid_range_is_a_failure_before_any_network_call(self):
        patcher, calls = helpers.patch_youtube_dl({})
        with patcher, self.assertRaises(ValueError):
            downloader.download_clip(VIDEO_URL, 40, 40, str(self.tmp / "clip.mp4"))
        self.assertEqual(calls, [])

    def test_3_cancellation_propagates_unchanged_out_of_download_clip(self):
        output = self.tmp / "clip.mp4"
        with self.capture() as cm:
            result, calls, recorded = self.run_download(
                {VIDEO_URL: self.info()},
                ffmpeg_outcome=DownloadCancelled("Download cancelled"),
                url=VIDEO_URL, start_sec=0, end_sec=10, output_path=str(output),
            )

        self.assertIsInstance(result, DownloadCancelled)
        self.assertFalse(output.exists(), "a cancelled clip left an artefact behind")
        self.assertNoLevel(cm, "ERROR")

    def test_4_an_unavailable_entry_passed_as_url_is_refused(self):
        entry = {"id": "priv1", "url": "https://www.youtube.com/watch?v=priv1",
                 "title": "[Private video]", "availability": "private"}
        patcher, calls = helpers.patch_youtube_dl({})
        with patcher, self.assertRaises(ValueError) as ctx:
            downloader.download_clip(entry, 0, 5, str(self.tmp / "clip.mp4"))
        self.assertIn("unavailable", str(ctx.exception))
        self.assertEqual(calls, [], "an unavailable video was fetched anyway")

    def test_5_a_non_http_url_is_refused(self):
        patcher, calls = helpers.patch_youtube_dl({})
        with patcher, self.assertRaises(ValueError) as ctx:
            downloader.download_clip("not-a-url", 0, 5, str(self.tmp / "clip.mp4"))
        self.assertIn("Invalid download URL", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_6_a_broken_safety_net_fails_open_observably(self):
        class ExplodingUrl(str):
            """A URL whose own validation raises, so the guard cannot run."""

            def startswith(self, *args, **kwargs):
                raise RuntimeError("guard exploded")

        output = self.tmp / "clip.mp4"
        with self.capture() as cm:
            result, calls, recorded = self.run_download(
                {VIDEO_URL: self.info()},
                url=ExplodingUrl(VIDEO_URL), start_sec=0, end_sec=10,
                output_path=str(output),
            )

        self.assertEqual(result, str(output), "a broken guard aborted the download")
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("guard exploded", self.joined(cm))
        self.assertNoLevel(cm, "ERROR")

    def test_7_full_video_download_needs_a_usable_duration(self):
        output = self.tmp / "clip.mp4"
        result, calls, recorded = self.run_download(
            {VIDEO_URL: self.info(duration=None)},
            url=VIDEO_URL, start_sec=None, end_sec=None, output_path=str(output),
        )
        self.assertIsInstance(result, ValueError)
        self.assertIn("duration", str(result))

    def test_8_full_video_download_resolves_the_real_duration(self):
        output = self.tmp / "clip.mp4"
        result, calls, recorded = self.run_download(
            {VIDEO_URL: self.info(duration=123.0)},
            url=VIDEO_URL, start_sec=None, end_sec=None, output_path=str(output),
        )
        self.assertEqual(result, str(output))
        self.assertEqual((recorded["start"], recorded["end"]), (0.0, 123.0))

    def test_9_an_unusable_format_id_is_reported_with_a_reason(self):
        output = self.tmp / "clip.mp4"
        with self.capture() as cm:
            result, calls, recorded = self.run_download(
                {VIDEO_URL: DownloadError("requested format not available")},
                url=VIDEO_URL, start_sec=0, end_sec=10, output_path=str(output),
                format_id="999",
            )

        self.assertIsInstance(result, ValueError)
        self.assertIn("999", str(result))
        self.assertIn("WARNING", self.levels(cm))

    def test_10_missing_ffmpeg_is_a_clear_fatal_failure(self):
        class NoFFmpeg(FakeFFmpegPostProcessor):
            available = False

        output = self.tmp / "clip.mp4"
        patcher, calls = helpers.patch_youtube_dl({VIDEO_URL: self.info()})
        with patcher, mock.patch.object(downloader, "FFmpegPostProcessor", NoFFmpeg), \
                self.assertRaises(RuntimeError) as ctx:
            downloader.download_clip(VIDEO_URL, 0, 10, str(output))
        self.assertIn("FFmpeg was not found", str(ctx.exception))

    def test_11_retry_notifications_reach_the_hook_without_secrets(self):
        output = self.tmp / "clip.mp4"
        seen = []
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise DownloadError(
                    "HTTP Error 503 while requesting "
                    "https://media.example/v?signature=SIGSECRET&sapisid=abc")
            return self.info()

        with mock.patch.object(downloader.time, "sleep", lambda seconds: None), \
                self.capture() as cm:
            result, calls, recorded = self.run_download(
                {VIDEO_URL: flaky},
                url=VIDEO_URL, start_sec=0, end_sec=10, output_path=str(output),
                progress_hook=lambda data: seen.append(data),
            )

        self.assertEqual(result, str(output))
        retries = [item for item in seen if item["status"] == "retrying"]
        self.assertEqual(len(retries), 1, "the retry was not surfaced")
        self.assertNotIn("SIGSECRET", retries[0]["error"])
        self.assertIn("503", retries[0]["error"], "the retry reason was lost")
        self.assertIn("DEBUG", self.levels(cm))


# ---------------------------------------------------------------------------
# §10 — retry only retryables; exhaustion is observable
# ---------------------------------------------------------------------------

class TestRetryContracts(ContractTestCase):
    def test_1_transient_failure_is_retried_and_can_succeed(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("HTTP Error 503: Service Unavailable")
            return "ok"

        with mock.patch.object(downloader.time, "sleep", lambda seconds: None), \
                self.capture() as cm:
            result = downloader._retry_call(flaky, max_attempts=4, base_delay=0.001)

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(cm.count("DEBUG"), 2, "retries were not recorded")
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_2_exhausted_retries_are_observable_and_the_error_survives(self):
        def always_transient():
            raise RuntimeError("HTTP Error 503: Service Unavailable")

        with mock.patch.object(downloader.time, "sleep", lambda seconds: None), \
                self.capture() as cm:
            with self.assertRaises(RuntimeError):
                downloader._retry_call(always_transient, max_attempts=3, base_delay=0.001)

        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("Giving up after 3 attempt(s)", self.joined(cm))
        self.assertIn("503", self.joined(cm))

    def test_3_non_transient_failures_are_not_retried(self):
        attempts = []

        def fatal():
            attempts.append(1)
            raise ValueError("Video unavailable")

        with self.capture() as cm:
            with self.assertRaises(ValueError):
                downloader._retry_call(fatal, max_attempts=5, base_delay=0.001)

        self.assertEqual(len(attempts), 1, "a permanent failure was retried")
        self.assertNotIn("Giving up", self.joined(cm))
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_4_retry_backoff_is_announced_to_the_caller(self):
        notified = []
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("connection reset by peer")
            return "ok"

        with mock.patch.object(downloader.time, "sleep", lambda seconds: None):
            downloader._retry_call(
                flaky,
                on_retry=lambda attempt, maximum, delay, exc: notified.append(
                    (attempt, maximum, delay, str(exc))),
                max_attempts=3, base_delay=0.001,
            )

        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0][0], 1)
        self.assertEqual(notified[0][1], 3)


# ---------------------------------------------------------------------------
# §9/§16/§21 — queue terminal states, isolation, and honest reporting
# ---------------------------------------------------------------------------

class TestQueueContracts(ContractTestCase):
    def make_job(self, job_id, name="clip"):
        return DownloadJob(id=job_id, url=f"https://www.youtube.com/watch?v={name}",
                           label=name, start_sec=0, end_sec=5, quality="best",
                           audio_only=False, output_path=str(self.tmp / f"{name}.mp4"))

    def test_1_one_failing_video_does_not_abort_the_rest_of_the_batch(self):
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)
        done = []

        def download(url, start, end, output_path="clip.mp4", **kwargs):
            done.append(url)
            if "two" in output_path:
                raise RuntimeError("HTTP Error 403: Forbidden")
            Path(output_path).write_bytes(b"media")
            return output_path

        with mock.patch.object(downloader, "download_clip", side_effect=download), \
                self.capture() as cm:
            for job_id, name in ((1, "one"), (2, "two"), (3, "three")):
                harness.queue.enqueue(self.make_job(job_id, name))
            self.assertTrue(wait_for(lambda: harness.event_names().count("queue_idle") >= 1))

        self.assertEqual(len(done), 3, "the batch stopped after one failure")
        self.assertEqual(harness.event_names().count("job_done"), 2)
        self.assertEqual(harness.event_names().count("job_error"), 1)
        self.assertEqual(harness.payloads("job_error")[0][0], 2)
        self.assertIn("ERROR", self.levels(cm))
        self.assertIn("403", self.joined(cm))

    def test_2_a_failed_job_is_never_reported_as_completed(self):
        """§21: the invariant that matters most."""
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)

        def fail(url, start, end, output_path="clip.mp4", **kwargs):
            raise RuntimeError("disk full")

        with mock.patch.object(downloader, "download_clip", side_effect=fail), \
                self.capture():
            harness.queue.enqueue(self.make_job(1, "one"))
            self.assertTrue(wait_for(lambda: "job_error" in harness.event_names()))

        self.assertNotIn("job_done", harness.event_names())
        self.assertFalse((self.tmp / "one.mp4").exists())

    def test_3_the_failure_message_shown_to_users_is_concise_and_redacted(self):
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)

        def fail(url, start, end, output_path="clip.mp4", **kwargs):
            def deeper():
                raise RuntimeError(
                    f"HTTP Error 403 for cookie {SECRET_COOKIE} "
                    "and signature=SUPER_SIG")
            try:
                deeper()
            except RuntimeError:
                raise

        with mock.patch.object(downloader, "download_clip", side_effect=fail), \
                self.capture():
            harness.queue.enqueue(self.make_job(1, "one"))
            self.assertTrue(wait_for(lambda: "job_error" in harness.event_names()))

        message = harness.payloads("job_error")[0][1]
        self.assertNotIn("Traceback", message)
        self.assertNotIn("File \"", message, "a traceback frame reached the user")
        self.assertNotIn("SuperSecretValue123", message)
        self.assertNotIn("SUPER_SIG", message)
        self.assertIn("403", message, "the human-readable reason was lost")
        self.assertEqual(len(message.splitlines()), 1, "the message is not concise")

    def test_4_a_broken_listener_cannot_turn_success_into_failure(self):
        failures = []

        def listener(name, payload):
            if name == "job_done" and payload == (1,):
                raise RuntimeError("widget exploded")

        harness = QueueHarness(listener=listener)
        self.addCleanup(harness.shutdown)

        def download(url, start, end, output_path="clip.mp4", **kwargs):
            Path(output_path).write_bytes(b"media")
            return output_path

        with mock.patch.object(downloader, "download_clip", side_effect=download), \
                self.capture() as cm:
            harness.queue.enqueue(self.make_job(1, "one"))
            harness.queue.enqueue(self.make_job(2, "two"))
            self.assertTrue(wait_for(lambda: harness.event_names().count("job_done") >= 2
                                     or "queue_idle" in harness.event_names()))

        self.assertNotIn("job_error", harness.event_names(),
                         "a UI bug was reported as a download failure")
        self.assertTrue((self.tmp / "one.mp4").exists())
        self.assertTrue((self.tmp / "two.mp4").exists(),
                        "a UI bug stopped the rest of the queue")
        self.assertTrue(any(record.exc_info for record in cm.records),
                        "the listener bug was swallowed without a traceback")

    def test_5_a_broken_progress_listener_does_not_abort_the_download(self):
        def listener(name, payload):
            if name == "download_progress":
                raise RuntimeError("progress bar exploded")

        harness = QueueHarness(listener=listener)
        self.addCleanup(harness.shutdown)

        def download(url, start, end, output_path="clip.mp4", progress_hook=None, **kwargs):
            if progress_hook:
                progress_hook({"status": "downloading", "_percent_str": "10%"})
            Path(output_path).write_bytes(b"media")
            return output_path

        with mock.patch.object(downloader, "download_clip", side_effect=download), \
                self.capture() as cm:
            harness.queue.enqueue(self.make_job(1, "one"))
            self.assertTrue(wait_for(lambda: "job_done" in harness.event_names()))

        self.assertNotIn("job_error", harness.event_names())
        self.assertNoLevel(cm, "ERROR")

    def test_6_an_unwritable_output_folder_fails_only_that_job(self):
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)
        blocked = self.tmp / "blocked.mp4"

        calls = []

        def download(url, start, end, output_path="clip.mp4", **kwargs):
            calls.append(output_path)
            Path(output_path).write_bytes(b"media")
            return output_path

        job_one = self.make_job(1, "one")
        job_one.output_path = str(self.tmp / "nope" / "clip.mp4")

        real_mkdir = Path.mkdir

        def fake_mkdir(self_path, *args, **kwargs):
            if "nope" in str(self_path):
                raise OSError("read-only file system")
            return real_mkdir(self_path, *args, **kwargs)

        with mock.patch.object(downloader, "download_clip", side_effect=download), \
                mock.patch.object(download_queue_module.Path, "mkdir", fake_mkdir), \
                self.capture() as cm:
            harness.queue.enqueue(job_one)
            harness.queue.enqueue(self.make_job(2, "two"))
            self.assertTrue(wait_for(lambda: "queue_idle" in harness.event_names()))

        self.assertEqual(harness.event_names().count("job_error"), 1)
        self.assertIn("read-only file system", harness.payloads("job_error")[0][1])
        self.assertIn("ERROR", self.levels(cm))

    def test_7_cancelling_a_pending_job_never_starts_it(self):
        """§3.4: no new work after a cancel."""
        gate = threading.Event()
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)
        recorder = DownloadCallRecorder(gate=gate)

        with recorder.patch():
            harness.queue.enqueue(self.make_job(1, "one"))
            harness.queue.enqueue(self.make_job(2, "two"))
            self.assertTrue(wait_for(lambda: "job_started" in harness.event_names()))
            self.assertEqual(harness.queue.cancel(2), "pending")
            self.assertIn("job_cancelled", harness.event_names())
            gate.set()
            self.assertTrue(wait_for(lambda: "queue_idle" in harness.event_names()))

        self.assertEqual(len(recorder.calls), 1, "a cancelled pending job still ran")

    def test_8_cancelling_the_active_job_is_reported_as_cancelled(self):
        gate = threading.Event()
        harness = QueueHarness()
        self.addCleanup(harness.shutdown)
        recorder = DownloadCallRecorder(gate=gate)

        with recorder.patch(), self.capture() as cm:
            harness.queue.enqueue(self.make_job(1, "one"))
            self.assertTrue(wait_for(lambda: "job_started" in harness.event_names()))
            self.assertEqual(harness.queue.cancel(1), "active")
            gate.set()
            self.assertTrue(wait_for(lambda: "job_cancelled" in harness.event_names()))

        self.assertNotIn("job_error", harness.event_names())
        self.assertNotIn("job_done", harness.event_names())
        self.assertNoLevel(cm, "ERROR", "WARNING")
# ---------------------------------------------------------------------------
# §11 — thumbnails and other decorative failures are recoverable, not silent
# ---------------------------------------------------------------------------

class TestDecorativeFailureContracts(ContractTestCase):
    def test_1_thumbnail_fetch_failure_is_recoverable_and_recorded(self):
        loader = video_loader_module.VideoLoaderController(FakeLoaderApp())
        # Pillow may be absent (then Image is None and the fetch is skipped), so
        # stand one in to exercise the real network/decode path in every config.
        fake_pil = mock.MagicMock()
        with mock.patch.object(video_loader_module, "Image", fake_pil), \
                mock.patch.object(video_loader_module.urllib.request, "urlopen",
                                  side_effect=OSError("no route to host")), \
                self.capture() as cm:
            image, failed = loader._fetch_thumbnail("https://i.ytimg.com/vi/x/hq.jpg")

        self.assertIsNone(image)
        self.assertTrue(failed, "the UI would not learn the thumbnail is missing")
        self.assertIn("DEBUG", self.levels(cm))
        self.assertIn("no route to host", self.joined(cm))
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_1b_a_missing_pillow_degrades_to_no_thumbnail(self):
        """§11: an absent optional dependency is not an error."""
        loader = video_loader_module.VideoLoaderController(FakeLoaderApp())
        with mock.patch.object(video_loader_module, "Image", None), self.capture() as cm:
            image, failed = loader._fetch_thumbnail("https://i.ytimg.com/vi/x/hq.jpg")

        self.assertIsNone(image)
        self.assertTrue(failed)
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_2_a_thumbnail_failure_never_fails_the_load(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        result = single_video_result(VIDEO_URL)
        with mock.patch.object(downloader, "expand_playlist", return_value=result), \
                mock.patch.object(video_loader_module.VideoLoaderController,
                                  "_fetch_thumbnail",
                                  side_effect=RuntimeError("decoder exploded")), \
                self.capture() as cm:
            loader._load_video_worker(app._load_request_id, VIDEO_URL)

        self.assertIn("video_metadata", app.event_names(),
                      "a thumbnail problem aborted the load")
        self.assertIn("video_thumbnail", app.event_names())
        self.assertNotIn("video_error", app.event_names(),
                         "a decorative failure was reported as a failed load")
        self.assertEqual(app.payload("video_thumbnail")[2], True,
                         "the UI was not told the thumbnail is missing")
        self.assertIn("WARNING", self.levels(cm), "the thumbnail failure was silent")
        self.assertNoLevel(cm, "ERROR")

    def test_3_opening_a_video_in_the_browser_fails_open_but_loudly(self):
        from yt_clipper.gui.widgets.playlist_preview import PlaylistPreviewWidget

        with mock.patch("webbrowser.open", side_effect=OSError("no browser")), \
                self.capture() as cm:
            PlaylistPreviewWidget._open_url("https://www.youtube.com/watch?v=abc")
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("no browser", self.joined(cm))

        with mock.patch("webbrowser.open", return_value=False), self.capture() as cm:
            PlaylistPreviewWidget._open_url("https://www.youtube.com/watch?v=abc")
        self.assertIn("WARNING", self.levels(cm), "a click that did nothing was silent")

    def test_4_a_broken_preview_sort_keeps_every_video(self):
        """§12: ordering is optional, the videos are not."""
        entries = [playlist_entry("a1", "alpha"), playlist_entry("b2", "beta")]
        with mock.patch.object(playlist_utils, "extract_publish_date",
                               side_effect=RuntimeError("date exploded")), \
                self.capture() as cm:
            kept = playlist_utils.sort_videos_by_publish_date(entries)

        self.assertEqual(len(kept), 2, "a sorting failure dropped videos")
        self.assertIn("DEBUG", self.levels(cm))
        self.assertNoLevel(cm, "ERROR")

    def test_5_an_unformattable_publish_date_falls_back_to_text(self):
        class ExplodingDate:
            def strftime(self, fmt):
                raise ValueError("year out of range")

        with mock.patch.object(playlist_utils, "parse_publish_date",
                               return_value=ExplodingDate()), self.capture() as cm:
            text = playlist_utils.format_publish_date("20240101")

        self.assertEqual(text, "Unknown date")
        self.assertIn("DEBUG", self.levels(cm))

    def test_6_a_missing_publish_date_is_recoverable_and_reported(self):
        """§12: a missing publish date is recoverable and reported once.

        The canonical failure must stay recoverable (None, never a fatal
        download failure) and stay observable at WARNING, naming the real
        cause. The second "legacy location" attempt is gone: core.utils
        re-exports the very same function object, so that retry could only
        re-raise what the canonical call had just raised - it never recovered
        anything, it only duplicated the diagnostic. What is frozen here is
        the contract, not the dead retry.
        """
        from yt_clipper.core import utils as legacy_utils

        # Stands in for the retired legacy import location: if production ever
        # consults it again, this records the call instead of silently raising.
        legacy_attempt = mock.Mock(side_effect=RuntimeError("legacy fallback ran"))
        with mock.patch.object(playlist_utils, "extract_publish_date",
                               side_effect=RuntimeError("canonical date failure")), \
                mock.patch.object(legacy_utils, "extract_publish_date", legacy_attempt), \
                self.capture() as cm:
            value = downloader._extract_publish_date_from_info({"id": "x"})

        self.assertIsNone(value, "optional metadata became a hard failure")
        self.assertIn("WARNING", self.levels(cm), "the recoverable failure was silent")
        self.assertIn("continuing without it", self.joined(cm))
        self.assertIn("canonical date failure", self.joined(cm),
                      "the warning does not name the real cause")
        self.assertNotIn("legacy fallback ran", self.joined(cm),
                         "the retired legacy location was consulted")
        self.assertNoLevel(cm, "ERROR", "a recoverable absence was escalated")
        self.assertNotIn("DEBUG", self.levels(cm),
                         "the impossible legacy fallback attempt still runs")
        legacy_attempt.assert_not_called()

        # User-visible behaviour is unchanged: every other metadata field still
        # arrives through the public entry point, and only the optional date is
        # absent - the failure is contained to its own field.
        patcher, _ = helpers.patch_youtube_dl({VIDEO_URL: {
            "id": "contractvid1",
            "title": "Boundary video",
            "duration": 120,
            "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
            "upload_date": "20240101",
            "timestamp": 1704067200,
        }})
        with patcher, mock.patch.object(playlist_utils, "extract_publish_date",
                                        side_effect=RuntimeError("canonical date failure")), \
                self.capture() as cm:
            info = downloader.get_video_info(VIDEO_URL)

        self.assertEqual(info["title"], "Boundary video")
        self.assertEqual(info["duration"], 120)
        self.assertEqual(info["thumbnail"], "https://i.ytimg.com/vi/x/hq.jpg")
        self.assertEqual(info["upload_date"], "20240101")
        self.assertEqual(info["timestamp"], 1704067200)
        self.assertIsNone(info["publish_date"],
                          "an unrelated metadata field absorbed the failure")
        self.assertNoLevel(cm, "ERROR")


# ---------------------------------------------------------------------------
# §13/§14 — update check and JS runtime detection never interfere
# ---------------------------------------------------------------------------

class TestOptionalSubsystemContracts(ContractTestCase):
    def test_1_update_check_failure_degrades_to_no_update_known(self):
        with mock.patch.object(updater.urllib.request, "urlopen",
                               side_effect=OSError("network blocked")), \
                self.capture() as cm:
            outcome = updater.check_for_update("owner", "repo", "1.2.0")

        self.assertEqual(outcome, (False, None, None))
        self.assertIn("DEBUG", self.levels(cm))
        self.assertNoLevel(cm, "ERROR", "WARNING",
                           )

    def test_2_a_malformed_update_response_is_not_fatal(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"not json at all"

        with mock.patch.object(updater.urllib.request, "urlopen",
                               return_value=FakeResponse()), self.capture() as cm:
            outcome = updater.check_for_update("owner", "repo", "1.2.0")

        self.assertEqual(outcome, (False, None, None))
        self.assertIn("DEBUG", self.levels(cm))

    def test_3_the_update_worker_never_escapes_into_the_gui(self):
        class BoomApp:
            def _post_ui_event(self, *args):
                raise RuntimeError("banner exploded")

        checker = UpdateChecker(BoomApp())
        with mock.patch.object(updater, "check_for_update",
                               return_value=(True, "9.9.9", "https://example/rel")), \
                self.capture() as cm:
            checker._check_update_worker()   # must not raise

        self.assertIn("DEBUG", self.levels(cm))

    def test_4_opening_the_release_page_fails_open_but_loudly(self):
        import webbrowser

        checker = UpdateChecker(FakeLoaderApp())
        with mock.patch.object(webbrowser, "open", side_effect=OSError("no browser")), \
                self.capture() as cm:
            checker._open_release("https://example/rel")
        self.assertIn("WARNING", self.levels(cm))

    def test_5_js_runtime_probe_failure_degrades_to_no_runtime(self):
        with mock.patch.object(js_runtime.shutil, "which",
                               side_effect=OSError("PATH unreadable")), \
                self.capture() as cm:
            found = js_runtime.find_available_runtime()

        self.assertIsNone(found)
        self.assertIn("DEBUG", self.levels(cm))
        self.assertNoLevel(cm, "ERROR", "WARNING")

        with mock.patch.object(js_runtime.shutil, "which",
                               side_effect=OSError("PATH unreadable")):
            self.assertIsNone(js_runtime.build_ydl_js_runtime_option(),
                              "a broken probe still produced ydl options")

    def test_6_bundled_runtime_path_failure_does_not_block_startup(self):
        with mock.patch.object(js_runtime.Path, "is_dir",
                               side_effect=OSError("permission denied")), \
                self.capture() as cm:
            js_runtime.ensure_bundled_runtime_on_path()   # must not raise

        self.assertIn("WARNING", self.levels(cm))


# ---------------------------------------------------------------------------
# §15 — configuration problems are recoverable and never fatal
# ---------------------------------------------------------------------------

class TestConfigContracts(ContractTestCase):
    def test_1_save_failure_returns_false_instead_of_raising(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")), \
                self.capture() as cm:
            saved = config_module.save_config({"last_output_dir": str(self.tmp)})

        self.assertIs(saved, False)
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("disk full", self.joined(cm))

    def test_2_unserialisable_settings_are_recoverable(self):
        with self.capture() as cm:
            saved = config_module.save_config({"bad": object()})

        self.assertIs(saved, False)
        self.assertIn("WARNING", self.levels(cm))

    def test_3_a_successful_save_is_reported_as_such(self):
        target = self.tmp / "config.json"
        with mock.patch.object(config_module, "CONFIG_PATH", target), self.capture():
            saved = config_module.save_config({"last_output_dir": str(self.tmp)})
        self.assertIs(saved, True)
        self.assertEqual(json.loads(target.read_text())["last_output_dir"], str(self.tmp))

    def test_4_corrupt_settings_fall_back_to_defaults(self):
        target = self.tmp / "config.json"
        target.write_text("{not json", encoding="utf-8")
        with mock.patch.object(config_module, "CONFIG_PATH", target), \
                self.capture() as cm:
            loaded = config_module.load_config()

        self.assertEqual(loaded, config_module.DEFAULTS)
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("corrupt", self.joined(cm))

    def test_5_unreadable_settings_fall_back_to_defaults(self):
        target = self.tmp / "config.json"
        target.write_text("{}", encoding="utf-8")
        with mock.patch.object(config_module, "CONFIG_PATH", target), \
                mock.patch("builtins.open", side_effect=OSError("access denied")), \
                self.capture() as cm:
            loaded = config_module.load_config()

        self.assertEqual(loaded, config_module.DEFAULTS)
        self.assertIn("WARNING", self.levels(cm))

    def test_6_an_uncreatable_settings_folder_does_not_break_the_app(self):
        with mock.patch.object(config_module.Path, "mkdir",
                               side_effect=OSError("read-only")), self.capture() as cm:
            folder = config_module._make_config_dir(str(self.tmp))

        self.assertEqual(folder.name, "YTClipper")
        self.assertIn("WARNING", self.levels(cm))
        self.assertIn("will not be saved", self.joined(cm))

    def test_7_the_gui_says_when_settings_could_not_be_remembered(self):
        app = FakeLoaderApp()
        controller = QueueController(app)
        controller.render_queue = lambda: None

        with mock.patch.object(config_module, "save_config", return_value=False), \
                self.capture():
            controller._remember_output_dir(app, self.tmp)
        self.assertIn("could not be saved", app.statuses[-1][0].lower())

        app.statuses.clear()
        with mock.patch.object(config_module, "save_config", return_value=True):
            controller._remember_output_dir(app, self.tmp)
        self.assertEqual(app.statuses, [], "a successful save warned the user")

    def test_8_a_legacy_save_config_returning_none_is_not_treated_as_failure(self):
        """Existing callers/tests patch save_config with a None-returning stub."""
        app = FakeLoaderApp()
        controller = QueueController(app)
        controller.render_queue = lambda: None
        with mock.patch.object(config_module, "save_config", lambda *a, **k: None):
            controller._remember_output_dir(app, self.tmp)
        self.assertEqual(app.statuses, [])


# ---------------------------------------------------------------------------
# §16/§21 — the GUI reports Completed / Failed / Cancelled honestly
# ---------------------------------------------------------------------------

class FakeQueueApp(FakeLoaderApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue_jobs = []
        self._active_job_id = None
        self._queue_status_labels = {}


class TestGuiStateContracts(ContractTestCase):
    def make_controller(self, app):
        controller = QueueController(app)
        controller.render_queue = lambda: None   # presentation only
        return controller

    def make_job(self, app, job_id=1, name="clip"):
        job = DownloadJob(id=job_id, url=f"https://www.youtube.com/watch?v={name}",
                          label=name, start_sec=0, end_sec=5, quality="best",
                          audio_only=False, output_path=str(self.tmp / f"{name}.mp4"))
        app.queue_jobs.append(job)
        return job

    def test_1_completed_job_shows_a_saved_message(self):
        app = FakeQueueApp()
        controller = self.make_controller(app)
        job = self.make_job(app)
        controller._apply_job_done(job.id)

        self.assertEqual(job.status, "Done")
        self.assertIn("Saved to", app.statuses[-1][0])
        self.assertEqual(app.statuses[-1][1], "#4caf50")

    def test_2_failed_job_shows_the_reason_in_the_failure_colour(self):
        app = FakeQueueApp()
        controller = self.make_controller(app)
        job = self.make_job(app)
        controller._apply_job_error(job.id, "HTTP Error 403: Forbidden")

        self.assertEqual(job.status, "Error")
        self.assertEqual(job.error, "HTTP Error 403: Forbidden")
        self.assertIn("403", app.statuses[-1][0])
        self.assertEqual(app.statuses[-1][1], "#e05252")

    def test_3_cancelled_job_shows_cancelled_and_leaves_the_queue(self):
        app = FakeQueueApp()
        controller = self.make_controller(app)
        job = self.make_job(app)
        app._active_job_id = job.id
        controller._apply_job_cancelled(job.id)

        self.assertEqual(app.queue_jobs, [], "a cancelled row stayed in the queue")
        self.assertIsNone(app._active_job_id)
        self.assertIn("Cancelled", app.statuses[-1][0])
        self.assertEqual(app.statuses[-1][1], "gray",
                         "a cancel was styled like a failure")

    def test_4_events_for_unknown_jobs_are_ignored_quietly(self):
        app = FakeQueueApp()
        controller = self.make_controller(app)
        controller._apply_job_done(999)
        controller._apply_job_error(999, "boom")
        controller._apply_job_cancelled(999)
        self.assertEqual(app.statuses, [])

    def test_5_a_failed_video_load_is_shown_without_a_traceback(self):
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_video_error(app._load_request_id, "HTTP Error 403: Forbidden")

        self.assertIn("Couldn't load video", app.video_info_label.text)
        self.assertIn("403", app.video_info_label.text)
        self.assertEqual(app.video_info_label.configured[-1]["text_color"], "#e05252")
        self.assertIsNone(app.loaded_url)
        self.assertEqual(app.statuses[-1][1], "#e05252")

    def test_6_the_event_loop_survives_a_broken_handler_and_keeps_a_traceback(self):
        class LoopApp:
            _closing = False
            handled = []

            def __init__(self):
                self._ui_events = __import__("queue").Queue()
                self.rescheduled = 0

            def after(self, delay, callback=None):
                self.rescheduled += 1

            def _poll_ui_events(self):
                pass

            def _handle_ui_event(self, name, payload):
                self.handled.append(name)
                if name == "boom":
                    raise RuntimeError("handler exploded")

        loop = LoopApp()
        loop._ui_events.put(("boom", ()))
        loop._ui_events.put(("fine", ()))

        with self.capture() as cm:
            ClipperApp._poll_ui_events(loop)

        self.assertEqual(loop.handled, ["boom", "fine"],
                         "one bad event stopped the whole UI loop")
        self.assertEqual(loop.rescheduled, 1, "the loop was not rescheduled")
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertTrue(error_records, "the handler bug was not logged")
        self.assertTrue(error_records[0].exc_info, "the traceback was discarded")
        self.assertNotIn("print_exc", self.joined(cm))


# ---------------------------------------------------------------------------
# §17 — the CLI keeps its exit codes and stays concise
# ---------------------------------------------------------------------------

class TestCliPresentationContracts(ContractTestCase):
    def test_1_success_exits_zero(self):
        recorder = DownloadCallRecorder(result=str(self.tmp / "clip.mp4"))
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)), \
                recorder.patch():
            code, out, err = run_cli([VIDEO_URL, "00:05", "00:10",
                                      "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0, err)
        self.assertIn("Saved to", out)
        self.assertEqual(err, "", "a successful run wrote to stderr")

    def test_2_usage_errors_keep_the_argparse_exit_code(self):
        code, out, err = run_cli([VIDEO_URL, "00:05"])
        self.assertEqual(code, 2)
        self.assertIn("start and end times must be given together", err)

    def test_3_a_failure_exits_one_with_a_concise_secret_free_message(self):
        def fail(*args, **kwargs):
            raise RuntimeError(f"HTTP Error 403 for {SECRET_COOKIE} signature=SUPER_SIG")

        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)), \
                mock.patch.object(downloader, "download_clip", side_effect=fail):
            code, out, err = run_cli([VIDEO_URL, "00:05", "00:10",
                                      "-o", str(self.tmp / "clip.mp4")])

        self.assertEqual(code, 1)
        self.assertIn("403", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("SuperSecretValue123", err)
        self.assertNotIn("SUPER_SIG", err)

    def test_4_a_partial_playlist_failure_keeps_the_batch_going(self):
        entries = [playlist_entry("v1", "one"), playlist_entry("v2", "two")]
        calls = []

        def download(url, start, end, output_path="clip.mp4", quality="best",
                     **kwargs):
            calls.append(url)
            if "v2" in url:
                raise RuntimeError("HTTP Error 403: Forbidden")
            return output_path

        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(entries)), \
                mock.patch.object(downloader, "download_clip", side_effect=download):
            code, out, err = run_cli([PLAYLIST_URL, "00:05", "00:10",
                                      "-o", str(self.tmp / "batch")])

        self.assertEqual(len(calls), 2, "one failure aborted the batch")
        self.assertEqual(code, 0, "a partial failure changed the exit code")
        self.assertIn("1 succeeded, 1 failed", out)
        self.assertIn("403", err)
        self.assertNotIn("Traceback", err)

    def test_5_a_fully_failed_playlist_exits_non_zero(self):
        entries = [playlist_entry("v1", "one")]
        calls = []

        def download(url, start, end, output_path="clip.mp4", quality="best",
                     **kwargs):
            calls.append(url)
            raise RuntimeError("HTTP Error 403: Forbidden")

        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(entries)), \
                mock.patch.object(downloader, "download_clip", side_effect=download):
            code, out, err = run_cli([PLAYLIST_URL, "00:05", "00:10",
                                      "-o", str(self.tmp / "batch")])

        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1, "the video was never attempted")
        self.assertIn("0 succeeded, 1 failed", out)

    def test_6_download_failure_never_returns_a_path(self):
        """§21: download_clip must not look successful when FFmpeg failed."""
        output = self.tmp / "clip.mp4"
        with mock.patch.object(downloader, "FFmpegPostProcessor", FakeFFmpegPostProcessor), \
                mock.patch.object(downloader, "run_ffmpeg_clip",
                                  side_effect=RuntimeError("Conversion failed!")):
            patcher, calls = helpers.patch_youtube_dl({
                VIDEO_URL: {"id": "contractvid1", "duration": 100.0,
                            "requested_formats": [{"url": "https://media.example/v"}]},
            })
            with patcher, self.assertRaises(RuntimeError):
                downloader.download_clip(VIDEO_URL, 0, 10, str(output))
        self.assertFalse(output.exists())


# ---------------------------------------------------------------------------
# §18/§19 — the logging layer and the yt-dlp filter, tested both ways
# ---------------------------------------------------------------------------

class TestLoggingContracts(ContractTestCase):
    def test_1_secrets_are_redacted(self):
        samples = {
            f"Cookie: {SECRET_COOKIE}": "SuperSecretValue123",
            "Authorization: Bearer abc.def.ghi": "abc.def.ghi",
            "url?signature=TOPSECRET&expire=1": "TOPSECRET",
            "-cookies SAPISID=xyz; path=/; domain=.youtube.com": "SAPISID=xyz",
            "-headers 'User-Agent: Secret/1.0'": "Secret/1.0",
            "token=hunter2": "hunter2",
        }
        for text, secret in samples.items():
            self.assertNotIn(secret, redact_secrets(text), f"leaked in {text!r}")

    def test_2_useful_diagnostics_survive_redaction(self):
        text = ("Unable to download webpage for "
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ: HTTP Error 403")
        redacted = redact_secrets(text)
        self.assertIn("dQw4w9WgXcQ", redacted, "the video id was redacted away")
        self.assertIn("HTTP Error 403", redacted)

    def test_3_error_text_is_length_capped(self):
        self.assertLessEqual(len(redact_secrets("x" * 100000)), 2100)

    def test_4_failure_descriptions_name_the_type_and_stay_safe(self):
        exc = RuntimeError(f"boom {SECRET_COOKIE}")
        self.assertTrue(describe_failure(exc).startswith("RuntimeError:"))
        self.assertNotIn("SuperSecretValue123", describe_failure(exc))
        self.assertEqual(safe_message(ValueError("")), "ValueError")

    def test_5_expected_noise_from_yt_dlp_is_still_filtered(self):
        logger_double = _FilteredYtDlpLogger()
        noisy = [
            "[youtube:tab] YouTube said: INFO - 1 unavailable video is hidden",
            "YouTube said: INFO - 3 unavailable videos are hidden",
            "[youtube:tab] YouTube said: INFO - 1 unavailable video is hidden (1/2)",
        ]
        with self.capture() as cm:
            for message in noisy:
                logger_double.warning(message)
            logger_double.debug("[debug] chunk 1234")
            logger_double.info("[info] extracting")

        self.assertEqual(cm.records, [], "filtered noise became observable")

    def test_6_unexpected_warnings_are_recorded_at_debug_only(self):
        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            logger_double.warning("[youtube] Some other problem worth knowing")

        self.assertEqual(self.levels(cm), ["DEBUG"])
        self.assertIn("Some other problem", self.joined(cm))

    def test_7_error_level_messages_reach_the_logging_path(self):
        """§19: the whole point — errors must not vanish."""
        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            logger_double.error("[youtube] Unable to download webpage: HTTP Error 403")

        self.assertEqual(self.levels(cm), ["ERROR"])
        self.assertIn("HTTP Error 403", self.joined(cm))

    def test_8_repeated_errors_are_not_duplicated(self):
        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            for _ in range(5):
                logger_double.error("Unable to download webpage: HTTP Error 403")

        self.assertEqual(cm.count("ERROR"), 1, "the same failure was logged 5 times")
        self.assertEqual(cm.count("DEBUG"), 4, "the repeats were not recorded at all")

    def test_9_distinct_errors_are_all_recorded(self):
        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            logger_double.error("Unable to download webpage: HTTP Error 403")
            logger_double.error("Requested format is not available")

        self.assertEqual(cm.count("ERROR"), 2)

    def test_10_error_text_is_redacted_before_it_is_logged(self):
        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            logger_double.error(
                f"Unable to download media: https://x/y?signature=SUPER_SIG "
                f"with Cookie: {SECRET_COOKIE}")

        text = self.joined(cm)
        self.assertNotIn("SUPER_SIG", text)
        self.assertNotIn("SuperSecretValue123", text)
        self.assertIn("Unable to download media", text)

    def test_11_a_listener_can_receive_errors_directly(self):
        """The filter forwards to an injectable logger, which tests can observe."""
        records = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record)

        collector_logger = logging.getLogger("yt_clipper.test_collector")
        collector_logger.addHandler(Collector())
        collector_logger.setLevel(logging.DEBUG)
        collector_logger.propagate = False
        self.addCleanup(collector_logger.removeHandler, collector_logger.handlers[0])

        logger_double = _FilteredYtDlpLogger(error_logger=collector_logger)
        logger_double.error("Something failed badly")
        logger_double.error("Something failed badly")

        self.assertEqual(len(records), 1, "duplicates reached the listener")
        self.assertEqual(records[0].levelno, logging.ERROR)

    def test_12_an_uninspectable_warning_cannot_break_extraction(self):
        class ExplodingMessage:
            def __str__(self):
                raise RuntimeError("cannot stringify")

        logger_double = _FilteredYtDlpLogger()
        with self.capture() as cm:
            logger_double.warning(ExplodingMessage())   # must not raise

        self.assertIn("DEBUG", self.levels(cm))

    def test_13_the_filter_is_used_by_every_extraction_entry_point(self):
        """All three yt-dlp call sites must install the filtering logger."""
        seen = []

        class RecordingYoutubeDL:
            def __init__(self, options=None):
                seen.append(options or {})
                self.params = dict(options or {})
                self.cookiejar = None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                return {"id": "x", "duration": 10.0}

        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", RecordingYoutubeDL), \
                mock.patch.object(downloader, "FFmpegPostProcessor", FakeFFmpegPostProcessor), \
                mock.patch.object(downloader, "run_ffmpeg_clip", lambda **kwargs: None):
            downloader.get_video_info(VIDEO_URL)
            downloader.expand_playlist(VIDEO_URL)
            downloader.download_clip(VIDEO_URL, 0, 5, str(self.tmp / "clip.mp4"))

        self.assertEqual(len(seen), 3)
        for options in seen:
            self.assertIsInstance(options.get("logger"), _FilteredYtDlpLogger,
                                  "an extraction ran without the filtering logger")
