"""Windows UI — the tkinter overlay plus the pystray tray icon.

Laid out like the macOS panel it is the counterpart of: a title bar, a status
block, a flexible stage that holds whatever the current mode has to show (the
waveform while recording, the transcript running in while transcribing), then
the optional queue or history list, then a footer of keycap hints. Sizes are
fixed per mode, so changing state never resizes the window under the pointer.

Tk paints literal hex values, so everything the macOS build gets from system
colours is assembled here instead: `whispertype.ui.theme` resolves the palette
(including the user's Windows accent) and `_apply_palette` repaints every
widget that was registered with `_themed`, which is what lets the theme change
without a restart.

This module is Windows-only. On macOS Tk activates the application on every
window map, which would steal focus from the window we are about to type into.
"""
import ctypes
import ctypes.wintypes
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime
from pathlib import Path

import pystray

from .. import audio
from ..config import API_KEY_PATH
from ..icon import make_tray_icon
from ..jobs import JobStatus
from ..log import LOG_PATH, log
from . import theme, winicon

OFF_SCREEN = "-9999+-9999"
OV_W = 380

#: Fixed panel heights, the counterpart of the macOS panel's. Sizing to content
#: meant expanding a history row or showing a two-line error resized the window
#: under the pointer; stable sizes are calmer and still proportionate — a
#: recording HUD should not be as tall as a 50-entry archive.
OV_H_COMPACT = 224      # idle / recording / transcribing / notice / error
OV_H_QUEUE = 312        # transcribing with jobs actually queued behind it
OV_H_HISTORY = 484
GPU_GRAPH_H = 56        # added to any of the above when the graph is shown

MAX_QUEUE_VISIBLE = 5
HISTORY_ITEM_H = 22     # benchmark rows, which are still a plain table

#: How long the finished transcript takes to run across the stage. The worker
#: waits 900 ms before hiding the overlay, so this has to stay under it.
TICKER_SECONDS = 0.7

#: Transcript length at which a history row clamps and offers "Show more".
CLAMP_CHARS = 190

from .common import IDLE_CHOICES, LANGUAGES  # noqa: F401  (menu content)


def _round_rect(canvas, x0, y0, x1, y1, r=4, **kw):
    """Tk has no rounded rectangle; a smoothed polygon is the usual stand-in."""
    points = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r,
              x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r,
              x0, y0 + r, x0, y0]
    return canvas.create_polygon(points, smooth=True, **kw)


def _day_label(stamp):
    """Sticky group heading for the history list."""
    if not stamp:
        # Entries written before `at` existed. They still belong somewhere.
        return "Earlier"
    day = datetime.fromtimestamp(stamp).date()
    delta = (date.today() - day).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return day.strftime("%A")
    return day.strftime("%d %b %Y")


class _Slider(tk.Canvas):
    """Flat slider drawn to the app palette.

    tk.Spinbox brings native Windows chrome — a hairline border and two tiny
    grey arrows — which looks like damage on the panel. This is drawn, so it
    matches, and it can carry a reference marker: for the silence threshold,
    knowing where your microphone actually idles is the whole difference
    between guessing at a number and setting one.
    """

    H = 30
    PAD = 12
    READOUT = 84          # space reserved on the right for the value

    def __init__(self, parent, palette, *, lo, hi, step, value, unit,
                 on_change, width=300, marker=None):
        super().__init__(parent, width=width, height=self.H,
                         bg=palette["bar_bg"], highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=True)
        self.C = palette
        self.lo, self.hi, self.step = lo, hi, step
        self.unit, self.on_change, self.marker = unit, on_change, marker
        # Deliberately not self._w — Tk stores the widget's own path there, and
        # overwriting it breaks every subsequent Tcl call on this canvas.
        self._track_w = width
        self._value = self._snap(value)

        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<FocusIn>", lambda e: self._redraw())
        self.bind("<FocusOut>", lambda e: self._redraw())
        self._redraw()

    # ── value ↔ pixels ──

    @property
    def _x0(self):
        return self.PAD

    @property
    def _x1(self):
        return max(self._x0 + 10, self._track_w - self.READOUT)

    def _snap(self, v):
        v = min(max(float(v), self.lo), self.hi)
        return round((v - self.lo) / self.step) * self.step + self.lo

    def _to_x(self, v):
        span = (self.hi - self.lo) or 1
        return self._x0 + (self._x1 - self._x0) * (v - self.lo) / span

    def _from_x(self, x):
        span = self._x1 - self._x0 or 1
        return self._snap(self.lo + (self.hi - self.lo) * (x - self._x0) / span)

    # ── interaction ──

    def _on_press(self, event):
        self.focus_set()
        self._set(self._from_x(event.x), commit=False)

    def _on_drag(self, event):
        self._set(self._from_x(event.x), commit=False)

    def _on_release(self, _event):
        self.on_change(self._value)

    def _nudge(self, direction):
        self._set(self._value + direction * self.step, commit=True)

    def _set(self, value, commit):
        value = self._snap(value)
        if value != self._value:
            self._value = value
            self._redraw()
        if commit:
            self.on_change(self._value)

    def set_value(self, value):
        self._value = self._snap(value)
        self._redraw()

    def set_marker(self, marker):
        self.marker = marker
        self._redraw()

    def set_palette(self, palette):
        self.C = palette
        self.configure(bg=palette["bar_bg"])
        self._redraw()

    # ── drawing ──

    def _redraw(self):
        C = self.C
        self.delete("all")
        y = self.H // 2
        x0, x1, hx = self._x0, self._x1, self._to_x(self._value)

        self.create_line(x0, y, x1, y, fill=C["sep"], width=4, capstyle="round")
        if hx > x0 + 1:
            self.create_line(x0, y, hx, y, fill=C["accent"], width=4,
                             capstyle="round")

        if self.marker is not None and self.lo <= self.marker <= self.hi:
            mx = self._to_x(self.marker)
            self.create_line(mx, y - 9, mx, y + 9, fill=C["bar_mid"], width=2)

        r = 7
        ring = C["accent"] if self.focus_get() is self else C["text"]
        self.create_oval(hx - r, y - r, hx + r, y + r,
                         fill=C["bg"], outline=ring, width=2)

        text = f"{self._value:g}" + (f" {self.unit}" if self.unit else "")
        self.create_text(self._track_w - self.PAD, y, text=text, anchor="e",
                         fill=C["text"], font=("Segoe UI", 9))


class TkUI:

    def __init__(self, app):
        self.app = app
        self.visible = False
        self.history_mode = False
        self._message = None        # transient notice / loading text

        self.C = theme.resolve(app.cfg.theme)
        #: (widget, {option: palette key}) for everything _apply_palette
        #: repaints. Registered at creation via _themed.
        self._painted = []
        #: Sliders draw themselves, so they take the palette rather than a set
        #: of options.
        self._sliders = []

        self.root = tk.Tk()
        self.root.attributes("-alpha", 0.0)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.title("WhisperType")
        self.root.configure(bg=self.C["bg"])
        self.root.resizable(False, False)
        self.root.geometry(OFF_SCREEN)

        cw = OV_W - 28
        self._cw = cw

        # ── Title bar ──
        self.title_bar = self._themed(tk.Frame(self.root), bg="bg")
        self._title_lbl = self._themed(
            tk.Label(self.title_bar, text="WhisperType", font=("Segoe UI", 8)),
            bg="bg", fg="faint")
        self._title_lbl.pack(side="left")
        exit_btn = self._themed(
            tk.Label(self.title_bar, text="✕", font=("Segoe UI", 9),
                     cursor="hand2"), bg="bg", fg="faint")
        exit_btn.pack(side="right")
        exit_btn.bind("<Button-1>", lambda e: self.hide())
        exit_btn.bind("<Enter>", lambda e: exit_btn.config(fg=self.C["text"]))
        exit_btn.bind("<Leave>", lambda e: exit_btn.config(fg=self.C["faint"]))
        self.title_bar.bind("<Button-1>", lambda e: setattr(self, "_d", (e.x, e.y)))
        self.title_bar.bind("<B1-Motion>", self._drag)
        self._title_lbl.bind("<Button-1>", lambda e: setattr(self, "_d", (e.x, e.y)))
        self._title_lbl.bind("<B1-Motion>", self._drag)

        # ── Status ──
        self.status_frame = self._themed(tk.Frame(self.root), bg="bg")
        hdr = self._themed(tk.Frame(self.status_frame), bg="bg")
        hdr.pack(fill="x", padx=14, pady=(2, 0))
        self.dot = self._themed(
            tk.Label(hdr, text="●", font=("Segoe UI", 11)), bg="bg", fg="rec")
        self.dot.pack(side="left")
        self.state_lbl = self._themed(
            tk.Label(hdr, text="Ready", font=("Segoe UI Semibold", 12)),
            bg="bg", fg="text")
        self.state_lbl.pack(side="left", padx=(5, 0))
        self.timer = self._themed(
            tk.Label(hdr, text="", font=("Consolas", 11)), bg="bg", fg="dim")
        self.timer.pack(side="right")

        info_row = self._themed(tk.Frame(self.status_frame), bg="bg")
        info_row.pack(fill="x", padx=14, pady=(2, 0))
        self.model_lbl = self._themed(
            tk.Label(info_row, text="", font=("Segoe UI", 8)), bg="bg", fg="dim")
        self.model_lbl.pack(side="left")
        self.target_lbl = self._themed(
            tk.Label(info_row, text="", font=("Segoe UI", 8)), bg="bg", fg="dim")
        self.target_lbl.pack(side="right")
        # The GPU number survives as a chip even when the graph is off — it is
        # the one part of the telemetry that answers a question you actually
        # have while waiting ("is it working?").
        self.gpu_chip = self._themed(
            tk.Label(info_row, text="", font=("Consolas", 8)), bg="bg", fg="faint")

        hdr.bind("<Button-1>", lambda e: setattr(self, "_d", (e.x, e.y)))
        hdr.bind("<B1-Motion>", self._drag)

        # ── Stage: waveform while recording, ticker while transcribing ──
        self.stage = self._themed(tk.Frame(self.root), bg="bg")
        # 66, not 52: the panel has the slack, and a waveform needs amplitude
        # to be a waveform rather than a texture.
        self.stage_cv = self._themed(
            tk.Canvas(self.stage, width=cw, height=66, highlightthickness=0),
            bg="bg")
        self.stage_cv.pack(padx=14, expand=True)
        # Measured with, not just drawn with — and created once, because every
        # tkfont.Font is a Tcl object that outlives the call that made it.
        self._stage_font = tkfont.Font(family="Segoe UI", size=10)
        self._key_font = tkfont.Font(family="Segoe UI Semibold", size=8)
        self._label_font = tkfont.Font(family="Segoe UI", size=8)

        # ── GPU graph (off by default; show_gpu_graph turns it on) ──
        self.gpu_frame = self._themed(tk.Frame(self.root), bg="bg")
        gpu_hdr = self._themed(tk.Frame(self.gpu_frame), bg="bg")
        gpu_hdr.pack(fill="x", padx=14, pady=(0, 2))
        self._gpu_title = self._themed(
            tk.Label(gpu_hdr, text=app.backend.gpu_label,
                     font=("Segoe UI", 8, "bold")), bg="bg", fg="faint")
        self._gpu_title.pack(side="left")
        self.gpu_pct = self._themed(
            tk.Label(gpu_hdr, text="--%", font=("Consolas", 9, "bold")),
            bg="bg", fg="ok")
        self.gpu_pct.pack(side="right")
        self.gpu_cv = self._themed(
            tk.Canvas(self.gpu_frame, width=cw, height=34, highlightthickness=0),
            bg="bar_bg")
        self.gpu_cv.pack(padx=14, pady=(0, 2))

        # ── Message (error / notice) ──
        self.message_frame = self._themed(tk.Frame(self.root), bg="bg")
        self.message_lbl = self._themed(
            tk.Label(self.message_frame, text="", font=("Segoe UI", 10),
                     justify="center", wraplength=cw),
            bg="bg", fg="text")
        self.message_lbl.pack(padx=14, expand=True)

        # ── Queue ──
        self.queue_frame = self._themed(tk.Frame(self.root), bg="bg")
        self.queue_hdr_lbl = self._themed(
            tk.Label(self.queue_frame, text="", font=("Segoe UI Semibold", 8),
                     anchor="w"), bg="bg", fg="faint")
        self.queue_hdr_lbl.pack(fill="x", padx=14, pady=(0, 2))
        self.queue_items_frame = self._themed(tk.Frame(self.queue_frame), bg="bg")
        self.queue_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.queue_item_labels = []

        # ── History ──
        self.history_frame = self._themed(tk.Frame(self.root), bg="bg")
        h_head = self._themed(tk.Frame(self.history_frame), bg="bg")
        h_head.pack(fill="x", padx=14, pady=(0, 4))
        self._themed(tk.Label(h_head, text="History",
                              font=("Segoe UI Semibold", 10)),
                     bg="bg", fg="text").pack(side="left")
        self.h_count = self._themed(
            tk.Label(h_head, text="", font=("Segoe UI", 8)), bg="bg", fg="faint")
        self.h_count.pack(side="left", padx=(6, 0))
        self.h_clear = self._themed(
            tk.Label(h_head, text="Clear All", font=("Segoe UI", 8),
                     cursor="hand2"), bg="bg", fg="faint")
        self.h_clear.pack(side="right")
        self.h_clear.bind("<Button-1>", lambda e: self._clear_history_clicked())
        self._clear_armed = False
        self._clear_disarm_job = None

        history_scroll_container = self._themed(
            tk.Frame(self.history_frame), bg="bg")
        # Bottom margin so the viewport does not cut a row off flush against
        # the window border, which reads as damage rather than as "scroll".
        history_scroll_container.pack(fill="both", expand=True, padx=(14, 6),
                                      pady=(0, 6))
        self._history_canvas = self._themed(
            tk.Canvas(history_scroll_container, highlightthickness=0, height=330),
            bg="bg")
        self._history_scrollbar = tk.Scrollbar(
            history_scroll_container, orient="vertical",
            command=self._history_canvas.yview)
        self.history_items_frame = self._themed(
            tk.Frame(self._history_canvas), bg="bg")
        self.history_items_frame.bind(
            "<Configure>",
            lambda e: self._history_canvas.configure(
                scrollregion=self._history_canvas.bbox("all")))
        self._history_canvas_win = self._history_canvas.create_window(
            (0, 0), window=self.history_items_frame, anchor="nw")
        self._history_canvas.configure(yscrollcommand=self._history_scrollbar.set)
        self._history_scrollbar.configure(
            bg=self.C["bar_bg"], troughcolor=self.C["bg"],
            activebackground=self.C["sep"], bd=0, relief="flat",
            highlightthickness=0, width=10)
        self._pack_history_scroll(False)
        self._history_canvas.bind(
            "<Configure>",
            lambda e: self._history_canvas.itemconfig(
                self._history_canvas_win, width=e.width))

        def _on_history_mousewheel(event):
            self._history_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._on_history_mousewheel = _on_history_mousewheel
        self._history_canvas.bind("<MouseWheel>", _on_history_mousewheel)

        self.history_item_widgets = []
        self._expanded = set()          # history indices showing their full text

        # ── Benchmark panel (Windows only — the macOS overlay has no table) ──
        self.benchmark_frame = self._themed(tk.Frame(self.root), bg="bg")
        bench_hdr = self._themed(tk.Frame(self.benchmark_frame), bg="bg")
        bench_hdr.pack(fill="x", padx=14)
        bench_hdr.columnconfigure(2, weight=1)
        _bf = ("Consolas", 8)
        self._themed(tk.Label(bench_hdr, text="BENCHMARK",
                              font=("Segoe UI", 8, "bold"), anchor="w"),
                     bg="bg", fg="faint").grid(row=0, column=0, sticky="w")
        self.bench_title_lbl = self._themed(
            tk.Label(bench_hdr, text="", font=_bf, anchor="w"),
            bg="bg", fg="faint")
        self.bench_title_lbl.grid(row=0, column=2, sticky="we", padx=(8, 0))
        bench_close = self._themed(
            tk.Label(bench_hdr, text="✕", font=("Segoe UI", 10, "bold"),
                     cursor="hand2"), bg="bg", fg="danger")
        bench_close.grid(row=0, column=3, sticky="e", padx=(4, 0))
        bench_close.bind("<Button-1>", lambda e: self._close_benchmark())

        bench_cols = self._themed(tk.Frame(self.benchmark_frame), bg="bg")
        bench_cols.pack(fill="x", padx=14, pady=(2, 0))
        bench_cols.columnconfigure(2, weight=1)
        for txt, w, col in (("MODEL", 16, 0), ("TIME", 8, 1)):
            self._themed(tk.Label(bench_cols, text=txt, font=_bf, width=w,
                                  anchor="w"),
                         bg="bg", fg="faint").grid(row=0, column=col, sticky="w")
        self._themed(tk.Label(bench_cols, text="TEXT", font=_bf, anchor="w"),
                     bg="bg", fg="faint").grid(row=0, column=2, sticky="we",
                                               padx=(4, 0))

        # Not scrollable: at most one row per downloaded model.
        self.benchmark_items_frame = self._themed(
            tk.Frame(self.benchmark_frame), bg="bg")
        self.benchmark_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.benchmark_item_widgets = []
        self._benchmark_view_job = None
        self.benchmark_mode = False

        # ── Footer: keycap hints ──
        self.hint_cv = self._themed(
            tk.Canvas(self.root, width=OV_W, height=28, highlightthickness=0),
            bg="bg")

        self._tooltip = None

        self.root.geometry(f"{OV_W}x120")
        self.root.update_idletasks()
        self.root.geometry(OFF_SCREEN)

        self._timer_job = None
        self._blink_job = None
        self._level_job = None
        self._ticker_job = None
        self._gpu_refresh_job = None
        self._blink_on = True
        self._pos = None
        self._d = (0, 0)            # drag anchor, set on every press
        self._t0 = time.time()
        self._level = 0.0
        #: Rolling RMS window the waveform scrolls through. Filled on the Tk
        #: thread from the value the audio thread last stored.
        self._wave = [0.0] * (cw // 4)
        self._wave_ceiling = 400.0
        self._silence_since = None
        self._ticker_text = ""
        self._ticker_t0 = 0.0
        self._sweep = 0.0

        #: Win32 HWNDs of our own windows, cached as plain ints. Read from the
        #: keyboard thread by the backend's capture_target(), which must not
        #: touch Tcl. Refreshed on the main thread whenever we show ourselves.
        self._own_hwnds = ()
        self._refresh_own_hwnds()

        self.tray_icon = None
        self._tray_thread = None
        self._tray_state = "idle"

    # ── Palette plumbing ──

    def _themed(self, widget, **roles):
        """Register `widget`'s colour options so the theme can change later.

        roles maps a Tk option to a palette key: `bg="bar_bg", fg="dim"`.
        """
        self._painted.append((widget, roles))
        self._paint_one(widget, roles)
        return widget

    def _paint_one(self, widget, roles):
        try:
            widget.configure(**{opt: self.C[key] for opt, key in roles.items()})
            return True
        except tk.TclError:
            return False        # destroyed — dropped on the next full pass

    def _apply_palette(self):
        alive = []
        for widget, roles in self._painted:
            if self._paint_one(widget, roles):
                alive.append((widget, roles))
        self._painted = alive
        try:
            self.root.configure(bg=self.C["bg"])
        except tk.TclError:
            pass

    def set_theme(self, name):
        """Re-resolve the palette and repaint. Tk thread only."""
        self.C = theme.resolve(name)
        self._apply_palette()
        alive = []
        for slider in self._sliders:
            try:
                slider.set_palette(self.C)
                alive.append(slider)
            except tk.TclError:
                pass                    # its window was closed
        self._sliders = alive
        win = getattr(self, "_settings_win", None)
        if win is not None:
            try:
                self._titlebar_theme(win)
            except tk.TclError:
                pass
        self._draw_stage()
        self._draw_hints()
        self._refresh_gpu_now()
        if self.history_mode:
            self._rebuild_history()

    # ── UI interface: thread marshalling ──

    def call_soon(self, fn):
        self.root.after(0, fn)

    def call_later(self, ms, fn):
        self.root.after(ms, fn)

    def _refresh_own_hwnds(self):
        """Main thread only. Must include wm_frame(), not just winfo_id().

        On Windows Tk nests a "TkChild" inside a "TkTopLevel" wrapper:
        winfo_id() returns the child, while GetForegroundWindow() reports the
        wrapper. Comparing a foreground HWND against winfo_id() alone therefore
        never matches, so the overlay was never recognised as our own window —
        and once the user clicked it (dragging it, or using the history
        buttons) the next dictation targeted the overlay itself.
        """
        ids = []
        windows = [self.root]
        settings = getattr(self, "_settings_win", None)
        if settings is not None:
            # Dictating with the settings window in front is the same trap.
            windows.append(settings)
        for window in windows:
            for getter in (lambda w=window: int(w.wm_frame(), 16),
                           lambda w=window: w.winfo_id()):
                try:
                    value = getter()
                except Exception:
                    continue
                if value and value not in ids:
                    ids.append(value)
        self._own_hwnds = tuple(ids)

    def own_window_ids(self):
        return self._own_hwnds

    # ── Geometry ──

    def _drag(self, e):
        dx, dy = self._d
        x = self.root.winfo_x() + e.x - dx
        y = self.root.winfo_y() + e.y - dy
        self._pos = (x, y)
        self.root.geometry(f"+{x}+{y}")

    @staticmethod
    def _work_area_under_cursor():
        """(left, top, right, bottom) of the work area of the display the
        pointer is on, or None.

        The counterpart of the macOS build asking which Space the user is
        actually on: with one monitor this changes nothing, and with three it
        is the difference between the overlay appearing where you are looking
        and appearing on a screen you are not.
        """
        try:
            user32 = ctypes.windll.user32

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

            point = POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return None
            MONITOR_DEFAULTTONEAREST = 2
            handle = user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                return None
            work = info.rcWork
            return work.left, work.top, work.right, work.bottom
        except Exception as e:
            log(f"Could not read the monitor layout: {e}")
            return None

    def _get_pos(self, height):
        """Where to put the panel for this appearance.

        A dragged position is kept only while it is still on the screen the
        user is on; otherwise the panel sits on a display — or past an edge —
        where it cannot be seen, which looks exactly like the app not having
        started.
        """
        area = self._work_area_under_cursor()
        if area is None:
            width = self.root.winfo_screenwidth()
            if self._pos:
                return f"+{self._pos[0]}+{self._pos[1]}"
            return f"+{width // 2 - OV_W // 2}+20"

        left, top, right, bottom = area
        if self._pos:
            x, y = self._pos
            # Enough of the panel has to remain on this display to grab and
            # move it — its title bar plus a corner is the minimum.
            if (left - 40 <= x <= right - 40
                    and top <= y <= bottom - min(height, 60)):
                return f"+{x}+{y}"
        x = left + (right - left - OV_W) // 2
        y = top + 20
        return f"+{x}+{y}"

    # ── Layout ──

    def _gpu_graph_on(self):
        return (self.app.backend.gpu_available
                and bool(self.app.cfg.get("show_gpu_graph", False)))

    def _mode(self):
        # Same precedence as the macOS panel, deliberately: work in flight
        # outranks a message about work that already finished, so a queue
        # draining behind an error still shows what it is doing. The error is
        # not lost — it survives in last_error until acknowledged.
        if self.app.recording:
            return "recording"
        if self.benchmark_mode:
            return "benchmark"
        if self.history_mode:
            return "history"
        if self.app.jobs.busy():
            return "transcribing"
        if self.app.last_error:
            return "error"
        if self._message:
            return "notice"
        return "idle"

    def _repack(self):
        for widget in (self.title_bar, self.status_frame, self.stage,
                       self.gpu_frame, self.message_frame, self.queue_frame,
                       self.history_frame, self.benchmark_frame, self.hint_cv):
            widget.pack_forget()

        mode = self._mode()
        # Order matters: side="bottom" widgets stack upwards in call order, so
        # the footer has to be claimed before anything that sits above it.
        self.title_bar.pack(side="top", fill="x", padx=10, pady=(6, 0))
        self.status_frame.pack(side="top", fill="x")
        # The keycaps used to sit on the window border, close enough that the
        # descenders of "Transcribe" ran into it.
        self.hint_cv.pack(side="bottom", fill="x", pady=(2, 10))
        if mode == "benchmark":
            self.benchmark_frame.pack(side="bottom", fill="x")
        elif mode == "history":
            self.history_frame.pack(side="bottom", fill="both", expand=True)
        elif self.app.jobs.active_count() > 1:
            # Only when something is genuinely queued behind the job in flight;
            # a one-row table under a one-job queue was noise.
            self.queue_frame.pack(side="bottom", fill="x")
        if self._gpu_graph_on():
            self.gpu_frame.pack(side="bottom", fill="x")
        if mode in ("error", "notice"):
            # The message takes the flexible middle rather than sitting on the
            # footer with an empty stage above it: what went wrong is the whole
            # content of this state, so it belongs where the eye already is.
            self.message_frame.pack(side="top", fill="both", expand=True)
        elif mode not in ("history", "benchmark"):
            self.stage.pack(side="top", fill="both", expand=True)

        self._update_state_display()
        self._draw_hints()
        # Load-bearing: the stage only redraws itself on a tick, and neither
        # the waveform nor the ticker ticks outside its own mode — so without
        # this the last transcript stayed painted across the error panel.
        self._draw_stage()

    def _calc_height(self):
        mode = self._mode()
        if mode == "benchmark":
            # A run only covers downloaded models, so sizing off the full
            # catalogue would leave dead space under the last row.
            job = self._benchmark_view_job
            rows = len(job.results) if job and job.results else 1
            height = 140 + rows * HISTORY_ITEM_H
        elif mode == "history":
            height = OV_H_HISTORY
        elif self.app.jobs.active_count() > 1:
            height = OV_H_QUEUE
        else:
            height = OV_H_COMPACT
        if self._gpu_graph_on():
            height += GPU_GRAPH_H
        return height

    def _update_state_display(self):
        C = self.C
        mode = self._mode()
        self.model_lbl.config(text=f"Model: {self.app.model_name}")
        if mode == "recording":
            armed = self.app.benchmark_next
            self.dot.config(fg=C["bar_mid"] if armed else C["rec"])
            self.state_lbl.config(
                text="Recording (benchmark)" if armed else "Recording",
                fg=C["bar_mid"] if armed else C["text"])
        elif mode == "benchmark":
            running = self.app.benchmark_job is not None
            self.dot.config(fg=C["bar_mid"] if running else C["ok"])
            self.state_lbl.config(
                text="Benchmarking…" if running else "Benchmark results",
                fg=C["bar_mid"] if running else C["ok"])
            self.timer.config(text="")
        elif mode == "history":
            self.dot.config(fg=C["ok"])
            self.state_lbl.config(text="History", fg=C["text"])
            self.timer.config(text="")
        elif mode == "error":
            self.dot.config(fg=C["danger"])
            self.state_lbl.config(text="Failed", fg=C["danger"])
            self.timer.config(text="")
            self.message_lbl.config(text=str(self.app.last_error), fg=C["text"])
        elif mode == "notice":
            self.dot.config(fg=C["faint"])
            self.state_lbl.config(text="Ready", fg=C["dim"])
            self.timer.config(text="")
            self.message_lbl.config(text=self._message, fg=C["dim"])
        elif mode == "transcribing":
            self.dot.config(fg=C["trans"])
            self.state_lbl.config(text="Transcribing", fg=C["trans"])
            self.timer.config(text="")
        else:
            self.dot.config(fg=C["faint"])
            self.state_lbl.config(text="Ready", fg=C["dim"])
            self.timer.config(text="")

        if mode != "recording":
            self.target_lbl.config(text="")
        self._update_gpu_chip()

    def _update_gpu_chip(self):
        """The utilisation as a chip, for the states where the graph is not
        drawn but the number still answers "is anything happening?"."""
        series = self.app.gpu_series() if self.app.backend.gpu_available else []
        show = (self._mode() == "transcribing" and series
                and not self._gpu_graph_on())
        if show:
            self.gpu_chip.config(text=f"{self.app.backend.gpu_label} "
                                      f"{int(series[-1] * 100)}%")
            self.gpu_chip.pack(side="right", padx=(0, 10))
        else:
            self.gpu_chip.pack_forget()

    def _sync_auto_theme(self):
        """Follow the system light/dark setting without a restart.

        One registry read per appearance, and only a repaint when the answer
        actually changed — the macOS panel gets this from system colours for
        free, and a HUD that is still dark an hour after the machine went light
        looks broken rather than pinned.
        """
        if self.app.cfg.theme != "auto":
            return
        if theme.system_is_dark() != self.C["dark"]:
            self.set_theme("auto")

    def _show_overlay(self):
        self._sync_auto_theme()
        self._repack()
        self._rebuild_queue()
        h = self._calc_height()
        self.root.geometry(f"{OV_W}x{h}{self._get_pos(h)}")
        self.root.update_idletasks()
        self.root.attributes("-alpha", 1.0)
        self.visible = True
        self._refresh_own_hwnds()
        self._start_gpu_refresh()

    # ── UI interface: state ──

    def show_recording(self, target_name=""):
        # A leaked after-job from a previous recording would double the blink
        # and level rate and could never be cancelled again.
        self._cancel_timer_blink()
        self.history_mode = False
        self.benchmark_mode = False
        self._message = None
        self._ticker_text = ""
        self._t0 = time.time()
        self._wave = [0.0] * len(self._wave)
        self._wave_ceiling = max(self.app.cfg.silence_threshold * 3.0, 400.0)
        self._silence_since = None
        self.target_lbl.config(text=f"→ {target_name}" if target_name else "")
        self._show_overlay()
        self._level_tick()
        self._tick()
        self._blink()

    def _show_rec_idle(self):
        self._cancel_timer_blink()
        self.target_lbl.config(text="")

    def on_recording_stopped(self):
        self._cancel_timer_blink()
        # A panel the user deliberately opened must not be pulled away by a
        # job landing behind it.
        if self.history_mode or self.benchmark_mode:
            self._show_overlay()
            return
        if self.app.jobs.busy():
            self._show_rec_idle()
            self._show_overlay()
            self._stage_tick()
        else:
            self.hide()

    def refresh(self):
        if (self.app.recording or self.app.jobs.busy() or self.history_mode
                or self.benchmark_mode or self.app.last_error or self._message):
            if not self.app.recording:
                self._show_rec_idle()
            if self.benchmark_mode:
                self._rebuild_benchmark()
            elif self.history_mode:
                self._rebuild_history()
            self._show_overlay()
            if not self.app.recording and self.app.jobs.busy():
                self._stage_tick()
        else:
            self.hide()

    def check_hide(self):
        # An unacknowledged error keeps the overlay up — it is the only place
        # the reason is written down.
        if (not self.app.jobs.busy() and not self.app.recording
                and not self.history_mode and not self.benchmark_mode
                and not self.app.last_error and not self._message):
            self.hide()

    # ── Transient states ──

    def on_capture_started(self):
        """Audio is genuinely flowing — restart the elapsed timer from here so
        the stream-open latency is not counted as recorded time."""
        self.root.after(0, lambda: setattr(self, "_t0", time.time()))

    def show_notice(self, text, seconds=1.6):
        def run():
            self._message = text
            self.refresh()
            self.root.after(int(seconds * 1000), self._clear_message)
        self.root.after(0, run)

    def set_ticker(self, text):
        """Run the finished transcript across the stage before it is typed, so
        there is a moment where you can see what is about to land in your
        document."""
        def run():
            self._ticker_text = text or ""
            self._ticker_t0 = time.time()
            self._stage_tick()
        self.root.after(0, run)

    def show_loading(self, model_name):
        self.root.after(0, lambda: (setattr(self, "_message",
                                            f"Loading {model_name}…"),
                                    self.refresh()))

    def _clear_message(self):
        self._message = None
        self.refresh()

    def set_history_mode(self, on):
        self.history_mode = on
        if on:
            # _repack draws the benchmark panel ahead of history, so switching
            # to history would otherwise silently do nothing while it is up.
            self.benchmark_mode = False
            self._benchmark_view_job = None
            self._cancel_timer_blink()
            self._rebuild_history()
        elif self.app.recording:
            self._level_tick()
            self._tick()
            self._blink()
        h = self._calc_height()
        self._repack()
        self.root.geometry(f"{OV_W}x{h}{self._get_pos(h)}")
        self.root.update_idletasks()
        # History mode is exactly the state in which the overlay holds focus,
        # so the backend must be able to recognise it as ours.
        self._refresh_own_hwnds()
        self.root.attributes("-alpha", 1.0)
        self.visible = True

    def hide(self):
        self._cancel_timer_blink()
        self._stop_gpu_refresh()
        self._hide_tooltip()
        for widget in (self.title_bar, self.status_frame, self.stage,
                       self.gpu_frame, self.message_frame, self.queue_frame,
                       self.history_frame, self.benchmark_frame, self.hint_cv):
            widget.pack_forget()
        self.history_mode = False
        self.benchmark_mode = False
        self._ticker_text = ""
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)
        self.visible = False

    def push_level(self, rms):
        """Called from the audio thread ~16x/s. Store only — Tcl is not
        thread-safe, so _level_tick() does the redraw on the main loop."""
        self._level = rms
        # Tracked here rather than in the recorder so the overlay can show how
        # far into the auto-stop a pause has run without the core growing a
        # UI-shaped callback.
        if rms >= self.app.cfg.silence_threshold:
            self._silence_since = None
        elif self._silence_since is None:
            self._silence_since = time.monotonic()

    # ── Stage: waveform and ticker ──

    def _level_tick(self):
        self._wave.pop(0)
        self._wave.append(self._level)
        self._draw_stage()
        self._level_job = self.root.after(60, self._level_tick)

    def _stage_tick(self):
        """Animation for the transcribing stage — the reveal, or the sweep that
        stands in for it until Whisper returns anything at all."""
        if self._ticker_job:
            self.root.after_cancel(self._ticker_job)
            self._ticker_job = None
        if self.app.recording or not self.visible:
            return
        if self._mode() != "transcribing":
            return                  # nothing to animate; do not spin forever
        self._sweep = (self._sweep + 0.02) % 1.0
        self._draw_stage()
        done = (self._ticker_text
                and time.time() - self._ticker_t0 > TICKER_SECONDS)
        if not done:
            self._ticker_job = self.root.after(40, self._stage_tick)

    def _draw_stage(self):
        c = self.stage_cv
        try:
            c.delete("all")
        except tk.TclError:
            return
        W, H = int(c["width"]), int(c["height"])
        mode = self._mode()
        if mode == "recording":
            self._draw_waveform(c, W, H)
        elif mode == "transcribing":
            self._draw_ticker(c, W, H)

    def _wave_scale(self):
        """(floor, ceiling) the waveform is drawn between.

        Auto-ranging, because a fixed ceiling of 4000 is wrong for every
        microphone that is not the one it was picked for: a webcam mic across
        the desk peaks around 1300, which drew speech as a few pixels of dirt
        on the centre line. The floor is the silence threshold, so a pause is
        genuinely flat rather than a low hum — that is the thing worth seeing.

        The ceiling attacks instantly and releases slowly, so one loud syllable
        does not permanently shrink everything after it.
        """
        floor = self.app.cfg.silence_threshold
        target = max(max(self._wave, default=0.0) * 1.1, floor * 3.0, 400.0)
        if target > self._wave_ceiling:
            self._wave_ceiling = target
        else:
            self._wave_ceiling += (target - self._wave_ceiling) * 0.05
        return floor, max(self._wave_ceiling, floor + 1.0)

    def _draw_waveform(self, c, W, H):
        """A scrolling waveform, not a bar: the shape of the last few seconds
        says whether the microphone is hearing you far better than a level that
        only ever shows the present instant."""
        C = self.C
        mid = H // 2
        bars = len(self._wave)
        step = W / float(bars)
        floor, ceiling = self._wave_scale()
        span = ceiling - floor
        room = H / 2 - 3

        c.create_line(0, mid, W, mid, fill=C["sep"])
        for i, rms in enumerate(self._wave):
            norm = (rms - floor) / span
            if norm <= 0:
                continue
            # Slightly compressed rather than linear: speech spends most of its
            # time well below its own peak, and a linear meter renders that as
            # nothing.
            amp = max(min(norm, 1.0) ** 0.7 * room, 1.0)
            x = i * step
            # The newest samples are the ones being spoken now; fading the tail
            # is what makes the strip read as moving rather than jittering.
            colour = C["accent"] if i > bars * 0.55 else theme.mix(
                C["accent"], C["bg"], 0.45)
            c.create_line(x, mid - amp, x, mid + amp, fill=colour,
                          width=max(1, int(step) - 1))

        # No threshold tick: at a typical threshold of 200 against a 4000
        # ceiling it lands a single pixel off the centre line, where it reads
        # as dirt rather than as information. The settings window shows the
        # threshold against the measured noise floor, which is where that
        # comparison actually helps.

        hold = self.app.cfg.silence_duration
        if hold > 0 and self._silence_since is not None:
            # The run-up to the automatic stop. Without it a recording that
            # ends itself mid-thought looks like a crash.
            frac = min((time.monotonic() - self._silence_since) / hold, 1.0)
            c.create_line(0, H - 1, W, H - 1, fill=C["sep"])
            c.create_line(0, H - 1, W * frac, H - 1,
                          fill=C["trans"] if frac < 0.75 else C["rec"], width=2)

    def _draw_ticker(self, c, W, H):
        C = self.C
        y = H // 2
        if not self._ticker_text:
            # Nothing to show yet: a sweeping hairline, which says "working"
            # without pretending to know how far along it is.
            span = W * 0.28
            x = -span + (W + span) * self._sweep
            c.create_line(0, y, W, y, fill=C["sep"])
            c.create_line(max(0, x), y, min(W, x + span), y,
                          fill=C["accent"], width=2)
            return

        frac = min((time.time() - self._ticker_t0) / TICKER_SECONDS, 1.0)
        shown = self._ticker_text[:max(1, int(len(self._ticker_text) * frac))]
        right = W - 10
        # Anchored to the right and allowed to overflow off the left edge: the
        # end of the sentence — the part still arriving — is always readable.
        c.create_text(right, y, text=shown, anchor="e", fill=C["text"],
                      font=self._stage_font)
        if frac < 1.0:
            caret_x = right + 3
            c.create_line(caret_x, y - 8, caret_x, y + 8, fill=C["accent"],
                          width=2)
        if self._stage_font.measure(shown) > W - 16:
            # Dithered plates instead of a gradient: Tk canvas items are
            # opaque, but a stippled fill covers a fraction of the pixels, and
            # four of them stepping down reads as a fade at this size.
            for i, stipple in enumerate(("gray75", "gray50", "gray25", "gray12")):
                c.create_rectangle(i * 7, 0, (i + 1) * 7, H, outline="",
                                   fill=C["bg"], stipple=stipple)

    # ── Footer: keycap hints ──

    def _hints(self):
        """Structured key/label pairs, rendered as keycaps rather than a
        pipe-separated command line."""
        k = self.app.ptt_label
        mode = self._mode()
        if mode == "recording":
            # The run-up to the automatic stop is on the waveform, so it does
            # not also need a chip here — the footer has ~360px to work with.
            return [(k, "Transcribe"), ("↵", "+ Enter"), ("Esc", "Discard")]
        if mode == "history":
            return [("Esc", "Hide")]
        if mode == "benchmark":
            return [("✕", "Close"), ("Esc", "Hide")]
        if mode == "error":
            return [("Esc", "Dismiss")]
        return [(k, "Double-tap to record"), ("Esc", "Hide")]

    def _draw_hints(self):
        c = self.hint_cv
        try:
            c.delete("all")
        except tk.TclError:
            return
        C = self.C
        key_font = ("Segoe UI Semibold", 8)
        label_font = ("Segoe UI", 8)

        items = list(self._hints())
        while True:
            widths = []
            for key, label in items:
                kw = self._key_font.measure(key) + 12
                widths.append((kw, kw + 4 + self._label_font.measure(label)))
            total = sum(w for _kw, w in widths) + 12 * (len(items) - 1)
            # Rather than let the row run off both edges, drop the least
            # important hint — they are ordered most-useful-first.
            if total <= OV_W - 16 or len(items) == 1:
                break
            items.pop()

        x = max(8, (OV_W - total) // 2)
        y = 14
        for (key, label), (kw, full) in zip(items, widths):
            _round_rect(c, x, y - 8, x + kw, y + 8, r=4,
                        fill=C["key_bg"], outline=C["key_edge"])
            c.create_text(x + kw / 2, y, text=key, fill=C["key_fg"],
                          font=key_font)
            c.create_text(x + kw + 4, y, text=label, anchor="w",
                          fill=C["faint"], font=label_font)
            x += full + 12

    # ── Queue ──

    def _rebuild_queue(self):
        for w in self.queue_item_labels:
            w.destroy()
        self.queue_item_labels = []

        jobs = self.app.jobs.active()
        if len(jobs) < 2:
            return

        waiting = sum(1 for j in jobs if j.status != JobStatus.TRANSCRIBING)
        self.queue_hdr_lbl.config(text=f"{waiting} WAITING")

        _qf = ("Consolas", 8)
        for job in jobs[:MAX_QUEUE_VISIBLE]:
            colour = (self.C["trans"] if job.status == JobStatus.TRANSCRIBING
                      else self.C["faint"])
            ts = time.strftime("%H:%M:%S", time.localtime(job.created_at))

            # Not registered with _themed: these are rebuilt from scratch on
            # every redraw, and a registry that collects short-lived widgets
            # grows for the life of the process.
            row = tk.Frame(self.queue_items_frame, bg=self.C["bg"])
            row.pack(fill="x")
            row.columnconfigure(3, weight=1)
            self.queue_item_labels.append(row)

            for i, (txt, w) in enumerate([(ts, 9), (f"{job.audio_duration:.1f}s", 5),
                                          ((job.app_name or "?")[:10], 10)]):
                tk.Label(row, text=txt, bg=self.C["bg"], fg=colour, font=_qf,
                         width=w, anchor="w").grid(row=0, column=i, sticky="w")
            tk.Label(row, text=job.window_name, bg=self.C["bg"], fg=colour,
                     font=_qf, anchor="w").grid(row=0, column=3, sticky="we")

            xbtn = tk.Label(row, text="✕", bg=self.C["bg"], fg=self.C["danger"],
                            font=("Segoe UI", 8, "bold"), cursor="hand2")
            xbtn.grid(row=0, column=4, sticky="e", padx=(4, 2))
            xbtn.bind("<Button-1>", lambda e, j=job: self.app.cancel_job(j))

        if len(jobs) > MAX_QUEUE_VISIBLE:
            extra = tk.Label(self.queue_items_frame,
                             text=f"  +{len(jobs) - MAX_QUEUE_VISIBLE} more…",
                             bg=self.C["bg"], fg=self.C["faint"], font=_qf)
            extra.pack(anchor="w")
            self.queue_item_labels.append(extra)

    # ── History ──

    def _clear_history_clicked(self):
        """Arms first, clears second. Fifty transcripts are not worth losing to
        one stray click on a panel that appears under the pointer."""
        if not self._clear_armed:
            if not self.app.jobs.history_count():
                return
            self._clear_armed = True
            self.h_clear.config(text="Clear all?", fg=self.C["danger"])
            self._clear_disarm_job = self.root.after(3000, self._disarm_clear)
            return
        self._disarm_clear()
        self.app.jobs.clear_history()
        self._expanded.clear()
        self._rebuild_history()
        self._show_overlay()

    def _disarm_clear(self):
        if self._clear_disarm_job:
            self.root.after_cancel(self._clear_disarm_job)
            self._clear_disarm_job = None
        self._clear_armed = False
        try:
            self.h_clear.config(text="Clear All", fg=self.C["faint"])
        except tk.TclError:
            pass

    def _pack_history_scroll(self, needed):
        """The scrollbar has to be packed BEFORE the canvas.

        The canvas is packed with expand=True, so it claims the whole cavity;
        anything packed after it is allocated the nothing that is left, which
        is why the scrollbar was invisible however long the list got.
        """
        self._history_scrollbar.pack_forget()
        self._history_canvas.pack_forget()
        if needed:
            self._history_scrollbar.pack(side="right", fill="y")
        self._history_canvas.pack(side="left", fill="both", expand=True)

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._on_history_mousewheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _monogram(self, parent, name):
        """The fallback when an application has no extractable icon — a plate
        with its initial, which still tells the rows apart at a glance."""
        C = self.C
        cv = tk.Canvas(parent, width=18, height=18, bg=C["bg"],
                       highlightthickness=0)
        letter = (name or "?").strip()[:1].upper() or "?"
        tint = theme.mix(C["accent"], C["bg"], 0.55)
        _round_rect(cv, 1, 1, 17, 17, r=4, fill=tint, outline="")
        cv.create_text(9, 9, text=letter, fill=C["text"],
                       font=("Segoe UI Semibold", 8))
        return cv

    def _rebuild_history(self):
        for w in self.history_item_widgets:
            w.destroy()
        self.history_item_widgets = []

        entries = self.app.jobs.history()
        items = list(reversed(entries))
        self.h_count.config(text=str(len(items)) if items else "")
        self.h_clear.config(
            text="Clear all?" if self._clear_armed else "Clear All",
            fg=self.C["danger"] if self._clear_armed else self.C["faint"])

        if not items:
            wrap = tk.Frame(self.history_items_frame, bg=self.C["bg"])
            wrap.pack(fill="x", pady=(40, 0))
            self.history_item_widgets.append(wrap)
            tk.Label(wrap, text="No transcriptions yet", bg=self.C["bg"],
                     fg=self.C["dim"], font=("Segoe UI Semibold", 10)).pack()
            tk.Label(wrap, text="Dictation that cannot be typed lands here.",
                     bg=self.C["bg"], fg=self.C["faint"],
                     font=("Segoe UI", 8)).pack(pady=(2, 0))
            self._pack_history_scroll(False)
            self._bind_wheel(wrap)
            return

        C = self.C
        last_group = None
        for idx, entry in enumerate(items):
            group = _day_label(entry.get("at"))
            if group != last_group:
                last_group = group
                head = tk.Frame(self.history_items_frame, bg=self.C["bg"])
                head.pack(fill="x", pady=(6, 2))
                self.history_item_widgets.append(head)
                tk.Label(head, text=group.upper(), bg=C["bg"], fg=C["faint"],
                         font=("Segoe UI Semibold", 7), anchor="w").pack(
                             side="left")
                self._bind_wheel(head)

            real_idx = len(items) - 1 - idx
            row = self._build_history_row(entry, real_idx)
            self.history_item_widgets.append(row)

        # Measured, not guessed from the row count: a row is one line or four
        # depending on how long the transcript is.
        self.history_items_frame.update_idletasks()
        self._pack_history_scroll(
            self.history_items_frame.winfo_reqheight()
            > self._history_canvas.winfo_height())
        self._history_canvas.yview_moveto(0.0)

    def _build_history_row(self, entry, real_idx):
        C = self.C
        text = entry.get("text") or ""
        row = tk.Frame(self.history_items_frame, bg=self.C["bg"])
        row.pack(fill="x", pady=(2, 4))
        row.columnconfigure(1, weight=1)

        icon = winicon.photo(entry.get("bundle") or "", 18, master=self.root)
        if icon is not None:
            plate = tk.Label(row, image=icon, bg=C["bg"])
            plate.image = icon      # the only reference keeping it alive
        else:
            plate = self._monogram(row, entry.get("app"))
        plate.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 8))

        expanded = real_idx in self._expanded
        clamped = len(text) > CLAMP_CHARS and not expanded
        shown = (text[:CLAMP_CHARS].rstrip() + "…") if clamped else text
        # The transcript is the largest, highest-contrast thing in the row,
        # because it is the only thing anyone is scanning for.
        body = tk.Label(row, text=shown or "(empty)", bg=C["bg"],
                        fg=C["text"] if text else C["faint"],
                        font=("Segoe UI", 9), justify="left", anchor="w",
                        wraplength=OV_W - 110)
        body.grid(row=0, column=1, sticky="we")

        meta_bits = [entry.get("app") or "?"]
        window = entry.get("window")
        if window and window != entry.get("app"):
            meta_bits.append(window)
        words = len(text.split())
        if words:
            meta_bits.append(f"{words} word{'s' if words != 1 else ''}")
        meta_row = tk.Frame(row, bg=C["bg"])
        meta_row.grid(row=1, column=1, sticky="we")
        tk.Label(meta_row, text=" · ".join(meta_bits), bg=C["bg"],
                 fg=C["faint"], font=("Segoe UI", 8), anchor="w").pack(side="left")
        if len(text) > CLAMP_CHARS:
            more = tk.Label(meta_row, text="Show less" if expanded else "Show more",
                            bg=C["bg"], fg=C["accent"], font=("Segoe UI", 8),
                            cursor="hand2")
            more.pack(side="left", padx=(8, 0))
            more.bind("<Button-1>",
                      lambda e, i=real_idx: self._toggle_expanded(i))

        # Fixed-width slot: the stamp cross-fades into the actions on hover, so
        # revealing them cannot resize the row under the pointer.
        slot = tk.Frame(row, bg=C["bg"], width=64, height=18)
        slot.grid(row=0, column=2, rowspan=2, sticky="ne")
        slot.grid_propagate(False)
        stamp = tk.Label(slot, text=entry.get("ts", "")[:5], bg=C["bg"],
                         fg=C["faint"], font=("Consolas", 8))
        stamp.place(relx=1.0, rely=0.0, anchor="ne")
        actions = tk.Frame(slot, bg=C["bg"])
        copy_btn = tk.Label(actions, text="Copy", bg=C["bg"], fg=C["accent"],
                            font=("Segoe UI", 8), cursor="hand2")
        copy_btn.pack(side="left")
        copy_btn.bind("<Button-1>",
                      lambda e, t=text, b=copy_btn: self._copy_text(t, b))
        del_btn = tk.Label(actions, text="✕", bg=C["bg"], fg=C["danger"],
                           font=("Segoe UI", 8, "bold"), cursor="hand2")
        del_btn.pack(side="left", padx=(6, 0))
        del_btn.bind("<Button-1>", lambda e, i=real_idx: self._delete_history(i))

        def enter(_e=None):
            stamp.place_forget()
            actions.place(relx=1.0, rely=0.0, anchor="ne")

        def leave(_e=None):
            # Tk sends Leave when the pointer crosses onto a child, so trust
            # the pointer position rather than the event.
            x, y = row.winfo_pointerxy()
            rx, ry = row.winfo_rootx(), row.winfo_rooty()
            if (rx <= x <= rx + row.winfo_width()
                    and ry <= y <= ry + row.winfo_height()):
                return
            actions.place_forget()
            stamp.place(relx=1.0, rely=0.0, anchor="ne")

        for widget in (row, body, meta_row, plate, slot, stamp):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
        self._bind_wheel(row)
        return row

    def _toggle_expanded(self, idx):
        if idx in self._expanded:
            self._expanded.discard(idx)
        else:
            self._expanded.add(idx)
        self._rebuild_history()

    def _copy_text(self, text, button=None):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        if button is not None:
            # Confirms, because a clipboard write is otherwise completely
            # invisible and the only way to check is to paste somewhere.
            try:
                button.config(text="Copied ✓", fg=self.C["ok"])
                self.root.after(1200, lambda: button.winfo_exists()
                                and button.config(text="Copy", fg=self.C["accent"]))
            except tk.TclError:
                pass

    def _delete_history(self, idx):
        self.app.jobs.delete_history(idx)
        self._expanded.clear()
        self._rebuild_history()
        self._show_overlay()

    # ── Benchmark panel ──

    #: The AppKit overlay is an HTML page with no per-model table, so the
    #: benchmark UI is Tk-only. App.benchmark_supported() reads this.
    supports_benchmark = True

    def show_benchmark(self, job):
        self._benchmark_view_job = job
        self.benchmark_mode = True
        self.history_mode = False
        # The panel takes the overlay; a live recording would fight it for the
        # same window, and its audio is not what the benchmark is measuring.
        if self.app.recording:
            self.app.stop_recording(discard=True)
            self._cancel_timer_blink()
            log("Recording discarded — the benchmark panel took the overlay")
        self._rebuild_benchmark()
        self._show_overlay()

    def refresh_benchmark(self):
        job = (self.app.benchmark_job or self.app.last_benchmark
               or self._benchmark_view_job)
        if job is None:
            return
        self._benchmark_view_job = job
        if not self.benchmark_mode:
            return
        self._rebuild_benchmark()
        self._show_overlay()

    def _close_benchmark(self):
        # Only the panel closes — a run still in flight keeps going.
        self.benchmark_mode = False
        self._benchmark_view_job = None
        self.hide()

    def _rebuild_benchmark(self):
        for w in self.benchmark_item_widgets:
            w.destroy()
        self.benchmark_item_widgets = []

        job = self._benchmark_view_job
        if job is None:
            return

        created = time.strftime("%H:%M:%S", time.localtime(job.created_at))
        self.bench_title_lbl.config(
            text=f"{created}  •  {job.audio_duration:.1f}s  •  {job.app_name or '?'}")

        _f = ("Consolas", 8)
        C = self.C
        icons = {"waiting": ("○", C["faint"]),
                 "loading": ("↻", C["bar_mid"]),
                 "running": ("▶", C["trans"]),
                 "done":    ("✓", C["ok"]),
                 "error":   ("✕", C["danger"])}

        for r in job.results:
            row = tk.Frame(self.benchmark_items_frame, bg=self.C["bg"])
            row.pack(fill="x")
            row.columnconfigure(2, weight=1)
            self.benchmark_item_widgets.append(row)

            icon, icon_col = icons.get(r.status, ("?", C["faint"]))
            tk.Label(row, text=f"{icon} {r.model}", bg=C["bg"], fg=icon_col,
                     font=_f, width=18, anchor="w").grid(row=0, column=0, sticky="w")

            if r.transcribe_secs is not None:
                time_txt, time_fg = f"{r.transcribe_secs:.2f}s", C["ok"]
            elif r.status in ("running", "loading"):
                time_txt, time_fg = "…", C["bar_mid"]
            elif r.status == "error":
                time_txt, time_fg = "ERR", C["danger"]
            else:
                time_txt, time_fg = "—", C["faint"]
            tk.Label(row, text=time_txt, bg=C["bg"], fg=time_fg, font=_f,
                     width=8, anchor="w").grid(row=0, column=1, sticky="w")

            if r.status == "done" and r.text is not None:
                preview, preview_fg = (r.text[:40] + ("…" if len(r.text) > 40 else ""),
                                       C["text"])
            elif r.status == "error":
                preview, preview_fg = (r.error or "error")[:40], C["danger"]
            elif r.status == "loading":
                preview, preview_fg = "loading model…", C["bar_mid"]
            elif r.status == "running":
                preview, preview_fg = "transcribing…", C["bar_mid"]
            else:
                preview, preview_fg = "", C["faint"]

            lbl = tk.Label(row, text=preview, bg=C["bg"], fg=preview_fg,
                           font=_f, anchor="w")
            lbl.grid(row=0, column=2, sticky="we", padx=(4, 0))

            if r.status == "done" and r.text:
                tip = r.text[:500] + ("…" if len(r.text) > 500 else "")
                lbl.bind("<Enter>", lambda e, p=tip: self._show_tooltip(e, p))
                lbl.bind("<Leave>", lambda e: self._hide_tooltip())
                copy_btn = tk.Label(row, text="Copy", bg=C["bg"], fg=C["accent"],
                                    font=("Segoe UI", 8), cursor="hand2")
                copy_btn.grid(row=0, column=3, sticky="e", padx=(4, 0))
                copy_btn.bind("<Button-1>",
                              lambda e, t=r.text, b=copy_btn: self._copy_text(t, b))

    # ── Tooltip ──

    def _show_tooltip(self, event, text):
        self._hide_tooltip()
        x = event.widget.winfo_rootx()
        self._tooltip = tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg=self.C["sep"])
        tk.Label(tw, text=text, bg=self.C["bar_bg"], fg=self.C["text"],
                 font=("Segoe UI", 9), wraplength=360, justify="left",
                 padx=8, pady=5).pack(padx=1, pady=1)
        tw.update_idletasks()
        tw_w, tw_h = tw.winfo_reqwidth(), tw.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        widget_y = event.widget.winfo_rooty()
        y = widget_y - tw_h - 4
        if y < 0:
            y = widget_y + event.widget.winfo_height() + 4
        if y + tw_h > sh:
            y = sh - tw_h - 4
        if x + tw_w > sw:
            x = sw - tw_w - 4
        x = max(x, 4)
        tw.geometry(f"+{x}+{y}")

    def _hide_tooltip(self):
        if self._tooltip:
            try:
                self._tooltip.destroy()
            except tk.TclError:
                pass
            self._tooltip = None

    # ── Animation ──

    def _cancel_timer_blink(self):
        for j in (self._timer_job, self._blink_job, self._level_job,
                  self._ticker_job):
            if j:
                self.root.after_cancel(j)
        self._timer_job = self._blink_job = self._level_job = None
        self._ticker_job = None
        self._level = 0.0

    def _tick(self):
        e = time.time() - self._t0
        self.timer.config(text=f"{int(e) // 60}:{int(e) % 60:02d}")
        self._timer_job = self.root.after(500, self._tick)

    def _blink(self):
        self._blink_on = not self._blink_on
        self.dot.config(fg=self.C["rec"] if self._blink_on else self.C["bg"])
        self._blink_job = self.root.after(500, self._blink)

    # ── GPU graph ──

    def _start_gpu_refresh(self):
        if not self.app.backend.gpu_available or self._gpu_refresh_job:
            return
        self._refresh_gpu()

    def _stop_gpu_refresh(self):
        if self._gpu_refresh_job:
            self.root.after_cancel(self._gpu_refresh_job)
            self._gpu_refresh_job = None

    def _refresh_gpu(self):
        self._refresh_gpu_now()
        self._gpu_refresh_job = self.root.after(1000, self._refresh_gpu)

    def _refresh_gpu_now(self):
        history = self.app.gpu_series()
        if history:
            pct = int(history[-1] * 100)
            colour = self.C["ok"] if pct < 50 else (
                self.C["bar_mid"] if pct < 80 else self.C["bar_hi"])
            self.gpu_pct.config(text=f"{pct}%", fg=colour)
        self._update_gpu_chip()
        if self._gpu_graph_on():
            self._draw_gpu_graph(history)

    def _draw_gpu_graph(self, history):
        c = self.gpu_cv
        try:
            c.delete("all")
        except tk.TclError:
            return
        W, H = int(c["width"]), int(c["height"])
        for p in (0.25, 0.5, 0.75):
            y = int(H * (1.0 - p))
            c.create_line(0, y, W, y, fill=self.C["sep"])

        n = len(history)
        if n < 2:
            return

        graph_w = int(W * (n / 60.0))
        x_off = W - graph_w
        step = graph_w / max(n - 1, 1)

        points = [(x_off + int(i * step), int(H * (1.0 - v)))
                  for i, v in enumerate(history)]
        last_x = x_off + int((n - 1) * step)
        c.create_polygon([(x_off, H)] + points + [(last_x, H)],
                         fill=self.C["gpu_fill"], outline="")
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            c.create_line(x1, y1, x2, y2, fill=self.C["ok"], width=2)

    # ── Tray ──

    def _build_tray_menu(self):
        app = self.app
        items = []
        if app.model_switching:
            # The catalogue still belongs to the outgoing engine while its
            # replacement loads. Showing those entries would invite a click
            # that cannot work; say what is happening instead.
            items.append(pystray.MenuItem("Switching engine…", None, enabled=False))
        for info in ([] if app.model_switching else app.model_catalog()):
            label = f"{info.name}\t{info.size_label}"
            if not info.downloaded:
                label = "↓ " + label

            def make_act(n):
                def act(icon, item):
                    app.request_model(n)
                return act

            def make_checked(n):
                return lambda item: n == app.model_name

            items.append(pystray.MenuItem(label, make_act(info.name),
                                          checked=make_checked(info.name)))

        local = app.engine_kind == "local"

        engines = pystray.Menu(
            pystray.MenuItem(
                "Local (this machine)",
                lambda icon, item: app.request_engine("local"),
                checked=lambda item: app.engine_kind == "local", radio=True),
            pystray.MenuItem(
                "OpenAI API",
                lambda icon, item: app.request_engine("openai"),
                checked=lambda item: app.engine_kind == "openai", radio=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: ("Set API key…" if not app.cfg.api_key_source
                              else f"Replace API key ({app.cfg.api_key_source})…"),
                lambda icon, item: self.ask_api_key()),
        )

        languages = pystray.Menu(*[
            pystray.MenuItem(
                f"{name} ({code})",
                (lambda c: lambda icon, item: app.set_language(c))(code),
                checked=(lambda c: lambda item: c == app.cfg.language)(code),
                radio=True)
            for code, name in LANGUAGES])

        # Same submenu the macOS menu bar carries: the settings window is the
        # better place to change it, but a device that stopped working should
        # be switchable without opening a window first.
        mic_items = [pystray.MenuItem(
            "System default",
            lambda icon, item: app.set_config("input_device", None),
            checked=lambda item: app.cfg.input_device is None, radio=True)]
        for index, name in audio.list_input_devices():
            mic_items.append(pystray.MenuItem(
                f"[{index}] {name}",
                (lambda i: lambda icon, item: app.set_config("input_device", i))(index),
                checked=(lambda i: lambda item:
                         str(app.cfg.input_device) == str(i))(index),
                radio=True))
        microphones = pystray.Menu(*mic_items)

        idle = pystray.Menu(*[
            pystray.MenuItem(
                label,
                (lambda m: lambda icon, item: app.set_idle_unload(m))(minutes),
                checked=(lambda m: lambda item:
                         abs(app.cfg.idle_unload_seconds - m * 60) < 1)(minutes),
                radio=True)
            for minutes, label in IDLE_CHOICES])

        return pystray.Menu(
            pystray.MenuItem("WhisperType", None, enabled=False),
            pystray.MenuItem(lambda item: f"Record: double-tap {app.ptt_label}",
                             None, enabled=False),
            # What the overlay would say if it were up. The tray icon's colour
            # says something is wrong; this says what.
            pystray.MenuItem(
                lambda item: (f"⚠ {str(app.last_error)[:60]}" if app.last_error
                              else f"Loading {app.model_name}…"),
                None, enabled=False,
                visible=lambda item: bool(app.last_error) or not app.model_ready),
            pystray.Menu.SEPARATOR,
            # First, and the reason the submenus below are now a convenience
            # rather than the only route: a Win32 menu closes on every click,
            # so changing three things meant opening it three times.
            pystray.MenuItem("Settings…",
                             lambda icon, item: self.open_settings(),
                             default=True),
            pystray.MenuItem("Show history",
                             lambda icon, item: app.show_history()),
            pystray.MenuItem("Pause dictation",
                             lambda icon, item: app.set_paused(not app.paused),
                             checked=lambda item: app.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Engine", engines),
            pystray.MenuItem("Model", pystray.Menu(*items)),
            pystray.MenuItem("Language", languages),
            pystray.MenuItem("Microphone", microphones),
            pystray.MenuItem("Release model when idle", idle),
            pystray.MenuItem(
                "Download all models",
                lambda icon, item: app.download_all_models(),
                # Nothing to download when the models live on OpenAI's servers.
                visible=lambda item: local,
                enabled=lambda item: (not app.downloading_all
                                      and any(not i.downloaded
                                              for i in app.model_catalog())),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Benchmark next recording",
                lambda icon, item: app.toggle_benchmark_next(),
                checked=lambda item: app.benchmark_next,
            ),
            pystray.MenuItem(
                "Open last benchmark",
                lambda icon, item: app.open_last_benchmark(),
                enabled=lambda item: app.last_benchmark is not None,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda icon, item: self.open_log()),
            pystray.MenuItem("Restart WhisperType",
                             lambda icon, item: self.restart()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: app.quit()),
        )

    def open_log(self):
        try:
            os.startfile(LOG_PATH)          # noqa: S606 — Windows shell open
        except Exception as e:
            log(f"Could not open the log: {e}")

    def restart(self):
        """Relaunch, the way the macOS menu's Restart does through launchd.

        Windows has no supervisor to ask, and the single-instance mutex means a
        replacement cannot simply be spawned alongside us — it would show the
        "already running" box and exit. So a detached helper waits for this
        process to be gone and only then starts the new one.
        """
        script = Path(__file__).resolve().parents[2] / "whispertype.pyw"
        # Relaunch what was actually started, but only if it is one of the
        # known entry points. Trusting argv[0] blindly means anything that
        # imports this module — a probe, a test runner — gets relaunched in
        # its place, and since that thing may itself restart, in a loop.
        try:
            argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        except OSError:
            argv0 = None
        if (argv0 is not None and argv0.exists()
                and argv0.name in ("whispertype.pyw", "main.py")):
            script = argv0
        if not script.exists():
            log(f"Restart requested but {script} is missing — quitting instead")
            self.app.quit()
            return

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        # DEVNULL on both spawns is load-bearing, not tidiness: a DETACHED
        # child has no console and therefore no valid std handles, and
        # subprocess.Popen inside it then fails with "The handle is invalid"
        # — which is exactly how the first version of this managed to quit
        # without ever coming back.
        helper = (
            "import ctypes,subprocess,time\n"
            "SYNCHRONIZE=0x00100000\n"
            f"h=ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE,False,{os.getpid()})\n"
            "if h:\n"
            "    ctypes.windll.kernel32.WaitForSingleObject(h,20000)\n"
            "    ctypes.windll.kernel32.CloseHandle(h)\n"
            "time.sleep(1.0)\n"
            f"subprocess.Popen([{sys.executable!r},{str(script)!r}],"
            f"cwd={str(script.parent)!r},creationflags={CREATE_NO_WINDOW},"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)\n"
        )
        try:
            subprocess.Popen([sys.executable, "-c", helper],
                             creationflags=DETACHED_PROCESS
                             | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             close_fds=True)
            log("Restart: relauncher spawned, quitting")
        except Exception as e:
            log(f"Restart failed: {e}")
            return
        self.app.quit()

    # ── Settings window ──
    #
    # A Win32 tray menu cannot stay open across a click: pystray shows it with
    # TrackPopupMenuEx(TPM_RETURNCMD), which is modal — the OS tears the menu
    # down and *then* returns the chosen index, so the callback never runs
    # while the menu is up. radio=True only swaps the tick for a bullet
    # (MFT_RADIOCHECK); it changes nothing about dismissal. So changing three
    # settings meant opening the menu three times. A window does not have that
    # problem, and it can show everything at once.

    SET_W = 460          # content width; every row lines up to this
    CTRL_W = 300         # every control is this wide, so the column lines up

    def open_settings(self):
        self.root.after(0, self._open_settings)

    @property
    def settings_open(self):
        win = getattr(self, "_settings_win", None)
        try:
            return win is not None and bool(win.winfo_exists())
        except tk.TclError:
            return False

    def _titlebar_theme(self, win):
        """Ask DWM for a title bar that matches the panel.

        Tk draws the frame with the system theme, so a dark window otherwise
        gets a white caption bar stuck on top of it. 20 is
        DWMWA_USE_IMMERSIVE_DARK_MODE on current Windows; 19 was the attribute
        number before build 19041, so try that as a fallback.
        """
        try:
            win.update_idletasks()
            hwnd = int(win.wm_frame(), 16)
            value = ctypes.c_int(1 if self.C["dark"] else 0)
            for attribute in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.wintypes.HWND(hwnd), ctypes.c_int(attribute),
                        ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    return True
        except Exception as e:
            log(f"Could not theme the title bar: {e}")
        return False

    def _settings_option(self, parent, values, current, on_pick):
        """Dark dropdown in a filled shell. Returns (frame, var, repopulate).

        tk.OptionMenu's own indicator is a small raised rectangle that looks
        wrong on the panel, so it is switched off and replaced with a chevron
        of our own.
        """
        C = self.C
        shell = self._themed(tk.Frame(parent, width=self.CTRL_W, height=28),
                             bg="bar_bg")
        shell.pack_propagate(False)

        var = tk.StringVar(value=current)
        box = tk.OptionMenu(shell, var, *(values or ["—"]),
                            command=lambda v: on_pick(v))
        box.configure(highlightthickness=0, bd=0, relief="flat",
                      indicatoron=False, anchor="w", padx=10,
                      font=("Segoe UI", 9), cursor="hand2")
        # Registered rather than configured once: the theme control lives in
        # this very window, so it has to repaint itself when it is used.
        self._themed(box, bg="bar_bg", fg="text", activebackground="bar_bg",
                     activeforeground="accent", disabledforeground="faint")
        box["menu"].configure(bd=0, relief="flat", font=("Segoe UI", 9))
        self._themed(box["menu"], bg="bar_bg", fg="text",
                     activebackground="sep", activeforeground="text")
        chevron = self._themed(tk.Label(shell, text="⌄", font=("Segoe UI", 10)),
                               bg="bar_bg", fg="dim")
        chevron.pack(side="right", padx=(0, 10))
        box.pack(side="left", fill="both", expand=True)

        def repopulate(new_values, new_current, enabled=True):
            menu = box["menu"]
            menu.delete(0, "end")
            for v in (new_values or ["—"]):
                menu.add_command(
                    label=v, command=lambda value=v: (var.set(value), on_pick(value)))
            var.set(new_current)
            box.configure(state="normal" if enabled else "disabled",
                          fg=C["text"] if enabled else C["faint"])
            chevron.configure(fg=C["dim"] if enabled else C["bar_bg"])

        return shell, var, repopulate

    def _settings_slider(self, parent, *, lo, hi, step, value, unit,
                         on_change, marker=None):
        """Slider in the same shell as the dropdowns, so the column lines up."""
        shell = self._themed(
            tk.Frame(parent, width=self.CTRL_W, height=_Slider.H), bg="bar_bg")
        shell.pack_propagate(False)
        slider = _Slider(shell, self.C, lo=lo, hi=hi, step=step, value=value,
                         unit=unit, on_change=on_change,
                         width=self.CTRL_W, marker=marker)
        slider.pack(fill="both", expand=True)
        self._sliders.append(slider)
        return shell, slider

    def _settings_button(self, parent, text, command):
        return self._themed(
            tk.Button(parent, text=text, command=command, bd=0, relief="flat",
                      padx=14, pady=4, cursor="hand2", font=("Segoe UI", 9)),
            bg="bar_bg", fg="text", activebackground="sep",
            activeforeground="text")

    def _open_settings(self):
        if getattr(self, "_settings_win", None) is not None:
            try:
                self._settings_win.deiconify()
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
            except tk.TclError:
                pass                       # it was closed behind our back

        app, C = self.app, self.C
        win = tk.Toplevel(self.root)
        self._settings_win = win
        self._set = {}                      # widgets the refresh needs
        win.title("WhisperType settings")
        win.configure(bg=C["bg"])
        win.resizable(False, False)
        # Before the first map, so the frame is drawn in the right colour
        # rather than repainted from white a moment later.
        self._titlebar_theme(win)

        def on_close():
            self._settings_win = None
            self._set = {}
            win.destroy()
            self._refresh_own_hwnds()
        win.protocol("WM_DELETE_WINDOW", on_close)
        win.bind("<Escape>", lambda e: on_close())

        # Scrollable body. Six sections of fixed-height rows come to a little
        # over a thousand pixels, which does not fit a 1080p screen once the
        # taskbar has had its share — and fits far less on a laptop. The
        # scrollbar only appears when the content genuinely does not fit.
        shell = self._themed(tk.Frame(win), bg="bg")
        shell.pack(fill="both", expand=True)
        canvas = self._themed(tk.Canvas(shell, highlightthickness=0), bg="bg")
        vbar = tk.Scrollbar(shell, orient="vertical", command=canvas.yview,
                            bg=C["bar_bg"], troughcolor=C["bg"],
                            activebackground=C["sep"], bd=0, relief="flat",
                            highlightthickness=0, width=10)
        canvas.configure(yscrollcommand=vbar.set)
        body = self._themed(tk.Frame(canvas), bg="bg")
        body_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(body_id, width=e.width))

        def on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        # bind_all would fight the overlay's own wheel handler; the settings
        # window is a separate toplevel, so bind_class on its tree is enough.
        win.bind("<MouseWheel>", on_wheel)

        # ── Header ──
        head = self._themed(tk.Frame(body), bg="bg")
        head.pack(fill="x", padx=22, pady=(18, 0))
        self._themed(tk.Label(head, text="Settings",
                              font=("Segoe UI Semibold", 15)),
                     bg="bg", fg="text").pack(anchor="w")
        self._themed(
            tk.Label(head, text="Every change takes effect on your next "
                                "dictation. Only the push-to-talk key needs a "
                                "restart.",
                     font=("Segoe UI", 8), justify="left", anchor="w",
                     wraplength=self.SET_W),
            bg="bg", fg="dim").pack(anchor="w", fill="x", pady=(3, 0))

        def section(title):
            wrap = self._themed(tk.Frame(body), bg="bg")
            wrap.pack(fill="x", padx=22, pady=(16, 0))
            self._themed(tk.Frame(wrap, height=1), bg="sep").pack(
                fill="x", pady=(0, 10))
            self._themed(tk.Label(wrap, text=title.upper(),
                                  font=("Segoe UI Semibold", 8)),
                         bg="bg", fg="accent").pack(anchor="w")
            inner = self._themed(tk.Frame(wrap), bg="bg")
            inner.pack(fill="x", pady=(8, 0))
            inner.columnconfigure(1, weight=1)
            return inner

        def row(parent, n, text, widget, hint=None):
            self._themed(tk.Label(parent, text=text, font=("Segoe UI", 9),
                                  anchor="w", width=15),
                         bg="bg", fg="text").grid(row=n, column=0, sticky="w",
                                                  pady=(0, 2))
            widget.grid(row=n, column=1, sticky="w", pady=(0, 2))
            if hint:
                # height in text lines, fixed: these labels change at runtime
                # (the engine one especially), and letting them reflow made the
                # whole window jump a row taller or shorter mid-click.
                lbl = self._themed(
                    tk.Label(parent, text=hint, font=("Segoe UI", 8), anchor="nw",
                             justify="left", wraplength=self.CTRL_W, height=2),
                    bg="bg", fg="dim")
                lbl.grid(row=n + 1, column=1, sticky="w", pady=(0, 6))
                return lbl
            self._themed(tk.Frame(parent, height=6), bg="bg").grid(
                row=n + 1, column=1)
            return None

        # ══ Transcription ══
        sec = section("Transcription")

        engines = self._themed(tk.Frame(sec), bg="bg")
        engine_var = tk.StringVar(value=app.engine_kind)
        for value, text in (("local", "Local"), ("openai", "OpenAI API")):
            self._themed(
                tk.Radiobutton(engines, text=text, value=value,
                               variable=engine_var,
                               command=lambda: app.request_engine(engine_var.get()),
                               highlightthickness=0, bd=0, font=("Segoe UI", 9)),
                bg="bg", fg="text", selectcolor="bar_bg", activebackground="bg",
                activeforeground="text").pack(side="left", padx=(0, 18))
        engine_hint = row(sec, 0, "Engine", engines,
                          "Local runs on your GPU. The API uploads the audio "
                          "and charges per minute.")
        self._set["engine_var"] = engine_var
        self._set["engine_hint"] = engine_hint

        names = [m.name for m in app.model_catalog()] or [app.model_name]
        model_box, model_var, model_fill = self._settings_option(
            sec, names, app.model_name, app.request_model)
        row(sec, 2, "Model", model_box)
        self._set["model_fill"] = model_fill

        lang_labels = [f"{name} ({code})" for code, name in LANGUAGES]
        current_lang = next((f"{n} ({c})" for c, n in LANGUAGES
                             if c == app.cfg.language), app.cfg.language)
        lang_box, _, _ = self._settings_option(
            sec, lang_labels, current_lang,
            lambda v: app.set_language(v.rsplit("(", 1)[1].rstrip(")")))
        row(sec, 4, "Language", lang_box)

        # ══ Audio ══
        sec = section("Audio")

        devices = audio.list_input_devices()
        dev_labels = ["System default"] + [f"[{i}] {n}" for i, n in devices]
        cur_dev = app.cfg.input_device
        cur_label = next((f"[{i}] {n}" for i, n in devices
                          if str(i) == str(cur_dev)), "System default")

        def pick_device(v):
            app.set_config("input_device", None if v == "System default"
                           else int(v.split("]")[0].lstrip("[")))
        dev_box, _, _ = self._settings_option(sec, dev_labels, cur_label, pick_device)
        row(sec, 0, "Microphone", dev_box,
            "The same microphone often appears more than once, over different "
            "drivers. The index picks which one is used.")

        stop_box, _ = self._settings_slider(
            sec, lo=0, hi=15, step=0.5, value=app.cfg.silence_duration,
            unit="s",
            on_change=lambda v: app.set_config("silence_duration", v))
        row(sec, 2, "Stop after silence", stop_box,
            "0 disables it — then only the key ends a dictation.")

        # The slider covers the useful range, but a hand-edited config can sit
        # above it; widen rather than silently clamp the value.
        thr_now = app.cfg.silence_threshold
        thr_max = max(2000, int((thr_now + 499) // 500) * 500)
        thr_box, thr_slider = self._settings_slider(
            sec, lo=0, hi=thr_max, step=25, value=thr_now, unit="",
            on_change=lambda v: app.set_config("silence_threshold", v),
            marker=getattr(app, "noise_floor", None))
        # Non-empty: row() only creates the label when there is text, and
        # _refresh_settings fills in the measured level once it is known.
        floor_hint = row(sec, 4, "Silence threshold", thr_box,
                         "Anything quieter counts as silence.")
        self._set["thr_slider"] = thr_slider
        self._set["floor_hint"] = floor_hint

        # ══ Appearance ══
        #
        # The macOS panel is built out of system colours and needs no setting
        # at all; Tk paints literal values, so following the system is a thing
        # this window has to offer explicitly.
        sec = section("Appearance")
        theme_labels = {"auto": "Follow Windows", "dark": "Dark", "light": "Light"}
        theme_box, _, _ = self._settings_option(
            sec, list(theme_labels.values()), theme_labels.get(app.cfg.theme,
                                                               "Follow Windows"),
            lambda v: self._pick_theme(
                next(k for k, t in theme_labels.items() if t == v)))
        row(sec, 0, "Theme", theme_box,
            "Follow Windows re-reads the system light/dark setting every time "
            "the overlay appears.")

        gpu_var = tk.BooleanVar(value=bool(app.cfg.get("show_gpu_graph", False)))
        gpu_check = self._themed(
            tk.Checkbutton(sec, text="Show the GPU graph on the overlay",
                           variable=gpu_var, highlightthickness=0, bd=0,
                           font=("Segoe UI", 9),
                           command=lambda: self._pick_gpu_graph(gpu_var.get())),
            bg="bg", fg="text", selectcolor="bar_bg", activebackground="bg",
            activeforeground="text")
        row(sec, 2, "GPU graph", gpu_check,
            "Off by default: it is developer telemetry sitting above "
            "\"am I recording?\". The utilisation still shows as a chip.")
        if not app.backend.gpu_available:
            gpu_check.configure(state="disabled", disabledforeground=C["faint"])

        # ══ Memory ══
        sec = section("Memory")
        idle_labels = [text for _m, text in IDLE_CHOICES]
        cur_idle = next((t for m, t in IDLE_CHOICES
                         if abs(app.cfg.idle_unload_seconds - m * 60) < 1),
                        idle_labels[0])
        idle_box, _, _ = self._settings_option(
            sec, idle_labels, cur_idle,
            lambda v: app.set_idle_unload(
                next(m for m, t in IDLE_CHOICES if t == v)))
        row(sec, 0, "Release model", idle_box,
            "Reloading from the local cache takes about a second, so holding "
            "1.6 GB all day is usually the worse trade.")

        # ══ OpenAI ══
        sec = section("OpenAI")
        key_wrap = self._themed(tk.Frame(sec), bg="bg")
        key_state = self._themed(tk.Label(key_wrap, text="", font=("Segoe UI", 9)),
                                 bg="bg", fg="dim")
        key_state.pack(side="left", padx=(0, 12))
        self._settings_button(key_wrap, "Set / replace…",
                              self._ask_api_key).pack(side="left")
        # Home-relative: the absolute path wraps mid-directory and reads as a
        # broken string rather than a location.
        try:
            short_path = "~" + os.sep + str(API_KEY_PATH.relative_to(Path.home()))
        except ValueError:
            short_path = str(API_KEY_PATH)
        row(sec, 0, "API key", key_wrap,
            f"Kept in {short_path} — never in config.json, never in the repo.")
        self._set["key_state"] = key_state

        # ── Footer ──
        foot = self._themed(tk.Frame(body), bg="bg")
        foot.pack(fill="x", padx=22, pady=(18, 18))
        self._themed(tk.Frame(foot, height=1), bg="sep").pack(fill="x", pady=(0, 12))
        self._settings_button(foot, "Close", on_close).pack(side="right")

        self._refresh_settings()
        win.update_idletasks()

        area = self._work_area_under_cursor()
        if area:
            left, top, right, bottom = area
        else:
            left, top = 0, 0
            right = self.root.winfo_screenwidth()
            bottom = self.root.winfo_screenheight()

        content_h = body.winfo_reqheight()
        h = min(content_h, bottom - top - 60)
        scrolls = content_h > h
        vbar.pack_forget()
        canvas.pack_forget()
        if scrolls:
            # Before the canvas: the canvas expands into the whole cavity and
            # would leave the scrollbar nothing.
            vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        w = body.winfo_reqwidth() + (12 if scrolls else 0)

        x = left + (right - left - w) // 2
        y = top + max(20, (bottom - top - h) // 2)
        # Pin the size as well as the position: with the height fixed, no
        # amount of label text can make the window resize under the cursor.
        win.geometry(f"{w}x{h}+{max(x, left)}+{y}")
        self._titlebar_theme(win)
        win.lift()
        win.focus_force()
        self._refresh_own_hwnds()

    def _pick_theme(self, name):
        self.app.set_config("theme", name)
        self.set_theme(name)

    def _pick_gpu_graph(self, on):
        self.app.set_config("show_gpu_graph", bool(on))
        if self.visible:
            self._show_overlay()

    def _refresh_settings(self):
        """Re-sync the live parts. Tk thread only.

        The model list belongs to whichever engine is loaded, so switching
        engines has to repopulate it — otherwise the window keeps offering the
        previous engine's models, which is exactly the trap the tray menu had.
        """
        win = getattr(self, "_settings_win", None)
        if win is None:
            return
        try:
            if not win.winfo_exists():
                self._settings_win = None
                return
        except tk.TclError:
            self._settings_win = None
            return

        app = self.app
        switching = app.model_switching

        if "engine_var" in self._set:
            self._set["engine_var"].set(app.engine_kind)
        if "engine_hint" in self._set and self._set["engine_hint"]:
            self._set["engine_hint"].config(
                text="Switching engine — reloading the model…" if switching
                else "Local runs on your GPU. The API uploads the audio "
                     "and charges per minute.",
                fg=self.C["bar_mid"] if switching else self.C["dim"])
        if "model_fill" in self._set:
            if switching:
                self._set["model_fill"](["Switching…"], "Switching…", enabled=False)
            else:
                names = [m.name for m in app.model_catalog()] or [app.model_name]
                self._set["model_fill"](names, app.model_name, enabled=True)
        if "key_state" in self._set:
            source = app.cfg.api_key_source
            self._set["key_state"].config(
                text=f"set — from the {source}" if source else "not set",
                fg=self.C["ok"] if source else self.C["dim"])
        floor = getattr(app, "noise_floor", None)
        if "thr_slider" in self._set:
            self._set["thr_slider"].set_marker(floor)
        if "floor_hint" in self._set and self._set["floor_hint"]:
            # One decimal below 10: a noise-suppressing mic idles at 0.5, and
            # rounding that to "0" hides the very thing worth seeing.
            shown = ("" if floor is None
                     else f"{floor:.1f}" if floor < 10 else f"{floor:.0f}")
            self._set["floor_hint"].config(
                text="Anything quieter counts as silence."
                if floor is None else
                f"Anything quieter counts as silence. The ▎mark is where your "
                f"microphone idles: {shown}.")

    def ask_api_key(self):
        """Prompt for the OpenAI key. Runs on the Tk thread; the tray callback
        arrives on pystray's thread, so it has to be marshalled."""
        self.root.after(0, self._ask_api_key)

    def _ask_api_key(self):
        C = self.C
        win = tk.Toplevel(self.root)
        win.title("OpenAI API key")
        win.configure(bg=C["bg"])
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self._titlebar_theme(win)

        source = self.app.cfg.api_key_source
        blurb = (f"A key is already set (from the {source})."
                 if source else "No key is set yet.")
        tk.Label(win, text=blurb, bg=C["bg"], fg=C["dim"],
                 font=("Segoe UI", 9)).pack(padx=16, pady=(14, 2), anchor="w")
        tk.Label(win,
                 text=f"Stored in {API_KEY_PATH} — never in config.json,\n"
                      f"and never inside the repository.",
                 bg=C["bg"], fg=C["faint"], font=("Segoe UI", 8),
                 justify="left").pack(padx=16, pady=(0, 8), anchor="w")

        # show="•": the key must not be readable over the user's shoulder, and
        # it is never echoed to the log either.
        entry = tk.Entry(win, width=52, show="•", font=("Consolas", 10),
                         bg=C["bar_bg"], fg=C["text"], insertbackground=C["text"],
                         relief="flat", bd=6)
        entry.pack(padx=16, pady=(0, 4))
        entry.focus_set()

        status = tk.Label(win, text="", bg=C["bg"], fg=C["danger"],
                          font=("Segoe UI", 8))
        status.pack(padx=16, anchor="w")

        def save(_event=None):
            key = entry.get().strip()
            if not key:
                status.config(text="Enter a key, or press Cancel.")
                return
            if not key.startswith("sk-"):
                status.config(text="That does not look like an OpenAI key (sk-…).")
                return
            entry.delete(0, "end")
            if self.app.set_api_key(key):
                win.destroy()
                self.show_notice("API key saved")
            else:
                status.config(text="Could not write the key file — see the log.")

        buttons = tk.Frame(win, bg=C["bg"])
        buttons.pack(padx=16, pady=12, anchor="e")
        self._settings_button(buttons, "Cancel", win.destroy).pack(side="right")
        self._settings_button(buttons, "Save", save).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", save)
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        x = self.root.winfo_screenwidth() // 2 - win.winfo_reqwidth() // 2
        win.geometry(f"+{x}+240")
        win.lift()
        win.grab_set()

    def set_tray_state(self, state):
        self._tray_state = state
        if self.tray_icon:
            self.tray_icon.icon = make_tray_icon(state)

    def refresh_tray(self):
        """Rebuild the tray menu.

        pystray bakes the item list into a native HMENU when the menu is
        assigned; the callables it re-evaluates are only the per-item text and
        state. So anything that changes *which* items exist — switching engine,
        finishing a download — has to come through here or the menu keeps
        showing the previous engine's models.

        Safe from any thread: this touches no Tk, only app state and Win32.
        """
        # First, and before the tray_icon guard: the settings window shows the
        # same state and has to move with it whether or not a tray icon exists.
        # Touching Tk happens on the Tk thread — this is called from the worker.
        if getattr(self, "_settings_win", None) is not None:
            self.root.after(0, self._refresh_settings)
        if not self.tray_icon:
            return
        try:
            self.tray_icon.menu = self._build_tray_menu()
            self.tray_icon.icon = make_tray_icon(self._tray_state or "idle")
        except Exception as e:
            # A raise here used to vanish into the Tk callback handler and
            # leave the menu silently frozen on its previous contents.
            import traceback
            log(f"Tray menu rebuild failed: {e}")
            log(traceback.format_exc())

    def stop_tray(self):
        if self.tray_icon:
            self.tray_icon.stop()

    def _run_tray(self):
        icon = pystray.Icon("WhisperType", make_tray_icon("idle"),
                            "WhisperType", menu=self._build_tray_menu())
        self.tray_icon = icon
        icon.run()

    # ── Main loop ──

    def run(self):
        self._tray_thread = threading.Thread(target=self._run_tray, daemon=True)
        self._tray_thread.start()
        self.root.mainloop()

    def destroy(self):
        try:
            self.root.destroy()
        except Exception as e:
            log(f"Error destroying overlay: {e}")
