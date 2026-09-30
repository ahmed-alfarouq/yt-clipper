"""Phase 4A — the canonical playlist availability boundary.

One implementation decides whether a playlist entry is AVAILABLE, UNAVAILABLE or
UNKNOWN (`playlist_utils.classify_video_entry`) and one implementation applies
that decision to a list of entries (`playlist_utils.filter_available_videos`).
These tests pin that boundary from the outside:

  * every consumer delegates to the canonical objects rather than keeping a
    private copy of the decision (Step 6);
  * each boundary applies the decision exactly once, so no list is filtered
    twice in a row (Step 4);
  * the semantics established in Phase 1 (tri-state, conservative fail-open,
    cancellation), Phase 2 (playlist clipping) and Phase 3 (observability) are
    unchanged at every one of those boundaries (Steps 1, 5, 7, 8).

Scaffolding is reused from tests/helpers.py and from the frozen Phase 1/2/3
modules instead of being duplicated: realistic flat/thin entry shapes and the
scripted yt-dlp driver come from test_playlist_availability, the loader app
double and log capture from test_failure_contracts, the queue app double and
driver from test_playlist_clipping.

Nothing here touches the network or creates a Tk window.
"""

import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F402  (sets sys.path + a throwaway XDG_CONFIG_HOME)

from helpers import (  # noqa: E402
    DownloadCallRecorder,
    flat_playlist,
    playlist_entry,
    run_cli,
)

from yt_clipper.core import downloader  # noqa: E402
from yt_clipper.core import playlist_utils  # noqa: E402
from yt_clipper.core import utils as legacy_utils  # noqa: E402

gui = helpers.import_gui_modules()  # noqa: E402

from yt_clipper.gui.controllers import queue_controller as queue_controller_module  # noqa: E402
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402

from test_playlist_availability import (  # noqa: E402
    DOWNLOAD_ERROR,
    expand,
    flat_entry,
    playlist_info,
    thin_entry,
    video_url,
)
from test_playlist_availability import PLAYLIST_URL  # noqa: E402
from test_failure_contracts import ContractTestCase, FakeLoaderApp  # noqa: E402
from test_playlist_clipping import GuiTestCase  # noqa: E402
from test_playlist_clipping import PLAYLIST_URL as CLIP_PLAYLIST_URL  # noqa: E402


# ---------------------------------------------------------------------------
# Local test doubles (only what the frozen scaffolding does not already give)
# ---------------------------------------------------------------------------

def counting(original):
    """Wrap `original`, counting calls while still returning its real answer.

    Used to prove *how often* the canonical decision is consulted, which is the
    only way to detect a duplicated filtering pass from the outside.
    """
    calls = []

    def wrapper(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    wrapper.calls = calls
    return wrapper


class RecordingPreview:
    """Records the dataset handed to the playlist preview widget."""

    def __init__(self):
        self.videos = None
        self.cleared = 0
        self.empty_state = None

    def set_videos(self, videos):
        self.videos = list(videos)

    def clear(self):
        self.cleared += 1

    def _show_empty_state(self, text):
        self.empty_state = text


class StubQueue:
    """Records enqueue() without starting a worker thread.

    The filtering decision is what is under test, not the download service, so
    no job is ever executed here.
    """

    def __init__(self):
        self.enqueued = []

    def enqueue(self, job):
        self.enqueued.append(job)
        return False


def private_entry(video_id, title=None):
    """A normalized entry that is *explicitly* unavailable."""
    entry = playlist_entry(video_id, title)
    entry["availability"] = "private"
    return entry


def deleted_entry(video_id):
    entry = playlist_entry(video_id)
    entry["title"] = "[Deleted video]"
    return entry


# ---------------------------------------------------------------------------
# The canonical implementation is shared, not copied
# ---------------------------------------------------------------------------

class TestCanonicalImplementationIsShared(ContractTestCase):
    def test_1_every_caller_binds_the_same_canonical_objects(self):
        """No consumer may hold a private copy of the availability decision."""
        self.assertIs(legacy_utils.classify_video_entry,
                      playlist_utils.classify_video_entry)
        self.assertIs(legacy_utils.is_video_entry_available,
                      playlist_utils.is_video_entry_available)
        self.assertIs(legacy_utils.is_video_available,
                      playlist_utils.is_video_available)
        self.assertIs(legacy_utils.filter_available_videos,
                      playlist_utils.filter_available_videos)
        self.assertIs(video_loader_module.filter_available_videos,
                      playlist_utils.filter_available_videos)
        self.assertIs(queue_controller_module.filter_available_videos,
                      playlist_utils.filter_available_videos)

    def test_2_the_vocabulary_is_exactly_three_distinct_states(self):
        states = {
            playlist_utils.AVAILABILITY_AVAILABLE,
            playlist_utils.AVAILABILITY_UNAVAILABLE,
            playlist_utils.AVAILABILITY_UNKNOWN,
        }
        self.assertEqual(states, {"available", "unavailable", "unknown"})

    def test_3_the_downloader_delegates_classification_to_the_canonical_module(self):
        """Overriding the canonical classifier changes the downloader's answer,
        which is only possible if the downloader has no copy of its own."""
        forced_unavailable = counting(
            lambda video: playlist_utils.AVAILABILITY_UNAVAILABLE)
        with mock.patch.object(playlist_utils, "classify_video_entry",
                               forced_unavailable):
            result, _ = expand({PLAYLIST_URL: playlist_info([
                flat_entry("aaaaaaaaaaa"), flat_entry("bbbbbbbbbbb"),
            ])})
        self.assertEqual(result["entries"], [],
                         "the downloader ignored the canonical UNAVAILABLE answer")
        self.assertTrue(forced_unavailable.calls,
                        "the canonical classifier was never consulted")

        forced_available = counting(
            lambda video: playlist_utils.AVAILABILITY_AVAILABLE)
        with mock.patch.object(playlist_utils, "classify_video_entry",
                               forced_available):
            result, calls = expand({PLAYLIST_URL: playlist_info([
                thin_entry("ccccccccccc"),
            ])})
        self.assertEqual(len(result["entries"]), 1)
        # AVAILABLE needs no per-item round trip: one flat listing request only.
        self.assertEqual(calls, [PLAYLIST_URL])


# ---------------------------------------------------------------------------
# The extraction boundary is what the CLI consumes
# ---------------------------------------------------------------------------

class TestExtractionBoundaryFeedsCallers(ContractTestCase):
    def test_4_the_cli_downloads_exactly_what_the_canonical_boundary_returned(self):
        """The CLI keeps no availability logic of its own and is still protected:
        an explicitly unavailable entry never becomes a download."""
        entries = [
            flat_entry("aaaaaaaaaaa"),
            flat_entry("priv0000001", availability="private"),
            flat_entry("priv0000002", title="[Deleted video]"),
            flat_entry("bbbbbbbbbbb"),
        ]
        recorder = DownloadCallRecorder()
        patcher, _ = helpers.patch_youtube_dl({PLAYLIST_URL: playlist_info(entries)})
        with patcher, recorder.patch(), self.capture() as cm:
            code, out, err = run_cli([PLAYLIST_URL, "--output", str(self.tmp)])

        self.assertEqual(code, 0, err)
        self.assertEqual(recorder.urls,
                         [video_url("aaaaaaaaaaa"), video_url("bbbbbbbbbbb")])
        self.assertEqual(recorder.ranges, [(None, None), (None, None)],
                         "Phase 2 playlist semantics changed: no range = full videos")
        self.assertNotIn("priv0000001", out + err)
        self.assertNotIn("priv0000002", out + err)
        # Expected filtering is not an error (§18).
        self.assertNoLevel(cm, "ERROR", "WARNING")


# ---------------------------------------------------------------------------
# The GUI load boundary (shared state -> preview + queue)
# ---------------------------------------------------------------------------

class TestGuiLoadBoundary(ContractTestCase):
    def setUp(self):
        super().setUp()
        self.app = FakeLoaderApp()
        self.app.playlist_preview = RecordingPreview()
        self.loader = video_loader_module.VideoLoaderController(self.app)

    def test_5_unavailable_entries_are_dropped_before_shared_state_and_preview(self):
        entries = [
            playlist_entry("aaaaaaaaaaa", "alpha"),
            private_entry("priv0000001", "private one"),
            deleted_entry("priv0000002"),
            playlist_entry("bbbbbbbbbbb", "beta"),
        ]
        expected = [entries[0]["url"], entries[3]["url"]]
        with self.capture() as cm:
            self.loader._apply_playlist_metadata(
                self.app._load_request_id, PLAYLIST_URL, "P", entries)

        kept = [entry["url"] for entry in self.app.loaded_playlist_entries]
        self.assertEqual(kept, expected)
        self.assertEqual([entry["url"] for entry in self.app.playlist_preview.videos],
                         kept, "preview and download no longer share one dataset")
        self.assertIn("2 available videos", self.app.video_info_label.text)
        self.assertNoLevel(cm, "ERROR", "WARNING")

    def test_6_unknown_entries_are_kept_at_the_gui_load_boundary(self):
        """Insufficient metadata is not unavailability: nothing is silently dropped."""
        thin = {"url": "https://www.youtube.com/watch?v=unknownvid1"}
        self.assertEqual(playlist_utils.classify_video_entry(thin),
                         playlist_utils.AVAILABILITY_UNKNOWN)
        entries = [thin, playlist_entry("aaaaaaaaaaa", "alpha")]
        with self.capture() as cm:
            self.loader._apply_playlist_metadata(
                self.app._load_request_id, PLAYLIST_URL, "P", entries)

        self.assertEqual(len(self.app.loaded_playlist_entries), 2)
        self.assertNoLevel(cm, "ERROR", "WARNING")


# ---------------------------------------------------------------------------
# The queue boundary (job creation)
# ---------------------------------------------------------------------------

class TestQueueBoundary(GuiTestCase):
    def test_7_the_job_creation_gate_filters_a_list_handed_to_it_directly(self):
        """Whatever reaches job creation is filtered there, so no caller can
        bypass the canonical decision by taking an internal path."""
        entries = self.entries + [private_entry("priv0000001", "private one")]
        app = self.make_app(url=CLIP_PLAYLIST_URL, entries=entries)
        controller = self.make_controller(app)
        app.download_queue = StubQueue()
        requested = Path(app._path_with_expected_extension(
            str(self.tmp / "clip.mp4"), False))

        with self._no_config_write():
            controller._enqueue_playlist(entries, False, requested)

        self.assertEqual([job.url for job in app.queue_jobs],
                         [entry["url"] for entry in self.entries])
        self.assertEqual(app.statuses[-1], "Queued 3 full videos from playlist")

    def test_8_unknown_entries_still_become_jobs(self):
        entries = [{"url": "https://www.youtube.com/watch?v=unknownvid1"},
                   playlist_entry("aaaaaaaaaaa", "alpha")]
        app = self.make_app(url=CLIP_PLAYLIST_URL, entries=entries)
        controller = self.make_controller(app)
        app.download_queue = StubQueue()

        with self._no_config_write():
            controller._enqueue_playlist(entries, False, Path(self.tmp / "clip.mp4"))

        self.assertEqual(len(app.queue_jobs), 2,
                         "an UNKNOWN entry was silently dropped at the queue")
        self.assertEqual([job.start_sec for job in app.queue_jobs], [None, None],
                         "Phase 2: playlist jobs are full videos")

    def test_9_an_unavailable_entry_never_reaches_a_download_job_end_to_end(self):
        """Loader boundary -> shared state -> queue boundary -> download calls."""
        raw = self.entries + [private_entry("priv0000001", "private one")]
        app = self.make_app(url=CLIP_PLAYLIST_URL, entries=None)
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_playlist_metadata(app._load_request_id, CLIP_PLAYLIST_URL,
                                        "P", raw)
        self.assertEqual(len(app.loaded_playlist_entries), 3,
                         "the load boundary did not filter the playlist")

        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, 3)
        self.assertEqual(len(recorder.calls), 3)
        self.assertNotIn("priv0000001", " ".join(recorder.urls))
        self.assertEqual(set(recorder.ranges), {(None, None)},
                         "Phase 2: every playlist job is a full video")

    def test_10_the_queue_path_applies_the_canonical_filter_once(self):
        """Filtering the same list twice in a row is the duplication this phase
        removes; the outcome must not depend on which pass did it."""
        counter = counting(playlist_utils.filter_available_videos)
        app = self.make_app(url=CLIP_PLAYLIST_URL, entries=self.entries)
        controller = self.make_controller(app)
        app.download_queue = StubQueue()

        with mock.patch.object(queue_controller_module, "filter_available_videos",
                               counter), self._no_config_write():
            controller.download_clip()

        self.assertEqual(len(app.queue_jobs), 3)
        self.assertEqual(len(counter.calls), 1,
                         "the canonical filter ran more than once for one queueing")


# ---------------------------------------------------------------------------
# One application of the canonical decision per boundary
# ---------------------------------------------------------------------------

class TestSingleApplicationPerBoundary(ContractTestCase):
    def test_11_the_extraction_boundary_decides_once_per_entry(self):
        entries = [flat_entry("aaaaaaaaaaa"), flat_entry("bbbbbbbbbbb"),
                   flat_entry("ccccccccccc")]
        counter = counting(playlist_utils.classify_video_entry)
        with mock.patch.object(playlist_utils, "classify_video_entry", counter):
            result, calls = expand({PLAYLIST_URL: playlist_info(entries)})

        self.assertEqual(len(result["entries"]), 3)
        self.assertEqual(len(counter.calls), 3,
                         "the canonical decision was applied more than once per entry")
        self.assertEqual(calls, [PLAYLIST_URL],
                         "a per-item extraction round trip appeared")

    def test_12_the_gui_load_path_applies_the_canonical_filter_once(self):
        counter = counting(playlist_utils.filter_available_videos)
        app = FakeLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        entries = [playlist_entry("aaaaaaaaaaa", "alpha"),
                   playlist_entry("bbbbbbbbbbb", "beta")]

        with mock.patch.object(video_loader_module, "filter_available_videos",
                               counter), \
                mock.patch.object(downloader, "expand_playlist",
                                  return_value=flat_playlist(entries)):
            loader._load_video_worker(app._load_request_id, PLAYLIST_URL)
            # The real app dispatches this event from its UI loop.
            loader._apply_playlist_metadata(*app.payload("playlist_metadata"))

        self.assertEqual(len(app.loaded_playlist_entries), 2)
        self.assertEqual(len(counter.calls), 1,
                         "the same list was filtered twice on the load path")


# ---------------------------------------------------------------------------
# The error-text half of the decision is canonical too
# ---------------------------------------------------------------------------

class TestCanonicalUnavailableErrorText(ContractTestCase):
    """Resolving an UNKNOWN entry ends in either metadata (classified by
    classify_video_entry) or an error - and the error wording is read here, in
    the same module, instead of by a private copy inside the downloader."""

    EXPLICIT = (
        "Private video. Sign in if you've been granted access to this video.",
        "Deleted video. This video has been removed by the uploader.",
        "This video is unavailable",
        "Video unavailable",
        "The video has been removed by the uploader",
        "removed",
        "deleted",
        "UNAVAILABLE",
    )
    INCONCLUSIVE = (
        "The uploader has not made this video available in your country",
        "Sign in to confirm your age",
        "HTTP Error 403: Forbidden",
        "Unable to download webpage: <urlopen error name resolution failed>",
        "",
        None,
    )

    def test_13_explicit_unavailability_wording_is_recognised(self):
        for text in self.EXPLICIT:
            self.assertTrue(playlist_utils.is_unavailable_error_text(text), text)
        # The downloader passes the exception itself, not a formatted string.
        self.assertTrue(playlist_utils.is_unavailable_error_text(
            DOWNLOAD_ERROR(self.EXPLICIT[0])))

    def test_14_inconclusive_wording_is_not_treated_as_unavailable(self):
        """Conservative fail-open (§4): an error we cannot interpret keeps the
        entry so the download itself can report a real failure."""
        for text in self.INCONCLUSIVE:
            self.assertFalse(playlist_utils.is_unavailable_error_text(text), repr(text))

    def test_15_the_downloader_delegates_the_error_text_decision(self):
        """A geo-block normally KEEPS the entry; forcing the canonical answer to
        True must drop it, which only works if the downloader asks the module."""
        entries = [thin_entry("kkkkkkkkkkk")]
        script = {
            PLAYLIST_URL: playlist_info(entries),
            video_url("kkkkkkkkkkk"): DOWNLOAD_ERROR(
                "The uploader has not made this video available in your country"),
        }

        with self.capture():
            result, _ = expand(script)
        self.assertEqual(len(result["entries"]), 1,
                         "baseline changed: an inconclusive error must keep the entry")

        with mock.patch.object(playlist_utils, "is_unavailable_error_text",
                               return_value=True), self.capture():
            result, _ = expand(script)
        self.assertEqual(result["entries"], [],
                         "the downloader still decides unavailable wording itself")


if __name__ == "__main__":
    unittest.main()
