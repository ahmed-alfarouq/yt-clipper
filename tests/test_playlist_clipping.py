"""Phase 2 tests: GUI and CLI playlist clipping semantics.

Contract under test (observable behavior, not implementation details):

  * which start_sec / end_sec reach downloader.download_clip() - and, one level
    deeper, which start/end reach the FFmpeg runner;
  * which jobs the GUI queue service runs, in which order, and how they cancel;
  * what the CLI accepts, and that it still fails fast on bad usage.

The GUI cannot be instantiated headlessly (CustomTkinter needs Tk, Tk needs a
display), so the closest stable boundary is driven instead: QueueController /
VideoLoaderController with a fake ClipperApp, wired to the REAL
SequentialDownloadQueue service and a patched downloader.download_clip. The
toolkit is stubbed only when it is not installed (see helpers.py).

Run with:  python -m unittest discover -s tests -v
      or:  python -m pytest tests -q
"""

import itertools
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import (
    DownloadCallRecorder,
    FakeFFmpegPostProcessor,
    flat_playlist,
    playlist_entry,
    run_cli,
    single_video_result,
    wait_for,
)

from yt_clipper.core import downloader

gui = helpers.import_gui_modules()
ClipperApp = gui["ClipperApp"]
QueueController = gui["QueueController"]
SequentialDownloadQueue = gui["SequentialDownloadQueue"]

from yt_clipper.gui.controllers import queue_controller as queue_controller_module  # noqa: E402
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402
from yt_clipper.core.playlist_utils import detect_youtube_url_type  # noqa: E402

# Presentation-only GUI doubles shared with the failure-contract tests. They
# were defined here verbatim a second time; import the single definition so the
# two suites cannot drift apart.
from test_failure_contracts import (  # noqa: E402,F401
    FakeVar,
    Permissive,
    RecordingSlider,
    RecordingWidget,
)

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLPHASE2TEST"
VIDEO_URL = "https://www.youtube.com/watch?v=singlevideo1"


# ---------------------------------------------------------------------------
# Test doubles for the GUI layer
#
# Permissive, RecordingWidget and FakeVar are imported from
# test_failure_contracts (identical definitions, single source of truth). The
# doubles below are deliberately NOT shared: each records state the failure
# contracts do not (FakeEntry.deleted, FakeTimeInput.set_calls) or exposes a
# different queue API (this QueueHarness has no listener/payloads).
# ---------------------------------------------------------------------------

class FakeEntry:
    def __init__(self, text=""):
        self._text = text
        self.deleted = False

    def get(self):
        return self._text

    def delete(self, *args):
        self.deleted = True
        self._text = ""

    def insert(self, *args):
        self._text = args[-1] if args else ""


class FakeTimeInput:
    def __init__(self, seconds):
        self._seconds = seconds
        self.set_calls = []

    def get_seconds(self):
        return self._seconds

    def set_seconds(self, seconds):
        self.set_calls.append(seconds)
        self._seconds = seconds


class QueueHarness:
    """The REAL SequentialDownloadQueue plus an event recorder."""

    def __init__(self):
        self.events = []
        self.queue = SequentialDownloadQueue(self._on_event)

    def _on_event(self, name, *payload):
        self.events.append((name, payload))

    def event_names(self):
        return [name for name, _ in self.events]

    def shutdown(self):
        self.queue.shutdown()


class FakeApp:
    """Stands in for ClipperApp.

    Explicit attributes cover everything the tested paths read or write;
    presentation-only widgets fall through to a permissive no-op so no display
    is needed. The real ClipperApp helpers that contain actual logic
    (_path_with_expected_extension, _default_output_path) are reused verbatim
    instead of being re-implemented here.
    """

    _path_with_expected_extension = staticmethod(ClipperApp._path_with_expected_extension)

    def __init__(self, url="", entries=None, duration=None, output_path=None,
                 start_seconds=0, end_seconds=0, quality="best", audio_only=False):
        self.url_entry = FakeEntry(url)
        self.output_entry = FakeEntry(str(output_path) if output_path else "")
        self.audio_only_var = FakeVar(audio_only)
        self.quality_var = FakeVar(quality)
        self.open_folder_var = FakeVar(False)
        self.video_info_label = RecordingWidget()

        self.loaded_url = url if (entries or duration) else None
        self.loaded_title = "Test Playlist" if entries else ("Single video" if duration else None)
        self.loaded_playlist_entries = entries
        self.video_duration = duration

        self.start_input = FakeTimeInput(start_seconds)
        self.end_input = FakeTimeInput(end_seconds)

        self.queue_jobs = []
        self._job_id_counter = itertools.count(1)
        self._active_job_id = None
        self._queue_status_labels = {}
        self._load_request_id = 0
        self._thumbnail_image = None
        self.app_config = {}

        self.harness = QueueHarness()
        self.download_queue = self.harness.queue

        self.statuses = []
        self.time_range_visibility = []

    def _default_output_path(self):
        return ClipperApp._default_output_path(self)

    def set_status(self, text, color="gray"):
        self.statuses.append(text)

    def set_time_range_visible(self, visible):
        self.time_range_visibility.append(visible)

    def video_info_label_text(self):
        return self.video_info_label.text

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return Permissive()


class TimeRangeApp(FakeApp):
    """FakeApp with observable clip-range widgets and the real reset method.

    FakeApp answers unknown attributes with a permissive no-op, so the sliders
    and the clip-length label have to be real doubles for the reset to be
    observable. `reset_time_range` is delegated to the shipped ClipperApp so the
    test exercises the real implementation rather than a copy of it.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.start_slider = RecordingSlider(self.start_input.get_seconds())
        self.end_slider = RecordingSlider(self.end_input.get_seconds())
        self.clip_length_label = RecordingWidget()

    def reset_time_range(self):
        return ClipperApp.reset_time_range(self)

    def loaded_range_state(self):
        return {
            "start_input": self.start_input.get_seconds(),
            "end_input": self.end_input.get_seconds(),
            "start_slider": (self.start_slider.get(), self.start_slider.to,
                             self.start_slider.state),
            "end_slider": (self.end_slider.get(), self.end_slider.to,
                           self.end_slider.state),
            "clip_length": self.clip_length_label.text,
        }


# The clip-range block as ClipperApp builds it for a window with no video in it.
EMPTY_RANGE = {
    "start_input": 0,
    "end_input": 0,
    "start_slider": (0, 100, "disabled"),
    "end_slider": (100, 100, "disabled"),
    "clip_length": "Clip length: —",
}


class GuiTestCase(unittest.TestCase):
    """Base class: temp output dir, harness cleanup, shared driver."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-gui-"))
        self.entries = [
            playlist_entry("aaaaaaaaaaa", "alpha"),
            playlist_entry("bbbbbbbbbbb", "beta"),
            playlist_entry("ccccccccccc", "gamma"),
        ]

    def make_app(self, **kwargs):
        kwargs.setdefault("output_path", self.tmp / "clip.mp4")
        app = FakeApp(**kwargs)
        self.addCleanup(app.harness.shutdown)
        return app

    def make_controller(self, app):
        controller = QueueController(app)
        # Presentation only (builds CTk widgets, needs a display); not under test.
        controller.render_queue = lambda: None
        return controller

    def run_playlist_download(self, app, recorder, expected_jobs):
        controller = self.make_controller(app)
        # recorder.calls is cumulative, and a test may reuse one recorder across
        # phases (test_10 queues a single-video clip first, then a playlist), so
        # wait for the jobs belonging to THIS queueing. A wait on the total count
        # is satisfied by calls recorded before it started, which releases the
        # helper while playlist jobs are still running.
        recorded_before = len(recorder.calls)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(
                wait_for(lambda: len(recorder.calls) - recorded_before >= expected_jobs),
                f"only {len(recorder.calls) - recorded_before} of {expected_jobs} "
                f"playlist jobs ran",
            )
        return controller

    @staticmethod
    def _no_config_write():
        return mock.patch("yt_clipper.core.config.save_config", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# CLI: single video (semantics must be unchanged)
# ---------------------------------------------------------------------------

class TestCliSingleVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-cli-"))
        self.result = single_video_result(VIDEO_URL)

    def test_1_valid_range_reaches_download_clip(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand, \
                recorder.patch():
            code, out, err = run_cli([VIDEO_URL, "00:25", "01:28", "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0, err)
        expand.assert_called_once_with(VIDEO_URL)
        self.assertEqual(len(recorder.calls), 1)
        call = recorder.calls[0]
        # Single-video clipping unchanged: same values as before this phase.
        self.assertEqual(call["url"], VIDEO_URL)
        self.assertEqual(call["start_sec"], 25.0)
        self.assertEqual(call["end_sec"], 88.0)
        self.assertEqual(call["quality"], "best")
        self.assertFalse(call["audio_only"])
        self.assertIn("Saved to", out)

    def test_2_no_range_is_a_usage_error_before_any_network_call(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand, \
                recorder.patch():
            code, _, err = run_cli([VIDEO_URL, "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 2)
        self.assertIn("start and end times are required unless using --list-formats", err)
        expand.assert_not_called()            # still fails fast, no extraction
        self.assertEqual(recorder.calls, [])  # nothing downloaded

    def test_3_start_only_is_rejected(self):
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand:
            code, _, err = run_cli([VIDEO_URL, "00:25"])
        self.assertEqual(code, 2)
        self.assertIn("start and end times must be given together", err)
        expand.assert_not_called()

    def test_4_end_only_cannot_be_expressed_and_is_rejected(self):
        """start/end are positional, so a lone time always lands in `start`;
        either way a partial range is rejected exactly as before."""
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand:
            code, _, err = run_cli([VIDEO_URL, "01:28"])
        self.assertEqual(code, 2)
        self.assertIn("start and end times must be given together", err)
        expand.assert_not_called()

    def test_5_invalid_range_keeps_current_error_behavior(self):
        """end <= start: the real download_clip rejects it and the CLI exits 1."""
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result):
            code, _, err = run_cli([VIDEO_URL, "01:28", "00:25", "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 1)
        self.assertIn("End time must be after start time", err)

    def test_6_list_formats_still_needs_no_range(self):
        formats = [{"format_id": "137", "kind": "video only", "height": 1080,
                    "ext": "mp4", "vbr": None, "abr": None}]
        with mock.patch.object(downloader, "list_formats", return_value=formats) as list_formats:
            code, out, err = run_cli([VIDEO_URL, "--list-formats"])
        self.assertEqual(code, 0, err)
        list_formats.assert_called_once_with(VIDEO_URL)
        self.assertIn("137:", out)

    def test_7_playlist_url_resolving_to_single_video_still_requires_range(self):
        """Guard: never download a single video in full just because the URL
        looked like a playlist."""
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand:
            code, _, err = run_cli([PLAYLIST_URL])
        self.assertEqual(code, 2)
        self.assertIn("start and end times are required for a single video", err)
        expand.assert_called_once_with(PLAYLIST_URL)


# ---------------------------------------------------------------------------
# CLI: playlist
# ---------------------------------------------------------------------------

class TestCliPlaylist(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-cli-pl-"))
        self.entries = [
            playlist_entry("aaaaaaaaaaa", "alpha"),
            playlist_entry("bbbbbbbbbbb", "beta"),
            playlist_entry("ccccccccccc", "gamma"),
        ]
        self.result = flat_playlist(self.entries)

    def test_6_playlist_without_range_downloads_every_video_in_full(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result) as expand, \
                recorder.patch():
            code, out, err = run_cli([PLAYLIST_URL, "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0, err)
        expand.assert_called_once_with(PLAYLIST_URL)
        self.assertEqual(len(recorder.calls), 3)
        # The contract that matters: explicit None/None for EVERY playlist item.
        self.assertEqual(recorder.ranges, [(None, None)] * 3)
        self.assertEqual(recorder.urls, [e["url"] for e in self.entries])
        self.assertEqual(recorder.filenames, ["01 - alpha.mp4", "02 - beta.mp4", "03 - gamma.mp4"])
        self.assertIn("Playlist detected: 3 videos", out)
        self.assertIn("Downloading each video in full", out)
        self.assertIn("Done: 3 succeeded, 0 failed.", out)

    def test_7_playlist_with_range_still_clips_every_video(self):
        """Released, documented CLI behavior preserved: an explicit range is
        applied to each video in the playlist."""
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result), \
                recorder.patch():
            code, out, err = run_cli([PLAYLIST_URL, "00:25", "01:28",
                                      "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0, err)
        self.assertEqual(recorder.ranges, [(25.0, 88.0)] * 3)
        self.assertEqual(recorder.urls, [e["url"] for e in self.entries])
        self.assertIn("Clipping 00:25–01:28 from each", out)

    def test_8_no_range_leaks_between_playlist_items(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result), \
                recorder.patch():
            run_cli([PLAYLIST_URL, "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(len(set(recorder.urls)), 3)
        self.assertEqual(len(set(recorder.filenames)), 3)
        for call in recorder.calls:
            self.assertIsNone(call["start_sec"])
            self.assertIsNone(call["end_sec"])

    def test_9_audio_only_playlist_without_range(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist", return_value=self.result), \
                recorder.patch():
            code, _, err = run_cli([PLAYLIST_URL, "-a", "-o", str(self.tmp / "clip.mp3")])
        self.assertEqual(code, 0, err)
        self.assertEqual(recorder.ranges, [(None, None)] * 3)
        self.assertTrue(all(call["audio_only"] for call in recorder.calls))
        self.assertEqual(recorder.filenames, ["01 - alpha.mp3", "02 - beta.mp3", "03 - gamma.mp3"])

    def test_10_one_failing_video_does_not_abort_the_batch(self):
        """Pre-existing batch behavior must survive the change."""
        def flaky(url, start_sec, end_sec, *args, **kwargs):
            if url.endswith("bbbbbbbbbbb"):
                raise RuntimeError("extractor exploded")
            return "/tmp/ok.mp4"

        with mock.patch.object(downloader, "expand_playlist", return_value=self.result), \
                mock.patch.object(downloader, "download_clip", side_effect=flaky):
            code, out, err = run_cli([PLAYLIST_URL, "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0)
        self.assertIn("Done: 2 succeeded, 1 failed.", out)
        self.assertIn("extractor exploded", err)


# ---------------------------------------------------------------------------
# Core: what actually reaches the media pipeline
# ---------------------------------------------------------------------------

class TestDownloadClipRangeContract(unittest.TestCase):
    """Prove the values reaching the real download operation (FFmpeg runner)."""

    def _run(self, start_sec, end_sec, duration=600.0):
        captured = {}

        def fake_run_ffmpeg_clip(**kwargs):
            captured.update(kwargs)

        info = {
            "id": "singlevideo1",
            "title": "Single video",
            "duration": duration,
            "requested_formats": [
                {"format_id": "137", "vcodec": "avc1", "acodec": "none",
                 "url": "https://example.test/video", "http_headers": {}},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
                 "url": "https://example.test/audio", "http_headers": {}},
            ],
        }
        patcher, _ = helpers.patch_youtube_dl({VIDEO_URL: info})
        with patcher, \
                mock.patch.object(downloader, "FFmpegPostProcessor", FakeFFmpegPostProcessor), \
                mock.patch.object(downloader, "run_ffmpeg_clip", side_effect=fake_run_ffmpeg_clip):
            downloader.download_clip(VIDEO_URL, start_sec, end_sec,
                                     "/tmp/out.mp4", quality="best")
        return captured

    def test_16_explicit_range_reaches_ffmpeg_unchanged(self):
        captured = self._run(25.0, 88.0)
        self.assertEqual((captured["start_sec"], captured["end_sec"]), (25.0, 88.0))

    def test_16_no_range_means_full_video_at_the_ffmpeg_boundary(self):
        captured = self._run(None, None, duration=600.0)
        self.assertEqual((captured["start_sec"], captured["end_sec"]), (0.0, 600.0))

    def test_16_partial_ranges_are_supported_by_the_core(self):
        start_only = self._run(30.0, None, duration=600.0)
        self.assertEqual((start_only["start_sec"], start_only["end_sec"]), (30.0, 600.0))
        end_only = self._run(None, 45.0, duration=600.0)
        self.assertEqual((end_only["start_sec"], end_only["end_sec"]), (0.0, 45.0))

    def test_full_video_with_unknown_duration_still_errors(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(None, None, duration=None)
        self.assertIn("duration", str(ctx.exception))


# ---------------------------------------------------------------------------
# GUI: playlist jobs (the Download button path)
# ---------------------------------------------------------------------------

class TestGuiPlaylistJobs(GuiTestCase):
    def test_6_gui_playlist_jobs_explicitly_receive_no_range(self):
        # The range widgets still hold values (they are merely hidden for
        # playlists); they must be ignored, not applied to every video.
        app = self.make_app(url=PLAYLIST_URL, entries=self.entries,
                            start_seconds=25, end_seconds=88)
        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, len(self.entries))
        self.assertEqual(recorder.ranges, [(None, None)] * 3)
        self.assertEqual(recorder.urls, [e["url"] for e in self.entries])
        self.assertEqual(recorder.filenames, ["01 - alpha.mp4", "02 - beta.mp4", "03 - gamma.mp4"])
        self.assertIn("Queued 3 full videos from playlist", app.statuses[-1])

    def test_9_every_queued_job_stores_no_range(self):
        app = self.make_app(url=PLAYLIST_URL, entries=self.entries)
        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, len(self.entries))
        self.assertEqual(len(app.queue_jobs), 3)
        for job in app.queue_jobs:
            self.assertIsNone(job.start_sec)
            self.assertIsNone(job.end_sec)

    def test_10_ranges_cannot_leak_between_jobs(self):
        """A playlist queued after a single-video clip must not inherit its range."""
        recorder = DownloadCallRecorder()

        single = self.make_app(url=VIDEO_URL, entries=None, duration=200.0,
                               start_seconds=25, end_seconds=88)
        controller = self.make_controller(single)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))
        self.assertEqual(recorder.ranges, [(25, 88)])

        playlist_app = self.make_app(url=PLAYLIST_URL, entries=self.entries)
        self.run_playlist_download(playlist_app, recorder, 3)
        self.assertEqual(recorder.ranges, [(25, 88), (None, None), (None, None), (None, None)])

    def test_10b_a_reused_recorder_waits_for_this_phase_jobs_only(self):
        """run_playlist_download() must wait for the jobs of *this* queueing.

        `DownloadCallRecorder.calls` is cumulative on purpose - test_10 asserts
        the single-video range alongside the playlist ranges, so the earlier
        call has to survive - which makes a wait on a *total* count release
        early as soon as the recorder already holds a call. The per-call delay
        turns that into a deterministic reproduction instead of a race: while
        the third playlist job is still sleeping, a total-count wait has
        already been satisfied by the second one.
        """
        recorder = DownloadCallRecorder(delay=0.05)

        single = self.make_app(url=VIDEO_URL, entries=None, duration=200.0,
                               start_seconds=25, end_seconds=88)
        controller = self.make_controller(single)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))
        before = len(recorder.calls)
        self.assertEqual(before, 1, "the single-video phase did not record its call")

        playlist_app = self.make_app(url=PLAYLIST_URL, entries=self.entries)
        self.run_playlist_download(playlist_app, recorder, len(self.entries))

        self.assertEqual(len(recorder.calls) - before, len(self.entries),
                         "the helper returned before every job of this phase ran")
        self.assertEqual(recorder.ranges,
                         [(25, 88)] + [(None, None)] * len(self.entries))

    def test_14_queue_order_is_fifo(self):
        app = self.make_app(url=PLAYLIST_URL, entries=self.entries)
        recorder = DownloadCallRecorder(delay=0.02)
        self.run_playlist_download(app, recorder, 3)
        self.assertEqual(recorder.urls, [e["url"] for e in self.entries])
        self.assertEqual(app.harness.event_names().count("job_started"), 3)
        self.assertTrue(wait_for(lambda: app.harness.event_names().count("job_done") == 3))

    def test_15_pending_playlist_job_can_be_cancelled(self):
        app = self.make_app(url=PLAYLIST_URL, entries=self.entries)
        gate = threading.Event()
        recorder = DownloadCallRecorder(gate=gate)
        controller = self.make_controller(app)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))
            third = app.queue_jobs[2]
            # Job 1 is blocked in download_clip, so jobs 2 and 3 are still pending.
            self.assertEqual(app.download_queue.cancel(third.id), "pending")
            self.assertTrue(wait_for(lambda: "job_cancelled" in app.harness.event_names()))
            gate.set()
            self.assertTrue(wait_for(lambda: app.harness.event_names().count("job_done") == 2))
        self.assertEqual(recorder.urls, [self.entries[0]["url"], self.entries[1]["url"]])
        self.assertNotIn(third.url, recorder.urls)

    def test_15_active_playlist_job_receives_a_set_cancel_event(self):
        app = self.make_app(url=PLAYLIST_URL, entries=[playlist_entry("aaaaaaaaaaa", "alpha")])
        gate = threading.Event()
        recorder = DownloadCallRecorder(gate=gate)
        controller = self.make_controller(app)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))
            job = app.queue_jobs[0]
            self.assertEqual(app.download_queue.cancel(job.id), "active")
            self.assertTrue(wait_for(lambda: recorder.calls[0]["cancel_event"].is_set()))
            gate.set()
            self.assertTrue(wait_for(lambda: "job_cancelled" in app.harness.event_names()))
        self.assertEqual(recorder.ranges, [(None, None)])

    def test_12_phase1_availability_filtering_still_applies_to_gui_jobs(self):
        entries = self.entries + [playlist_entry("priv0000001", "private one")]
        entries[-1]["availability"] = "private"
        app = self.make_app(url=PLAYLIST_URL, entries=entries)
        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, 3)
        self.assertEqual(len(recorder.calls), 3)
        self.assertNotIn("https://www.youtube.com/watch?v=priv0000001", recorder.urls)

    def test_gui_single_video_clip_is_unchanged(self):
        app = self.make_app(url=VIDEO_URL, entries=None, duration=200.0,
                            start_seconds=25, end_seconds=88)
        recorder = DownloadCallRecorder()
        controller = self.make_controller(app)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))
        call = recorder.calls[0]
        self.assertEqual((call["start_sec"], call["end_sec"]), (25, 88))
        self.assertEqual(call["url"], VIDEO_URL)
        self.assertEqual(len(app.queue_jobs), 1)

    def test_gui_invalid_range_still_shows_error_and_queues_nothing(self):
        app = self.make_app(url=VIDEO_URL, entries=None, duration=200.0,
                            start_seconds=100, end_seconds=10)
        recorder = DownloadCallRecorder()
        controller = self.make_controller(app)
        with recorder.patch(), \
                mock.patch.object(queue_controller_module, "messagebox") as messagebox, \
                self._no_config_write():
            controller.download_clip()
        messagebox.showerror.assert_called_once()
        self.assertEqual(messagebox.showerror.call_args[0][0], "Invalid range")
        self.assertEqual(recorder.calls, [])
        self.assertEqual(app.queue_jobs, [])

    def test_gui_range_beyond_duration_still_rejected(self):
        app = self.make_app(url=VIDEO_URL, entries=None, duration=60.0,
                            start_seconds=10, end_seconds=120)
        recorder = DownloadCallRecorder()
        controller = self.make_controller(app)
        with recorder.patch(), \
                mock.patch.object(queue_controller_module, "messagebox") as messagebox, \
                self._no_config_write():
            controller.download_clip()
        messagebox.showerror.assert_called_once()
        self.assertIn("duration", messagebox.showerror.call_args[0][1])
        self.assertEqual(recorder.calls, [])

    def test_gui_queueing_a_clip_empties_the_clip_range(self):
        """Queueing must not leave the queued range on screen for no video.

        `download_clip()` resets the form, and the clip range belongs to the
        video that was just queued: leaving it behind shows "No video loaded
        yet" next to a Start/End pair and a clip length that describe a video
        the app has already dropped, and leaves the sliders enabled over that
        video's scale.
        """
        app = TimeRangeApp(url=VIDEO_URL, entries=None, duration=200.0,
                           start_seconds=25, end_seconds=88,
                           output_path=self.tmp / "clip.mp4")
        # What _apply_video_metadata leaves behind for the loaded video.
        app.start_slider.configure(to=200, state="normal")
        app.end_slider.configure(to=200, state="normal")
        app.start_slider.set(25)
        app.end_slider.set(88)
        app.clip_length_label.configure(text="Clip length: 1:03")
        self.assertNotEqual(app.loaded_range_state(), EMPTY_RANGE)

        recorder = DownloadCallRecorder()
        controller = self.make_controller(app)
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))

        self.assertEqual(
            (recorder.calls[0]["start_sec"], recorder.calls[0]["end_sec"]),
            (25, 88), "the queued range itself must not change")
        self.assertEqual(
            app.loaded_range_state(), EMPTY_RANGE,
            "queueing a clip left the previous clip range on screen")


class TestGuiHasNoPlaylistRangeInput(unittest.TestCase):
    """Why "playlist + range" has no GUI counterpart: the range controls are
    hidden for playlists and the UI states every video downloads in full."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-loader-"))

    def _app(self, url):
        app = FakeApp(url=url, entries=None, output_path=self.tmp / "clip.mp4")
        app.loaded_url = None
        app.loaded_playlist_entries = None
        # start_load_video() increments this before the worker thread runs.
        app._load_request_id = 1
        self.addCleanup(app.harness.shutdown)
        return app

    def test_loading_a_playlist_hides_the_range_and_states_full_download(self):
        app = self._app(PLAYLIST_URL)
        controller = video_loader_module.VideoLoaderController(app)
        entries = [playlist_entry("aaaaaaaaaaa", "alpha")]

        controller._apply_playlist_metadata(1, PLAYLIST_URL, "Test Playlist", entries)

        self.assertIn(False, app.time_range_visibility)
        self.assertEqual(app.loaded_playlist_entries, entries)
        self.assertIsNone(app.video_duration)
        self.assertIn("Each video will be downloaded in full", app.video_info_label_text())

    def test_loading_a_single_video_shows_the_range(self):
        app = self._app(VIDEO_URL)
        controller = video_loader_module.VideoLoaderController(app)

        controller._apply_video_metadata(
            1, VIDEO_URL, "Single video", 200.0, {"duration": 200.0, "thumbnail": None}
        )

        self.assertIn(True, app.time_range_visibility)
        self.assertEqual(app.video_duration, 200.0)
        self.assertEqual(app.loaded_playlist_entries, None)


# ---------------------------------------------------------------------------
# Action-state consistency: the Download button vs. the Download action
# ---------------------------------------------------------------------------

OTHER_URL = "https://www.youtube.com/watch?v=someothervideo"


class ActionStateApp(FakeApp):
    """FakeApp with the shipped URL/loaded-video comparison bound to it.

    FakeApp answers unknown attributes with a permissive no-op, so the helper
    `_has_valid_download_data` calls would silently succeed and the button would
    look correct for the wrong reason. Binding the real implementation - the
    same approach `test_playlist_item_removal` uses for the download-button
    decision - makes the test observe the shipped logic.
    """

    def _url_names_the_loaded_video(self):
        return ClipperApp._url_names_the_loaded_video(self)


class TestDownloadActionStateConsistency(unittest.TestCase):
    """The Download button must describe the video the Download action will use.

    `download_clip()` picks its branch - and with it whether the end time is
    validated against the loaded duration - by comparing the URL in the box
    with the URL that was loaded:

        if app.loaded_url == url and app.video_duration is not None:  # clamp
        else: label = url                                          # no clamp

    The button's enabled state is computed by a different predicate
    (`_has_valid_download_data`) that never looks at the box at all, so the two
    can disagree: the button is green for loaded state belonging to a video the
    box no longer names, and the action it green-lights then runs the
    unvalidated branch.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ytclip-actionstate-"))

    def loaded_video_app(self, duration=200.0, start=25, end=88):
        """An app in the state `_apply_video_metadata` leaves after a load."""
        app = ActionStateApp(url=VIDEO_URL, entries=None, duration=duration,
                             start_seconds=start, end_seconds=end,
                             output_path=self.tmp / "clip.mp4")
        self.addCleanup(app.harness.shutdown)
        return app

    def retype_url(self, app, url):
        app.url_entry.delete(0, "end")
        app.url_entry.insert(0, url)

    # ---- the button state the GUI actually renders ----

    def test_the_button_is_green_for_the_url_that_was_loaded(self):
        app = self.loaded_video_app()
        self.assertTrue(ClipperApp._has_valid_download_data(app),
                        "a loaded video whose URL is still in the box is not "
                        "downloadable")

    def test_the_button_goes_dark_once_the_box_names_another_url(self):
        app = self.loaded_video_app()
        self.retype_url(app, OTHER_URL)
        self.assertFalse(
            ClipperApp._has_valid_download_data(app),
            "the Download button stayed enabled for a URL the app never loaded")

    def test_the_button_stays_dark_for_an_emptied_box(self):
        app = self.loaded_video_app()
        self.retype_url(app, "")
        self.assertFalse(ClipperApp._has_valid_download_data(app))

    # ---- what the stale green light actually allows ----

    def test_a_mismatched_url_queues_an_unvalidated_clip(self):
        """The defect, pinned: the guard the button was implying is skipped."""
        app = self.loaded_video_app(duration=60.0, start=150, end=180)
        self.retype_url(app, OTHER_URL)

        recorder = DownloadCallRecorder()
        controller = QueueController(app)
        controller.render_queue = lambda: None
        with recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(recorder.calls) == 1))

        self.assertEqual(len(recorder.calls), 1,
                         "the mismatched URL did not reach the downloader")
        self.assertEqual(recorder.calls[0]["url"], OTHER_URL)
        # 150-180 s is beyond the loaded video's 60 s and nothing complained:
        # the clamp lives behind the URL comparison the box no longer satisfies.
        self.assertEqual(recorder.calls[0]["start_sec"], 150)
        self.assertEqual(recorder.calls[0]["end_sec"], 180)
        self.assertEqual(app.queue_jobs[0].label, OTHER_URL,
                         "the row was not labelled with the raw URL")

    def test_the_same_range_is_refused_while_the_url_still_matches(self):
        """Control: the guard exists and is only skipped by the mismatch."""
        app = self.loaded_video_app(duration=60.0, start=150, end=180)

        recorder = DownloadCallRecorder()
        controller = QueueController(app)
        controller.render_queue = lambda: None
        with recorder.patch(), \
                mock.patch.object(queue_controller_module, "messagebox") as messagebox, \
                self._no_config_write():
            controller.download_clip()

        messagebox.showerror.assert_called_once()
        self.assertIn("duration", messagebox.showerror.call_args[0][1])
        self.assertEqual(recorder.calls, [])

    @staticmethod
    def _no_config_write():
        return mock.patch("yt_clipper.core.config.save_config", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# GUI/CLI parity
# ---------------------------------------------------------------------------

class TestGuiCliPlaylistParity(GuiTestCase):
    """Equivalent inputs through both surfaces must produce equivalent jobs."""

    def setUp(self):
        super().setUp()
        self.parity_entries = [
            playlist_entry("aaaaaaaaaaa", "alpha"),
            playlist_entry("bbbbbbbbbbb", "Beta"),   # exercises sanitize_filename equally
            playlist_entry("ccccccccccc", "gamma"),
        ]

    def test_playlist_without_range_produces_identical_download_jobs(self):
        gui_dir = Path(tempfile.mkdtemp(prefix="ytclip-parity-gui-"))
        cli_dir = Path(tempfile.mkdtemp(prefix="ytclip-parity-cli-"))

        gui_recorder = DownloadCallRecorder()
        app = self.make_app(url=PLAYLIST_URL, entries=self.parity_entries,
                            output_path=gui_dir / "clip.mp4",
                            start_seconds=25, end_seconds=88)  # hidden residual values
        self.run_playlist_download(app, gui_recorder, len(self.parity_entries))

        cli_recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(self.parity_entries)), \
                cli_recorder.patch():
            code, _, err = run_cli([PLAYLIST_URL, "-o", str(cli_dir / "clip.mp4")])
        self.assertEqual(code, 0, err)

        self.assertEqual(len(gui_recorder.calls), len(cli_recorder.calls))
        self.assertEqual(gui_recorder.urls, cli_recorder.urls)
        self.assertEqual(gui_recorder.filenames, cli_recorder.filenames)
        self.assertEqual(gui_recorder.ranges, cli_recorder.ranges)
        self.assertEqual(gui_recorder.ranges, [(None, None)] * len(self.parity_entries))
        self.assertEqual([c["quality"] for c in gui_recorder.calls],
                         [c["quality"] for c in cli_recorder.calls])
        self.assertEqual([c["audio_only"] for c in gui_recorder.calls],
                         [c["audio_only"] for c in cli_recorder.calls])

    def test_both_surfaces_share_playlist_detection_and_expansion(self):
        """Parity comes from shared core, not from duplicated rules."""
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(self.parity_entries)) as expand, \
                DownloadCallRecorder().patch():
            run_cli([PLAYLIST_URL, "-o", str(self.tmp / "clip.mp4")])
        expand.assert_called_once_with(PLAYLIST_URL)
        # The CLI validates arguments with the same offline helper the GUI uses.
        self.assertEqual(detect_youtube_url_type(PLAYLIST_URL), "playlist")
        self.assertEqual(detect_youtube_url_type(VIDEO_URL), "video")

    def test_gui_and_cli_single_video_ranges_agree(self):
        gui_recorder = DownloadCallRecorder()
        app = self.make_app(url=VIDEO_URL, entries=None, duration=200.0,
                            start_seconds=25, end_seconds=88)
        controller = self.make_controller(app)
        with gui_recorder.patch(), self._no_config_write():
            controller.download_clip()
            self.assertTrue(wait_for(lambda: len(gui_recorder.calls) == 1))

        cli_recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)), \
                cli_recorder.patch():
            code, _, err = run_cli([VIDEO_URL, "00:25", "01:28", "-o", str(self.tmp / "out.mp4")])
        self.assertEqual(code, 0, err)

        # Same range semantics; the GUI's TimeInput yields ints, the CLI's
        # time_to_seconds yields floats - download_clip normalizes both.
        self.assertEqual([(c["start_sec"], c["end_sec"]) for c in gui_recorder.calls], [(25, 88)])
        self.assertEqual([(c["start_sec"], c["end_sec"]) for c in cli_recorder.calls], [(25.0, 88.0)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
