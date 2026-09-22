import io
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

import customtkinter as ctk

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

from yt_clipper.core.utils import (
    format_publish_date,
    sort_videos_by_publish_date,
)

THUMBNAIL_SIZE = (120, 68)
# Two items visible: each item ~ 84px + padding, so ~ 190px height
SCROLL_FRAME_HEIGHT = 190
ITEM_HEIGHT = 84


class PlaylistPreviewWidget(ctk.CTkFrame):
    """Scrollable playlist preview sorted oldest→newest.

    Displays thumbnail, title, publish date for each video.
    Clicking a video opens its YouTube URL in the default browser.

    Responsibilities:
    - Receives sorted (or unsorted) video list, sorts internally via
      sort_videos_by_publish_date for safety
    - Renders scrollable container with fixed max height (~2 items)
    - Handles thumbnail fetching with in-memory cache, background threads
    - Handles empty, missing thumbnail/title/date, invalid URL gracefully
    - Clears previous preview when new playlist loaded
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)

        self._videos: List[Dict[str, Any]] = []
        self._item_frames: List[ctk.CTkFrame] = []
        self._thumbnail_cache: Dict[str, Any] = {}  # url -> CTkImage or None placeholder
        self._thumbnail_images_refs: List[Any] = []  # keep CTkImage alive
        self._request_id = 0

        # Header
        self.header_label = ctk.CTkLabel(
            self,
            text="Playlist Preview (oldest → newest)",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.header_label.pack(fill="x", padx=2, pady=(0, 5))

        # Scrollable area with fixed height
        self.scroll_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="gray14",
            corner_radius=10,
            height=SCROLL_FRAME_HEIGHT,
        )
        self.scroll_frame.pack(fill="x", expand=False)

        # Configure inner frame to not expand unnecessarily
        # Empty state label (managed dynamically)
        self._empty_label: Optional[ctk.CTkLabel] = None

        self._show_empty_state("No playlist loaded yet")

    # ---------- Public API ----------

    def set_videos(self, videos: List[Dict[str, Any]]):
        """Set new playlist videos, sorted oldest→newest.

        Clears previous preview, handles stale data via request_id.
        """
        self._request_id += 1
        current_req = self._request_id

        # Defensive copy + sort (does not mutate original)
        try:
            sorted_videos = sort_videos_by_publish_date(videos or [])
        except Exception:
            # If sorting fails for any reason, fall back to original order
            sorted_videos = list(videos or [])

        self._videos = sorted_videos
        self._clear_items()

        if not sorted_videos:
            self._show_empty_state("This playlist has no videos")
            return

        # Remove empty label if present
        self._hide_empty_state()

        for idx, video in enumerate(sorted_videos):
            self._create_video_item(video, idx, current_req)

        # Update header with count
        self.header_label.configure(
            text=f"Playlist Preview — {len(sorted_videos)} videos (oldest → newest)"
        )

    def clear(self):
        """Clear preview (e.g., when loading new playlist or on reset)."""
        self._request_id += 1
        self._videos = []
        self._clear_items()
        self._show_empty_state("No playlist loaded yet")
        self.header_label.configure(text="Playlist Preview (oldest → newest)")

    def show_loading(self, message: str = "Loading playlist..."):
        """Show loading state while playlist metadata is being fetched."""
        self._request_id += 1
        self._clear_items()
        self._show_empty_state(message)
        self.header_label.configure(text="Playlist Preview (oldest → newest)")

    # ---------- Internal helpers ----------

    def _clear_items(self):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self._item_frames.clear()
        # Keep thumbnail cache but not image refs that are no longer needed?
        # Keep cache to avoid re-fetching same thumbnails when switching playlists
        # that share videos. Clear refs to allow GC of unused CTkImages.
        self._thumbnail_images_refs.clear()
        self._hide_empty_state()

    def _show_empty_state(self, message: str):
        self._hide_empty_state()
        self._empty_label = ctk.CTkLabel(
            self.scroll_frame,
            text=message,
            text_color="gray",
            anchor="w",
            justify="left",
            wraplength=380,
        )
        self._empty_label.pack(pady=14, padx=10, anchor="w")

    def _hide_empty_state(self):
        if self._empty_label is not None:
            try:
                self._empty_label.destroy()
            except Exception:
                pass
            self._empty_label = None

    def _create_video_item(self, video: Dict[str, Any], index: int, request_id: int):
        title = video.get("title") or "Unknown title"
        url = video.get("url") or ""
        thumbnail_url = video.get("thumbnail")
        publish_date_display = format_publish_date(video)

        # Item frame
        item_frame = ctk.CTkFrame(
            self.scroll_frame,
            fg_color="gray20",
            corner_radius=8,
            height=ITEM_HEIGHT,
        )
        item_frame.pack(fill="x", padx=6, pady=4)
        # Prevent frame from shrinking due to children
        item_frame.pack_propagate(False)

        # Make entire frame clickable if URL valid
        is_valid_url = isinstance(url, str) and url.startswith("http")

        # Thumbnail label
        thumb_label = ctk.CTkLabel(
            item_frame,
            text="",
            width=THUMBNAIL_SIZE[0],
            height=THUMBNAIL_SIZE[1],
            fg_color="gray25",
            corner_radius=6,
        )
        thumb_label.pack(side="left", padx=8, pady=8)

        # Try cache first
        if thumbnail_url and thumbnail_url in self._thumbnail_cache:
            cached = self._thumbnail_cache[thumbnail_url]
            if cached is not None:
                thumb_label.configure(image=cached, text="")
                self._thumbnail_images_refs.append(cached)
            else:
                thumb_label.configure(text="⚠")
        else:
            # Placeholder, fetch in background
            thumb_label.configure(text="…", text_color="gray")
            if thumbnail_url:
                threading.Thread(
                    target=self._fetch_thumbnail_worker,
                    args=(thumbnail_url, thumb_label, request_id),
                    daemon=True,
                ).start()
            else:
                thumb_label.configure(text="⚠")

        # Text container
        text_frame = ctk.CTkFrame(item_frame, fg_color="transparent")
        text_frame.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=6)

        title_label = ctk.CTkLabel(
            text_frame,
            text=title,
            anchor="w",
            justify="left",
            wraplength=280,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        title_label.pack(fill="x", anchor="w")

        date_label = ctk.CTkLabel(
            text_frame,
            text=publish_date_display,
            anchor="w",
            justify="left",
            text_color="gray70",
            font=ctk.CTkFont(size=11),
        )
        date_label.pack(fill="x", anchor="w", pady=(2, 0))

        # Click handling
        if is_valid_url:
            # Bind click to all relevant widgets
            for widget in (item_frame, thumb_label, text_frame, title_label, date_label):
                widget.bind("<Button-1>", lambda e, u=url: self._open_url(u))
                widget.configure(cursor="hand2")

            # Hover effect
            def _on_enter(e, frame=item_frame):
                try:
                    frame.configure(fg_color="gray24")
                except Exception:
                    pass

            def _on_leave(e, frame=item_frame):
                try:
                    frame.configure(fg_color="gray20")
                except Exception:
                    pass

            for widget in (item_frame, thumb_label, text_frame, title_label, date_label):
                widget.bind("<Enter>", _on_enter)
                widget.bind("<Leave>", _on_leave)

        self._item_frames.append(item_frame)

    def _fetch_thumbnail_worker(self, thumbnail_url: str, label: ctk.CTkLabel, request_id: int):
        """Background thread: fetch thumbnail, then update UI on main thread."""
        if request_id != self._request_id:
            return  # Stale request, playlist changed

        if Image is None:
            # Pillow not available, mark as failed in cache
            self._thumbnail_cache[thumbnail_url] = None
            return

        try:
            req = urllib.request.Request(
                thumbnail_url,
                headers={"User-Agent": "YT-Clipper/1.0"},
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = resp.read()
            pil_image = Image.open(io.BytesIO(data)).convert("RGB")
            pil_image.thumbnail(THUMBNAIL_SIZE)
            pil_image.load()

            # Schedule UI update on main thread
            def _update():
                if request_id != self._request_id:
                    return
                try:
                    # Check if label still exists
                    if not label.winfo_exists():
                        return
                    ctk_img = ctk.CTkImage(
                        light_image=pil_image,
                        dark_image=pil_image,
                        size=pil_image.size,
                    )
                    label.configure(image=ctk_img, text="")
                    self._thumbnail_images_refs.append(ctk_img)
                    self._thumbnail_cache[thumbnail_url] = ctk_img
                except Exception:
                    # Thumbnail display failure should not crash app
                    try:
                        label.configure(text="⚠", image=None)
                    except Exception:
                        pass
                    self._thumbnail_cache[thumbnail_url] = None

            # Use after to run on main thread
            try:
                label.after(0, _update)
            except Exception:
                # If after fails (widget destroyed), ignore
                pass

        except Exception:
            # Cache failure to avoid repeated attempts
            self._thumbnail_cache[thumbnail_url] = None

            def _fail_update():
                if request_id != self._request_id:
                    return
                try:
                    if label.winfo_exists():
                        label.configure(text="⚠", image=None)
                except Exception:
                    pass

            try:
                label.after(0, _fail_update)
            except Exception:
                pass

    @staticmethod
    def _open_url(url: str):
        if not url or not isinstance(url, str):
            return
        if not url.startswith("http"):
            return
        try:
            webbrowser.open(url)
        except Exception:
            # Opening browser is best-effort, never crash app
            pass
