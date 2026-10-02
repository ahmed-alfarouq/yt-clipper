import argparse
import sys
from pathlib import Path

from yt_clipper.core import downloader
from yt_clipper.core.errors import DownloadCancelled
from yt_clipper.core.log import safe_message
from yt_clipper.core.utils import (
    detect_youtube_url_type,
    sanitize_filename,
    time_to_seconds,
)

#: Exit status used when the user interrupts a run (128 + SIGINT, the usual
#: shell convention). The repository defined no cancellation status before; a
#: cancel is not a failure, but it is also not a success, so it stays non-zero.
EXIT_CANCELLED = 130


def _cli_progress_hook(data):
    """Prints only retry notices to stderr; per-frame progress stays quiet
    to match the CLI's existing behavior of not spamming download percent."""
    if data.get("status") == "retrying":
        attempt = data.get("attempt")
        max_attempts = data.get("max_attempts")
        delay = data.get("delay") or 0
        error = data.get("error") or ""
        print(
            f"  ⚠ network hiccup, retrying ({attempt}/{max_attempts}) "
            f"in {delay:.0f}s: {error}",
            file=sys.stderr,
        )


def _resolve_clip_range(parser, args):
    """Turn the optional start/end arguments into (start_sec, end_sec).

    (None, None) means "no clip range", which is the same contract the GUI
    playlist path and download_clip() already use for downloading a video in
    full. A playlist may omit the range; anything else still requires it, so
    single-video usage is unchanged (and still fails fast, before any network
    call, using the same offline URL detection the GUI uses).
    """
    if bool(args.start) != bool(args.end):
        parser.error("start and end times must be given together")
    if not args.start:
        if detect_youtube_url_type(args.url) != "playlist":
            parser.error("start and end times are required unless using --list-formats")
        return None, None
    return time_to_seconds(args.start), time_to_seconds(args.end)


def main():
    parser = argparse.ArgumentParser(description="Download a clip from a YouTube video")
    parser.add_argument("url", help="A single video URL, or a playlist URL. For a playlist, "
                                     "start/end clip the same time range from every video in "
                                     "it; omit both to download every video in full, matching "
                                     "the GUI's playlist behavior.")
    parser.add_argument("start", nargs="?",
                         help="Clip start (HH:MM:SS, MM:SS or SS). Required for a single "
                              "video; omit together with end for a playlist to download "
                              "each video in full.")
    parser.add_argument("end", nargs="?",
                         help="Clip end (same formats as start). Must be given together "
                              "with start.")
    parser.add_argument("-o", "--output", default="clip.mp4",
                         help="Output path for a single video. For a playlist, only "
                              "the containing folder is used; each clip is named "
                              "'NN - <video title>.<ext>'.")
    parser.add_argument("-q", "--quality", default=downloader.DEFAULT_QUALITY,
                         choices=list(downloader.QUALITY_CHOICES),
                         help="Ignored if -f/--format-id is given.")
    parser.add_argument("-f", "--format-id",
                         help="Exact yt-dlp format selector from --list-formats, e.g. "
                              "137 (only if it's video+audio) or 137+140 (video-only "
                              "id + audio-only id, merged). Overrides --quality. "
                              "Ignored if --audio-only is set. Not used for playlists, "
                              "since format availability can differ across videos.")
    parser.add_argument("-a", "--audio-only", action="store_true",
                         help="Extract audio only and save as MP3")
    parser.add_argument("--list-formats", action="store_true")
    args = parser.parse_args()

    try:
        if args.list_formats:
            for f in downloader.list_formats(args.url):
                size = f"{f['height']}p" if f['height'] else "audio"
                bitrate = f['vbr'] or f['abr']
                bitrate_text = f", {bitrate:.0f}kbps" if bitrate else ""
                print(f"{f['format_id']}: {f['kind']:<11} {size}, {f['ext']}{bitrate_text}")
            return
        start_sec, end_sec = _resolve_clip_range(parser, args)

        result = downloader.expand_playlist(args.url)
        entries = result["entries"]

        if not result["is_playlist"]:
            if start_sec is None or end_sec is None:
                # A playlist-looking URL that resolved to a single video: the
                # range was omitted, and single videos still require one.
                parser.error(
                    "start and end times are required for a single video "
                    "unless using --list-formats"
                )
            output_path = downloader.download_clip(
                args.url,
                start_sec,
                end_sec,
                args.output,
                args.quality,
                audio_only=args.audio_only,
                format_id=args.format_id,
                progress_hook=_cli_progress_hook,
            )
            print(f"✅ Saved to {output_path}")
            return

        # Playlist: one download per video, one at a time. With start/end the
        # same [start, end) range is clipped from every video; without them
        # every video is downloaded in full (start_sec/end_sec stay None, the
        # contract download_clip() and the GUI playlist path share). A failure
        # on one video is reported but does not abort the rest of the batch.
        range_text = (
            f"Clipping {args.start}–{args.end} from each"
            if start_sec is not None
            else "Downloading each video in full"
        )
        print(f"📃 Playlist detected: {len(entries)} videos. {range_text}...")

        output_arg = Path(args.output)
        output_dir = output_arg.parent if output_arg.suffix else output_arg
        output_dir.mkdir(parents=True, exist_ok=True)
        extension = ".mp3" if args.audio_only else (output_arg.suffix or ".mp4")

        succeeded = 0
        failed = 0
        cancelled = False
        for index, entry in enumerate(entries, start=1):
            safe_title = sanitize_filename(entry.get("title") or f"video_{index}")
            candidate = output_dir / f"{index:02d} - {safe_title}{extension}"
            try:
                saved = downloader.download_clip(
                    entry["url"],
                    start_sec,
                    end_sec,
                    str(candidate),
                    args.quality,
                    audio_only=args.audio_only,
                    progress_hook=_cli_progress_hook,
                )
                print(f"✅ [{index}/{len(entries)}] {entry.get('title')}: saved to {saved}")
                succeeded += 1
            except DownloadCancelled:
                # Cancellation stops the batch: no further video may be started
                # after the user asked to stop (§3.4). It is reported as
                # cancelled, never counted as a failure (§3.5).
                print(f"⏹ [{index}/{len(entries)}] {entry.get('title')}: cancelled",
                      file=sys.stderr)
                cancelled = True
                break
            except Exception as exc:
                # One video failing must not abort the rest of the playlist.
                print(f"❌ [{index}/{len(entries)}] {entry.get('title')}: "
                      f"{safe_message(exc)}", file=sys.stderr)
                failed += 1

        print(f"Done: {succeeded} succeeded, {failed} failed."
              + (" Cancelled." if cancelled else ""))
        if cancelled:
            sys.exit(EXIT_CANCELLED)
        if failed and not succeeded:
            sys.exit(1)
    except DownloadCancelled:
        print("⏹ Cancelled.", file=sys.stderr)
        sys.exit(EXIT_CANCELLED)
    except KeyboardInterrupt:
        # Ctrl-C is the CLI's cancellation mechanism: a concise message, not a
        # traceback (§3, §17).
        print("⏹ Cancelled.", file=sys.stderr)
        sys.exit(EXIT_CANCELLED)
    except Exception as e:
        print(f"❌ Error: {safe_message(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()