import ctypes
import ctypes.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import time
from bisect import bisect_right
from urllib.error import HTTPError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request

import dbus
import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageTk
from io import BytesIO
from urllib.request import urlopen


PLAYER_PREFIX = "org.mpris.MediaPlayer2."
ART_SIZE = 296
CHROMA_GREEN = "#00FF00"
ART_FADE_START_US = 30_000_000
ART_FADE_DURATION_US = 10_000_000
ART_OPEN_HOLD_SECONDS = 30
LRCLIB_API_URL = "https://lrclib.net/api"
LYRICS_TIMEOUT_SECONDS = 12
LRC_TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
LRC_OFFSET_PATTERN = re.compile(r"^\[offset:([+-]?\d+)\]$", re.IGNORECASE)
TITLE_FONT_PATHS = (
    "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


class XClientMessageEvent(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    )


class XEvent(ctypes.Union):
    _fields_ = (("type", ctypes.c_int), ("xclient", XClientMessageEvent),
                ("padding", ctypes.c_long * 24))


def find_player(bus):
    names = [name for name in bus.list_names() if name.startswith(PLAYER_PREFIX)]
    return sorted(names)[0] if names else None


def format_time(microseconds):
    seconds = max(0, int(microseconds // 1_000_000))
    return f"{seconds // 60}:{seconds % 60:02}"


def estimate_word_timings(text, start_ms, end_ms):
    words = re.findall(r"\S+\s*", text)
    if not words:
        return []
    duration_ms = end_ms - start_ms
    if duration_ms <= 0:
        duration_ms = 3000
    duration_ms = min(duration_ms, 5000)
    total_weight = sum(len(word.strip()) for word in words)
    elapsed_weight = 0
    timings = []
    for word in words:
        word_start = start_ms + duration_ms * elapsed_weight // total_weight
        timings.append((word_start, word))
        elapsed_weight += len(word.strip())
    return timings


def parse_lrc(synced_lyrics, duration_ms=None):
    entries = []
    offset_ms = 0
    for line in synced_lyrics.splitlines():
        offset_match = LRC_OFFSET_PATTERN.match(line.strip())
        if offset_match:
            offset_ms = int(offset_match.group(1))
            continue
        timestamps = list(LRC_TIMESTAMP_PATTERN.finditer(line))
        if not timestamps:
            continue
        text = LRC_TIMESTAMP_PATTERN.sub("", line).strip()
        if not text:
            continue
        for timestamp in timestamps:
            minutes = int(timestamp.group(1))
            seconds = int(timestamp.group(2))
            fraction = (timestamp.group(3) or "").ljust(3, "0")
            milliseconds = int(fraction or "0")
            start_ms = (minutes * 60 + seconds) * 1000 + milliseconds + offset_ms
            entries.append((max(0, start_ms), text, []))
    entries.sort(key=lambda entry: entry[0])
    return add_estimated_word_timings(entries, duration_ms)


def add_estimated_word_timings(entries, duration_ms=None):
    timed_entries = []
    for index, (start_ms, text, words) in enumerate(entries):
        if words:
            timed_entries.append((start_ms, text, words))
            continue
        next_start = (
            entries[index + 1][0]
            if index + 1 < len(entries)
            else duration_ms
        )
        if next_start is None or next_start <= start_ms:
            next_start = start_ms + 3000
        words = estimate_word_timings(text, start_ms, next_start)
        timed_entries.append((start_ms, text, words))
    return timed_entries


def parse_lyricsfile(lyricsfile, duration_ms=None):
    document = yaml.safe_load(lyricsfile) or {}
    metadata = document.get("metadata") or {}
    offset_ms = int(metadata.get("offset_ms") or 0)
    entries = []
    for line in document.get("lines") or []:
        try:
            start_ms = int(line["start_ms"]) + offset_ms
        except (KeyError, TypeError, ValueError):
            continue
        try:
            end_ms = int(line["end_ms"]) + offset_ms
        except (KeyError, TypeError, ValueError):
            end_ms = None
        words = []
        for word in line.get("words") or []:
            try:
                word_start_ms = int(word["start_ms"]) + offset_ms
                word_text = str(word.get("text") or "")
            except (KeyError, TypeError, ValueError):
                continue
            if word_text:
                words.append((max(0, word_start_ms), word_text))
        line_text = str(line.get("text") or "")
        if not line_text:
            line_text = "".join(text for _, text in words).strip()
        if line_text.strip():
            entries.append((max(0, start_ms), line_text, words, end_ms))
    entries.sort(key=lambda entry: entry[0])
    timed_entries = []
    for index, (start_ms, text, words, end_ms) in enumerate(entries):
        if not words:
            if end_ms is None and index + 1 < len(entries):
                end_ms = entries[index + 1][0]
            if end_ms is None:
                end_ms = duration_ms
            if end_ms is None or end_ms <= start_ms:
                end_ms = start_ms + 3000
            words = estimate_word_timings(text, start_ms, end_ms)
        timed_entries.append((start_ms, text, words))
    return timed_entries


def load_title_font(size=18):
    for path in TITLE_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def load_album_font(size=14):
    for path in (
        "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def load_regular_font(size=10):
    for path in (
        "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap_title(title, font, max_width):
    lines = []
    line = ""
    for word in title.split():
        candidate = f"{line} {word}" if line else word
        if line and font.getlength(candidate) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\n".join(lines)


def load_art(uri, title, album):
    if not uri:
        return None

    try:
        parsed = urlparse(str(uri))
        if parsed.scheme == "file":
            path = unquote(parsed.path)
            image = Image.open(path)
        elif parsed.scheme in ("http", "https"):
            with urlopen(str(uri), timeout=3) as response:
                image = Image.open(BytesIO(response.read()))
        else:
            return None

        image = ImageOps.fit(image.convert("RGB"), (ART_SIZE, ART_SIZE))
        fade = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(fade)
        fade_start = int(ART_SIZE * 0.48)
        for y in range(fade_start, ART_SIZE):
            progress = (y - fade_start) / (ART_SIZE - fade_start)
            alpha = int(240 * progress ** 0.55)
            draw.line((0, y, ART_SIZE, y), fill=(0, 0, 0, alpha))

        image = Image.alpha_composite(image.convert("RGBA"), fade)
        font = load_title_font()
        wrapped_title = wrap_title(title, font, ART_SIZE - 28)
        mask = Image.new("L", image.size, 0)
        title_draw = ImageDraw.Draw(mask)
        bounds = title_draw.multiline_textbbox(
            (0, 0), wrapped_title, font=font, spacing=0
        )
        title_height = bounds[3] - bounds[1]
        title_draw.multiline_text(
            (14 - bounds[0], 174 - title_height - bounds[1]),
            wrapped_title, font=font, fill=255, spacing=0,
        )
        shadow_mask = Image.new("L", image.size, 0)
        shadow_mask.paste(mask, (2, 2))
        shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(1.5))
        shadow_mask = shadow_mask.point(lambda value: int(value * 0.75))
        album_mask = Image.new("L", image.size, 0)
        album_font = load_album_font()
        album_line = album
        while album_line and album_font.getlength(album_line) > ART_SIZE - 28:
            album_line = album_line[:-1]
        if album_line != album:
            album_line = album_line.rstrip() + "..."
        ImageDraw.Draw(album_mask).text(
            (14, 199), album_line, font=album_font, fill=255, anchor="ls"
        )
        rgb_image = image.convert("RGB")
        return rgb_image, mask, shadow_mask, album_mask
    except Exception:
        return None


def render_art(
    base_image, title_mask, title_shadow_mask, album_mask, fade_progress,
    controls_alpha, playback_status, size, lyrics_text="", active_word="",
):
    width, height = size
    if base_image.size != size:
        base_image = base_image.resize(size, Image.Resampling.LANCZOS)
        title_mask = title_mask.resize(size, Image.Resampling.LANCZOS)
        title_shadow_mask = title_shadow_mask.resize(size, Image.Resampling.LANCZOS)
        album_mask = album_mask.resize(size, Image.Resampling.LANCZOS)

    fade = min(max(fade_progress, 0), 1)
    shadow = Image.new("RGB", base_image.size, "#000000")
    art = Image.composite(shadow, base_image, title_shadow_mask)
    green = Image.new("RGB", art.size, CHROMA_GREEN)
    image = Image.blend(art, green, fade)

    inverted_text = ImageOps.invert(art)
    image = Image.composite(inverted_text, image, title_mask)
    image = Image.composite(inverted_text, image, album_mask)

    if controls_alpha > 0:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        alpha = int(controls_alpha * 255)
        scale_x = width / ART_SIZE
        scale_y = height / ART_SIZE
        scale = min(scale_x, scale_y)
        centers = (84, 148, 212)
        for center_x in centers:
            center_x = center_x * scale_x
            center_y = 145 * scale_y
            radius = 23 * scale
            draw.ellipse(
                (center_x - radius, center_y - radius,
                 center_x + radius, center_y + radius),
                fill=(0, 0, 0, int(alpha * 0.62)),
                outline=(255, 255, 255, alpha),
                width=max(1, round(scale)),
            )

        white = (255, 255, 255, alpha)
        def point(x, y):
            return (x * scale_x, y * scale_y)

        draw.rectangle((*point(70, 137), *point(73, 153)), fill=white)
        draw.polygon(
            (point(87, 136), point(87, 154), point(76, 145)), fill=white
        )
        if playback_status == "Playing":
            draw.rounded_rectangle(
                (*point(140, 136), *point(145, 154)),
                radius=max(1, round(2 * scale)), fill=white,
            )
            draw.rounded_rectangle(
                (*point(151, 136), *point(156, 154)),
                radius=max(1, round(2 * scale)), fill=white,
            )
        else:
            draw.polygon(
                (point(142, 136), point(142, 154), point(157, 145)), fill=white
            )
        draw.polygon(
            (point(207, 136), point(207, 154), point(218, 145)), fill=white
        )
        draw.rectangle((*point(222, 137), *point(225, 153)), fill=white)
        close_x = width - 20 * scale
        close_y = 20 * scale
        close_radius = 11 * scale
        draw.ellipse(
            (close_x - close_radius, close_y - close_radius,
             close_x + close_radius, close_y + close_radius),
            fill=(0, 0, 0, int(alpha * 0.62)),
            outline=white,
            width=max(1, round(scale)),
        )
        cross_radius = 4 * scale
        draw.line(
            (close_x - cross_radius, close_y - cross_radius,
             close_x + cross_radius, close_y + cross_radius),
            fill=white, width=max(1, round(2 * scale)),
        )
        draw.line(
            (close_x + cross_radius, close_y - cross_radius,
             close_x - cross_radius, close_y + cross_radius),
            fill=white, width=max(1, round(2 * scale)),
        )
        settings_box = (10 * scale_x, 10 * scale_y, 76 * scale_x, 34 * scale_y)
        draw.rounded_rectangle(
            settings_box, radius=max(2, round(4 * scale)),
            fill=(0, 0, 0, int(alpha * 0.62)),
            outline=white, width=max(1, round(scale)),
        )
        draw.text(
            (17 * scale_x, 16 * scale_y), "Settings",
            font=load_regular_font(max(8, round(10 * scale))), fill=white,
        )
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

    if lyrics_text:
        scale = min(width / ART_SIZE, height / ART_SIZE)
        font = load_title_font(max(10, round(15 * scale)))
        max_width = width * 0.82
        wrapped_lyrics = wrap_title(lyrics_text, font, max_width)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        bounds = draw.multiline_textbbox(
            (0, 0), wrapped_lyrics, font=font, spacing=max(1, round(3 * scale)),
            align="center",
        )
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        padding_x = 12 * scale
        padding_y = 8 * scale
        center_x = width / 2
        center_y = height * 0.42
        draw.rounded_rectangle(
            (center_x - text_width / 2 - padding_x,
             center_y - text_height / 2 - padding_y,
             center_x + text_width / 2 + padding_x,
             center_y + text_height / 2 + padding_y),
            radius=max(2, round(5 * scale)), fill=(0, 0, 0, 190),
        )
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)
        draw.multiline_text(
            (center_x - text_width / 2 - bounds[0],
             center_y - text_height / 2 - bounds[1]),
            wrapped_lyrics, font=font, fill="white", align="center",
            spacing=max(1, round(3 * scale)),
        )

    if active_word:
        scale = min(width / ART_SIZE, height / ART_SIZE)
        font = load_title_font(max(12, round(19 * scale)))
        draw = ImageDraw.Draw(image)
        draw.text(
            (width / 2, height * 0.30), active_word, font=font,
            fill="#FFE36E", anchor="mm", stroke_width=max(1, round(scale)),
            stroke_fill="#111111",
        )

    return ImageTk.PhotoImage(image)


class NowPlaying:
    def __init__(self, root):
        self.root = root
        self.bus = dbus.SessionBus()
        self.opened_at = time.monotonic()
        self.player_name = None
        self.last_track = None
        self.last_title = None
        self.last_album = None
        self.art_image = None
        self.icon_image = None
        self.art_source = None
        self.title_mask = None
        self.title_shadow_mask = None
        self.album_mask = None
        self.album_mask = None
        self.fade_job = None
        self.fade_elapsed_us = 0
        self.fade_frame_time = None
        self.position_us = 0
        self.playback_status = ""
        self.controls_alpha = 0.0
        self.controls_visible_until = 0.0
        self.art_fade_enabled = tk.BooleanVar(value=True)
        self.show_lyrics = tk.BooleanVar(value=False)
        self.always_on_top = tk.BooleanVar(value=False)
        config_dir = os.environ.get(
            "XDG_CONFIG_HOME", os.path.expanduser("~/.config")
        )
        self.settings_path = os.path.join(
            config_dir, "mplaying", "settings.json"
        )
        self.window_geometry = None
        self.geometry_save_job = None
        self.load_settings()
        self.settings_window = None
        self.lyrics = []
        self.lyrics_track_id = None
        self.lyrics_generation = 0
        self.lyrics_results = queue.Queue()
        self.lyrics_status_text = "Waiting for a track"
        self.active_lyric_index = None
        self.active_word_index = None
        self.position_sampled_at = time.monotonic()

        root.title("")
        root.configure(background=CHROMA_GREEN)
        root.withdraw()
        root.overrideredirect(True)
        if root.tk.call("tk", "windowingsystem") == "x11":
            try:
                root.wm_attributes("-type", "splash")
            except tk.TclError:
                pass
        try:
            root.geometry(self.window_geometry or "320x320")
        except tk.TclError:
            root.geometry("320x320")
        root.minsize(240, 240)
        root.resizable(True, True)
        try:
            root.wm_aspect(1, 1, 1, 1)
        except tk.TclError:
            pass
        root.bind("<Configure>", self.schedule_geometry_save)
        self.update_topmost_setting()

        self.art = tk.Canvas(root, width=ART_SIZE, height=ART_SIZE,
                             background=CHROMA_GREEN, highlightthickness=0)
        self.art.pack(fill="both", expand=True)
        self.art_image_item = self.art.create_image(0, 0, anchor="nw")
        self.art_fallback = self.art.create_text(
            ART_SIZE // 2, ART_SIZE // 2, text="No album art",
            fill="white", font=("sans-serif", 12),
        )
        self.title_shadow = self.art.create_text(
            16, 200, anchor="sw", width=ART_SIZE - 28, text="Nothing playing",
            fill="#111111", font=("sans-serif", 14, "bold"),
        )
        self.title = self.art.create_text(
            14, 174, anchor="sw", width=ART_SIZE - 28, text="Nothing playing",
            fill="white", font=("sans-serif", 14, "bold"),
        )
        self.album = self.art.create_text(
            14, 199, anchor="sw", width=ART_SIZE - 28, text="",
            fill="white", font=("sans-serif", 10, "italic"),
        )
        self.artist_shadow = self.art.create_text(
            16, 238, anchor="sw", width=ART_SIZE - 28,
            fill="#EEEEEE", font=("sans-serif", 10, "italic"),
        )
        self.artist = self.art.create_text(
            14, 236, anchor="sw", width=ART_SIZE - 28,
            fill="black", font=("sans-serif", 10, "italic"),
        )
        self.progress_shadow = self.art.create_line(
            14, 257, ART_SIZE - 14, 257,
            fill="#111111", width=7, capstyle=tk.ROUND,
        )
        self.progress_track = self.art.create_line(
            14, 254, ART_SIZE - 14, 254,
            fill="#999999", width=5, capstyle=tk.ROUND,
        )
        self.progress_fill = self.art.create_line(
            14, 254, 14, 254, fill="white", width=5, capstyle=tk.ROUND,
            state="hidden",
        )
        self.time_shadow = self.art.create_text(
            15, 279, text="0:00", anchor="sw", fill="#EEEEEE",
            font=("sans-serif", 9),
        )
        self.time = self.art.create_text(
            14, 278, text="0:00", anchor="sw", fill="black",
            font=("sans-serif", 9),
        )
        self.duration_shadow = self.art.create_text(
            ART_SIZE - 13, 279, text="0:00", anchor="se", fill="#EEEEEE",
            font=("sans-serif", 9),
        )
        self.duration = self.art.create_text(
            ART_SIZE - 14, 278, text="0:00", anchor="se", fill="black",
            font=("sans-serif", 9),
        )
        self.status = self.art.create_text(
            ART_SIZE // 2, 292, text="", anchor="s", fill="black",
            font=("sans-serif", 8),
        )
        self.resize_grip = self.art.create_line(
            ART_SIZE - 18, ART_SIZE, ART_SIZE, ART_SIZE - 18,
            fill="white", width=2,
        )
        self.resize_start = None
        self.wm_action_active = False
        self.decorations_removed = False
        self.art.bind("<Configure>", self.resize_art)
        self.art.bind("<Enter>", self.reveal_controls)
        self.art.bind("<Motion>", self.reveal_controls)
        self.art.bind("<Button-1>", self.begin_canvas_interaction)
        self.art.bind("<Button-3>", self.copy_window_screenshot)
        self.art.bind("<B1-Motion>", self.drag_canvas)
        self.art.bind("<ButtonRelease-1>", self.stop_resize)
        self.root.bind("<Control-comma>", self.open_settings)
        self.root.bind("<Control-l>", self.toggle_lyrics)

        self.root.update_idletasks()
        self.remove_window_decorations()
        self.root.deiconify()
        self.root.after(100, self.process_lyrics_results)
        self.root.after(80, self.sync_lyrics_position)
        self.update()

    def start_drag(self, event):
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def drag_window(self, event):
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        self.root.geometry(f"+{x}+{y}")

    def remove_window_decorations(self, event=None):
        if (
            self.decorations_removed
            or self.root.tk.call("tk", "windowingsystem") != "x11"
        ):
            return

        library_path = ctypes.util.find_library("X11")
        if not library_path:
            return

        display = None
        try:
            x11 = ctypes.CDLL(library_path)
            x11.XOpenDisplay.argtypes = (ctypes.c_char_p,)
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XInternAtom.argtypes = (
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            )
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XChangeProperty.argtypes = (
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
            )
            x11.XChangeProperty.restype = ctypes.c_int
            x11.XFlush.argtypes = (ctypes.c_void_p,)
            x11.XCloseDisplay.argtypes = (ctypes.c_void_p,)

            display = x11.XOpenDisplay(None)
            if not display:
                return

            motif_hints = x11.XInternAtom(display, b"_MOTIF_WM_HINTS", 0)
            hints = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)
            x11.XChangeProperty(
                display, self.root.winfo_id(), motif_hints, motif_hints,
                32, 0, ctypes.cast(hints, ctypes.POINTER(ctypes.c_ubyte)), 5,
            )
            x11.XFlush(display)
            self.decorations_removed = True
        except Exception:
            pass
        finally:
            if display:
                x11.XCloseDisplay(display)

    def resize_art(self, event):
        width = max(1, event.width)
        height = max(1, event.height)
        scale_x = width / ART_SIZE
        scale_y = height / ART_SIZE
        scale = min(scale_x, scale_y)

        self.art.coords(self.art_fallback, width / 2, height / 2)
        self.art.coords(self.title_shadow, 16 * scale_x, 176 * scale_y)
        self.art.coords(self.title, 14 * scale_x, 174 * scale_y)
        self.art.coords(self.album, 14 * scale_x, 199 * scale_y)
        self.art.coords(self.artist_shadow, 16 * scale_x, 238 * scale_y)
        self.art.coords(self.artist, 14 * scale_x, 236 * scale_y)
        text_width = (ART_SIZE - 28) * scale_x
        for item in (
            self.title_shadow, self.title, self.album,
            self.artist_shadow, self.artist,
        ):
            self.art.itemconfigure(item, width=text_width)
        self.art.itemconfigure(
            self.title_shadow, font=("sans-serif", max(8, round(14 * scale)), "bold")
        )
        self.art.itemconfigure(
            self.title, font=("sans-serif", max(8, round(14 * scale)), "bold")
        )
        self.art.itemconfigure(
            self.album, font=("sans-serif", max(7, round(10 * scale)), "italic")
        )
        self.art.itemconfigure(
            self.artist_shadow,
            font=("sans-serif", max(7, round(10 * scale)), "italic"),
        )
        self.art.itemconfigure(
            self.artist, font=("sans-serif", max(7, round(10 * scale)), "italic")
        )

        x_start = 14 * scale_x
        x_end = width - x_start
        track_y = 254 * scale_y
        self.art.coords(self.progress_shadow, x_start, 257 * scale_y, x_end, 257 * scale_y)
        self.art.coords(self.progress_track, x_start, track_y, x_end, track_y)
        self.art.coords(self.progress_fill, x_start, track_y, x_start, track_y)
        self.art.itemconfigure(
            self.progress_shadow, width=max(2, round(7 * scale))
        )
        self.art.itemconfigure(self.progress_track, width=max(2, round(5 * scale)))
        self.art.itemconfigure(self.progress_fill, width=max(2, round(5 * scale)))
        self.art.coords(self.time_shadow, 15 * scale_x, 279 * scale_y)
        self.art.coords(self.time, 14 * scale_x, 278 * scale_y)
        self.art.coords(self.duration_shadow, width - 13 * scale_x, 279 * scale_y)
        self.art.coords(self.duration, width - 14 * scale_x, 278 * scale_y)
        self.art.coords(self.status, width / 2, 292 * scale_y)
        self.art.coords(
            self.resize_grip, width - 18 * scale, height,
            width, height - 18 * scale,
        )
        if self.art_source:
            self.render_current_art()

    def reveal_controls(self, event=None):
        if not self.player_name:
            return
        self.controls_visible_until = time.monotonic() + 5
        self.art.configure(cursor="hand2")
        self.start_art_animation()

    def handle_art_click(self, event):
        width = max(1, self.art.winfo_width())
        height = max(1, self.art.winfo_height())
        reference_x = event.x * ART_SIZE / width
        reference_y = event.y * ART_SIZE / height
        if event.x >= width - 20 and event.y >= height - 20:
            self.wm_action_active = False
            self.resize_start = (
                event.x_root, event.y_root,
                self.root.winfo_width(), self.root.winfo_height(),
            )
            return "break"

        if (
            self.controls_alpha >= 0.2
            and reference_x <= 82
            and reference_y <= 40
        ):
            self.open_settings()
            return "break"

        if (
            self.controls_alpha >= 0.2
            and reference_x >= ART_SIZE - 34
            and reference_y <= 36
        ):
            self.close_window()
            return "break"

        if self.controls_alpha < 0.2:
            return

        if abs(reference_y - 145) > 24:
            return
        if 60 <= reference_x < 116 and abs(reference_y - 145) <= 24:
            self.control_player("Previous")
        elif 120 <= reference_x < 176:
            self.control_player("PlayPause")
        elif 184 <= reference_x < 240:
            self.control_player("Next")
        else:
            return
        self.reveal_controls()
        return "break"

    def copy_window_screenshot(self, event=None):
        try:
            self.root.update_idletasks()
            screenshot = self.capture_canvas_image()
            image_data = BytesIO()
            screenshot.save(image_data, format="PNG")

            wl_copy = shutil.which("wl-copy") or next(
                (path for path in ("/usr/bin/wl-copy", "/bin/wl-copy")
                 if os.path.isfile(path) and os.access(path, os.X_OK)),
                None,
            )
            if wl_copy:
                subprocess.run(
                    [wl_copy, "--type", "image/png"],
                    input=image_data.getvalue(), check=True, timeout=5,
                )
            else:
                host_exec = shutil.which("distrobox-host-exec") or next(
                    (path for path in (
                        "/usr/bin/distrobox-host-exec",
                        "/bin/distrobox-host-exec",
                    ) if os.path.isfile(path) and os.access(path, os.X_OK)),
                    None,
                )
                if host_exec:
                    subprocess.run(
                        [host_exec, "wl-copy", "--type", "image/png"],
                        input=image_data.getvalue(), check=True, timeout=5,
                    )
                else:
                    xclip = shutil.which("xclip") or next(
                        (path for path in ("/usr/bin/xclip", "/bin/xclip")
                         if os.path.isfile(path) and os.access(path, os.X_OK)),
                        None,
                    )
                    if not xclip:
                        raise RuntimeError("No image clipboard utility found")
                    subprocess.run(
                        [xclip, "-selection", "clipboard", "-t", "image/png", "-i"],
                        input=image_data.getvalue(), check=True, timeout=5,
                    )
            self.art.itemconfigure(self.status, text="Screenshot copied")
        except Exception as error:
            self.art.itemconfigure(
                self.status, text=f"Copy failed: {type(error).__name__}"
            )
            print(f"Screenshot copy failed: {error!r}", file=sys.stderr)
        return "break"

    def capture_canvas_image(self):
        width = max(1, self.art.winfo_width())
        height = max(1, self.art.winfo_height())
        if self.art_source:
            screenshot_image = render_art(
                self.art_source[0], self.title_mask, self.title_shadow_mask,
                self.album_mask, 0, 0,
                self.playback_status, (width, height),
                self.current_lyric_text(),
                self.current_active_word(),
            )
            screenshot = ImageTk.getimage(screenshot_image).convert("RGB")
        else:
            screenshot = Image.new("RGB", (width, height), CHROMA_GREEN)

        draw = ImageDraw.Draw(screenshot)

        def draw_text(item):
            if self.art.itemcget(item, "state") == "hidden":
                return
            text = self.art.itemcget(item, "text")
            if not text:
                return

            font_parts = self.art.itemcget(item, "font").split()
            size = int(float(font_parts[1])) if len(font_parts) > 1 else 10
            styles = font_parts[2:]
            if "bold" in styles:
                font = load_title_font(size)
            elif "italic" in styles:
                font = load_album_font(size)
            else:
                font = load_regular_font(size)

            coords = self.art.coords(item)
            anchor = self.art.itemcget(item, "anchor")
            pillow_anchor = {
                "center": "mm", "n": "mt", "ne": "rt", "e": "rm",
                "se": "rd", "s": "md", "sw": "ld", "w": "lm",
                "nw": "lt",
            }.get(anchor, "lt")
            max_width = float(self.art.itemcget(item, "width") or width)
            text = wrap_title(text, font, max_width)
            draw.multiline_text(
                (coords[0], coords[1]), text,
                font=font, fill=self.art.itemcget(item, "fill"),
                anchor=pillow_anchor, spacing=0,
            )

        def draw_line(item):
            if self.art.itemcget(item, "state") == "hidden":
                return
            coords = self.art.coords(item)
            if len(coords) < 4:
                return
            color = self.art.itemcget(item, "fill")
            line_width = max(1, round(float(self.art.itemcget(item, "width"))))
            draw.line(coords, fill=color, width=line_width)
            radius = line_width / 2
            for x, y in (coords[:2], coords[-2:]):
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius), fill=color
                )

        for item in (
            self.art_fallback, self.title_shadow, self.title, self.album,
            self.artist_shadow, self.artist,
        ):
            draw_text(item)
        for item in (self.progress_shadow, self.progress_track, self.progress_fill):
            draw_line(item)
        for item in (
            self.time_shadow, self.time, self.duration_shadow,
            self.duration, self.status,
        ):
            draw_text(item)
        draw_line(self.resize_grip)
        return screenshot

    def request_window_manager_action(self, event, direction):
        if self.root.tk.call("tk", "windowingsystem") != "x11":
            return False

        library_path = ctypes.util.find_library("X11")
        if not library_path:
            return False

        display = None
        try:
            x11 = ctypes.CDLL(library_path)
            x11.XOpenDisplay.argtypes = (ctypes.c_char_p,)
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XDefaultRootWindow.argtypes = (ctypes.c_void_p,)
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XInternAtom.argtypes = (
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
            )
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XSendEvent.argtypes = (
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                ctypes.c_long, ctypes.POINTER(XEvent),
            )
            x11.XSendEvent.restype = ctypes.c_int
            x11.XFlush.argtypes = (ctypes.c_void_p,)
            x11.XCloseDisplay.argtypes = (ctypes.c_void_p,)

            display = x11.XOpenDisplay(None)
            if not display:
                return False

            message = XEvent()
            client_message = message.xclient
            client_message.type = 33
            client_message.send_event = 1
            client_message.display = display
            client_message.window = self.root.winfo_id()
            client_message.message_type = x11.XInternAtom(
                display, b"_NET_WM_MOVERESIZE", 0
            )
            client_message.format = 32
            client_message.data[0] = event.x_root
            client_message.data[1] = event.y_root
            client_message.data[2] = direction
            client_message.data[3] = 1
            client_message.data[4] = 1

            root_window = x11.XDefaultRootWindow(display)
            event_mask = (1 << 20) | (1 << 19)
            sent = x11.XSendEvent(
                display, root_window, 0, event_mask, ctypes.byref(message)
            )
            x11.XFlush(display)
            return bool(sent)
        except Exception:
            return False
        finally:
            if display:
                x11.XCloseDisplay(display)

    def begin_canvas_interaction(self, event):
        if self.handle_art_click(event) == "break":
            return "break"
        self.wm_action_active = False
        self.start_drag(event)

    def drag_canvas(self, event):
        if self.wm_action_active:
            return
        if self.resize_start is not None:
            self.resize_window(event)
        else:
            self.drag_window(event)

    def resize_window(self, event):
        if self.resize_start is None:
            return
        start_x, start_y, start_width, start_height = self.resize_start
        size = max(
            240,
            start_width + event.x_root - start_x,
            start_height + event.y_root - start_y,
        )
        self.root.geometry(f"{size}x{size}")

    def stop_resize(self, event=None):
        self.resize_start = None
        self.wm_action_active = False

    def update(self):
        try:
            if not self.player_name or self.player_name not in self.bus.list_names():
                self.player_name = find_player(self.bus)
                self.last_track = None
                self.last_title = None
                self.last_album = None

            if self.player_name:
                obj = self.bus.get_object(
                    self.player_name, "/org/mpris/MediaPlayer2"
                )
                props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")

                metadata = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
                position = props.Get("org.mpris.MediaPlayer2.Player", "Position")
                playback = props.Get(
                    "org.mpris.MediaPlayer2.Player", "PlaybackStatus"
                )
                self.position_us = max(0, int(position))
                self.position_sampled_at = time.monotonic()
                self.playback_status = str(playback)
                if self.position_us < ART_FADE_START_US:
                    self.fade_elapsed_us = 0
                title = str(metadata.get("xesam:title", "Unknown title"))
                album = str(metadata.get("xesam:album", ""))
                artists = metadata.get("xesam:artist", [])
                artist = ", ".join(str(a) for a in artists) if artists else ""
                length = int(metadata.get("mpris:length", 0))
                track_id = str(metadata.get("mpris:trackid", ""))

                self.art.itemconfigure(self.title, text=title)
                self.art.itemconfigure(self.title_shadow, text=title)
                self.art.itemconfigure(self.album, text=album)
                self.art.itemconfigure(self.artist_shadow, text=artist)
                self.art.itemconfigure(self.artist, text=artist)
                self.art.itemconfigure(self.time_shadow, text=format_time(position))
                self.art.itemconfigure(self.time, text=format_time(position))
                self.art.itemconfigure(
                    self.duration_shadow, text=format_time(length)
                )
                self.art.itemconfigure(self.duration, text=format_time(length))
                self.update_progress(position, length)
                self.art.itemconfigure(self.status, text=self.playback_status)

                if (
                    track_id != self.last_track
                    or title != self.last_title
                    or album != self.last_album
                ):
                    self.cancel_art_animation()
                    if track_id != self.last_track:
                        self.fade_elapsed_us = 0
                    self.last_track = track_id
                    self.last_title = title
                    self.last_album = album
                    duration_seconds = round(length / 1_000_000) if length > 0 else None
                    self.fetch_lyrics(artist, title, album, duration_seconds)
                    self.art_source = load_art(
                        metadata.get("mpris:artUrl"), title, album
                    )
                    if self.art_source:
                        self.title_mask = self.art_source[1]
                        self.title_shadow_mask = self.art_source[2]
                        self.album_mask = self.art_source[3]
                        icon = ImageOps.fit(
                            self.art_source[0], (64, 64), Image.Resampling.LANCZOS
                        )
                        self.icon_image = ImageTk.PhotoImage(icon, master=self.root)
                        self.root.iconphoto(True, self.icon_image)
                        self.art.itemconfigure(self.art_fallback, state="hidden")
                        self.art.itemconfigure(self.title_shadow, state="hidden")
                        self.art.itemconfigure(self.title, state="hidden")
                        self.art.itemconfigure(self.album, state="hidden")
                    else:
                        self.title_mask = None
                        self.title_shadow_mask = None
                        self.album_mask = None
                        self.art.itemconfigure(self.art_image_item, image="")
                        self.art.itemconfigure(self.art_fallback, state="normal")
                        self.art.itemconfigure(self.title_shadow, state="normal")
                        self.art.itemconfigure(self.title, state="normal")
                        self.art.itemconfigure(self.album, state="normal")

                if self.art_source:
                    self.render_current_art()
                self.update_lyrics_position()

                if self.art_fade_ready() or self.controls_animation_active():
                    self.start_art_animation()
                else:
                    self.cancel_art_animation()
            else:
                self.cancel_art_animation()
                self.lyrics_generation += 1
                self.lyrics = []
                self.active_lyric_index = None
                self.active_word_index = None
                self.lyrics_track_id = None
                self.set_lyrics_status("Waiting for a track")
                self.position_us = 0
                self.fade_elapsed_us = 0
                self.controls_visible_until = 0
                self.controls_alpha = 0
                self.art.configure(cursor="arrow")
                self.art.itemconfigure(self.title, text="Nothing playing")
                self.art.itemconfigure(self.title_shadow, text="Nothing playing")
                self.art.itemconfigure(self.album, text="")
                self.art.itemconfigure(self.title, state="normal")
                self.art.itemconfigure(self.title_shadow, state="normal")
                self.art.itemconfigure(self.album, state="normal")
                self.art.itemconfigure(self.artist_shadow, text="")
                self.art.itemconfigure(self.artist, text="")
                self.art.itemconfigure(self.time_shadow, text="0:00")
                self.art.itemconfigure(self.time, text="0:00")
                self.art.itemconfigure(self.duration_shadow, text="0:00")
                self.art.itemconfigure(self.duration, text="0:00")
                self.update_progress(0, 0)
                self.art.itemconfigure(self.status, text="")

        except Exception:
            # A player may disappear or change tracks during a poll.
            self.player_name = None

        self.root.after(1000, self.update)

    def control_player(self, method):
        if not self.player_name:
            return
        try:
            obj = self.bus.get_object(
                self.player_name, "/org/mpris/MediaPlayer2"
            )
            player = dbus.Interface(obj, "org.mpris.MediaPlayer2.Player")
            getattr(player, method)()
        except Exception:
            self.player_name = None

    def fetch_lyrics(self, artist, title, album, duration_seconds):
        self.lyrics_generation += 1
        generation = self.lyrics_generation
        self.lyrics_track_id = (artist, title)
        self.lyrics = []
        self.active_lyric_index = None
        self.active_word_index = None
        self.set_lyrics_status("Looking up synchronized lyrics...")
        self.render_current_art()
        if not artist or not title:
            self.set_lyrics_status("Track artist or title is unavailable")
            return

        def request_lyrics():
            try:
                def request_json(endpoint, params):
                    query = urlencode(params)
                    request = Request(
                        f"{LRCLIB_API_URL}{endpoint}?{query}",
                        headers={"User-Agent": "MPlaying/1.0"},
                    )
                    with urlopen(request, timeout=LYRICS_TIMEOUT_SECONDS) as response:
                        return json.loads(response.read().decode("utf-8"))

                params = {"artist_name": artist, "track_name": title}
                if album:
                    params["album_name"] = album
                if duration_seconds:
                    params["duration"] = duration_seconds
                try:
                    record = request_json("/get", params)
                except HTTPError as exception:
                    if exception.code != 404:
                        raise
                    record = None

                if not record or not record.get("syncedLyrics"):
                    records = request_json("/search", {
                        "artist_name": artist,
                        "track_name": title,
                    })
                    matches = [
                        candidate for candidate in records
                        if candidate.get("syncedLyrics")
                        and str(candidate.get("trackName", "")).casefold()
                        == title.casefold()
                        and str(candidate.get("artistName", "")).casefold()
                        == artist.casefold()
                    ]
                    if duration_seconds:
                        matches = [
                            candidate for candidate in matches
                            if abs(float(candidate.get("duration", 0))
                                   - duration_seconds) <= max(5, duration_seconds * 0.02)
                        ]
                    if matches:
                        matches.sort(key=lambda candidate: (
                            str(candidate.get("albumName", "")).casefold()
                            != album.casefold(),
                            abs(float(candidate.get("duration", 0))
                                - duration_seconds) if duration_seconds else 0,
                        ))
                        record = matches[0]

                entries = []
                track_duration_ms = (
                    duration_seconds * 1000 if duration_seconds else None
                )
                if record and record.get("lyricsfile"):
                    try:
                        entries = parse_lyricsfile(
                            record["lyricsfile"], track_duration_ms
                        )
                    except (AttributeError, TypeError, ValueError, yaml.YAMLError):
                        entries = []
                if not entries and record:
                    entries = parse_lrc(
                        record.get("syncedLyrics") or "", track_duration_ms
                    )
                error = None if entries else "No synchronized lyrics found"
            except Exception as exception:
                entries = []
                error = f"Lyrics unavailable ({type(exception).__name__})"

            self.lyrics_results.put((generation, entries, error))

        threading.Thread(target=request_lyrics, daemon=True).start()

    def process_lyrics_results(self):
        while True:
            try:
                generation, entries, error = self.lyrics_results.get_nowait()
            except queue.Empty:
                break
            if generation != self.lyrics_generation:
                continue
            self.lyrics = entries
            self.active_lyric_index = None
            self.active_word_index = None
            self.set_lyrics_status(error or "")
            self.update_lyrics_position()
        self.root.after(100, self.process_lyrics_results)

    def set_lyrics_status(self, text):
        self.lyrics_status_text = text
        self.render_current_art()

    def update_lyrics_position(self):
        self.sync_lyrics_position(render=True, schedule=False)

    def sync_lyrics_position(self, render=False, schedule=True):
        position_us = self.position_us
        if self.playback_status == "Playing":
            position_us += int(
                (time.monotonic() - self.position_sampled_at) * 1_000_000
            )
        position_ms = position_us // 1000
        line_index = -1
        word_index = -1
        if self.lyrics:
            line_index = bisect_right(
                [start_ms for start_ms, _, _ in self.lyrics], position_ms
            ) - 1
            if line_index >= 0:
                words = self.lyrics[line_index][2]
                word_index = bisect_right(
                    [start_ms for start_ms, _ in words], position_ms
                ) - 1
                while word_index >= 0 and not words[word_index][1].strip():
                    word_index -= 1
        changed = (
            line_index != self.active_lyric_index
            or word_index != self.active_word_index
        )
        self.active_lyric_index = line_index
        self.active_word_index = word_index
        if render or changed:
            self.render_current_art()
        if schedule:
            self.root.after(80, self.sync_lyrics_position)

    def current_lyric_text(self):
        if not self.show_lyrics.get() or not self.lyrics:
            return ""
        if self.current_active_word():
            return ""
        if self.active_lyric_index is None or self.active_lyric_index < 0:
            return ""
        return self.lyrics[self.active_lyric_index][1]

    def current_active_word(self):
        if (
            not self.show_lyrics.get()
            or self.active_lyric_index is None
            or not 0 <= self.active_lyric_index < len(self.lyrics)
        ):
            return ""
        words = self.lyrics[self.active_lyric_index][2]
        if self.active_word_index is None or self.active_word_index < 0:
            return ""
        return words[self.active_word_index][1].strip()

    def toggle_lyrics(self, event=None):
        self.show_lyrics.set(not self.show_lyrics.get())
        self.save_settings()
        self.render_current_art()
        return "break" if event else None

    def load_settings(self):
        try:
            with open(self.settings_path, encoding="utf-8") as settings_file:
                settings = json.load(settings_file)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(settings, dict):
            return
        geometry = settings.get("window_geometry")
        if isinstance(geometry, str) and re.fullmatch(
            r"\d+x\d+[+-]\d+[+-]\d+", geometry
        ):
            self.window_geometry = geometry
        for key, variable in (
            ("art_fade_enabled", self.art_fade_enabled),
            ("show_lyrics", self.show_lyrics),
            ("always_on_top", self.always_on_top),
        ):
            value = settings.get(key)
            if isinstance(value, bool):
                variable.set(value)

    def save_settings(self):
        settings_dir = os.path.dirname(self.settings_path)
        temporary_path = self.settings_path + ".tmp"
        try:
            os.makedirs(settings_dir, exist_ok=True)
            with open(temporary_path, "w", encoding="utf-8") as settings_file:
                json.dump({
                    "art_fade_enabled": self.art_fade_enabled.get(),
                    "show_lyrics": self.show_lyrics.get(),
                    "always_on_top": self.always_on_top.get(),
                    "window_geometry": self.root.geometry(),
                }, settings_file, indent=2)
                settings_file.write("\n")
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_path, self.settings_path)
        except OSError:
            try:
                os.remove(temporary_path)
            except OSError:
                pass

    def schedule_geometry_save(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self.geometry_save_job is not None:
            self.root.after_cancel(self.geometry_save_job)
        self.geometry_save_job = self.root.after(400, self.save_geometry)

    def save_geometry(self):
        self.geometry_save_job = None
        self.save_settings()

    def close_window(self):
        self.save_settings()
        self.root.destroy()

    def open_settings(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            return

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("Settings")
        window.resizable(False, False)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        tk.Checkbutton(
            window, text="Fade album art", variable=self.art_fade_enabled,
            command=self.update_fade_setting, anchor="w",
        ).pack(fill="x", padx=12, pady=(12, 4))
        tk.Checkbutton(
            window, text="Keep window above others", variable=self.always_on_top,
            command=self.update_topmost_setting, anchor="w",
        ).pack(fill="x", padx=12, pady=(4, 12))
        tk.Checkbutton(
            window, text="Show synchronized lyrics over artwork",
            variable=self.show_lyrics, command=self.update_lyrics_setting,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(4, 12))

    def update_topmost_setting(self):
        enabled = self.always_on_top.get()
        if enabled and self.root.tk.call("tk", "windowingsystem") == "x11":
            try:
                self.root.wm_attributes("-type", "normal")
            except tk.TclError:
                pass
        self.root.wm_attributes("-topmost", enabled)
        self.save_settings()
        if enabled:
            self.root.lift()

    def update_lyrics_setting(self):
        self.save_settings()
        self.render_current_art()

    def update_fade_setting(self):
        self.save_settings()
        self.render_current_art()
        if self.art_fade_ready() or self.controls_animation_active():
            self.start_art_animation()
        else:
            self.cancel_art_animation()

    def render_current_art(self):
        if not self.art_source:
            return
        size = (
            max(1, self.art.winfo_width()),
            max(1, self.art.winfo_height()),
        )
        self.art_image = render_art(
            self.art_source[0], self.title_mask, self.title_shadow_mask,
            self.album_mask,
            (self.fade_elapsed_us / ART_FADE_DURATION_US
             if self.art_fade_enabled.get() else 0),
            self.controls_alpha, self.playback_status, size,
            self.current_lyric_text(),
            self.current_active_word(),
        )
        self.art.itemconfigure(self.art_image_item, image=self.art_image)

    def art_fade_ready(self):
        return (
            self.art_fade_enabled.get()
            and
            self.playback_status == "Playing"
            and time.monotonic() - self.opened_at >= ART_OPEN_HOLD_SECONDS
            and self.position_us >= ART_FADE_START_US
            and self.fade_elapsed_us < ART_FADE_DURATION_US
        )

    def controls_animation_active(self):
        return (
            self.player_name is not None
            and (
                time.monotonic() < self.controls_visible_until
                or self.controls_alpha > 0
            )
        )

    def start_art_animation(self):
        if self.fade_job is None:
            self.fade_frame_time = time.monotonic()
            self.fade_job = self.root.after(33, self.animate_art)

    def cancel_art_animation(self):
        if self.fade_job is not None:
            self.root.after_cancel(self.fade_job)
            self.fade_job = None
        self.fade_frame_time = None

    def animate_art(self):
        self.fade_job = None
        now = time.monotonic()
        delta = now - self.fade_frame_time if self.fade_frame_time else 0
        self.fade_frame_time = now
        if self.art_fade_ready():
            self.fade_elapsed_us = min(
                self.fade_elapsed_us + int(delta * 1_000_000),
                ART_FADE_DURATION_US,
            )

        control_target = (
            1.0 if now < self.controls_visible_until and self.player_name else 0.0
        )
        alpha_step = delta / 0.2
        if self.controls_alpha < control_target:
            self.controls_alpha = min(self.controls_alpha + alpha_step, control_target)
        else:
            self.controls_alpha = max(self.controls_alpha - alpha_step, control_target)

        if self.art_source:
            self.render_current_art()

        if self.art_fade_ready() or self.controls_animation_active():
            self.fade_job = self.root.after(33, self.animate_art)
        else:
            self.fade_frame_time = None
            self.art.configure(cursor="arrow")

    def update_progress(self, position, length):
        if length <= 0 or position <= 0:
            self.art.itemconfigure(self.progress_fill, state="hidden")
            return

        ratio = min(position / length, 1.0)
        width = max(1, self.art.winfo_width())
        height = max(1, self.art.winfo_height())
        start = 14 * width / ART_SIZE
        end = start + (width - 28 * width / ART_SIZE) * ratio
        y = 254 * height / ART_SIZE
        self.art.coords(self.progress_fill, start, y, end, y)
        self.art.itemconfigure(self.progress_fill, state="normal")


root = tk.Tk()
NowPlaying(root)
root.mainloop()
