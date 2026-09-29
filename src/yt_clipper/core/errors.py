"""Cross-layer exception types.

`DownloadCancelled` lives here - and is re-exported by `core.ffmpeg_runner` and
`core.downloader` for backward compatibility - so that *any* layer (including
the logging helpers) can honour the cancellation contract without creating an
import cycle:

  * cancellation is not a failure;
  * it must never be swallowed by a broad `except Exception:`;
  * it must never be retried;
  * it must never be reported as success or as "video unavailable";
  * it propagates unchanged up to the cancellation boundary (the download queue
    worker, or the CLI), which translates it into a user-facing "Cancelled".
"""


class DownloadCancelled(Exception):
    """Raised after the user cancels an active clip download."""
