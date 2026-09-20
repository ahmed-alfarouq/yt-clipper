import argparse
import sys
from pathlib import Path

from yt_clipper.core import downloader
from yt_clipper.core.utils import sanitize_filename, time_to_seconds


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


def main():
    parser = argparse.ArgumentParser(description="Download a clip from a YouTube video")
    parser.add_argument("url", help="A single video URL, or a playlist URL to "
                                     "clip the same time range from every video in it.")
    parser.add_argument("start", nargs="?")
    parser.add_argument("end", nargs="?")
    parser.add_argument("-o", "--output", default="clip.mp4",
                         help="Output path for a single video. For a playlist, only "
                              "the containing folder is used; each clip is named "
                              "'NN - <video title>.<ext>'.")
    parser.add_argument("-q", "--quality", default="best", choices=["best", "4k", "1080p", "720p"],
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
        if not args.start or not args.end:
            parser.error("start and end times are required unless using --list-formats")

        start_sec = time_to_seconds(args.start)
        end_sec = time_to_seconds(args.end)

        result = downloader.expand_playlist(args.url)
        entries = result["entries"]

        if not result["is_playlist"]:
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

        # Playlist: clip the same [start, end) range from every video, one at
        # a time. A failure on one video is reported but does not abort the
        # rest of the batch.
        print(
            f"📃 Playlist detected: {len(entries)} videos. "
            f"Clipping {args.start}–{args.end} from each..."
        )

        output_arg = Path(args.output)
        output_dir = output_arg.parent if output_arg.suffix else output_arg
        output_dir.mkdir(parents=True, exist_ok=True)
        extension = ".mp3" if args.audio_only else (output_arg.suffix or ".mp4")

        succeeded = 0
        failed = 0
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
            except Exception as exc:
                print(f"❌ [{index}/{len(entries)}] {entry.get('title')}: {exc}", file=sys.stderr)
                failed += 1

        print(f"Done: {succeeded} succeeded, {failed} failed.")
        if failed and not succeeded:
            sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()