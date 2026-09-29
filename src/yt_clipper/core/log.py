"""Logging and safe-error-text helpers.

YT-Clipper had no logging architecture, so every suppressed exception was
invisible. This module is the single place that defines how failures become
observable, and how error text is made safe to show or log.

Level policy (each handler in the codebase references the contract it follows):

  DEBUG      optional-metadata or presentation-only failures that must not
             abort an operation: thumbnails, publish dates, progress stats,
             widget updates, non-essential update checks.
  INFO       normal lifecycle events, and *expected* conditions such as
             filtering an explicitly unavailable playlist entry. Expected
             private/deleted videos are never logged as errors.
  WARNING    recoverable abnormal conditions worth surfacing: configuration
             write failure, a fail-open filtering fallback, an unrecognised
             extraction error kept as "ambiguous", a folder-open that did
             nothing.
  ERROR      an operation failed: a download job, an FFmpeg run.
  EXCEPTION  unexpected programmer/system failure where a traceback is useful
             (the GUI event loop, the queue worker's catch-all).

No handler is attached here on purpose. Python's `logging.lastResort` surfaces
WARNING and above on stderr for CLI/development runs, while the packaged
windowed GUI (console=False) stays quiet and reports failures through its own
status label and queue rows. Tests observe records with a logging.Handler.

Secrets are never logged or displayed: run any text that could contain cookies,
HTTP headers, tokens, signatures or proxy credentials through
`describe_failure()` / `redact_secrets()` first.
"""

import logging
import re
from contextlib import contextmanager

from yt_clipper.core.errors import DownloadCancelled

ROOT_LOGGER_NAME = "yt_clipper"

#: Longest error text we are willing to log or display.
MAX_ERROR_TEXT = 2000

#: Header-style keys: everything up to the end of the line is sensitive.
_SENSITIVE_HEADER_KEYS = (
    "cookie", "cookies", "set-cookie", "authorization", "proxy-authorization",
    "x-goog-authuser", "login_token", "password", "passwd", "secret",
)

#: Value-style keys (also appear as URL query parameters): only the value is.
_SENSITIVE_VALUE_KEYS = (
    "token", "access_token", "refresh_token", "id_token", "api_key", "apikey",
    "api-key", "oauth_token", "signature", "sig", "sparams", "params",
    "sapisid", "hsid", "ssid", "auth", "user-agent", "referer",
)

_SENSITIVE_HEADER_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SENSITIVE_HEADER_KEYS) + r")(\s*[:=]\s*)([^\r\n]*)"
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(" + "|".join(re.escape(k) for k in _SENSITIVE_VALUE_KEYS) + r")(\s*[:=]\s*)([^&\s'\";,]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(basic|bearer|token)\s+[A-Za-z0-9._~+/\-=%]{6,}")
_COOKIE_LINE_RE = re.compile(
    r"(?i)\b[A-Z0-9_]{3,}=[^;\s]+;\s*path=[^;\s]*;?\s*domain=[^;\r\n]*"
)
_COMMAND_SWITCH_RE = re.compile(r"(?i)(-(?:cookies|headers|user-agent|referer)\s+)(\S+)")


def get_logger(name):
    """Return a namespaced logger, e.g. get_logger(__name__) or ("downloader")."""
    if name.startswith(ROOT_LOGGER_NAME + ".") or name == ROOT_LOGGER_NAME:
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def redact_secrets(text):
    """Strip cookies, auth headers, tokens, signatures and URL query strings.

    Used for anything that may end up in a log record or on screen: yt-dlp
    messages can embed signed media URLs, and FFmpeg failures can embed the
    request headers/cookies we passed on the command line.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # pragma: no cover - defensive: repr always works
            text = repr(text)

    # Order matters: recognise key/value shapes first, because the command-line
    # switch rule below blanks its whole argument and would otherwise hide the
    # key that identifies the secret (e.g. -headers 'User-Agent: ...').
    redacted = _COOKIE_LINE_RE.sub("[REDACTED_COOKIE]", text)
    redacted = _BEARER_RE.sub("[REDACTED_CREDENTIAL]", redacted)
    redacted = _SENSITIVE_HEADER_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _SENSITIVE_VALUE_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _COMMAND_SWITCH_RE.sub(r"\1[REDACTED]", redacted)

    if len(redacted) > MAX_ERROR_TEXT:
        redacted = redacted[:MAX_ERROR_TEXT] + "…[truncated]"
    return redacted


def safe_message(exc):
    """Concise, secret-free text for a user-facing message."""
    return redact_secrets(str(exc)) or exc.__class__.__name__


def describe_failure(exc):
    """Secret-free `Type: message` text for logs and diagnostics."""
    return f"{type(exc).__name__}: {redact_secrets(str(exc))}"


@contextmanager
def best_effort(description, logger=None, level=logging.DEBUG):
    """Run an optional step; never let its failure abort the surrounding flow.

    For presentation-only or optional-metadata work (widget updates, thumbnail
    decoding, preview refresh): the contract is that such a failure is
    recoverable, so it is logged and swallowed instead of propagating.

    Cancellation is the one exception that always propagates: swallowing it
    would turn a user-requested cancel into a silent no-op.
    """
    log = logger or get_logger("gui")
    try:
        yield
    except DownloadCancelled:
        raise
    except Exception as exc:
        log.log(level, "%s failed (ignored): %s", description, describe_failure(exc))
