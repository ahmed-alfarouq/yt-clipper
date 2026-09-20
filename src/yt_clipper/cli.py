import argparse
import sys
from yt_clipper.core import downloader
from yt_clipper.core.utils import time_to_seconds


def main():
    parser = argparse.ArgumentParser(description="Download a clip from a YouTube video")
    parser.add_argument("url")
    parser.add_argument("start", nargs="?")
    parser.add_argument("end", nargs="?")
    parser.add_argument("-o", "--output", default="clip.mp4")
    parser.add_argument("-q", "--quality", default="best", choices=["best", "4k", "1080p", "720p"],
                         help="Ignored if -f/--format-id is given.")
    parser.add_argument("-f", "--format-id",
                         help="Exact yt-dlp format selector from --list-formats, e.g. "
                              "137 (only if it's video+audio) or 137+140 (video-only "
                              "id + audio-only id, merged). Overrides --quality. "
                              "Ignored if --audio-only is set.")
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
        output_path = downloader.download_clip(
            args.url,
            time_to_seconds(args.start),
            time_to_seconds(args.end),
            args.output,
            args.quality,
            audio_only=args.audio_only,
            format_id=args.format_id,
        )
        print(f"✅ Saved to {output_path}")
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()