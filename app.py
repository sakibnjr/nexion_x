#!/usr/bin/env python3
"""
Linux Download Manager (GTK)

A lightweight desktop download manager for Linux using GTK 3 + PyGObject.
Features:
- Add downloads by URL and choose save location
- Parallel downloads
- Pause/Resume (server must support HTTP Range)
- Shows progress, speed, and ETA
- Start All / Pause All / Remove selected

Dependencies:
  sudo apt-get install -y python3-gi gir1.2-gtk-3.0
  pip install requests

Run:
  python3 download_manager.py

Test URLs:
  https://speed.hetzner.de/100MB.bin
  https://speed.hetzner.de/1GB.bin

Note:
- Resuming requires the server to support HTTP Range requests.
- Partial files are stored as <filename>.part until completion.
"""
import os
import sys
import math
import time
import errno
import queue
import signal
import shutil
import threading
from dataclasses import dataclass, field
from typing import Optional, Deque
from collections import deque

import requests
import urllib3

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GObject, GLib

# Make sure GLib is threads-aware
GObject.threads_init()

# ------------------------- Utilities -------------------------

def human_size(num_bytes: Optional[float]) -> str:
    if num_bytes is None:
        return "?"
    if num_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while num_bytes >= 1024 and i < len(units) - 1:
        num_bytes /= 1024.0
        i += 1
    return f"{num_bytes:.1f} {units[i]}"


def human_time(seconds: Optional[float]) -> str:
    if seconds is None or math.isinf(seconds) or seconds < 0:
        return "--"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:d}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m:d}m {s:02d}s"
    return f"{s:d}s"


# ------------------------- Download Worker -------------------------

@dataclass
class DownloadItem:
    url: str
    dest_path: str
    app_ref: 'DownloadManagerApp' = field(repr=False)

    id: int = field(default_factory=lambda: int(time.time() * 1000))
    filename: str = field(init=False)
    status: str = field(default="Queued")
    total_size: Optional[int] = None
    downloaded: int = 0
    supports_range: bool = False

    _thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # For speed calculation
    _speed_window: Deque = field(default_factory=lambda: deque(maxlen=50), init=False, repr=False)  # ~5s @ 10Hz
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None

    def __post_init__(self):
        self.filename = os.path.basename(self.dest_path)

    # ---- Public controls ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker, name=f"dl-{self.id}", daemon=True)
        self._thread.start()

    def pause(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self.status = "Pausing..."

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- Internal logic ----
    def _worker(self):
        try:
            GLib.idle_add(self._update_status, "Starting...")
            part_path = self.dest_path + ".part"
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.dest_path) or ".", exist_ok=True)

            # Determine resumption point
            existing = 0
            if os.path.exists(part_path):
                try:
                    existing = os.path.getsize(part_path)
                except OSError:
                    existing = 0

            # HEAD request: learn size and range support
            session = requests.Session()
            # Configure adapters with simpler retry strategy
            from requests.adapters import HTTPAdapter
            
            adapter = HTTPAdapter(max_retries=3)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            
            # Set headers for better compatibility
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            
            try:
                GLib.idle_add(self._update_status, "Checking file info...")
                head = session.head(self.url, timeout=30, allow_redirects=True, verify=False)
                self.supports_range = head.headers.get("Accept-Ranges", "").lower() == "bytes"
                total_from_head = head.headers.get("Content-Length")
                if total_from_head is not None:
                    self.total_size = int(total_from_head)
            except Exception as e:
                # Not fatal; proceed to GET
                GLib.idle_add(self._update_status, "Starting download...")
                pass

            # Prepare GET with Range if resuming
            headers = {}
            mode = "wb"
            if existing > 0:
                if self.supports_range:
                    headers["Range"] = f"bytes={existing}-"
                    mode = "ab"
                    self.downloaded = existing
                    GLib.idle_add(self._update_status, f"Resuming from {human_size(existing)}...")
                else:
                    # Cannot resume; restart
                    existing = 0
                    self.downloaded = 0
                    GLib.idle_add(self._update_status, "Cannot resume, restarting...")

            GLib.idle_add(self._update_status, "Connecting...")
            with session.get(self.url, stream=True, headers=headers, timeout=60, verify=False) as r:
                r.raise_for_status()

                # Update total size for resumed/unknown-length downloads
                if self.total_size is None:
                    cl = r.headers.get("Content-Length")
                    if cl is not None:
                        length = int(cl)
                        if existing and self.supports_range and r.status_code == 206:
                            # length is remaining; full size = existing + remaining
                            self.total_size = existing + length
                        else:
                            self.total_size = length

                # Content-Length may still be None (chunked). Handle gracefully.
                chunk_sz = 64 * 1024
                last_ui = 0.0
                start_time = time.time()
                last_bytes = self.downloaded
                last_time = start_time

                self.status = "Downloading"
                GLib.idle_add(self.app_ref.refresh_row, self)

                with open(part_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=chunk_sz):
                        if self._stop_event.is_set():
                            self.status = "Paused"
                            GLib.idle_add(self.app_ref.refresh_row, self)
                            return
                        if chunk:
                            f.write(chunk)
                            with self._progress_lock:
                                self.downloaded += len(chunk)

                        now = time.time()
                        if now - last_ui >= 0.2:  # update UI ~5x/sec
                            # speed calc over rolling window (~5s)
                            dt = now - last_time
                            if dt > 0:
                                delta = self.downloaded - last_bytes
                                inst_speed = delta / dt
                                self._speed_window.append((now, inst_speed))
                                # average
                                cutoff = now - 5
                                speeds = [s for t, s in self._speed_window if t >= cutoff]
                                self.speed_bps = sum(speeds) / len(speeds) if speeds else 0.0

                                if self.total_size:
                                    remain = max(self.total_size - self.downloaded, 0)
                                    self.eta_seconds = remain / self.speed_bps if self.speed_bps > 0 else None
                                else:
                                    self.eta_seconds = None

                            last_time = now
                            last_bytes = self.downloaded
                            last_ui = now
                            GLib.idle_add(self.app_ref.refresh_row, self)

                # Completed
                try:
                    shutil.move(part_path, self.dest_path)
                except Exception:
                    # If move fails, keep part file
                    pass
                self.status = "Done"
                self.speed_bps = 0.0
                self.eta_seconds = 0.0
                GLib.idle_add(self.app_ref.refresh_row, self)
        except requests.exceptions.ConnectionError as e:
            self.status = f"Connection error: Network unreachable"
            GLib.idle_add(self.app_ref.refresh_row, self)
        except requests.exceptions.Timeout as e:
            self.status = f"Timeout error: Server took too long to respond"
            GLib.idle_add(self.app_ref.refresh_row, self)
        except requests.exceptions.RequestException as e:
            self.status = f"Request error: {str(e)}"
            GLib.idle_add(self.app_ref.refresh_row, self)
        except requests.HTTPError as e:
            self.status = f"HTTP error: {e.response.status_code}"
            GLib.idle_add(self.app_ref.refresh_row, self)
        except Exception as e:
            self.status = f"Error: {str(e)}"
            GLib.idle_add(self.app_ref.refresh_row, self)

    def _update_status(self, text: str):
        self.status = text
        self.app_ref.refresh_row(self)
        return False


# ------------------------- GTK App -------------------------

class DownloadManagerApp(Gtk.Window):
    COL_FILENAME = 0
    COL_PROGRESS = 1
    COL_STATUS = 2
    COL_SPEED = 3
    COL_ETA = 4
    COL_URL = 5
    COL_OBJ = 6

    def __init__(self):
        super().__init__(title="Linux Download Manager")
        self.set_default_size(900, 420)
        self.set_border_width(6)
        self.connect("destroy", self.on_destroy)

        # Headerbar
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.props.title = "Download Manager"
        self.set_titlebar(hb)

        self.add_button = Gtk.Button.new_from_icon_name("list-add", Gtk.IconSize.BUTTON)
        self.add_button.set_tooltip_text("Add download")
        self.add_button.connect("clicked", self.on_add_clicked)
        hb.pack_start(self.add_button)

        self.start_all_btn = Gtk.Button.new_from_icon_name("media-playback-start", Gtk.IconSize.BUTTON)
        self.start_all_btn.set_tooltip_text("Start all")
        self.start_all_btn.connect("clicked", self.on_start_all)
        hb.pack_start(self.start_all_btn)

        self.pause_all_btn = Gtk.Button.new_from_icon_name("media-playback-pause", Gtk.IconSize.BUTTON)
        self.pause_all_btn.set_tooltip_text("Pause all")
        self.pause_all_btn.connect("clicked", self.on_pause_all)
        hb.pack_start(self.pause_all_btn)

        self.remove_btn = Gtk.Button.new_from_icon_name("edit-delete", Gtk.IconSize.BUTTON)
        self.remove_btn.set_tooltip_text("Remove selected")
        self.remove_btn.connect("clicked", self.on_remove_selected)
        hb.pack_end(self.remove_btn)

        # ListStore model
        self.store = Gtk.ListStore(str, int, str, str, str, str, object)

        # TreeView
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_rules_hint(True)
        self.view.set_headers_clickable(True)
        self.view.connect("row-activated", self.on_row_activated)

        # Columns
        # Filename
        renderer_text = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("File", renderer_text, text=self.COL_FILENAME)
        col.set_sort_column_id(self.COL_FILENAME)
        col.set_resizable(True)
        self.view.append_column(col)

        # Progress
        renderer_prog = Gtk.CellRendererProgress()
        col = Gtk.TreeViewColumn("Progress", renderer_prog, value=self.COL_PROGRESS, text=self.COL_STATUS)
        col.set_resizable(True)
        self.view.append_column(col)

        # Status
        renderer_text = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Status", renderer_text, text=self.COL_STATUS)
        col.set_resizable(True)
        self.view.append_column(col)

        # Speed
        renderer_text = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Speed", renderer_text, text=self.COL_SPEED)
        col.set_resizable(True)
        self.view.append_column(col)

        # ETA
        renderer_text = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("ETA", renderer_text, text=self.COL_ETA)
        col.set_resizable(True)
        self.view.append_column(col)

        # URL
        renderer_text = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("URL", renderer_text, text=self.COL_URL)
        col.set_resizable(True)
        self.view.append_column(col)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.view)

        self.add(scroll)
        self.show_all()

    # --------- Events ---------
    def on_destroy(self, *_):
        # Try to stop active downloads gracefully
        for row in list(self.store):
            item: DownloadItem = row[self.COL_OBJ]
            if item and item.is_active():
                item.pause()
        Gtk.main_quit()

    def on_add_clicked(self, *_):
        dialog = AddDownloadDialog(self)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            url, dest = dialog.get_values()
            if url and dest:
                self.add_download(url, dest)
        dialog.destroy()

    def on_start_all(self, *_):
        for row in self.store:
            item: DownloadItem = row[self.COL_OBJ]
            if item and not item.is_active() and item.status in ("Queued", "Paused", "Error", "HTTP error: 416"):
                item.start()

    def on_pause_all(self, *_):
        for row in self.store:
            item: DownloadItem = row[self.COL_OBJ]
            if item and item.is_active():
                item.pause()

    def on_remove_selected(self, *_):
        sel = self.view.get_selection()
        model, treeiter = sel.get_selected()
        if treeiter:
            item: DownloadItem = model[treeiter][self.COL_OBJ]
            if item and item.is_active():
                item.pause()
            # Do not delete files; only remove from list. Partial file remains for possible resume.
            self.store.remove(treeiter)

    def on_row_activated(self, view, path, column):  # toggle start/pause on double-click
        treeiter = self.store.get_iter(path)
        item: DownloadItem = self.store[treeiter][self.COL_OBJ]
        if not item:
            return
        if item.is_active():
            item.pause()
        else:
            item.start()

    # --------- Data ops ---------
    def add_download(self, url: str, dest_path: str):
        item = DownloadItem(url=url, dest_path=dest_path, app_ref=self)
        progress = 0
        speed = "0 B/s"
        eta = "--"
        self.store.append([item.filename, progress, item.status, speed, eta, url, item])

    def refresh_row(self, item: DownloadItem):
        # Find row by object
        for row in self.store:
            if row[self.COL_OBJ] is item:
                # Compute progress percentage
                if item.total_size and item.total_size > 0:
                    pct = int((item.downloaded / item.total_size) * 100)
                    pct = max(0, min(100, pct))
                else:
                    pct = 0

                speed = f"{human_size(item.speed_bps)}/s" if item.speed_bps else "0 B/s"
                eta = human_time(item.eta_seconds)

                row[self.COL_FILENAME] = item.filename
                row[self.COL_PROGRESS] = pct
                row[self.COL_STATUS] = item.status
                row[self.COL_SPEED] = speed
                row[self.COL_ETA] = eta
                row[self.COL_URL] = item.url
                break
        return False


class AddDownloadDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title="Add Download", transient_for=parent, flags=0)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        self.set_default_size(640, 100)

        box = self.get_content_area()

        grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=12)
        box.add(grid)

        # URL entry
        lbl_url = Gtk.Label(label="URL:")
        lbl_url.set_halign(Gtk.Align.END)
        self.entry_url = Gtk.Entry()
        self.entry_url.set_placeholder_text("https://example.com/file.zip")

        # Save location chooser (native save dialog)
        self.choose_btn = Gtk.Button(label="Choose save location…")
        self.choose_btn.connect("clicked", self.on_choose_dest)
        self.dest_label = Gtk.Label(label="No file chosen")
        self.dest_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.dest_path: Optional[str] = None

        grid.attach(lbl_url, 0, 0, 1, 1)
        grid.attach(self.entry_url, 1, 0, 2, 1)
        grid.attach(self.choose_btn, 1, 1, 1, 1)
        grid.attach(self.dest_label, 2, 1, 1, 1)

        self.show_all()

    def on_choose_dest(self, *_):
        # Try to guess filename from URL
        guessed = None
        url_text = self.entry_url.get_text().strip()
        if url_text:
            guessed = os.path.basename(url_text.split("?")[0].split("#")[0]) or "download.bin"
        dialog = Gtk.FileChooserDialog(
            title="Save As",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        if guessed:
            dialog.set_current_name(guessed)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.dest_path = dialog.get_filename()
            self.dest_label.set_text(self.dest_path)
        dialog.destroy()

    def get_values(self):
        return self.entry_url.get_text().strip(), self.dest_path


# ------------------------- Main -------------------------

def main():
    # Handle Ctrl+C
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = DownloadManagerApp()
    Gtk.main()


if __name__ == "__main__":
    main()
