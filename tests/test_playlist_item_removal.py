"""Phase 5A — removing individual videos from a loaded playlist preview.

The playlist selection is one piece of shared application state
(`ClipperApp.loaded_playlist_entries`), written by the loader at load time and
read by the queue when it creates download jobs. This module pins the contract
that lets the user exclude videos from that selection *before* the jobs are
created:

  * removing a video drops it from the shared selection, not just from the
    preview, so it can never become a download job;
  * removing several videos in a row keeps the remaining order;
  * removing the last video leaves an empty selection that queues nothing;
  * loading another playlist starts from a fresh selection, so a removal can
    never leak into a playlist the user has not seen yet.

The playlist availability filter (Phase 1/4A) is applied at load time and again
at the job-creation gate; this phase never re-implements or bypasses it. What
reaches the preview is what this phase operates on.

CustomTkinter needs a display, so the toolkit is stubbed (tests/helpers.py).
The widget class itself is real and is driven directly under that stub; the
wiring of the removal control into the card is verified statically, and what
could not be executed is reported rather than faked.
"""

import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: E402  (sets sys.path + a throwaway XDG_CONFIG_HOME)
from helpers import DownloadCallRecorder, playlist_entry  # noqa: E402

gui = helpers.import_gui_modules()  # noqa: E402

from yt_clipper.gui.app import ClipperApp  # noqa: E402
from yt_clipper.gui.controllers import queue_controller as queue_controller_module  # noqa: E402
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402

from yt_clipper.gui.widgets import playlist_preview as playlist_preview_module  # noqa: E402

from test_failure_contracts import (  # noqa: E402
    ContractTestCase,
    FakeLoaderApp,
    RecordingWidget,
)
from test_failure_contracts import PLAYLIST_URL as LOADER_PLAYLIST_URL  # noqa: E402
from test_playlist_clipping import GuiTestCase  # noqa: E402
from test_playlist_clipping import PLAYLIST_URL  # noqa: E402

WIDGET_SOURCE = Path(playlist_preview_module.__file__)


# ---------------------------------------------------------------------------
# Local doubles (only what the frozen scaffolding does not already give)
# ---------------------------------------------------------------------------

class RemovalRecordingPreview:
    """Records what the loader hands the playlist preview widget.

    Same role as test_playlist_centralization.RecordingPreview, plus a count of
    how often the preview was re-rendered: a removal that changes nothing must
    not re-render, and one that does must.
    """

    def __init__(self):
        self.videos = None
        self.renders = 0
        self.cleared = 0
        self.empty_state = None

    def set_videos(self, videos):
        self.videos = list(videos)
        self.renders += 1

    def clear(self):
        self.cleared += 1

    def _show_empty_state(self, text):
        self.empty_state = text


class RemovalLoaderApp(FakeLoaderApp):
    """FakeLoaderApp plus the removal affordance and a button-state recorder.

    The download-button decision is the REAL `ClipperApp` helper bound to this
    double, so the tests observe the same logic the GUI uses rather than a
    re-implementation of it.
    """

    def __init__(self, request_id=1, output_path=None):
        super().__init__(request_id, output_path)
        self.playlist_preview = RemovalRecordingPreview()
        self.button_states = []

    def update_download_button_state(self):
        self.button_states.append(ClipperApp._has_valid_download_data(self))


def entry(video_id, title):
    return playlist_entry(video_id, title)


def urls_of(entries):
    return [item["url"] for item in entries]


def defines(name):
    """True when the widget really defines `name`.

    The stubbed toolkit answers *any* attribute on a widget instance, so a
    missing method would otherwise look like a silent no-op and a test could
    pass for the wrong reason. Reading the class __dict__ bypasses that.
    """
    return name in vars(playlist_preview_module.PlaylistPreviewWidget)


# ---------------------------------------------------------------------------
# Test 1 / 2 — the shared selection loses exactly the removed videos
# ---------------------------------------------------------------------------

class TestSelectionAfterRemoval(ContractTestCase):

    def make_loader(self, entries, title="P", url=LOADER_PLAYLIST_URL):
        app = RemovalLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_playlist_metadata(app._load_request_id, url, title, entries)
        return app, loader

    def test_1_removing_one_video_leaves_the_other_two(self):
        entries = [entry("aaaaaaaaaaa", "alpha"),
                   entry("bbbbbbbbbbb", "beta"),
                   entry("ccccccccccc", "gamma")]
        app, loader = self.make_loader(entries)
        self.assertEqual(len(app.loaded_playlist_entries), 3)

        loader.remove_playlist_video(app.loaded_playlist_entries[1])

        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [entries[0]["url"], entries[2]["url"]],
                         "removing one video changed more than that video")
        self.assertEqual(urls_of(app.playlist_preview.videos),
                         [entries[0]["url"], entries[2]["url"]],
                         "the preview no longer shows the shared selection")

    def test_2_removing_several_videos_sequentially_keeps_the_rest(self):
        entries = [entry("aaaaaaaaaaa", "alpha"),
                   entry("bbbbbbbbbbb", "beta"),
                   entry("ccccccccccc", "gamma"),
                   entry("ddddddddddd", "delta")]
        app, loader = self.make_loader(entries)

        loader.remove_playlist_video(app.loaded_playlist_entries[1])   # beta
        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [entries[0]["url"], entries[2]["url"], entries[3]["url"]])

        loader.remove_playlist_video(app.loaded_playlist_entries[2])   # delta
        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [entries[0]["url"], entries[2]["url"]])

        loader.remove_playlist_video(app.loaded_playlist_entries[0])   # alpha
        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [entries[2]["url"]],
                         "the remaining order was corrupted by a removal")

    def test_removing_an_unknown_video_changes_nothing(self):
        entries = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        app, loader = self.make_loader(entries)
        renders_before = app.playlist_preview.renders

        loader.remove_playlist_video(entry("zzzzzzzzzzz", "not loaded"))

        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [entries[0]["url"], entries[1]["url"]],
                         "a stale removal request dropped a real video")
        self.assertEqual(app.playlist_preview.renders, renders_before,
                         "a removal that changed nothing still re-rendered")

    def test_the_selection_is_the_one_object_the_preview_reads(self):
        """One source of truth: the preview is handed the shared entries."""
        entries = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        app, loader = self.make_loader(entries)

        shown = app.playlist_preview.videos
        self.assertEqual(len(shown), len(app.loaded_playlist_entries))
        for preview_entry, shared_entry in zip(shown, app.loaded_playlist_entries):
            self.assertIs(preview_entry, shared_entry,
                          "the preview holds entries of its own")


# ---------------------------------------------------------------------------
# The status bar follows the selection
# ---------------------------------------------------------------------------

class TestRemovalUpdatesTheStatusBar(ContractTestCase):
    """The status bar must not keep advertising a playlist the user just shrank.

    While a playlist is loaded the single-video preview is hidden, and with it
    `video_info_label` - so the status bar is the only status text that is on
    screen. `remove_playlist_video` re-renders the preview, restates the hidden
    label and recomputes the Download button, but it never restates the status
    bar, which keeps the line it was given when the playlist loaded:
    "Playlist loaded (2 available...). Click Download to queue them all."
    That line then sits next to a shorter list and a disabled Download button,
    and once the last video is removed it invites a download there is nothing
    left to queue.
    """

    def make_loader(self, entries, title="P", url=LOADER_PLAYLIST_URL):
        app = RemovalLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        loader._apply_playlist_metadata(app._load_request_id, url, title, entries)
        return app, loader

    def test_removing_a_video_restates_the_status_bar(self):
        entries = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        app, loader = self.make_loader(entries)
        loaded_status = app.statuses[-1]

        loader.remove_playlist_video(app.loaded_playlist_entries[0])

        self.assertNotEqual(
            app.statuses[-1], loaded_status,
            "the status bar still repeats the message from when the playlist "
            "was loaded, after the selection changed")
        self.assertIn("1", app.statuses[-1][0],
                      "the status bar does not name the new selection size")
        self.assertNotIn("Click Download to queue them all", app.statuses[-1][0],
                         "the status bar still invites a download of the whole "
                         "playlist after a video was removed")

    def test_removing_the_last_video_says_nothing_is_selected(self):
        app, loader = self.make_loader([entry("aaaaaaaaaaa", "alpha")])

        loader.remove_playlist_video(app.loaded_playlist_entries[0])

        self.assertIn("No videos selected", app.statuses[-1][0],
                      "an emptied playlist still reports videos to download")
        self.assertNotIn("Click Download to queue them all", app.statuses[-1][0],
                         "the status bar invites a download nothing can satisfy")
        # The empty selection is a warning, not the success the load reported.
        self.assertEqual(app.statuses[-1][1], "#e6a817",
                         "an empty selection was reported in the success colour")

    def test_removing_one_of_many_keeps_the_selection_visible_in_the_status(self):
        entries = [entry("aaaaaaaaaaa", "alpha"),
                   entry("bbbbbbbbbbb", "beta"),
                   entry("ccccccccccc", "gamma")]
        app, loader = self.make_loader(entries)

        loader.remove_playlist_video(app.loaded_playlist_entries[1])

        self.assertIn("2", app.statuses[-1][0],
                      "the status bar does not name the two videos still selected")
        self.assertNotIn("3 available", app.statuses[-1][0],
                         "the status bar still counts the removed video")

    def test_a_removal_that_changes_nothing_leaves_the_status_bar_alone(self):
        """A no-op removal must not restate a status the user already has."""
        entries = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        app, loader = self.make_loader(entries)
        loaded_status = app.statuses[-1]

        loader.remove_playlist_video(entry("zzzzzzzzzzz", "not loaded"))

        self.assertEqual(app.statuses[-1], loaded_status,
                         "a removal that changed nothing rewrote the status bar")


# ---------------------------------------------------------------------------
# Test 3 / 4 — the excluded videos never become download jobs
# ---------------------------------------------------------------------------

class TestRemovedVideosNeverBecomeJobs(GuiTestCase):

    def load(self, entries, url=PLAYLIST_URL):
        app = self.make_app(url=url, entries=entries)
        app.playlist_preview = RemovalRecordingPreview()
        loader = video_loader_module.VideoLoaderController(app)
        # The loader's own load path already ran for this app double; only the
        # removal affordance is exercised here.
        return app, loader

    def test_3_removed_videos_never_become_download_jobs(self):
        app, loader = self.load(self.entries)
        self.assertEqual(len(app.loaded_playlist_entries), 3)
        # The shared selection is mutated in place, so take the expectation
        # from a snapshot taken before the removal.
        alpha, beta, gamma = urls_of(self.entries)

        loader.remove_playlist_video(app.loaded_playlist_entries[1])   # beta

        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, 2)

        self.assertEqual(recorder.urls, [alpha, gamma],
                         "a removed video still reached the downloader")
        self.assertEqual(recorder.filenames,
                         ["01 - alpha.mp4", "02 - gamma.mp4"],
                         "removing a video changed the numbering of the rest")
        self.assertEqual([job.url for job in app.queue_jobs], [alpha, gamma])
        self.assertEqual(urls_of(app.playlist_preview.videos), [alpha, gamma],
                         "the preview and the queue disagreed")
        self.assertIn("Queued 2 full videos from playlist", app.statuses[-1])

    def test_4_removing_the_final_video_selects_nothing_and_queues_nothing(self):
        app, loader = self.load([self.entries[0]])
        # The REAL button decision bound to this double, so the test observes
        # the logic the GUI uses rather than a copy of it.
        button_states = []
        app.update_download_button_state = lambda: button_states.append(
            ClipperApp._has_valid_download_data(app))

        loader.remove_playlist_video(app.loaded_playlist_entries[0])

        self.assertEqual(app.loaded_playlist_entries, [],
                         "the selection was not emptied")
        self.assertEqual(app.playlist_preview.videos, [],
                         "the preview was not told the last video went away")
        self.assertIn("No videos selected", app.playlist_preview.empty_state,
                      "an emptied playlist showed no empty state")
        self.assertFalse(ClipperApp._has_valid_download_data(app),
                         "an empty selection still looks downloadable")
        self.assertEqual(button_states[-1], False,
                         "the download button was not disabled at zero videos")

        controller = self.make_controller(app)
        with mock.patch("yt_clipper.core.config.save_config",
                        lambda *a, **k: None):
            controller.download_clip()

        self.assertEqual(app.queue_jobs, [],
                         "an empty selection produced download jobs")
        self.assertEqual(app.harness.event_names(), [],
                         "an empty selection reached the download service")

    def test_download_refuses_an_empty_playlist_selection(self):
        """The button is disabled, but the action must also refuse on its own."""
        app, loader = self.load([self.entries[0]])
        loader.remove_playlist_video(app.loaded_playlist_entries[0])

        controller = self.make_controller(app)
        with mock.patch.object(queue_controller_module.messagebox,
                               "showerror") as showerror, \
                mock.patch("yt_clipper.core.config.save_config",
                           lambda *a, **k: None):
            controller.download_clip()

        self.assertEqual(app.queue_jobs, [])
        self.assertTrue(showerror.called,
                        "an empty playlist download was not refused")
        self.assertIn("No videos selected", showerror.call_args[0][0])
        self.assertEqual(app.harness.event_names(), [],
                         "an empty playlist reached the download service")

    def test_6_an_untouched_playlist_still_downloads_every_video(self):
        """Removal is opt-in: the untouched path must be unchanged."""
        app, _loader = self.load(self.entries)
        recorder = DownloadCallRecorder()
        self.run_playlist_download(app, recorder, 3)

        self.assertEqual(recorder.urls, urls_of(self.entries))
        self.assertEqual(recorder.filenames,
                         ["01 - alpha.mp4", "02 - beta.mp4", "03 - gamma.mp4"])
        self.assertEqual(recorder.ranges, [(None, None)] * 3)
        self.assertIn("Queued 3 full videos from playlist", app.statuses[-1])


# ---------------------------------------------------------------------------
# Test 5 — a newly loaded playlist starts from a fresh selection
# ---------------------------------------------------------------------------

class TestReloadStartsFromAFreshSelection(ContractTestCase):

    def test_5_loading_a_new_playlist_starts_from_a_fresh_selection(self):
        app = RemovalLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        first = [entry("aaaaaaaaaaa", "alpha"),
                 entry("bbbbbbbbbbb", "beta"),
                 entry("ccccccccccc", "gamma")]
        second = [entry("xxxxxxxxxxx", "x-ray"), entry("yyyyyyyyyyy", "yankee")]

        loader._apply_playlist_metadata(app._load_request_id,
                                        LOADER_PLAYLIST_URL, "First", first)
        loader.remove_playlist_video(app.loaded_playlist_entries[1])
        self.assertEqual(len(app.loaded_playlist_entries), 2)

        loader._apply_playlist_metadata(app._load_request_id,
                                        "https://www.youtube.com/playlist?list=PLSECOND",
                                        "Second", second)

        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         [second[0]["url"], second[1]["url"]],
                         "a removal from the previous playlist leaked into the new one")
        self.assertEqual(urls_of(app.playlist_preview.videos),
                         [second[0]["url"], second[1]["url"]],
                         "the preview kept a video from the previous playlist")

    def test_reloading_the_same_playlist_also_starts_fresh(self):
        app = RemovalLoaderApp()
        loader = video_loader_module.VideoLoaderController(app)
        entries = [entry("aaaaaaaaaaa", "alpha"),
                   entry("bbbbbbbbbbb", "beta"),
                   entry("ccccccccccc", "gamma")]

        loader._apply_playlist_metadata(app._load_request_id,
                                        LOADER_PLAYLIST_URL, "P", entries)
        loader.remove_playlist_video(app.loaded_playlist_entries[0])
        self.assertEqual(len(app.loaded_playlist_entries), 2)

        # Same URL, same extractor answer: a reload is a new selection.
        loader._apply_playlist_metadata(app._load_request_id,
                                        LOADER_PLAYLIST_URL, "P", entries)

        self.assertEqual(urls_of(app.loaded_playlist_entries),
                         urls_of(entries),
                         "reloading the same playlist kept the earlier removal")


# ---------------------------------------------------------------------------
# The preview widget itself: count, empty state, removal routing
# ---------------------------------------------------------------------------

def make_widget():
    """A real PlaylistPreviewWidget under the stubbed toolkit."""
    preview = playlist_preview_module.PlaylistPreviewWidget(None)
    # The stub cannot enumerate Tk children; the widget only iterates them to
    # destroy them, so an empty list is an honest stand-in for "no rows yet".
    preview.scroll_frame.winfo_children = lambda: []
    preview.header_label = RecordingWidget()
    return preview


class TestPreviewWidgetRemoval(ContractTestCase):

    def test_the_count_and_the_empty_state_follow_the_selection(self):
        """What removal relies on: the preview renders exactly the list it is
        given, including a zero-length one."""
        preview = make_widget()
        entries = [entry("aaaaaaaaaaa", "alpha"),
                   entry("bbbbbbbbbbb", "beta"),
                   entry("ccccccccccc", "gamma")]

        preview.set_videos(entries)
        self.assertEqual(preview.header_label.text,
                         "Playlist Preview — 3 videos (oldest → newest)")

        # One video excluded: the preview shows the remaining two.
        preview.set_videos([entries[0], entries[2]])
        self.assertEqual(preview.header_label.text,
                         "Playlist Preview — 2 videos (oldest → newest)")
        self.assertEqual([v["url"] for v in preview._videos],
                         [entries[0]["url"], entries[2]["url"]])

        # Every video excluded: the count reaches zero and says so.
        preview.set_videos([])
        self.assertEqual(preview.header_label.text,
                         "Playlist Preview — 0 videos (oldest → newest)")
        self.assertEqual(preview._videos, [])
        self.assertIsNotNone(preview._empty_label,
                             "an emptied playlist showed no empty state")

    def test_the_removal_control_reports_that_video_to_the_handler(self):
        preview = make_widget()
        seen = []
        preview.set_remove_handler(seen.append)
        entries = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        preview.set_videos(entries)
        self.assertTrue(defines("_request_remove"),
                        "the widget no longer defines _request_remove")
        self.assertTrue(defines("set_remove_handler"),
                        "the widget no longer defines set_remove_handler")

        # Exactly what the per-card removal control's command invokes.
        preview._request_remove(entries[1])

        self.assertEqual(seen, [entries[1]],
                         "the removal control did not report the video it removed")

    def test_a_removal_reported_without_a_handler_is_inert(self):
        preview = make_widget()
        entries = [entry("aaaaaaaaaaa", "alpha")]
        preview.set_videos(entries)
        self.assertTrue(defines("_request_remove"),
                        "the widget no longer defines _request_remove")

        preview._request_remove(entries[0])   # no handler registered

        self.assertEqual([v["url"] for v in preview._videos],
                         [entries[0]["url"]],
                         "a removal with no handler changed the preview anyway")

    def test_a_new_playlist_replaces_the_previous_selection(self):
        preview = make_widget()
        first = [entry("aaaaaaaaaaa", "alpha"), entry("bbbbbbbbbbb", "beta")]
        second = [entry("xxxxxxxxxxx", "x-ray")]
        preview.set_videos(first)

        preview.set_videos(second)

        self.assertEqual([v["url"] for v in preview._videos],
                         [second[0]["url"]])
        self.assertEqual(preview.header_label.text,
                         "Playlist Preview — 1 videos (oldest → newest)")


# ---------------------------------------------------------------------------
# Title truncation + fixed remove-button sizing
# ---------------------------------------------------------------------------

LONG_TITLE = ("This Is An Extremely Long Video Title That Would Break The "
              "Playlist Preview Layout")
SHORT_TITLE = "My Cat Plays Piano"


class RecordingCtk:
    """Records the kwargs a stubbed toolkit widget was constructed with.

    The real toolkit needs a display, so the card is built under the stub and
    the *configuration* is read back - never a pixel measurement.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = dict(kwargs)
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None

        return _record


def render_card(title):
    """Build one preview card and return (widget, created labels, created buttons)."""
    preview = playlist_preview_module.PlaylistPreviewWidget(None)
    # The stub cannot enumerate Tk children; the widget only iterates them to
    # destroy them, so an empty list is an honest stand-in for "no rows yet".
    preview.scroll_frame.winfo_children = lambda: []

    labels, buttons = [], []

    def label(*args, **kwargs):
        widget = RecordingCtk(*args, **kwargs)
        labels.append(widget)
        return widget

    def button(*args, **kwargs):
        widget = RecordingCtk(*args, **kwargs)
        buttons.append(widget)
        return widget

    with mock.patch.object(playlist_preview_module.ctk, "CTkLabel", label), \
            mock.patch.object(playlist_preview_module.ctk, "CTkButton", button):
        preview.set_videos([playlist_entry("aaaaaaaaaaa", title)])

    return preview, labels, buttons


def card_of(title):
    """The widgets of one rendered card: (title label, remove button)."""
    _, labels, buttons = render_card(title)
    # Creation order in the card: thumbnail label, title label, date label.
    return labels[1], buttons[0]


class TestTitleDisplayTruncation(unittest.TestCase):
    """The preview shortens the title it shows; the data keeps the original."""

    def truncate(self, title):
        return playlist_preview_module.truncate_title_for_display(title)

    # ---- §5.4 truncation rules ----

    def test_an_empty_title_stays_empty(self):
        self.assertEqual(self.truncate(""), "")

    def test_a_missing_title_stays_empty(self):
        self.assertEqual(self.truncate(None), "")

    def test_a_one_word_title_is_unchanged(self):
        self.assertEqual(self.truncate("Piano"), "Piano")

    def test_a_short_title_is_unchanged(self):
        self.assertEqual(self.truncate(SHORT_TITLE), SHORT_TITLE)

    def test_a_title_exactly_at_the_word_limit_is_unchanged(self):
        limit = playlist_preview_module.TITLE_MAX_WORDS
        title = " ".join(f"word{i}" for i in range(limit))
        self.assertEqual(self.truncate(title), title,
                         "a title exactly at the word limit was still truncated")

    def test_a_title_over_the_word_limit_is_truncated_with_a_marker(self):
        shown = self.truncate(LONG_TITLE)
        self.assertTrue(shown.endswith("..."),
                        "the truncation marker is missing")
        self.assertNotIn("Layout", shown,
                         "words past the limit were still shown")
        self.assertEqual(
            shown[:-len("...")].split(" "),
            LONG_TITLE.split(" ")[:playlist_preview_module.TITLE_MAX_WORDS],
            "the cut did not land on the word limit")

    def test_a_very_long_title_is_bounded(self):
        shown = self.truncate("supercalifragilistic " * 40)
        self.assertLessEqual(len(shown), playlist_preview_module.TITLE_MAX_CHARS,
                             "the displayed title is not length-bounded")

    def test_repeated_whitespace_is_collapsed_and_deterministic(self):
        messy = "  My   Cat \t Plays \n Piano  "
        self.assertEqual(self.truncate(messy), "My Cat Plays Piano")
        self.assertEqual(self.truncate(messy), self.truncate(messy))

    def test_punctuation_only_titles_stay_visible(self):
        self.assertEqual(self.truncate("..."), "...")
        self.assertEqual(self.truncate("!?"), "!?")

    def test_a_cut_never_leaves_a_dangling_separator(self):
        shown = self.truncate("one two three, four five, six seven")
        self.assertTrue(shown.endswith("..."))
        self.assertFalse(shown[:-3].rstrip().endswith((",", ";", ":")),
                         "the cut left a dangling separator before the marker")

    def test_a_non_latin_title_is_bounded_whatever_the_glyphs(self):
        # Words and characters behave very differently in Arabic, so both
        # limits have to hold rather than only the word one.
        multi_word = "هذا عنوان فيديو طويل جدا لا يناسب بطاقة المعاينة"
        self.assertLessEqual(len(self.truncate(multi_word)),
                             playlist_preview_module.TITLE_MAX_CHARS)
        unbroken = "هذا" * 100
        self.assertLessEqual(len(self.truncate(unbroken)),
                             playlist_preview_module.TITLE_MAX_CHARS,
                             "a long unbroken non-Latin title was not "
                             "character-bounded")

    def test_truncation_is_pure(self):
        """The helper is a presentation transform: same input, same output."""
        self.assertEqual(self.truncate(LONG_TITLE), self.truncate(LONG_TITLE))

    # ---- §7 data vs presentation ----

    def test_the_original_title_is_never_mutated(self):
        entry = playlist_entry("aaaaaaaaaaa", LONG_TITLE)
        render_card(entry["title"])
        self.assertEqual(entry["title"], LONG_TITLE,
                         "rendering the card changed the playlist data")

    def test_the_card_shows_the_truncated_title_but_keeps_the_data(self):
        entry = playlist_entry("aaaaaaaaaaa", LONG_TITLE)
        title_label, _ = card_of(entry["title"])
        self.assertEqual(title_label.kwargs["text"],
                         playlist_preview_module.truncate_title_for_display(LONG_TITLE))
        self.assertNotEqual(title_label.kwargs["text"], LONG_TITLE)

    def test_a_short_title_is_shown_in_full(self):
        title_label, _ = card_of(SHORT_TITLE)
        self.assertEqual(title_label.kwargs["text"], SHORT_TITLE)


class TestRemoveButtonSizingIsTitleIndependent(unittest.TestCase):
    """§3.4: the X button's dimensions do not depend on the title length."""

    def test_the_button_is_configured_with_an_explicit_size(self):
        _, button = card_of(SHORT_TITLE)
        self.assertIn("width", button.kwargs,
                      "the remove button has no explicit width")
        self.assertIn("height", button.kwargs,
                      "the remove button has no explicit height")
        self.assertGreater(button.kwargs["width"], 0)
        self.assertGreater(button.kwargs["height"], 0)

    def test_a_long_title_does_not_change_the_configured_button_size(self):
        short_label, short_button = card_of(SHORT_TITLE)
        long_label, long_button = card_of(LONG_TITLE)

        # The two cards really do carry different titles...
        self.assertNotEqual(short_label.kwargs["text"], long_label.kwargs["text"])
        # ...yet the remove control is configured identically.
        self.assertEqual(short_button.kwargs["width"], long_button.kwargs["width"])
        self.assertEqual(short_button.kwargs["height"], long_button.kwargs["height"])

    def test_the_title_never_asks_for_more_width_than_the_row_leaves_it(self):
        """The row's request must stay inside the narrowest row the app supports.

        `ClipperApp.minsize(480, 500)` and `resizable(True, True)` mean the
        playlist row can be as narrow as ~348px. Tk's packer shrinks slaves
        when the row's requested width exceeds what it has, and the remove
        button - packed `side="right"` with no `expand` and no `fill` - is one
        of those slaves. So the title's wrap width has to leave room for the
        thumbnail and the button at that narrowest width.
        """
        _, labels, _ = render_card(LONG_TITLE)
        wrap = labels[1].kwargs["wraplength"]

        thumbnail = playlist_preview_module.THUMBNAIL_SIZE[0]
        button = card_of(SHORT_TITLE)[1].kwargs["width"]
        padding = 16 + 8          # thumbnail padx(8,8) + button padx(0,8)
        narrowest_row = 348       # derived from the app's own chrome at 480px

        self.assertLessEqual(
            wrap + thumbnail + button + padding, narrowest_row,
            "the title can request more width than the narrowest row has, so "
            "the packer has to shrink the remove button to fit it")


# ---------------------------------------------------------------------------
# Static verification of the card's removal control
# ---------------------------------------------------------------------------

def _card_method(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_create_video_item":
            return node
    raise AssertionError("_create_video_item not found in the playlist preview")


BUTTON1 = "<Button-1>"


def _click_bound_widgets(card):
    """The widgets the card binds <Button-1> (open-in-browser) to.

    Both forms the card uses are understood: a direct `name.bind(...)` and the
    `for widget in (a, b, c): widget.bind(...)` loop.
    """
    bound = set()

    def is_button1(call):
        return (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "bind"
                and any(isinstance(a, ast.Constant) and a.value == BUTTON1
                        for a in call.args))

    for node in ast.walk(card):
        if is_button1(node) and isinstance(node.func.value, ast.Name):
            bound.add(node.func.value.id)
        if isinstance(node, ast.For) and any(is_button1(s) for s in ast.walk(node)):
            if isinstance(node.iter, ast.Tuple):
                bound.update(elt.id for elt in node.iter.elts
                             if isinstance(elt, ast.Name))
    return sorted(bound)


def _removal_button_variable(card):
    """The local name the card's removal CTkButton is assigned to."""
    for node in ast.walk(card):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "CTkButton"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    return target.id
    return None


class TestCardRemovalControlIsWiredSafely(ContractTestCase):
    """The GUI cannot run here, so the wiring is proven by reading the source."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(WIDGET_SOURCE.read_text(), str(WIDGET_SOURCE))
        cls.card = _card_method(cls.tree)

    def test_every_card_carries_a_removal_control(self):
        buttons = [
            node for node in ast.walk(self.card)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "CTkButton"
        ]
        self.assertEqual(len(buttons), 1,
                         "the playlist card must offer exactly one removal control")
        button = buttons[0]
        labels = [kw.value.value for kw in button.keywords
                  if kw.arg == "text" and isinstance(kw.value, ast.Constant)]
        self.assertIn("✕", labels,
                      "the removal control is not labelled with a remove glyph")

    def test_the_removal_control_reports_the_video_it_removes(self):
        commands = [
            kw for kw in ast.walk(self.card)
            if isinstance(kw, ast.keyword) and kw.arg == "command"
        ]
        self.assertEqual(len(commands), 1,
                         "the removal control must be wired to one command")
        source = ast.get_source_segment(WIDGET_SOURCE.read_text(), commands[0].value)
        self.assertIn("_request_remove", source or "",
                      "the removal control does not report the video to remove")

    def test_the_removal_control_cannot_open_the_video(self):
        """Clicking Remove must not fall through to the card's open-in-browser."""
        bound = _click_bound_widgets(self.card)
        button = _removal_button_variable(self.card)
        self.assertIsNotNone(button, "the card has no removal button to check")
        self.assertNotIn(button, bound,
                         f"{button} is both a removal control and an open link")

    def test_widget_still_opens_a_video_on_click(self):
        """The existing click-to-open behaviour is untouched by this phase."""
        bound = _click_bound_widgets(self.card)
        for expected in ("item_frame", "thumb_label", "text_frame",
                         "title_label", "date_label"):
            self.assertIn(expected, bound,
                          f"the card stopped opening its video from {expected}")


# ---------------------------------------------------------------------------
# The application wiring (GUI cannot run here, so it is read from the source)
# ---------------------------------------------------------------------------

class TestRemovalIsWiredIntoTheApplication(ContractTestCase):
    """The preview reports a removal; the loader must be who it reports to."""

    @classmethod
    def setUpClass(cls):
        cls.app_source = Path(sys.modules["yt_clipper.gui.app"].__file__)
        cls.tree = ast.parse(cls.app_source.read_text(), str(cls.app_source))

    def _handler_calls(self):
        return [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_remove_handler"
        ]

    def test_the_preview_is_wired_to_the_loaders_removal(self):
        calls = self._handler_calls()
        self.assertEqual(len(calls), 1,
                         "the playlist preview is not wired to exactly one handler")
        target = calls[0].args[0]
        rendered = ast.unparse(target)
        self.assertEqual(rendered, "self.video_loader.remove_playlist_video",
                         f"the preview reports removals to {rendered!r}, not the loader")

    def test_the_loader_exists_before_the_preview_is_wired(self):
        """Otherwise the wiring line would raise at construction time."""
        handler_line = self._handler_calls()[0].lineno
        loader_line = None
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute)
                            and t.attr == "video_loader" for t in node.targets)):
                loader_line = node.lineno
                break
        self.assertIsNotNone(loader_line, "the loader is never constructed")
        self.assertLess(loader_line, handler_line,
                        "the preview is wired before the loader exists")


if __name__ == "__main__":
    unittest.main()
