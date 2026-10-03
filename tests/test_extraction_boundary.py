"""Characterization tests for the yt-dlp extraction boundary (Phase 4H).

`core/downloader.py` extracts metadata in two places - `_extract_info()` for a
single video and `expand_playlist()` for a playlist listing - and both wrapped
the *same* three-line closure around `yt_dlp.YoutubeDL` before handing it to the
shared retry policy. Phase 4H consolidates that duplicated closure into one
private helper.

These tests pin the observable contract at the yt-dlp boundary so the
consolidation can be proven behavior-preserving rather than merely "the suite
still passes":

  * which options each path hands to `yt_dlp.YoutubeDL` - they differ, and that
    difference IS the single-video vs playlist contract;
  * that extraction never downloads (`download=False`);
  * that every attempt builds a FRESH `YoutubeDL` and closes its own context
    manager, so a transient failure can never reuse a broken session;
  * that a non-transient failure is not retried;
  * that cancellation performs no extraction at all and stops the retry loop
    (no retry after cancellation);
  * that retry notifications reach the caller's hook from both paths.

Everything drives the PUBLIC entry points (`get_video_info`, `list_formats`,
`expand_playlist`); the private closure is never named, so these tests remain
valid across the refactor and assert behavior, not implementation.

No network access happens: `yt_dlp.YoutubeDL` is replaced with a scripted fake,
and `time.sleep` is neutralised wherever a retry backoff would occur.

Run with:  python -m unittest discover -s tests -v
      or:  python -m pytest tests -q
"""

import threading
import unittest
from unittest import mock

import helpers  # noqa: F401  (sets sys.path + a throwaway XDG_CONFIG_HOME)

from yt_clipper.core import downloader  # noqa: E402
from yt_clipper.core.errors import DownloadCancelled  # noqa: E402

VIDEO_URL = "https://www.youtube.com/watch?v=boundaryvid1"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBOUNDARY00"
AMBIGUOUS_ID = "ccccccccccc"
AMBIGUOUS_URL = f"https://www.youtube.com/watch?v={AMBIGUOUS_ID}"

#: Contains downloader's "http error 503" marker, so _retry_call treats it as
#: transient and retries.
TRANSIENT_ERROR = "HTTP Error 503: Service Unavailable"
#: Contains none of the transient markers, so it must NOT be retried.
FATAL_ERROR = "Video unavailable"


def single_video_info(video_id="boundaryvid1", title="Boundary video", duration=120):
    """An extraction result that is not a playlist, so _reject_playlist passes."""
    return {
        "id": video_id,
        "title": title,
        "duration": duration,
        "thumbnail": None,
        "upload_date": "20240101",
        "timestamp": 1704067200,
        "formats": [
            {"format_id": "18", "ext": "mp4", "height": 360, "vbr": 800, "abr": 96,
             "vcodec": "avc1.42001E", "acodec": "mp4a.40.2"},
        ],
    }


def playlist_info(entries, title="Boundary list"):
    return {"_type": "playlist", "playlist_title": title, "entries": entries}


def available_entry(video_id):
    """A flat entry that classifies AVAILABLE, so no full check is triggered."""
    return {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Video {video_id}",
        "availability": "public",
        "channel": "Boundary Channel",
        "channel_id": "UCboundary0000",
        "duration": 100,
    }


def ambiguous_entry(video_id):
    """A flat entry with no evidence either way: classify_video_entry -> UNKNOWN,
    which makes expand_playlist perform a full extraction of that one video."""
    return {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}"}


class ExtractionRecorder:
    """Replaces yt_dlp.YoutubeDL and records the whole boundary interaction.

    `script` maps a URL to the info dict to return; `result` is the fallback for
    any URL. `fail_times` makes the first N extract_info() calls raise `error`,
    so retry behavior is observable without a network.
    """

    def __init__(self, result=None, script=None, fail_times=0, error=None):
        self.result = single_video_info() if result is None else result
        self.script = script or {}
        self.fail_times = fail_times
        self.error = error or TRANSIENT_ERROR
        self.instances = []     # the options dict of every constructed YoutubeDL
        self.extractions = []   # (url, download) for every extract_info() call
        self.exited = 0         # __exit__ calls: proves each context manager closed
        self._calls = 0

    def patch(self):
        recorder = self

        class RecordingYoutubeDL:
            def __init__(self, options=None):
                recorder.instances.append(dict(options or {}))

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                recorder.exited += 1
                return False

            def extract_info(self, url, download=False):
                recorder.extractions.append((url, download))
                recorder._calls += 1
                if recorder._calls <= recorder.fail_times:
                    raise RuntimeError(recorder.error)
                if url in recorder.script:
                    return recorder.script[url]
                return recorder.result

        return mock.patch.object(downloader.yt_dlp, "YoutubeDL", RecordingYoutubeDL)


class TestSingleVideoExtractionBoundary(unittest.TestCase):
    """get_video_info()/list_formats() -> _extract_info()."""

    def test_1_the_single_video_path_forbids_playlists_and_never_downloads(self):
        recorder = ExtractionRecorder()
        with recorder.patch():
            info = downloader.get_video_info(VIDEO_URL)

        self.assertEqual(len(recorder.instances), 1,
                         "one extraction must build exactly one YoutubeDL")
        options = recorder.instances[0]
        self.assertIs(options.get("quiet"), True)
        self.assertIs(options.get("noplaylist"), True,
                      "the single-video path must forbid playlist expansion")
        self.assertIn("logger", options, "the filtered yt-dlp logger was not installed")
        self.assertEqual(recorder.extractions, [(VIDEO_URL, False)],
                         "extraction must not download")
        self.assertEqual(recorder.exited, 1, "the context manager did not close")
        self.assertEqual(info["title"], "Boundary video")
        self.assertEqual(info["duration"], 120)

    def test_2_list_formats_uses_the_same_single_video_extraction(self):
        recorder = ExtractionRecorder()
        with recorder.patch():
            listed = downloader.list_formats(VIDEO_URL)

        self.assertEqual(len(recorder.instances), 1)
        self.assertIs(recorder.instances[0].get("noplaylist"), True)
        self.assertEqual(recorder.extractions, [(VIDEO_URL, False)])
        self.assertEqual([f["format_id"] for f in listed], ["18"])
        self.assertEqual(listed[0]["kind"], "video+audio")

    def test_3_a_playlist_result_is_still_rejected_on_the_single_video_path(self):
        recorder = ExtractionRecorder(
            result=playlist_info([available_entry("aaaaaaaaaaa")]))
        with recorder.patch():
            with self.assertRaises(ValueError) as ctx:
                downloader.get_video_info(VIDEO_URL)

        self.assertIn("playlist", str(ctx.exception).lower())
        self.assertEqual(len(recorder.instances), 1)
        self.assertEqual(recorder.extractions, [(VIDEO_URL, False)])


class TestPlaylistExtractionBoundary(unittest.TestCase):
    """expand_playlist(): the flat listing, and the full check it may trigger."""

    def test_4_the_playlist_path_uses_the_flat_listing_options(self):
        recorder = ExtractionRecorder(result=playlist_info(
            [available_entry("aaaaaaaaaaa"), available_entry("bbbbbbbbbbb")]))
        with recorder.patch():
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual(len(recorder.instances), 1,
                         "a flat listing needs exactly one extraction")
        options = recorder.instances[0]
        self.assertIs(options.get("quiet"), True)
        self.assertEqual(options.get("extract_flat"), "in_playlist")
        self.assertEqual(options.get("extractor_args"),
                         {"youtubetab": {"approximate_date": ["true"]}})
        self.assertNotIn("noplaylist", options,
                         "the playlist path must not forbid playlist expansion")
        self.assertNotIn("playlistend", options,
                         "no limit was requested, so none may be sent")
        self.assertEqual(recorder.extractions, [(PLAYLIST_URL, False)])
        self.assertEqual(recorder.exited, 1)
        self.assertTrue(result["is_playlist"])
        self.assertEqual(len(result["entries"]), 2)

    def test_5_max_videos_becomes_playlistend_without_disturbing_the_rest(self):
        recorder = ExtractionRecorder(result=playlist_info(
            [available_entry("aaaaaaaaaaa")]))
        with recorder.patch():
            downloader.expand_playlist(PLAYLIST_URL, max_videos=7)

        options = recorder.instances[0]
        self.assertEqual(options.get("playlistend"), 7)
        self.assertEqual(options.get("extract_flat"), "in_playlist")
        self.assertIs(options.get("quiet"), True)
        self.assertEqual(len(recorder.instances), 1)

    def test_6_an_ambiguous_entry_triggers_a_second_single_video_extraction(self):
        """The UNKNOWN full check must cross the *single-video* boundary: its own
        YoutubeDL, with noplaylist, and still no download."""
        recorder = ExtractionRecorder(script={
            PLAYLIST_URL: playlist_info([ambiguous_entry(AMBIGUOUS_ID)]),
            AMBIGUOUS_URL: single_video_info(AMBIGUOUS_ID, "Recovered", 90),
        })
        with recorder.patch():
            result = downloader.expand_playlist(PLAYLIST_URL)

        self.assertEqual(len(recorder.instances), 2,
                         "the flat listing plus one full check must build two")
        listing, full_check = recorder.instances
        self.assertEqual(listing.get("extract_flat"), "in_playlist")
        self.assertNotIn("noplaylist", listing)
        self.assertIs(full_check.get("noplaylist"), True)
        self.assertNotIn("extract_flat", full_check)
        self.assertEqual(recorder.extractions,
                         [(PLAYLIST_URL, False), (AMBIGUOUS_URL, False)])
        self.assertEqual(recorder.exited, 2)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["title"], "Recovered")


class TestRetryAndCancellationAtTheBoundary(unittest.TestCase):
    """The retry/cancellation policy as observable through both paths."""

    def test_7_a_transient_failure_builds_a_fresh_youtubeldl_for_the_next_attempt(self):
        recorder = ExtractionRecorder(fail_times=1)
        notified = []
        with recorder.patch(), \
                mock.patch.object(downloader.time, "sleep", lambda seconds: None):
            info = downloader.get_video_info(
                VIDEO_URL,
                on_retry=lambda attempt, maximum, delay, exc: notified.append(
                    (attempt, maximum)),
            )

        self.assertEqual(len(recorder.instances), 2,
                         "each attempt must build a NEW YoutubeDL, never reuse one")
        self.assertEqual(len(recorder.extractions), 2)
        self.assertEqual(recorder.exited, 2,
                         "each attempt must close its own context manager")
        self.assertEqual([url for url, _ in recorder.extractions],
                         [VIDEO_URL, VIDEO_URL])
        self.assertTrue(all(download is False for _, download in recorder.extractions))
        self.assertEqual(notified, [(1, downloader.DEFAULT_MAX_ATTEMPTS)])
        self.assertEqual(info["title"], "Boundary video")

    def test_8_the_playlist_path_retries_the_same_way(self):
        recorder = ExtractionRecorder(
            result=playlist_info([available_entry("aaaaaaaaaaa")]), fail_times=1)
        notified = []
        with recorder.patch(), \
                mock.patch.object(downloader.time, "sleep", lambda seconds: None):
            result = downloader.expand_playlist(
                PLAYLIST_URL,
                on_retry=lambda attempt, maximum, delay, exc: notified.append(attempt),
            )

        self.assertEqual(len(recorder.instances), 2,
                         "each attempt must build a NEW YoutubeDL, never reuse one")
        self.assertEqual(recorder.exited, 2)
        self.assertEqual(recorder.extractions,
                         [(PLAYLIST_URL, False), (PLAYLIST_URL, False)])
        self.assertEqual(notified, [1])
        self.assertEqual(len(result["entries"]), 1)

    def test_9_a_non_transient_failure_is_not_retried(self):
        recorder = ExtractionRecorder(fail_times=5, error=FATAL_ERROR)
        with recorder.patch():
            with self.assertRaises(RuntimeError) as ctx:
                downloader.get_video_info(VIDEO_URL)

        self.assertIn("unavailable", str(ctx.exception).lower())
        self.assertEqual(len(recorder.instances), 1,
                         "a permanent failure must not build a second YoutubeDL")
        self.assertEqual(len(recorder.extractions), 1)

    def test_10_cancellation_before_the_first_attempt_performs_no_extraction(self):
        recorder = ExtractionRecorder()
        cancel = threading.Event()
        cancel.set()
        with recorder.patch():
            with self.assertRaises(DownloadCancelled):
                downloader.get_video_info(VIDEO_URL, cancel_event=cancel)

        self.assertEqual(recorder.instances, [],
                         "no YoutubeDL may be built once cancellation is requested")
        self.assertEqual(recorder.extractions, [])

    def test_11_cancellation_stops_the_retry_loop_on_the_playlist_path(self):
        """No retry after cancellation: the loop must stop before building again."""
        recorder = ExtractionRecorder(
            result=playlist_info([available_entry("aaaaaaaaaaa")]), fail_times=5)
        cancel = threading.Event()
        with recorder.patch(), \
                mock.patch.object(downloader.time, "sleep", lambda seconds: None):
            with self.assertRaises(DownloadCancelled):
                downloader.expand_playlist(
                    PLAYLIST_URL,
                    cancel_event=cancel,
                    on_retry=lambda attempt, maximum, delay, exc: cancel.set(),
                )

        self.assertEqual(len(recorder.instances), 1,
                         "cancellation must stop the retry loop immediately")
        self.assertEqual(len(recorder.extractions), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
