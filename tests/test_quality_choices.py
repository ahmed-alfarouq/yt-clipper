"""Phase 4E — the quality choices have one canonical definition.

Four quality tokens ("best", "4k", "1080p", "720p") are written down in three
places, all of them in the same original commit (dc2adb0):

  * `downloader.FORMAT_MAP` — token -> yt-dlp format expression (the mapping);
  * `cli.py` `-q/--quality` `choices=[...]` — the tokens the CLI offers;
  * `gui/app.py` `CTkOptionMenu(values=[...])` — the tokens the GUI offers.

The CLI and GUI lists are byte-identical copies of one another, and both must
equal FORMAT_MAP's key *set*: a token offered but not understood would silently
degrade to "best" through `FORMAT_MAP.get(quality, FORMAT_MAP["best"])`, and a
token understood but never offered would be unreachable from either surface.
Their *order* differs on purpose - FORMAT_MAP is keyed best/1080p/720p/4k while
both surfaces list best/4k/1080p/720p, which is what the dropdown shows and what
`--help` prints - so the surfaces cannot simply be derived from the dict keys.

Nothing pinned any of this before: a repository-wide search found no test
mentioning FORMAT_MAP, a format expression, or argparse `choices`. These tests
pin the contract that must survive centralization:

  * the canonical list: exact tokens, exact user-facing order, no level added or
    removed, and offered set == understood set in both directions;
  * the four yt-dlp format expressions, verbatim;
  * the selection precedence inside the real `download_clip()`: audio_only, then
    format_id, then the quality mapping, then the fail-open default;
  * what the CLI and the GUI actually hand to argparse / CTkOptionMenu, read
    from their real source so one assertion covers both the pre-refactor
    literals and the post-refactor constant reference;
  * the end-to-end CLI chain: `-q <token>` reaches `download_clip()` as that
    token, and an unsupported token is rejected with exit code 2 naming the
    choices.

Scaffolding is reused rather than duplicated: `ContractTestCase` and its log
helpers come from test_failure_contracts; `run_cli`, `DownloadCallRecorder` and
`single_video_result` come from helpers. The one local double is `FormatRecorder`
because `helpers.patch_youtube_dl()` scripts extraction results and records the
URLs requested, not the options dict - and the format expression is exactly what
this phase must pin.

Nothing here touches the network. The GUI cannot be instantiated in this
environment (no tkinter), so what the dropdown is *given* is read from its
construction call rather than from a live widget.
"""

import ast
import inspect
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F402  (sets sys.path + a throwaway XDG_CONFIG_HOME)

from helpers import (  # noqa: E402
    DownloadCallRecorder,
    run_cli,
    single_video_result,
)

from yt_clipper.core import downloader  # noqa: E402

from test_failure_contracts import ContractTestCase  # noqa: E402
from test_playlist_clipping import GuiTestCase  # noqa: E402
from test_playlist_clipping import PLAYLIST_URL  # noqa: E402

SRC_ROOT = Path(helpers.__file__).resolve().parent.parent / "src" / "yt_clipper"

VIDEO_URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"

# Golden values, copied verbatim from d0f89d8 (unchanged since dc2adb0).
SUPPORTED_CHOICES = ["best", "4k", "1080p", "720p"]
FORMAT_MAP_ORDER = ["best", "1080p", "720p", "4k"]
DEFAULT_QUALITY = "best"
EXPECTED_FORMATS = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}
AUDIO_ONLY_FORMAT = "bestaudio/best"

EVAL_NAMESPACE = {"downloader": downloader, "list": list, "tuple": tuple}


# ---------------------------------------------------------------------------
# Reading what the two surfaces actually pass, from their real source
# ---------------------------------------------------------------------------

def _matching_calls(rel_path, match):
    tree = ast.parse((SRC_ROOT / rel_path).read_text(), rel_path)
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and match(node)]


def _eval_keyword(call, keyword, origin):
    for kw in call.keywords:
        if kw.arg == keyword:
            return eval(compile(ast.Expression(kw.value), origin, "eval"),
                        dict(EVAL_NAMESPACE))
    raise AssertionError(f"{origin}: the matched call has no {keyword}= keyword")


def cli_quality_argument():
    """The `parser.add_argument("-q", "--quality", ...)` call in cli.py."""
    def match(node):
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            return False
        flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
        return "-q" in flags and "--quality" in flags

    found = _matching_calls("cli.py", match)
    if len(found) != 1:
        raise AssertionError(f"expected 1 -q/--quality argument in cli.py, found {len(found)}")
    return found[0]


def gui_assigned_call(attribute):
    """The call assigned to `self.<attribute>` in gui/app.py."""
    tree = ast.parse((SRC_ROOT / "gui" / "app.py").read_text(), "gui/app.py")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == attribute:
                found.append(node.value)
    if len(found) != 1:
        raise AssertionError(
            f"expected 1 `self.{attribute} = <call>` in gui/app.py, found {len(found)}")
    return found[0]


class FormatRecorder:
    """Captures the yt-dlp `format` expression download_clip() selects, then stops.

    The sentinel message contains none of downloader's transient-error markers,
    so `_retry_call` re-raises it immediately instead of retrying with sleeps.
    """

    def __init__(self):
        self.stop = RuntimeError("format-capture-stop")
        self.formats = []

    def __call__(self, options=None):
        self.formats.append((options or {}).get("format"))
        raise self.stop

    def selection_for(self, **kwargs):
        """Run the real download_clip() and return the format expression chosen."""
        with mock.patch.object(downloader.yt_dlp, "YoutubeDL", self):
            try:
                downloader.download_clip(**kwargs)
            except RuntimeError as exc:
                if exc is not self.stop:
                    raise
        return self.formats[-1]


# ---------------------------------------------------------------------------
# The canonical definition
# ---------------------------------------------------------------------------

class TestCanonicalQualityDefinition(unittest.TestCase):
    def test_1_the_canonical_list_is_exactly_the_four_supported_levels(self):
        choices = getattr(downloader, "QUALITY_CHOICES", None)
        self.assertIsNotNone(choices,
                             "downloader has no canonical quality-choice definition")
        self.assertEqual(list(choices), SUPPORTED_CHOICES,
                         "the user-facing quality list changed (tokens or order)")
        self.assertEqual(len(list(choices)), 4,
                         "a quality level was added or removed")

    def test_2_every_choice_offered_is_a_choice_understood(self):
        offered = set(getattr(downloader, "QUALITY_CHOICES", []))
        understood = set(downloader.FORMAT_MAP)
        self.assertEqual(offered - understood, set(),
                         "a token is offered that FORMAT_MAP cannot resolve, so it "
                         "would silently degrade to 'best'")
        self.assertEqual(understood - offered, set(),
                         "FORMAT_MAP resolves a token that no surface offers")

    def test_3_the_format_expressions_are_unchanged(self):
        self.assertEqual(downloader.FORMAT_MAP, EXPECTED_FORMATS,
                         "a yt-dlp format expression changed")
        self.assertEqual(list(downloader.FORMAT_MAP), FORMAT_MAP_ORDER,
                         "FORMAT_MAP's own key order changed")

    def test_4_the_default_quality_is_best_everywhere(self):
        default = getattr(downloader, "DEFAULT_QUALITY", None)
        self.assertIsNotNone(default,
                             "downloader has no canonical default quality")
        self.assertEqual(default, DEFAULT_QUALITY, "the default quality changed")
        self.assertIn(default, list(downloader.QUALITY_CHOICES),
                      "the default is not one of the offered choices")
        signature = inspect.signature(downloader.download_clip)
        self.assertEqual(signature.parameters["quality"].default, DEFAULT_QUALITY,
                         "download_clip()'s own default changed")


# ---------------------------------------------------------------------------
# The selection inside the real download_clip()
# ---------------------------------------------------------------------------

class TestFormatSelectionIsUnchanged(ContractTestCase):
    def setUp(self):
        super().setUp()
        self.recorder = FormatRecorder()

    def select(self, **kwargs):
        options = {
            "url": VIDEO_URL,
            "start_sec": 0,
            "end_sec": 5,
            "output_path": str(self.tmp / "clip.mp4"),
        }
        options.update(kwargs)
        with self.capture():
            return self.recorder.selection_for(**options)

    def test_5_each_supported_choice_selects_its_own_expression(self):
        for choice in SUPPORTED_CHOICES:
            with self.subTest(quality=choice):
                self.assertEqual(self.select(quality=choice), EXPECTED_FORMATS[choice])

    def test_6_audio_only_and_format_id_take_precedence_over_quality(self):
        for choice in SUPPORTED_CHOICES:
            with self.subTest(quality=choice):
                self.assertEqual(self.select(quality=choice, audio_only=True),
                                 AUDIO_ONLY_FORMAT,
                                 "audio-only no longer wins over the quality choice")
                self.assertEqual(self.select(quality=choice, format_id="137+140"),
                                 "137+140",
                                 "an explicit format_id no longer wins over quality")

    def test_7_an_unsupported_quality_fails_open_to_the_best_expression(self):
        for unsupported in ("480p", "360p", "bogus", "", None):
            with self.subTest(quality=unsupported):
                self.assertEqual(self.select(quality=unsupported),
                                 EXPECTED_FORMATS["best"],
                                 "the fail-open default changed")

    def test_8_the_default_selection_is_the_best_expression(self):
        self.assertEqual(self.select(), EXPECTED_FORMATS["best"])


# ---------------------------------------------------------------------------
# What the two surfaces actually hand to argparse / CTkOptionMenu
# ---------------------------------------------------------------------------

class TestSurfacesOfferTheCanonicalChoices(unittest.TestCase):
    def test_9_the_cli_quality_argument_offers_exactly_the_supported_choices(self):
        call = cli_quality_argument()
        self.assertEqual(_eval_keyword(call, "choices", "cli.py"), SUPPORTED_CHOICES,
                         "the CLI's offered qualities changed")
        self.assertEqual(_eval_keyword(call, "default", "cli.py"), DEFAULT_QUALITY,
                         "the CLI's default quality changed")

    def test_10_the_gui_dropdown_offers_exactly_the_supported_choices(self):
        menu = gui_assigned_call("quality_menu")
        self.assertEqual(_eval_keyword(menu, "values", "gui/app.py"), SUPPORTED_CHOICES,
                         "the GUI dropdown's qualities or their order changed")
        variable = gui_assigned_call("quality_var")
        self.assertEqual(_eval_keyword(variable, "value", "gui/app.py"), DEFAULT_QUALITY,
                         "the GUI's initial quality changed")

    def test_11_the_choice_list_is_written_down_once(self):
        """Structural pin: neither surface may hardcode the tokens again."""
        for rel in ("cli.py", "gui/app.py"):
            lines = (SRC_ROOT / rel).read_text().splitlines()
            hardcoded = [f"{rel}:{number}" for number, line in enumerate(lines, 1)
                         if '"4k"' in line]
            self.assertEqual(hardcoded, [],
                             f"{rel} still hardcodes the quality tokens")
            self.assertIn("QUALITY_CHOICES", "\n".join(lines),
                          f"{rel} does not reference the canonical definition")


# ---------------------------------------------------------------------------
# The CLI contract, end to end through argparse
# ---------------------------------------------------------------------------

class TestCliQualityContract(ContractTestCase):
    def test_12_an_unsupported_quality_is_rejected_naming_the_choices(self):
        code, _out, err = run_cli([VIDEO_URL, "0", "5", "-q", "480p",
                                   "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 2, "argparse accepted a quality no surface offers")
        self.assertIn("invalid choice", err)
        for choice in SUPPORTED_CHOICES:
            self.assertIn(choice, err, "the usage error stopped naming a choice")

    def test_13_every_supported_quality_reaches_download_clip_unchanged(self):
        for choice in SUPPORTED_CHOICES:
            with self.subTest(quality=choice):
                recorder = DownloadCallRecorder()
                with mock.patch.object(
                        downloader, "expand_playlist",
                        return_value=single_video_result(VIDEO_URL)), \
                        recorder.patch():
                    code, _out, err = run_cli(
                        [VIDEO_URL, "0", "5", "-q", choice,
                         "-o", str(self.tmp / "clip.mp4")])
                self.assertEqual(code, 0, err)
                self.assertEqual(len(recorder.calls), 1)
                self.assertEqual(recorder.calls[0]["quality"], choice,
                                 "the CLI did not pass the chosen quality through")

    def test_14_omitting_the_quality_still_defaults_to_best(self):
        recorder = DownloadCallRecorder()
        with mock.patch.object(downloader, "expand_playlist",
                               return_value=single_video_result(VIDEO_URL)), \
                recorder.patch():
            code, _out, err = run_cli([VIDEO_URL, "0", "5",
                                       "-o", str(self.tmp / "clip.mp4")])
        self.assertEqual(code, 0, err)
        self.assertEqual(recorder.calls[0]["quality"], DEFAULT_QUALITY)


# ---------------------------------------------------------------------------
# The GUI half of "UI value -> internal value"
# ---------------------------------------------------------------------------

class TestGuiQualityReachesTheDownloader(GuiTestCase):
    """A *chosen* quality must travel from the dropdown to every download call.

    The frozen Phase 2 parity test compares the quality the GUI and the CLI hand
    to download_clip(), but only ever with the default value. This pins the
    non-default case and re-confirms that GUI playlist jobs are still full
    videos. It reuses the Phase 2 GUI harness (FakeApp, QueueHarness, the
    render_queue no-op and the harness shutdown cleanup) instead of building a
    second one.
    """

    def test_15_a_chosen_gui_quality_reaches_every_download_call(self):
        recorder = DownloadCallRecorder()
        app = self.make_app(url=PLAYLIST_URL, entries=self.entries, quality="720p")
        self.run_playlist_download(app, recorder, len(self.entries))

        self.assertEqual([call["quality"] for call in recorder.calls],
                         ["720p"] * len(self.entries),
                         "the GUI did not pass the chosen quality to the downloader")
        self.assertEqual(recorder.ranges, [(None, None)] * len(self.entries),
                         "GUI playlist jobs stopped being full-video downloads")


if __name__ == "__main__":
    unittest.main()
