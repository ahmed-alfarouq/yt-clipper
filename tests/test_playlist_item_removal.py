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
