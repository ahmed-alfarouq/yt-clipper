import threading
import webbrowser

import customtkinter as ctk

from yt_clipper.core import updater
from yt_clipper.core.log import describe_failure, get_logger

logger = get_logger(__name__)

CURRENT_VERSION = "1.2.0"
UPDATE_OWNER = "ahmed-alfarouq"
UPDATE_REPO = "yt-clipper"


class UpdateChecker:
    """Checks GitHub for a newer release and shows a dismissible banner."""

    def __init__(self, app):
        self.app = app

    def check_update_async(self):
        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            is_newer, latest, url = updater.check_for_update(
                UPDATE_OWNER,
                UPDATE_REPO,
                CURRENT_VERSION,
            )
            if is_newer and latest and url:
                self.app._post_ui_event("update_available", latest, url)
        except Exception as exc:
            # Update checks are optional and must never interfere with downloads
            # (§13): recorded, then the worker thread ends quietly.
            logger.debug("Update banner check failed (ignored): %s",
                         describe_failure(exc))
            return

    def _open_release(self, url):
        """Open the release page; a failure to launch a browser is not fatal."""
        try:
            if not webbrowser.open(url):
                logger.warning("No browser could open the release page %s", url)
        except Exception as exc:
            logger.warning("Could not open the release page %s: %s",
                           url, describe_failure(exc))

    def _show_update_banner(self, latest, url):
        app = self.app
        for widget in app.banner_container.winfo_children():
            widget.destroy()

        app.banner_container.pack(
            fill="x",
            side="top",
            # title_row is a direct child of `self`, same as banner_container,
            # so Tk's `before` option (which requires a shared parent) is
            # valid here. header_label itself now lives inside title_row,
            # not directly under `self`, so it can no longer be used here.
            before=app.title_row,
        )
        banner = ctk.CTkFrame(
            app.banner_container,
            fg_color="#2d5f8a",
            corner_radius=0,
        )
        banner.pack(fill="x")
        ctk.CTkLabel(
            banner,
            text=f"🔔 Version {latest} is available",
            text_color="white",
        ).pack(side="left", padx=15, pady=8)
        ctk.CTkButton(
            banner,
            text="View Release",
            width=110,
            height=28,
            command=lambda: self._open_release(url),
        ).pack(side="right", padx=(0, 10), pady=8)
        ctk.CTkButton(
            banner,
            text="✕",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color="#1e4566",
            command=self._dismiss_update_banner,
        ).pack(side="right", pady=8)

    def _dismiss_update_banner(self):
        app = self.app
        for widget in app.banner_container.winfo_children():
            widget.destroy()
        app.banner_container.pack_forget()