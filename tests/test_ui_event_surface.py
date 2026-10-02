"""Phase 4C — the UI event surface has no orphan handlers.

`ClipperApp._handle_ui_event` is the single dispatcher for every background ->
main-thread UI event. Each of its branches names one event; each of those names
must be emitted somewhere in the application, or the branch can never run.

These tests pin that invariant from the outside:

  * every dispatched event name has a producer in src/ (structural, AST-based);
  * the supported playlist load path posts `playlist_metadata` and never
    `playlist_thumbnail` (behavioural, through the real loader worker);
  * the supported playlist presentation shows no thumbnail note, because the
    playlist path clears the thumbnail instead of fetching one;
  * the *live* thumbnail machinery is untouched: the single-video path still
    posts `video_thumbnail`, `_apply_video_thumbnail` still refreshes the info
    label and the download button, and the playlist preview widget still owns
    its own per-row thumbnail fetch/cache;
  * an event the dispatcher does not know is a no-op, so removing a branch can
    never turn into a crash.

Scaffolding is reused from tests/helpers.py and from the frozen Phase 1/2/3
modules instead of being duplicated: `FakeLoaderApp` and `ContractTestCase` come
from test_failure_contracts, the scripted `expand_playlist` results from
helpers.

Nothing here touches the network or creates a Tk window.
"""

import ast
import types
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F402  (sets sys.path + a throwaway XDG_CONFIG_HOME)

from helpers import (  # noqa: E402
    flat_playlist,
    playlist_entry,
    single_video_result,
)

helpers.import_gui_modules()  # noqa: E402

from yt_clipper.core import downloader  # noqa: E402
from yt_clipper.gui import app as app_module  # noqa: E402
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402
from yt_clipper.gui.widgets import playlist_preview as playlist_preview_module  # noqa: E402

from test_failure_contracts import ContractTestCase, FakeLoaderApp  # noqa: E402

SRC_ROOT = Path(helpers.__file__).resolve().parent.parent / "src" / "yt_clipper"

# How the application emits a UI event. `ClipperApp._post_ui_event` is the real
# entry point; the download queue reaches it through the `_emit` /
# `_event_callback` names it was constructed with (app.py:72).
EMITTER_CALLS = {"_post_ui_event", "_emit", "_event_callback"}

OBSOLETE_EVENT = "playlist_thumbnail"

VIDEO_URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def source_files():
    return sorted(SRC_ROOT.rglob("*.py"))


def dispatched_event_names():
    """Every event name `ClipperApp._handle_ui_event` compares against."""
    tree = ast.parse((SRC_ROOT / "gui" / "app.py").read_text(), "app.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_ui_event":
            names = []
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Compare)
                        and isinstance(sub.left, ast.Name)
                        and sub.left.id == "event_name"):
                    names.extend(c.value for c in sub.comparators
                                 if isinstance(c, ast.Constant) and isinstance(c.value, str))
            return names
    raise AssertionError("_handle_ui_event not found in gui/app.py")


def produced_event_names():
    """Every event name actually emitted anywhere in src/."""
    names = []
    for path in source_files():
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if callee in EMITTER_CALLS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.append(first.value)
    return names


class TestUiEventSurfaceIsComplete(unittest.TestCase):
    def test_1_every_dispatched_ui_event_has_a_producer(self):
        """No dispatch branch can be orphaned: one emitter per dispatched name.

        This is the general invariant behind Phase 4C. It failed before the
        removal with exactly one orphan - `playlist_thumbnail` - and it keeps
        failing if a future change adds a handler without a producer (or a
        producer without a handler).
        """
        dispatched = dispatched_event_names()
        produced = set(produced_event_names())

        self.assertEqual(len(dispatched), len(set(dispatched)),
                         "the dispatcher compares the same event name twice")
        orphans = sorted(set(dispatched) - produced)
        undeclared = sorted(produced - set(dispatched))

        self.assertEqual(orphans, [],
                         "a UI event is dispatched but nothing in src/ emits it")
        self.assertEqual(undeclared, [],
                         "a UI event is emitted but the dispatcher ignores it")

    def test_2_the_obsolete_event_name_is_gone_from_the_application(self):
        """No source file may still mention the removed event, in any role."""
        mentions = []
        for path in source_files():
            text = path.read_text()
            for lineno, line in enumerate(text.splitlines(), 1):
                if OBSOLETE_EVENT in line:
                    mentions.append(f"{path.relative_to(SRC_ROOT)}:{lineno}")
        self.assertEqual(mentions, [],
                         "the obsolete event path still exists somewhere")

    def test_3_the_obsolete_handler_method_is_gone_from_the_loader(self):
        self.assertFalse(
            hasattr(video_loader_module.VideoLoaderController, "_apply_playlist_thumbnail"),
            "the handler for an event nothing emits is still defined")

    def test_4_an_event_the_dispatcher_does_not_know_is_a_no_op(self):
        """The dispatcher has no `else` clause: an event it does not recognise
        falls through without raising and without touching application state.
        That is what makes deleting a branch safe - it degrades to this case."""
        stub = types.SimpleNamespace()  # no attributes at all
        app_module.ClipperApp._handle_ui_event(stub, "event_that_never_existed", ())
        self.assertEqual(vars(stub), {}, "an unknown event mutated application state")


class TestSupportedPlaylistPathNeedsNoThumbnailEvent(ContractTestCase):
    """Case A of the Phase 4C proof, from the runtime side."""

    def setUp(self):
        super().setUp()
        self.app = FakeLoaderApp()
        self.loader = video_loader_module.VideoLoaderController(self.app)
        self.entries = [playlist_entry("aaaaaaaaaaa", "alpha"),
                        playlist_entry("bbbbbbbbbbb", "beta")]

    def load_playlist(self):
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=flat_playlist(self.entries)):
            self.loader._load_video_worker(self.app._load_request_id, VIDEO_URL)
        return self.app.event_names()

    def test_5_the_playlist_load_posts_metadata_only(self):
        names = self.load_playlist()

        self.assertIn("playlist_metadata", names,
                      "the supported playlist path stopped reporting its result")
        self.assertNotIn(OBSOLETE_EVENT, names,
                         "something emitted the event this phase removes")
        self.assertNotIn("video_thumbnail", names,
                         "a playlist load must not report a single-video thumbnail")

    def test_6_the_playlist_presentation_shows_no_thumbnail_note(self):
        """What the user actually sees for a playlist is unchanged: the summary
        says every video downloads in full, and the thumbnail stays cleared
        because the playlist path never fetches one."""
        self.load_playlist()
        self.loader._apply_playlist_metadata(*self.app.payload("playlist_metadata"))

        text = self.app.video_info_label.text
        self.assertIn("2 available videos", text)
        self.assertIn("Each video will be downloaded in full", text)
        self.assertNotIn("thumbnail", text.lower(),
                         "a thumbnail note appeared in the playlist summary")
        self.assertIsNone(self.app._thumbnail_image)

    def test_7_a_stale_playlist_thumbnail_payload_cannot_reach_the_ui(self):
        """Even if an event were somehow queued with the removed name, the real
        dispatcher drops it without touching the widgets or shared state."""
        self.load_playlist()
        self.loader._apply_playlist_metadata(*self.app.payload("playlist_metadata"))
        before = self.app.video_info_label.text

        self.app._post_ui_event(OBSOLETE_EVENT, self.app._load_request_id, None, True)
        for name, payload in list(self.app.events):
            if name == OBSOLETE_EVENT:
                app_module.ClipperApp._handle_ui_event(
                    types.SimpleNamespace(video_loader=self.loader), name, payload)

        self.assertEqual(self.app.video_info_label.text, before,
                         "the removed path still rewrote the playlist summary")
        self.assertIsNone(self.app._thumbnail_image)


class TestLiveThumbnailBehaviourIsUntouched(ContractTestCase):
    """The other thumbnail machinery is a real feature and must survive."""

    def setUp(self):
        super().setUp()
        self.app = FakeLoaderApp()
        self.loader = video_loader_module.VideoLoaderController(self.app)

    def test_8_the_single_video_path_still_posts_its_thumbnail_event(self):
        """The live single-video thumbnail event is untouched by this phase.

        `single_video_result` carries no thumbnail URL, so there is nothing to
        fetch and the UI is told "not failed" rather than "missing"; the
        failed=True half of that contract is already pinned by the frozen
        Phase 3 test `test_2_a_thumbnail_failure_never_fails_the_load`.
        """
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)):
            self.loader._load_video_worker(self.app._load_request_id, VIDEO_URL)

        self.assertEqual(self.app.event_names(), ["video_metadata", "video_thumbnail"],
                         "the supported single-video thumbnail event changed")
        self.assertEqual(self.app.payload("video_thumbnail")[2], False)

    def test_9_the_single_video_thumbnail_handler_still_refreshes_the_ui(self):
        self.app.loaded_url = VIDEO_URL
        self.app.loaded_title = "Single video"
        self.app.video_duration = 200.0
        self.loader._apply_video_thumbnail(self.app._load_request_id, None, False)

        self.assertIn("Ready to clip", self.app.video_info_label.text)
        self.assertNotIn("Thumbnail unavailable", self.app.video_info_label.text)

    def test_10_the_preview_widget_keeps_its_own_row_thumbnails(self):
        """Playlist rows fetch their own thumbnails; that is not the removed
        event path and must not be deleted with it."""
        preview = playlist_preview_module.PlaylistPreviewWidget
        for name in ("set_videos", "clear", "_show_empty_state",
                     "_fetch_thumbnail_worker"):
            self.assertTrue(hasattr(preview, name),
                            f"playlist preview lost {name}")
        self.assertEqual(playlist_preview_module.THUMBNAIL_SIZE, (120, 68))

    def test_11_the_shared_thumbnail_helpers_are_still_wired_up(self):
        loader = video_loader_module.VideoLoaderController
        for name in ("_fetch_thumbnail", "_set_thumbnail", "_apply_video_thumbnail",
                     "_apply_playlist_metadata"):
            self.assertTrue(hasattr(loader, name), f"video loader lost {name}")


if __name__ == "__main__":
    unittest.main()
