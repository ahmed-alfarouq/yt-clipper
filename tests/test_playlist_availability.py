"""Focused tests for playlist availability detection (Phase 1).

These cover the bug where a valid YouTube video was silently dropped from a
playlist because its *flat* entry carried no title/availability metadata, and
the follow-on bug that the intended "ambiguous entry" full-check path could
never execute.

Runs with the standard library only:

    python -m unittest discover -s tests -v
    python -m pytest tests -q          # also works if pytest is installed

No network access happens: yt_dlp.YoutubeDL is replaced per test with a
scripted fake. If yt-dlp itself is not installed (it is a runtime dependency,
not needed to exercise this logic), a minimal stand-in module is registered so
`yt_clipper.core.downloader` stays importable.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _ensure_yt_dlp_importable():
    """Register a stub `yt_dlp` package when the real one is not installed.

    Only the three names downloader.py imports at module level are provided.
    Returns True when the stub is in use.
    """
    try:
        import yt_dlp  # noqa: F401
        import yt_dlp.utils  # noqa: F401
        from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor  # noqa: F401
        return False
    except ImportError:
        pass

    class DownloadError(Exception):
        """Stand-in for yt_dlp.utils.DownloadError."""

    class FFmpegPostProcessor:
        """Stand-in; never used because these tests do not download."""

    yt_dlp = types.ModuleType("yt_dlp")
    utils = types.ModuleType("yt_dlp.utils")
    postprocessor = types.ModuleType("yt_dlp.postprocessor")
    ffmpeg = types.ModuleType("yt_dlp.postprocessor.ffmpeg")

    yt_dlp.YoutubeDL = object
    yt_dlp.utils = utils
    yt_dlp.postprocessor = postprocessor
    utils.DownloadError = DownloadError
    postprocessor.ffmpeg = ffmpeg
    ffmpeg.FFmpegPostProcessor = FFmpegPostProcessor

    sys.modules["yt_dlp"] = yt_dlp
    sys.modules["yt_dlp.utils"] = utils
    sys.modules["yt_dlp.postprocessor"] = postprocessor
    sys.modules["yt_dlp.postprocessor.ffmpeg"] = ffmpeg
    return True


USING_YT_DLP_STUB = _ensure_yt_dlp_importable()

import yt_dlp  # noqa: E402  (real package or the stub registered above)

from yt_clipper.core import downloader  # noqa: E402
from yt_clipper.core import playlist_utils as pu  # noqa: E402
from yt_clipper.core.downloader import DownloadCancelled, expand_playlist  # noqa: E402

DOWNLOAD_ERROR = yt_dlp.utils.DownloadError

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLTEST0000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def flat_entry(video_id, **overrides):
    """A realistic yt-dlp *flat* playlist entry (extract_flat="in_playlist")."""
    entry = {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Video {video_id}",
        "availability": "public",
        "channel": "Test Channel",
        "channel_id": "UCtest0000000",
        "channel_url": "https://www.youtube.com/channel/UCtest0000000",
        "duration": 100,
        "view_count": 42,
    }
    entry.update(overrides)
    return entry


def thin_entry(video_id, **overrides):
    """A lockupViewModel-style entry: no title, no availability, no channel.

    This is the shape yt-dlp reports for hidden/private playlist items
    (boul2gom/yt-dlp#318) - and, before the fix, also the shape that made a
    merely thin-but-valid entry look private.
    """
    entry = {
        "id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": None,
        "availability": None,
        "duration": None,
        "view_count": None,
        "channel": None,
        "channel_id": None,
        "channel_url": None,
        "uploader": None,
        "uploader_id": None,
        "uploader_url": None,
    }
    entry.update(overrides)
    return entry


def playlist_info(entries, title="Test Playlist"):
    return {"_type": "playlist", "title": title, "entries": entries}


def patch_youtube_dl(script):
    """Replace yt_dlp.YoutubeDL with a scripted fake.

    `script` maps a URL to the value its extraction returns, an exception
    instance to raise, or a zero-arg callable producing either (used to mutate
    test state mid-extraction). Returns (patcher, calls) where `calls` records
    every URL yt-dlp was asked for, in order.
    """
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options=None):
            self.options = options or {}
            self.cookiejar = None
            self.params = dict(self.options)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=False):
            calls.append(url)
            if url not in script:
                raise AssertionError(f"unexpected yt-dlp extraction for {url!r}")
            outcome = script[url]
            if callable(outcome):
                outcome = outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    return mock.patch.object(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL), calls


def expand(script, url=PLAYLIST_URL, **kwargs):
    """Run expand_playlist() against a scripted yt-dlp; returns (result, calls)."""
    patcher, calls = patch_youtube_dl(script)
    with patcher:
        result = expand_playlist(url, **kwargs)
    return result, calls


def video_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# Case A/B/C/D/E at the classifier level (no I/O)
# ---------------------------------------------------------------------------

class TestClassifyVideoEntry(unittest.TestCase):
    """classify_video_entry() is the single authoritative decision point."""

    def test_case_a_explicitly_available_entry_is_available(self):
        entry = flat_entry("aaaaaaaaaaa")
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)
        self.assertTrue(pu.is_video_entry_available(entry))

    def test_case_b_unavailable_availability_values_are_rejected(self):
        for value in sorted(pu._UNAVAILABLE_AVAILABILITY):
            with self.subTest(availability=value):
                entry = flat_entry("bbbbbbbbbbb", availability=value)
                self.assertEqual(
                    pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE
                )
                self.assertFalse(pu.is_video_entry_available(entry))

    def test_case_b_placeholder_titles_are_rejected(self):
        # Exactly the marker set the project already used - this phase must not
        # widen or narrow it.
        for title in (
            "[Private video]",
            "[Deleted video]",
            "[Unavailable]",
            "Private video",
            "Deleted video",
            "Unavailable video",
            "Video unavailable",
            "Video has been removed by the uploader",
        ):
            with self.subTest(title=title):
                entry = flat_entry("bbbbbbbbbbb", title=title)
                self.assertEqual(
                    pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE
                )

    def test_explicit_signal_beats_positive_channel_evidence(self):
        """Filtering must not be weakened: a private entry that still carries
        channel metadata is still dropped."""
        entry = flat_entry("bbbbbbbbbbb", availability="private")
        self.assertEqual(
            pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE
        )
        placeholder = flat_entry("bbbbbbbbbbb", title="[Deleted video]")
        self.assertEqual(
            pu.classify_video_entry(placeholder), pu.AVAILABILITY_UNAVAILABLE
        )

    def test_case_c_missing_title_with_channel_metadata_is_available(self):
        """The regression this phase fixes: thin metadata != unavailable."""
        entry = flat_entry("ccccccccccc", title=None, availability=None)
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)
        self.assertTrue(pu.is_video_entry_available(entry))

    def test_case_c_duration_alone_is_sufficient_evidence(self):
        entry = thin_entry("ccccccccccc", duration=321)
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)

    def test_case_d_missing_availability_field_is_available(self):
        entry = flat_entry("ddddddddddd", availability=None)
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)
        self.assertTrue(pu.is_video_entry_available(entry))

    def test_case_e_entry_with_no_evidence_at_all_is_unknown(self):
        entry = thin_entry("eeeeeeeeeee")
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_UNKNOWN)

    def test_case_e_unknown_is_not_reported_as_unavailable(self):
        """Missing metadata must never be silently treated as 'private'."""
        entry = thin_entry("eeeeeeeeeee")
        self.assertIsNot(pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE)
        self.assertTrue(pu.is_video_entry_available(entry))

    def test_unlisted_and_public_availability_are_available(self):
        for value in ("public", "unlisted"):
            with self.subTest(availability=value):
                entry = thin_entry("fffffffffff", availability=value)
                self.assertEqual(
                    pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE
                )

    def test_entry_without_any_url_is_unavailable(self):
        for entry in ({"title": "No url"}, {"id": None, "title": "No url"}):
            with self.subTest(entry=entry):
                self.assertEqual(
                    pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE
                )

    def test_bare_video_id_url_is_not_rejected(self):
        """Flat entries may carry a bare id instead of a watch URL."""
        entry = {"id": "ggggggggggg", "url": "ggggggggggg", "title": "Video g"}
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)

    def test_webpage_url_is_accepted_as_url(self):
        entry = {"webpage_url": video_url("hhhhhhhhhhh"), "title": "Video h"}
        self.assertEqual(pu.classify_video_entry(entry), pu.AVAILABILITY_AVAILABLE)

    def test_normalized_downloader_entry_is_available(self):
        """Entries stored in shared state (title falls back to the URL) must
        survive the defensive re-filters in video_loader/queue_controller."""
        normalized = {
            "url": video_url("iiiiiiiiiii"),
            "title": video_url("iiiiiiiiiii"),
            "duration": None,
            "thumbnail": None,
            "publish_date": None,
            "upload_date": None,
            "timestamp": None,
            "release_timestamp": None,
            "availability": None,
        }
        self.assertEqual(
            pu.classify_video_entry(normalized), pu.AVAILABILITY_AVAILABLE
        )

    def test_non_dict_and_empty_inputs_are_unavailable(self):
        for entry in (None, {}, [], "https://youtube.com/watch?v=x", 42):
            with self.subTest(entry=entry):
                self.assertEqual(
                    pu.classify_video_entry(entry), pu.AVAILABILITY_UNAVAILABLE
                )
                self.assertFalse(pu.is_video_entry_available(entry))

    def test_unexpected_field_types_never_raise(self):
        """Classification is total: odd metadata is treated as absent."""
        hostile = {
            "url": 12345,
            "title": object(),
            "availability": 3.5,
            "channel": [],
            "duration": {},
        }
        try:
            decision = pu.classify_video_entry(hostile)
        except Exception as exc:  # pragma: no cover - failure path
            self.fail(f"classify_video_entry raised {exc!r}")
        self.assertIn(
            decision,
            (pu.AVAILABILITY_AVAILABLE, pu.AVAILABILITY_UNAVAILABLE, pu.AVAILABILITY_UNKNOWN),
        )

    def test_is_video_available_alias_matches(self):
        entry = thin_entry("jjjjjjjjjjj")
        self.assertEqual(
            pu.is_video_available(entry), pu.is_video_entry_available(entry)
        )


class TestFilterAvailableVideos(unittest.TestCase):
    def test_keeps_available_and_unknown_drops_unavailable(self):
        entries = [
            flat_entry("aaaaaaaaaaa"),                    # available
            thin_entry("eeeeeeeeeee"),                    # unknown -> kept
            flat_entry("bbbbbbbbbbb", availability="private"),   # unavailable
            flat_entry("bbbbbbbbbbb", title="[Private video]"),  # unavailable
            flat_entry("ccccccccccc", title=None, availability=None),  # available
        ]
        kept = pu.filter_available_videos(entries)
        self.assertEqual([e["id"] for e in kept], ["aaaaaaaaaaa", "eeeeeeeeeee", "ccccccccccc"])

    def test_does_not_mutate_input(self):
        entries = [flat_entry("aaaaaaaaaaa"), flat_entry("bbbbbbbbbbb", availability="private")]
        snapshot = [dict(e) for e in entries]
        pu.filter_available_videos(entries)
        self.assertEqual(entries, snapshot)
        self.assertEqual(len(entries), 2)

    def test_empty_input(self):
        self.assertEqual(pu.filter_available_videos([]), [])
        self.assertEqual(pu.filter_available_videos(None), [])

    def test_utils_reexports_new_classifier(self):
        """downloader's compatibility import falls back to core.utils."""
        from yt_clipper.core import utils

        self.assertIs(utils.classify_video_entry, pu.classify_video_entry)
        self.assertIs(utils.AVAILABILITY_UNAVAILABLE, pu.AVAILABILITY_UNAVAILABLE)
        self.assertIs(utils.AVAILABILITY_UNKNOWN, pu.AVAILABILITY_UNKNOWN)
        self.assertIs(utils.is_video_entry_available, pu.is_video_entry_available)


# ---------------------------------------------------------------------------
# expand_playlist(): end-to-end filtering with yt-dlp mocked at its boundary
# ---------------------------------------------------------------------------

class TestExpandPlaylistAvailability(unittest.TestCase):
    def test_case_a_available_entries_are_returned(self):
        entries = [flat_entry("aaaaaaaaaaa"), flat_entry("aaaaaaaaaab")]
        result, calls = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertTrue(result["is_playlist"])
        self.assertEqual([e["url"] for e in result["entries"]],
                         [video_url("aaaaaaaaaaa"), video_url("aaaaaaaaaab")])
        # One flat listing request only: no per-item round trips.
        self.assertEqual(calls, [PLAYLIST_URL])

    def test_case_b_explicitly_unavailable_entries_are_filtered(self):
        entries = [
            flat_entry("aaaaaaaaaaa"),
            flat_entry("p1", availability="private"),
            flat_entry("p2", title="[Private video]"),
            flat_entry("p3", availability="premium_only"),
            flat_entry("p4", title="[Deleted video]"),
            flat_entry("aaaaaaaaaab"),
        ]
        result, calls = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertEqual([e["url"] for e in result["entries"]],
                         [video_url("aaaaaaaaaaa"), video_url("aaaaaaaaaab")])
        self.assertEqual(calls, [PLAYLIST_URL])

    def test_case_c_missing_title_with_channel_metadata_is_kept(self):
        """Valid video, thin flat metadata: kept, and with no extra request."""
        entries = [flat_entry("ccccccccccc", title=None, availability=None)]
        result, calls = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertEqual(len(result["entries"]), 1)
        kept = result["entries"][0]
        self.assertEqual(kept["url"], video_url("ccccccccccc"))
        # Pre-existing normalization: a missing title falls back to the URL.
        self.assertEqual(kept["title"], video_url("ccccccccccc"))
        self.assertEqual(calls, [PLAYLIST_URL])

    def test_case_d_missing_availability_is_kept(self):
        entries = [flat_entry("ddddddddddd", availability=None)]
        result, _ = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["title"], "Video ddddddddddd")

    def test_case_e_ambiguous_entry_reaches_the_full_check(self):
        """The full-check path used to be unreachable; prove it now runs."""
        entries = [thin_entry("eeeeeeeeeee")]
        full = {
            "id": "eeeeeeeeeee",
            "webpage_url": video_url("eeeeeeeeeee"),
            "title": "Recovered title",
            "availability": "public",
            "duration": 123,
            "channel": "Some Channel",
        }
        result, calls = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("eeeeeeeeeee"): full,
        })
        # Case E: the single-video extraction actually happened.
        self.assertEqual(calls, [PLAYLIST_URL, video_url("eeeeeeeeeee")])
        self.assertEqual(len(result["entries"]), 1)

    def test_case_g_full_check_available_keeps_entry_and_fills_metadata(self):
        entries = [thin_entry("ggggggggggg")]
        full = {
            "id": "ggggggggggg",
            "webpage_url": video_url("ggggggggggg"),
            "title": "Recovered title",
            "availability": "public",
            "duration": 123,
            "thumbnail": "https://i.ytimg.com/vi/ggggggggggg/hqdefault.jpg",
            "upload_date": "20240115",
            "channel": "Some Channel",
        }
        result, calls = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("ggggggggggg"): full,
        })
        self.assertEqual(calls, [PLAYLIST_URL, video_url("ggggggggggg")])
        self.assertEqual(len(result["entries"]), 1)
        kept = result["entries"][0]
        # Metadata recovered from the full check instead of the URL fallback.
        self.assertEqual(kept["title"], "Recovered title")
        self.assertEqual(kept["duration"], 123)
        self.assertEqual(kept["availability"], "public")
        self.assertEqual(kept["thumbnail"], full["thumbnail"])
        self.assertEqual(kept["upload_date"], "20240115")
        self.assertIsNotNone(kept["publish_date"])

    def test_case_f_full_check_private_video_is_filtered(self):
        entries = [thin_entry("ffff0000001"), flat_entry("aaaaaaaaaaa")]
        result, calls = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("ffff0000001"): DOWNLOAD_ERROR(
                "Private video. Sign in if you've been granted access to this video."
            ),
        })
        self.assertEqual(calls, [PLAYLIST_URL, video_url("ffff0000001")])
        self.assertEqual([e["url"] for e in result["entries"]], [video_url("aaaaaaaaaaa")])

    def test_case_f_full_check_deleted_video_is_filtered(self):
        entries = [thin_entry("ffff0000002")]
        result, _ = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("ffff0000002"): DOWNLOAD_ERROR(
                "Deleted video. This video has been removed by the uploader."
            ),
        })
        self.assertEqual(result["entries"], [])

    def test_case_f_full_check_returning_placeholder_title_is_filtered(self):
        entries = [thin_entry("ffff0000003")]
        result, _ = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("ffff0000003"): {
                "id": "ffff0000003",
                "webpage_url": video_url("ffff0000003"),
                "title": "[Private video]",
                "availability": "private",
            },
        })
        self.assertEqual(result["entries"], [])

    def test_case_h_exception_during_full_check_keeps_entry(self):
        """Uninterpretable failure -> conservative keep, never a silent drop."""
        entries = [thin_entry("hhhhhhhhhhh")]
        result, calls = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("hhhhhhhhhhh"): RuntimeError("unexpected extractor failure"),
        })
        self.assertEqual(calls, [PLAYLIST_URL, video_url("hhhhhhhhhhh")])
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["url"], video_url("hhhhhhhhhhh"))
        # No metadata to recover from, so the existing URL fallback applies.
        self.assertEqual(result["entries"][0]["title"], video_url("hhhhhhhhhhh"))

    def test_unrecognized_download_error_keeps_entry(self):
        """e.g. geo-blocked / age-restricted: report at download time, don't drop."""
        entries = [thin_entry("kkkkkkkkkkk")]
        result, _ = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("kkkkkkkkkkk"): DOWNLOAD_ERROR(
                "The uploader has not made this video available in your country"
            ),
        })
        self.assertEqual(len(result["entries"]), 1)

    def test_cancellation_during_full_check_propagates(self):
        """Cancellation must not be swallowed by the conservative keep."""
        cancel_event = mock.Mock()
        cancel_event.is_set.side_effect = [False, True]
        entries = [thin_entry("lllllllllll")]

        def serve_playlist():
            return playlist_info(entries)

        with self.assertRaises(DownloadCancelled):
            expand(
                {PLAYLIST_URL: serve_playlist, video_url("lllllllllll"): {}},
                cancel_event=cancel_event,
            )

    def test_all_entries_unavailable_returns_empty_list(self):
        entries = [
            flat_entry("p1", availability="private"),
            flat_entry("p2", title="[Deleted video]"),
        ]
        result, _ = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertTrue(result["is_playlist"])
        self.assertEqual(result["entries"], [])

    def test_playlist_with_no_entries_raises(self):
        with self.assertRaises(ValueError):
            expand({PLAYLIST_URL: playlist_info([])})

    def test_bare_id_entries_are_expanded_to_watch_urls(self):
        entries = [{"id": "mmmmmmmmmmm", "url": "mmmmmmmmmmm", "title": "Video m"}]
        result, _ = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertEqual(result["entries"][0]["url"], video_url("mmmmmmmmmmm"))

    def test_entries_without_url_or_id_are_skipped(self):
        entries = [{"title": "no id at all"}, None, flat_entry("aaaaaaaaaaa")]
        result, _ = expand({PLAYLIST_URL: playlist_info(entries)})
        self.assertEqual([e["url"] for e in result["entries"]], [video_url("aaaaaaaaaaa")])

    def test_single_video_url_path_is_unchanged(self):
        url = video_url("nnnnnnnnnnn")
        info = {
            "id": "nnnnnnnnnnn",
            "title": "Single video",
            "duration": 200,
            "thumbnail": "https://i.ytimg.com/vi/nnnnnnnnnnn/hqdefault.jpg",
            "upload_date": "20240201",
        }
        result, calls = expand({url: info}, url=url)
        self.assertFalse(result["is_playlist"])
        self.assertIsNone(result["playlist_title"])
        self.assertEqual(calls, [url])
        entry = result["entries"][0]
        self.assertEqual(entry["title"], "Single video")
        self.assertEqual(entry["duration"], 200)
        self.assertEqual(entry["url"], url)

    def test_mixed_playlist_only_ambiguous_entries_trigger_full_checks(self):
        """Guards the perf intent: full extractions happen for ambiguous items only."""
        entries = [
            flat_entry("aaaaaaaaaaa"),
            thin_entry("ffff0000001"),
            flat_entry("ccccccccccc", title=None),
            thin_entry("ffff0000002"),
        ]
        result, calls = expand({
            PLAYLIST_URL: playlist_info(entries),
            video_url("ffff0000001"): DOWNLOAD_ERROR("Private video."),
            video_url("ffff0000002"): {
                "id": "ffff0000002",
                "title": "Recovered",
                "availability": "public",
            },
        })
        self.assertEqual(calls, [
            PLAYLIST_URL,
            video_url("ffff0000001"),
            video_url("ffff0000002"),
        ])
        self.assertEqual(
            [e["url"] for e in result["entries"]],
            [video_url("aaaaaaaaaaa"), video_url("ccccccccccc"), video_url("ffff0000002")],
        )
        self.assertEqual(result["entries"][2]["title"], "Recovered")

    def test_max_videos_is_passed_through_to_yt_dlp(self):
        """Unrelated option handling must survive the change."""
        seen = {}

        class RecordingYoutubeDL:
            def __init__(self, options=None):
                seen.update(options or {})

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def extract_info(self, url, download=False):
                return playlist_info([flat_entry("aaaaaaaaaaa")])

        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", RecordingYoutubeDL):
            expand_playlist(PLAYLIST_URL, max_videos=7)
        self.assertEqual(seen.get("playlistend"), 7)
        self.assertEqual(seen.get("extract_flat"), "in_playlist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
