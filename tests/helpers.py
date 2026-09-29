"""Shared test scaffolding for the YT-Clipper suite.

The project has no test infrastructure and its runtime dependencies (yt-dlp,
customtkinter, Pillow, tkinter) are media/GUI packages that a test runner should
not need. Everything here is therefore *stub-only-if-missing*: when the real
packages are installed they are used untouched, and tests patch the specific
external boundary they care about (yt_dlp.YoutubeDL, downloader.download_clip,
FFmpeg, messagebox...).

Nothing in this module touches the network or creates a Tk window.
"""

import contextlib
import io
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"

for _path in (str(SRC_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# yt_clipper.core.config creates its config directory at import time. Point it
# at a throwaway location *before* anything imports it, so tests never read or
# write the real user config (%APPDATA% / $XDG_CONFIG_HOME).
_TEST_CONFIG_HOME = tempfile.mkdtemp(prefix="yt-clipper-test-config-")
os.environ["XDG_CONFIG_HOME"] = _TEST_CONFIG_HOME
os.environ["APPDATA"] = _TEST_CONFIG_HOME


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------

def ensure_yt_dlp_importable():
    """Register a minimal `yt_dlp` stand-in when the real package is absent.

    Provides only what yt_clipper.core.downloader imports at module level.
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
        """Stand-in; tests that reach FFmpeg patch this anyway."""

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


USING_YT_DLP_STUB = ensure_yt_dlp_importable()


def patch_youtube_dl(script):
    """Replace yt_dlp.YoutubeDL with a scripted fake.

    `script` maps a URL to the info dict to return, an exception instance to
    raise, or a zero-arg callable producing either. Returns (patcher, calls),
    where `calls` records every URL yt-dlp was asked for, in order.
    """
    from yt_clipper.core import downloader

    calls = []

    class FakeYoutubeDL:
        def __init__(self, options=None):
            self.options = options or {}
            self.params = dict(self.options)
            self.cookiejar = None

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


class FakeFFmpegPostProcessor:
    """Stands in for yt_dlp.postprocessor.ffmpeg.FFmpegPostProcessor."""

    available = True
    executable = "/usr/bin/ffmpeg"

    def __init__(self, downloader=None):
        self._downloader = downloader

    def check_version(self):
        return None


# ---------------------------------------------------------------------------
# GUI toolkit
# ---------------------------------------------------------------------------

class _StubMeta(type):
    """Lets stub widgets answer any class-level attribute (e.g. messagebox.showerror)."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


class StubWidget(metaclass=_StubMeta):
    """Permissive stand-in for a CustomTkinter/Tk widget.

    Tests that exercise presentation patch it out (e.g. render_queue); this
    only has to exist so the controller modules can be imported headlessly.
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


def _stub_module(name, **attrs):
    module = types.ModuleType(name)

    def __getattr__(attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        return type(attr, (StubWidget,), {})

    module.__getattr__ = __getattr__
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def ensure_gui_toolkit_importable():
    """Stub tkinter/customtkinter when they are not installed.

    CustomTkinter needs Tk, which needs a display, so the GUI cannot be
    instantiated in CI/sandboxes. The controller/service layer can still be
    imported and driven with a fake app object, which is the closest stable
    boundary to the real Download button. Returns the list of stubbed packages.
    """
    stubbed = []
    try:
        import tkinter  # noqa: F401
    except ImportError:
        tkinter = _stub_module("tkinter")
        tkinter.messagebox = _stub_module(
            "tkinter.messagebox",
            showerror=mock.MagicMock(),
            showinfo=mock.MagicMock(),
            showwarning=mock.MagicMock(),
        )
        tkinter.filedialog = _stub_module(
            "tkinter.filedialog",
            asksaveasfilename=mock.MagicMock(return_value=""),
            askdirectory=mock.MagicMock(return_value=""),
        )
        stubbed.append("tkinter")

    try:
        import customtkinter  # noqa: F401
    except ImportError:
        ctk = _stub_module(
            "customtkinter",
            set_appearance_mode=lambda *a, **k: None,
            set_default_color_theme=lambda *a, **k: None,
        )
        ctk.widgets = _stub_module("customtkinter.widgets")
        stubbed.append("customtkinter")

    return stubbed


STUBBED_GUI_PACKAGES = ensure_gui_toolkit_importable()


def import_gui_modules():
    """Import the GUI layer (stubbed toolkit permitting) and return it."""
    from yt_clipper.gui.app import ClipperApp
    from yt_clipper.gui.controllers.queue_controller import QueueController
    from yt_clipper.gui.models import DownloadJob
    from yt_clipper.gui.services import SequentialDownloadQueue

    return {
        "ClipperApp": ClipperApp,
        "QueueController": QueueController,
        "DownloadJob": DownloadJob,
        "SequentialDownloadQueue": SequentialDownloadQueue,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv, cli_module=None):
    """Invoke cli.main() with `argv`; returns (exit_code, stdout, stderr).

    exit_code is 0 for a normal return, otherwise the SystemExit code
    (2 = argparse usage error, 1 = reported failure).
    """
    if cli_module is None:
        from yt_clipper import cli as cli_module

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["run_cli.py", *argv]):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_module.main()
            except SystemExit as exc:
                raw = exc.code
                code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def flat_playlist(entries, title="Test Playlist"):
    """An expand_playlist() result for a playlist."""
    return {"is_playlist": True, "playlist_title": title, "entries": entries}


def single_video_result(url, title="Single video", duration=200.0):
    """An expand_playlist() result for a single video URL."""
    return {
        "is_playlist": False,
        "playlist_title": None,
        "entries": [{
            "url": url,
            "title": title,
            "duration": duration,
            "thumbnail": None,
            "publish_date": None,
            "upload_date": None,
            "timestamp": None,
            "release_timestamp": None,
        }],
    }


def playlist_entry(video_id, title=None):
    """A normalized playlist entry, as expand_playlist() returns them."""
    return {
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title if title is not None else f"video {video_id}",
        "duration": 100,
        "thumbnail": None,
        "publish_date": None,
        "upload_date": None,
        "timestamp": None,
        "release_timestamp": None,
        "availability": "public",
    }


def wait_for(predicate, timeout=10.0, interval=0.005):
    """Poll `predicate` until true; returns False on timeout (no sleeping tests)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class DownloadCallRecorder:
    """Records the arguments that reach downloader.download_clip().

    `gate` (a threading.Event) blocks the FIRST call until it is set, which
    makes "cancel a pending job" / "cancel the active job" tests deterministic
    instead of racing the worker thread.
    """

    def __init__(self, result="/tmp/recorded.mp4", delay=0.0, gate=None):
        self.calls = []
        self.result = result
        self.delay = delay
        self.gate = gate
        self._gate_used = False

    def __call__(self, url, start_sec, end_sec, output_path="clip.mp4",
                 quality="best", audio_only=False, format_id=None,
                 progress_hook=None, cancel_event=None):
        self.calls.append({
            "url": url,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "output_path": output_path,
            "quality": quality,
            "audio_only": audio_only,
            "format_id": format_id,
            "cancel_event": cancel_event,
        })
        if self.gate is not None and not self._gate_used:
            self._gate_used = True
            self.gate.wait(10.0)
        if self.delay:
            time.sleep(self.delay)
        if cancel_event is not None and cancel_event.is_set():
            from yt_clipper.core.downloader import DownloadCancelled
            raise DownloadCancelled("Download cancelled")
        return self.result

    @property
    def ranges(self):
        return [(call["start_sec"], call["end_sec"]) for call in self.calls]

    @property
    def urls(self):
        return [call["url"] for call in self.calls]

    @property
    def filenames(self):
        return [Path(call["output_path"]).name for call in self.calls]

    def patch(self, module=None):
        """Patch downloader.download_clip with this recorder."""
        if module is None:
            from yt_clipper.core import downloader as module
        return mock.patch.object(module, "download_clip", side_effect=self)
