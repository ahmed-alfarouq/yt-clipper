"""Phase 4B — the canonical import surface after removing dead compat fallbacks.

Five `try: from …playlist_utils import X / except ImportError: from …utils
import X` branches were removed. They were provably unreachable: `core/utils.py`
imports `core/playlist_utils` unconditionally at module level, so whenever the
preferred import fails the fallback fails with it (verified by runtime probe),
and `playlist_utils` itself needs only the standard library plus `core.log`, so
no import cycle can make it fail.

These tests protect what the removal must not break:

  * the affected modules bind the canonical helpers directly;
  * the legacy `core.utils` re-export surface - a supported import path used by
    the CLI, the GUI and three test modules - stays intact and identical;
  * `playlist_utils` remains structurally mandatory, which is the invariant that
    made the fallbacks dead (if it ever stops being true, these tests fail and
    the removal has to be reconsidered);
  * the *retained* optional-dependency fallback for Pillow is still in place, so
    this suite pins the boundary between removed and kept, not just the removal.

Nothing here touches the network or creates a Tk window.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

import helpers  # noqa: F402  (sets sys.path + a throwaway XDG_CONFIG_HOME)

from yt_clipper.core import playlist_utils  # noqa: E402
from yt_clipper.core import utils as legacy_utils  # noqa: E402

gui = helpers.import_gui_modules()  # noqa: E402

from yt_clipper.gui.controllers import queue_controller as queue_controller_module  # noqa: E402
from yt_clipper.gui.controllers import video_loader as video_loader_module  # noqa: E402
from yt_clipper.gui.widgets import playlist_preview as playlist_preview_module  # noqa: E402

SRC = helpers.SRC_DIR
REPO_ROOT = helpers.REPO_ROOT

#: Modules whose legacy `core.utils` import fallback was removed in Phase 4B.
CLEANED_MODULES = {
    "yt_clipper.gui.controllers.video_loader": video_loader_module,
    "yt_clipper.gui.controllers.queue_controller": queue_controller_module,
    "yt_clipper.gui.widgets.playlist_preview": playlist_preview_module,
}

#: Names `core/utils.py` re-exports from `core/playlist_utils`.
REEXPORTED = [
    "parse_publish_date", "extract_publish_date", "sort_videos_by_publish_date",
    "format_publish_date", "AVAILABILITY_AVAILABLE", "AVAILABILITY_UNAVAILABLE",
    "AVAILABILITY_UNKNOWN", "classify_video_entry", "is_video_entry_available",
    "is_video_available", "filter_available_videos", "detect_youtube_url_type",
    "is_youtube_playlist_url", "is_youtube_video_url",
]


def _import_error_handlers(source_path):
    """AST: every `except ImportError/ModuleNotFoundError` handler in a file.

    Returns [(lineno, imports_from_core_utils, assigns_image_none), …] so a test
    can tell a removed legacy-layout fallback from the retained optional-Pillow
    one without matching on comment wording.
    """
    tree = ast.parse(Path(source_path).read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        kind = node.type
        name = kind.id if isinstance(kind, ast.Name) else getattr(kind, "attr", None)
        if name not in ("ImportError", "ModuleNotFoundError"):
            continue
        from_utils = any(
            isinstance(sub, ast.ImportFrom) and sub.module == "yt_clipper.core.utils"
            for sub in ast.walk(node)
        )
        image_none = any(
            isinstance(sub, ast.Assign) and sub.value is not None
            and isinstance(sub.value, ast.Constant) and sub.value.value is None
            for sub in ast.walk(node)
        )
        found.append((node.lineno, from_utils, image_none))
    return found


def _module_file(module):
    return Path(module.__file__)


class TestCanonicalImportsAfterFallbackRemoval(unittest.TestCase):
    def test_1_the_cleaned_modules_bind_the_canonical_helpers_directly(self):
        """The public names each module exposes are the canonical objects."""
        self.assertIs(video_loader_module.filter_available_videos,
                      playlist_utils.filter_available_videos)
        self.assertIs(video_loader_module.sort_videos_by_publish_date,
                      playlist_utils.sort_videos_by_publish_date)
        self.assertIs(video_loader_module.detect_youtube_url_type,
                      playlist_utils.detect_youtube_url_type)
        self.assertIs(queue_controller_module.filter_available_videos,
                      playlist_utils.filter_available_videos)
        self.assertIs(playlist_preview_module.format_publish_date,
                      playlist_utils.format_publish_date)
        self.assertIs(playlist_preview_module.sort_videos_by_publish_date,
                      playlist_utils.sort_videos_by_publish_date)

    def test_2_no_legacy_utils_import_fallback_remains_in_the_cleaned_modules(self):
        """Structural pin: the dead branch must not creep back."""
        for name, module in CLEANED_MODULES.items():
            handlers = _import_error_handlers(_module_file(module))
            rescued = [line for line, from_utils, _ in handlers if from_utils]
            self.assertEqual(rescued, [], f"{name} still falls back to core.utils")

        # downloader's two fallbacks were function-level; check the source too.
        downloader_handlers = _import_error_handlers(
            SRC / "yt_clipper" / "core" / "downloader.py")
        self.assertEqual([line for line, from_utils, _ in downloader_handlers
                          if from_utils], [],
                         "downloader still falls back to core.utils")

    def test_3_the_retained_optional_pillow_fallback_is_still_in_place(self):
        """Pillow is genuinely optional at runtime (this suite runs without it),
        so its ImportError fallback is kept - and this test pins that keeping."""
        for module in (video_loader_module, playlist_preview_module):
            handlers = _import_error_handlers(_module_file(module))
            self.assertEqual(len(handlers), 1,
                             f"{module.__name__}: expected exactly the Pillow fallback")
            _line, from_utils, image_none = handlers[0]
            self.assertFalse(from_utils)
            self.assertTrue(image_none, "the Pillow fallback no longer degrades to None")

    def test_4_the_legacy_core_utils_reexport_surface_is_intact(self):
        """`from yt_clipper.core.utils import X` is a supported import path (the
        CLI and GUI use it for sanitize_filename/format_seconds/time_to_seconds,
        three test modules use it for the playlist helpers)."""
        for name in REEXPORTED:
            self.assertIn(name, legacy_utils.__all__, f"{name} left utils.__all__")
            self.assertIs(getattr(legacy_utils, name), getattr(playlist_utils, name),
                          f"utils.{name} is no longer the canonical object")
            self.assertIn(name, dir(legacy_utils))

        # The non-playlist half of the surface is untouched by Phase 4B.
        for name in ("sanitize_filename", "open_containing_folder",
                     "format_seconds", "time_to_seconds"):
            self.assertTrue(callable(getattr(legacy_utils, name)), name)

    def test_5_the_cli_still_imports_its_helpers_from_core_utils(self):
        """The CLI's own import line is the live consumer of that surface."""
        from yt_clipper import cli as cli_module
        self.assertIs(cli_module.sanitize_filename, legacy_utils.sanitize_filename)
        self.assertIs(cli_module.detect_youtube_url_type,
                      playlist_utils.detect_youtube_url_type)
        self.assertIs(cli_module.time_to_seconds, legacy_utils.time_to_seconds)


class TestPlaylistUtilsIsStructurallyMandatory(unittest.TestCase):
    """The invariant that made the five fallbacks dead, checked in a fresh
    interpreter: if `core.playlist_utils` cannot be imported, `core.utils`
    cannot either - so no fallback through utils could ever rescue anything."""

    CHILD = r"""
import sys
sys.path.insert(0, %(tests)r)
sys.path.insert(0, %(src)r)
import helpers  # stubs tkinter/customtkinter when absent, isolates config

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "yt_clipper.core.playlist_utils":
            raise ImportError("blocked by probe")
        return None

sys.meta_path.insert(0, Blocker())

# Modules that import playlist_utils (directly or via core.utils) at import time.
for target in [
    "yt_clipper.core.utils",
    "yt_clipper.gui.controllers.video_loader",
    "yt_clipper.gui.controllers.queue_controller",
    "yt_clipper.gui.widgets.playlist_preview",
]:
    try:
        __import__(target)
        print("IMPORTED", target)
    except ImportError as exc:
        print("IMPORTERROR", target, str(exc))
    except Exception as exc:
        print("OTHER", target, type(exc).__name__, str(exc))

# downloader defers its playlist_utils import to call time, so the module itself
# still imports - but the deferred statement it runs inside expand_playlist()
# and download_clip() fails exactly the same way.
try:
    __import__("yt_clipper.core.downloader")
    print("IMPORTED", "yt_clipper.core.downloader")
except Exception as exc:
    print("OTHER", "yt_clipper.core.downloader", type(exc).__name__, str(exc))
try:
    from yt_clipper.core import playlist_utils
    print("IMPORTED", "downloader.deferred_import")
except ImportError as exc:
    print("IMPORTERROR", "downloader.deferred_import", str(exc))
"""

    def _run_child(self):
        script = self.CHILD % {"src": str(SRC), "tests": str(helpers.TESTS_DIR)}
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return dict(
            (line.split(" ", 2)[1], line)
            for line in proc.stdout.strip().splitlines() if line
        )

    def test_6_blocking_playlist_utils_breaks_utils_and_every_consumer(self):
        """Whenever any of these paths needs playlist_utils, its absence is
        fatal - which is why the removed `except ImportError -> core.utils`
        branches could never rescue anything (utils needs playlist_utils too)."""
        results = self._run_child()
        self.assertEqual(len(results), 6, results)
        for module in ("yt_clipper.core.utils",
                       "yt_clipper.gui.controllers.video_loader",
                       "yt_clipper.gui.controllers.queue_controller",
                       "yt_clipper.gui.widgets.playlist_preview",
                       "downloader.deferred_import"):
            self.assertIn(module, results)
            self.assertTrue(results[module].startswith("IMPORTERROR"),
                            f"{module} worked without playlist_utils: {results[module]}")
        # downloader's own module import is deferred, so it still succeeds; that
        # is unchanged by Phase 4B (the fallback was function-level too).
        self.assertTrue(
            results["yt_clipper.core.downloader"].startswith("IMPORTED"),
            results["yt_clipper.core.downloader"])

    def test_7_every_module_imports_cleanly_in_a_fresh_interpreter(self):
        """The supported case: with playlist_utils present, all of them import -
        in whichever interpreter runs this suite (with or without Pillow)."""
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(helpers.TESTS_DIR)!r});"
            f"sys.path.insert(0, {str(SRC)!r});"
            "import helpers;"
            "mods = ['yt_clipper.core.utils','yt_clipper.core.playlist_utils',"
            "'yt_clipper.core.downloader','yt_clipper.gui.controllers.video_loader',"
            "'yt_clipper.gui.controllers.queue_controller',"
            "'yt_clipper.gui.widgets.playlist_preview','yt_clipper.cli',"
            "'yt_clipper.gui.app'];"
            "[__import__(m) for m in mods];"
            "import yt_clipper.gui.controllers.video_loader as vl;"
            "print('OK', len(mods), 'PIL=', vl.Image is not None)"
        )
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertTrue(proc.stdout.startswith("OK 8"), proc.stdout)


if __name__ == "__main__":
    unittest.main()
