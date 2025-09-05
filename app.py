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
  sudo apt-get install -y python3-gi gir1.2-gtk-4.0
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
import json
from dataclasses import dataclass, field
from typing import Optional, Deque
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

import requests
import urllib3

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GObject, GLib, Gdk

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


# ------------------------- Configuration -------------------------

class ConfigManager:
    def __init__(self, config_file="~/.config/download_manager.json"):
        self.config_file = os.path.expanduser(config_file)
        self.config = self.load_config()
    
    def load_config(self):
        """Load configuration from file or return defaults"""
        default_config = {
            "default_download_path": os.path.expanduser("~/Downloads"),
            "max_concurrent_downloads": 3
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    # Merge with defaults to handle new config options
                    default_config.update(config)
        except (json.JSONDecodeError, IOError):
            pass
        
        return default_config
    
    def save_config(self):
        """Save configuration to file"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except IOError:
            pass
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
        self.save_config()


# ------------------------- HTTP Server for Chrome Extension -------------------------

class DownloadManagerHTTPHandler(BaseHTTPRequestHandler):
    def __init__(self, app_instance, *args, **kwargs):
        self.app = app_instance
        super().__init__(*args, **kwargs)
    
    def do_GET(self):
        if self.path == '/ping':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        if self.path == '/add_download':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                url = data.get('url')
                filename = data.get('filename', 'download')
                
                if url:
                    # Get default download path
                    default_path = self.app.config_manager.get("default_download_path", "~/Downloads")
                    dest_path = os.path.join(os.path.expanduser(default_path), filename)
                    
                    # Add download to the app
                    GLib.idle_add(self.app.add_download, url, dest_path)
                    
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status": "success"}')
                else:
                    self.send_response(400)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"error": "URL required"}')
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(f'{{"error": "{str(e)}"}}'.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress HTTP server logs
        pass


def create_http_handler(app_instance):
    def handler(*args, **kwargs):
        return DownloadManagerHTTPHandler(app_instance, *args, **kwargs)
    return handler


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

class DownloadManagerApp(Gtk.ApplicationWindow):
    COL_FILENAME = 0
    COL_PROGRESS = 1
    COL_STATUS = 2
    COL_SPEED = 3
    COL_ETA = 4
    COL_URL = 5
    COL_OBJ = 6

    def __init__(self, app):
        super().__init__(application=app)
        self.set_default_size(1000, 600)
        self.set_title("Download Manager")
        
        # Initialize config manager
        self.config_manager = ConfigManager()
        
        # Start HTTP server for Chrome extension
        self.start_http_server()
        
        # Apply modern styling
        self.setup_modern_styling()
        
        # Create header bar
        self.setup_headerbar()
        
        # Create main content
        self.setup_main_content()

    def setup_headerbar(self):
        # Headerbar
        hb = Gtk.HeaderBar()
        hb.set_show_title_buttons(True)
        
        # Title with clean styling
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_margin_start(16)
        
        # Icon
        title_icon = Gtk.Image()
        title_icon.set_from_icon_name("folder-download-symbolic")
        title_icon.set_pixel_size(24)
        title_box.append(title_icon)
        
        # Title text
        title_label = Gtk.Label()
        title_label.set_markup("<span size='large' weight='bold' color='#1f2937'>Download Manager</span>")
        title_label.set_halign(Gtk.Align.START)
        title_box.append(title_label)
        
        hb.set_title_widget(title_box)
        self.set_titlebar(hb)

        # Left side buttons with modern styling
        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        left_box.set_margin_start(16)
        left_box.set_margin_end(16)
        left_box.set_margin_top(8)
        left_box.set_margin_bottom(8)

        self.add_button = Gtk.Button()
        self.add_button.set_icon_name("list-add-symbolic")
        self.add_button.set_tooltip_text("Add Download")
        self.add_button.set_css_classes(["suggested-action"])
        self.add_button.connect("clicked", self.on_add_clicked)
        left_box.append(self.add_button)

        self.start_all_btn = Gtk.Button()
        self.start_all_btn.set_icon_name("media-playback-start-symbolic")
        self.start_all_btn.set_tooltip_text("Start All Downloads")
        self.start_all_btn.connect("clicked", self.on_start_all)
        left_box.append(self.start_all_btn)

        self.pause_all_btn = Gtk.Button()
        self.pause_all_btn.set_icon_name("media-playback-pause-symbolic")
        self.pause_all_btn.set_tooltip_text("Pause All Downloads")
        self.pause_all_btn.connect("clicked", self.on_pause_all)
        left_box.append(self.pause_all_btn)

        hb.pack_start(left_box)

        # Right side buttons
        right_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right_box.set_margin_start(16)
        right_box.set_margin_end(16)
        right_box.set_margin_top(8)
        right_box.set_margin_bottom(8)

        self.settings_btn = Gtk.Button()
        self.settings_btn.set_icon_name("preferences-system-symbolic")
        self.settings_btn.set_tooltip_text("Settings")
        self.settings_btn.connect("clicked", self.on_settings_clicked)
        right_box.append(self.settings_btn)

        self.remove_btn = Gtk.Button()
        self.remove_btn.set_icon_name("edit-delete-symbolic")
        self.remove_btn.set_tooltip_text("Remove Selected")
        self.remove_btn.set_css_classes(["destructive-action"])
        self.remove_btn.connect("clicked", self.on_remove_selected)
        right_box.append(self.remove_btn)

        hb.pack_end(right_box)

    def setup_main_content(self):
        # Main container with modern styling
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.set_margin_start(20)
        main_box.set_margin_end(20)
        main_box.set_margin_top(20)
        main_box.set_margin_bottom(20)

        # Stats header
        stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        stats_box.set_margin_bottom(16)
        
        # Download stats with icon
        stats_icon = Gtk.Image()
        stats_icon.set_from_icon_name("document-save-symbolic")
        stats_icon.set_pixel_size(20)
        stats_box.append(stats_icon)
        
        self.stats_label = Gtk.Label()
        self.stats_label.set_markup("<span size='large' weight='bold' color='#1f2937'>Downloads</span>")
        self.stats_label.set_halign(Gtk.Align.START)
        stats_box.append(self.stats_label)
        
        # Empty space
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        stats_box.append(spacer)
        
        # Connection status with icon
        connection_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        connection_icon = Gtk.Image()
        connection_icon.set_from_icon_name("network-workgroup-symbolic")
        connection_icon.set_pixel_size(16)
        connection_box.append(connection_icon)
        
        self.connection_label = Gtk.Label()
        self.connection_label.set_markup("<span color='#059669' weight='bold'>Connected</span>")
        self.connection_label.set_halign(Gtk.Align.END)
        connection_box.append(self.connection_label)
        
        stats_box.append(connection_box)
        
        main_box.append(stats_box)

        # ListStore model
        self.store = Gtk.ListStore(str, int, str, str, str, str, object)

        # TreeView with modern styling
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_clickable(True)
        self.view.connect("row-activated", self.on_row_activated)
        self.view.set_css_classes(["download-list"])

        # Columns with modern styling
        # Filename
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_padding(12, 8)
        col = Gtk.TreeViewColumn("File", renderer_text, text=self.COL_FILENAME)
        col.set_sort_column_id(self.COL_FILENAME)
        col.set_resizable(True)
        col.set_min_width(200)
        self.view.append_column(col)

        # Progress
        renderer_prog = Gtk.CellRendererProgress()
        renderer_prog.set_padding(12, 8)
        col = Gtk.TreeViewColumn("Progress", renderer_prog, value=self.COL_PROGRESS, text=self.COL_STATUS)
        col.set_resizable(True)
        col.set_min_width(150)
        self.view.append_column(col)

        # Status
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_padding(12, 8)
        col = Gtk.TreeViewColumn("Status", renderer_text, text=self.COL_STATUS)
        col.set_resizable(True)
        col.set_min_width(120)
        self.view.append_column(col)

        # Speed
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_padding(12, 8)
        col = Gtk.TreeViewColumn("Speed", renderer_text, text=self.COL_SPEED)
        col.set_resizable(True)
        col.set_min_width(100)
        self.view.append_column(col)

        # ETA
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_padding(12, 8)
        col = Gtk.TreeViewColumn("ETA", renderer_text, text=self.COL_ETA)
        col.set_resizable(True)
        col.set_min_width(80)
        self.view.append_column(col)

        # URL
        renderer_text = Gtk.CellRendererText()
        renderer_text.set_padding(12, 8)
        renderer_text.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        col = Gtk.TreeViewColumn("URL", renderer_text, text=self.COL_URL)
        col.set_resizable(True)
        col.set_min_width(200)
        self.view.append_column(col)

        # Scrollable container
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.view)
        scroll.set_css_classes(["download-scroll"])

        main_box.append(scroll)
        self.set_child(main_box)

    def update_stats_display(self):
        """Update the stats display with current download information"""
        total_downloads = len(self.store)
        active_downloads = 0
        completed_downloads = 0
        
        for row in self.store:
            item: DownloadItem = row[self.COL_OBJ]
            if item:
                if item.is_active():
                    active_downloads += 1
                elif item.status == "Done":
                    completed_downloads += 1
        
        stats_text = f"<span size='large' weight='bold' color='#1f2937'>Downloads</span>\n"
        stats_text += f"<span size='small' color='#6b7280'>{total_downloads} total • {active_downloads} active • {completed_downloads} completed</span>"
        
        self.stats_label.set_markup(stats_text)

    def setup_modern_styling(self):
        """Apply modern CSS styling to the application"""
        css_provider = Gtk.CssProvider()
        css = """
        /* Modern color palette */
        * {
            font-family: 'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        }
        
        /* Header bar styling */
        headerbar {
            background: #ffffff;
            color: #1f2937;
            border: none;
            border-bottom: 1px solid #e5e7eb;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        
        headerbar button {
            background: #f9fafb;
            color: #374151;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            padding: 8px 12px;
            margin: 0 4px;
            transition: all 0.2s ease;
        }
        
        headerbar button:hover {
            background: #f3f4f6;
            border-color: #9ca3af;
            transform: translateY(-1px);
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        headerbar button:active {
            transform: translateY(0);
            background: #e5e7eb;
        }
        
        headerbar button.suggested-action {
            background: #3b82f6;
            color: white;
            border-color: #2563eb;
        }
        
        headerbar button.suggested-action:hover {
            background: #2563eb;
            border-color: #1d4ed8;
        }
        
        headerbar button.destructive-action {
            background: #ef4444;
            color: white;
            border-color: #dc2626;
        }
        
        headerbar button.destructive-action:hover {
            background: #dc2626;
            border-color: #b91c1c;
        }
        
        /* Main window styling */
        window {
            background-color: #f8fafc;
        }
        
        /* TreeView styling */
        treeview {
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid #e5e7eb;
        }
        
        treeview:selected {
            background: #dbeafe;
            color: #1e40af;
        }
        
        treeview header {
            background: #f9fafb;
            border-bottom: 1px solid #e5e7eb;
            font-weight: 600;
            color: #374151;
            padding: 12px 16px;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        /* Download list specific styling */
        .download-list {
            background: white;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border: 1px solid #e5e7eb;
        }
        
        .download-scroll {
            background: transparent;
            border: none;
        }
        
        /* Progress bar styling */
        progressbar {
            background: #e5e7eb;
            border-radius: 4px;
            min-height: 6px;
        }
        
        progressbar progress {
            background: #3b82f6;
            border-radius: 4px;
        }
        
        /* Button styling */
        button {
            background: #f9fafb;
            color: #374151;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            padding: 10px 16px;
            font-weight: 500;
            transition: all 0.2s ease;
        }
        
        button:hover {
            background: #f3f4f6;
            border-color: #9ca3af;
            transform: translateY(-1px);
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        button:active {
            transform: translateY(0);
            background: #e5e7eb;
        }
        
        button.suggested-action {
            background: #3b82f6;
            color: white;
            border-color: #2563eb;
        }
        
        button.suggested-action:hover {
            background: #2563eb;
            border-color: #1d4ed8;
        }
        
        /* Entry styling */
        entry {
            background: white;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            padding: 12px 16px;
            font-size: 14px;
            transition: all 0.2s ease;
        }
        
        entry:focus {
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }
        
        /* Label styling */
        label {
            color: #374151;
        }
        
        /* Dialog styling */
        dialog {
            background: white;
            border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }
        
        .modern-dialog {
            background: white;
            border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
        }
        
        .modern-dialog headerbar {
            background: #ffffff;
            color: #1f2937;
            border-bottom: 1px solid #e5e7eb;
            border-radius: 8px 8px 0 0;
        }
        
        /* Status colors */
        .status-queued { color: #6b7280; }
        .status-downloading { color: #059669; }
        .status-paused { color: #d97706; }
        .status-done { color: #059669; }
        .status-error { color: #dc2626; }
        """
        
        css_provider.load_from_data(css.encode())
        # Apply CSS to the application
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def start_http_server(self):
        """Start HTTP server for Chrome extension communication"""
        try:
            self.http_server = HTTPServer(('localhost', 8080), create_http_handler(self))
            self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            self.http_thread.start()
            print("HTTP server started on localhost:8080 for Chrome extension")
        except Exception as e:
            print(f"Failed to start HTTP server: {e}")

    # --------- Events ---------
    def on_add_clicked(self, *_):
        dialog = AddDownloadDialog(self, self.config_manager)
        dialog.connect("response", self.on_add_dialog_response)
        dialog.show()
    
    def on_settings_clicked(self, *_):
        dialog = SettingsDialog(self, self.config_manager)
        dialog.connect("response", self.on_settings_dialog_response)
        dialog.show()
    
    def on_add_dialog_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            url, dest = dialog.get_values()
            if url and dest:
                self.add_download(url, dest)
        dialog.destroy()
    
    def on_settings_dialog_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            new_path = dialog.get_values()
            if new_path:
                # Expand user path and validate
                expanded_path = os.path.expanduser(new_path)
                if os.path.exists(expanded_path) and os.path.isdir(expanded_path):
                    self.config_manager.set("default_download_path", expanded_path)
                else:
                    # Show error dialog
                    error_dialog = Gtk.MessageDialog(
                        transient_for=self,
                        modal=True,
                        message_type=Gtk.MessageType.ERROR,
                        buttons=Gtk.ButtonsType.OK,
                        text="Invalid Directory",
                        secondary_text=f"The directory '{expanded_path}' does not exist or is not accessible."
                    )
                    error_dialog.run()
                    error_dialog.destroy()
        dialog.destroy()

    def on_start_all(self, *_):
        for row in self.store:
            item: DownloadItem = row[self.COL_OBJ]
            if item and not item.is_active() and (
                item.status in ("Queued", "Paused") or 
                item.status.startswith(("Connection error:", "Timeout error:", "Request error:", "HTTP error:", "Error:"))
            ):
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
        
        # Update stats display
        self.update_stats_display()
        
        # Automatically start the download
        item.start()

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
        
        # Update stats display
        self.update_stats_display()
        return False


class AddDownloadDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, config_manager: ConfigManager):
        super().__init__(title="Add Download", transient_for=parent, modal=True)
        self.config_manager = config_manager
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_OK", Gtk.ResponseType.OK)
        self.set_default_size(600, 200)
        self.set_css_classes(["modern-dialog"])

        box = self.get_content_area()

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        box.append(grid)

        # URL entry
        lbl_url = Gtk.Label(label="URL:")
        lbl_url.set_halign(Gtk.Align.END)
        self.entry_url = Gtk.Entry()
        self.entry_url.set_placeholder_text("https://example.com/file.zip")

        # Save location chooser (native save dialog)
        self.choose_btn = Gtk.Button(label="Choose save location…")
        self.choose_btn.connect("clicked", self.on_choose_dest)
        self.use_default_btn = Gtk.Button(label="Use Default")
        self.use_default_btn.connect("clicked", self.on_use_default)
        self.dest_label = Gtk.Label(label="No file chosen")
        self.dest_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.dest_path: Optional[str] = None
        
        # Initialize with default path
        default_path = self.config_manager.get("default_download_path", "~/Downloads")
        self.dest_path = os.path.expanduser(default_path)
        self.dest_label.set_text(self.dest_path)

        grid.attach(lbl_url, 0, 0, 1, 1)
        grid.attach(self.entry_url, 1, 0, 2, 1)
        grid.attach(self.choose_btn, 1, 1, 1, 1)
        grid.attach(self.use_default_btn, 2, 1, 1, 1)
        grid.attach(self.dest_label, 1, 2, 2, 1)

    def on_use_default(self, *_):
        # Reset to default path
        default_path = self.config_manager.get("default_download_path", "~/Downloads")
        self.dest_path = os.path.expanduser(default_path)
        self.dest_label.set_text(self.dest_path)

    def on_choose_dest(self, *_):
        # Try to guess filename from URL
        guessed = None
        url_text = self.entry_url.get_text().strip()
        if url_text:
            guessed = os.path.basename(url_text.split("?")[0].split("#")[0]) or "download.bin"
        
        dialog = Gtk.FileChooserDialog(
            title="Save As",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Save", Gtk.ResponseType.OK)
        
        # Start from default directory
        default_path = self.config_manager.get("default_download_path", "~/Downloads")
        expanded_default = os.path.expanduser(default_path)
        if os.path.exists(expanded_default):
            dialog.set_current_folder(expanded_default)
        
        if guessed:
            dialog.set_current_name(guessed)
        
        dialog.connect("response", self.on_file_chooser_response)
        dialog.show()
    
    def on_file_chooser_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            file = dialog.get_file()
            if file:
                self.dest_path = file.get_path()
                self.dest_label.set_text(self.dest_path)
        dialog.destroy()

    def get_values(self):
        url = self.entry_url.get_text().strip()
        if not url:
            return "", ""
        
        # If dest_path is just a directory, append filename from URL
        if self.dest_path and os.path.isdir(self.dest_path):
            filename = os.path.basename(url.split("?")[0].split("#")[0]) or "download.bin"
            full_path = os.path.join(self.dest_path, filename)
        else:
            full_path = self.dest_path or ""
        
        return url, full_path


class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, config_manager: ConfigManager):
        super().__init__(title="Settings", transient_for=parent, modal=True)
        self.config_manager = config_manager
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_OK", Gtk.ResponseType.OK)
        self.set_default_size(500, 200)
        self.set_css_classes(["modern-dialog"])

        box = self.get_content_area()

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        box.append(grid)

        # Default download path
        lbl_path = Gtk.Label(label="Default Download Path:")
        lbl_path.set_halign(Gtk.Align.START)
        lbl_path.set_valign(Gtk.Align.CENTER)
        
        self.entry_path = Gtk.Entry()
        self.entry_path.set_text(self.config_manager.get("default_download_path", "~/Downloads"))
        self.entry_path.set_placeholder_text("~/Downloads")
        
        self.choose_path_btn = Gtk.Button(label="Browse...")
        self.choose_path_btn.connect("clicked", self.on_choose_path)
        
        grid.attach(lbl_path, 0, 0, 1, 1)
        grid.attach(self.entry_path, 1, 0, 1, 1)
        grid.attach(self.choose_path_btn, 2, 0, 1, 1)

    def on_choose_path(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Select Default Download Directory",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Select", Gtk.ResponseType.OK)
        
        # Set current path if it exists
        current_path = self.entry_path.get_text().strip()
        if current_path and os.path.exists(os.path.expanduser(current_path)):
            dialog.set_current_folder(os.path.expanduser(current_path))
        
        dialog.connect("response", self.on_folder_chooser_response)
        dialog.show()
    
    def on_folder_chooser_response(self, dialog, response):
        if response == Gtk.ResponseType.OK:
            folder = dialog.get_file()
            if folder:
                self.entry_path.set_text(folder.get_path())
        dialog.destroy()

    def get_values(self):
        return self.entry_path.get_text().strip()


# ------------------------- Main -------------------------

class DownloadManagerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.example.downloadmanager")
        self.connect("activate", self.on_activate)
        self.connect("shutdown", self.on_shutdown)

    def on_activate(self, app):
        self.win = DownloadManagerApp(self)
        self.win.present()
    
    def on_shutdown(self, app):
        # Try to stop active downloads gracefully
        if hasattr(self, 'win') and self.win.store:
            for row in list(self.win.store):
                item: DownloadItem = row[self.win.COL_OBJ]
                if item and item.is_active():
                    item.pause()

def main():
    # Handle Ctrl+C
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = DownloadManagerApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    main()
