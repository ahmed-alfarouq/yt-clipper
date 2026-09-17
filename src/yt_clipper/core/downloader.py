import yt_dlp

FORMAT_MAP = {
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
    "4k": "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/best[height<=2160]",
}


def get_video_info(url):
    """Returns {'title': ..., 'duration': seconds} without downloading."""
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return {"title": info.get("title", "Unknown title"), "duration": info.get("duration", 0)}


def list_formats(url):
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        info = ydl.extract_info(url, download=False)
    return [
        {"format_id": f["format_id"], "height": f.get("height"),
         "ext": f.get("ext"), "vbr": f.get("vbr")}
        for f in info["formats"] if f.get("vcodec") != "none"
    ]


def download_clip(url, start_sec, end_sec, output_path="clip.mp4",
                   quality="best", progress_hook=None):
    if end_sec <= start_sec:
        raise ValueError("End time must be after start time")

    ydl_opts = {
        'format': FORMAT_MAP.get(quality, FORMAT_MAP["best"]),
        'outtmpl': output_path,
        'download_ranges': yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
        'force_keyframes_at_cuts': True,
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    if progress_hook:
        ydl_opts['progress_hooks'] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])