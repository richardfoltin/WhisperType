"""Windows UI — the original tkinter overlay plus the pystray tray icon.

The drawing code is carried over from the single-file version unchanged; only
the data sources moved (globals -> `self.app`) and the public methods now match
the platform-neutral UI interface that `whispertype.app.App` drives.

This module is Windows-only. On macOS Tk activates the application on every
window map, which would steal focus from the window we are about to type into.
"""
import threading
import time
import tkinter as tk

import pystray

from ..icon import make_tray_icon
from ..jobs import JobStatus
from ..log import log

OFF_SCREEN = "-9999+-9999"
OV_W = 380
MAX_QUEUE_VISIBLE = 5
VISIBLE_HISTORY = 8
HISTORY_ITEM_H = 22


class TkUI:
    C = {"bg": "#0f172a", "rec": "#f87171", "trans": "#fbbf24", "text": "#f1f5f9",
         "dim": "#64748b", "bar_bg": "#1e293b",
         "bar_lo": "#6ee7b7", "bar_mid": "#fbbf24", "bar_hi": "#f87171",
         "gpu_fill": "#1a3a2a", "sep": "#334155"}

    def __init__(self, app):
        self.app = app
        self.visible = False
        self.history_mode = False
        self._message = None        # transient notice / loading text

        self.root = tk.Tk()
        self.root.attributes("-alpha", 0.0)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.title("VR")
        self.root.configure(bg=self.C["bg"])
        self.root.resizable(False, False)
        self.root.geometry(OFF_SCREEN)

        cw = OV_W - 28

        # ── Title bar ──
        self.title_bar = tk.Frame(self.root, bg=self.C["bg"])
        self.title_bar.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(self.title_bar, text="WhisperType", bg=self.C["bg"],
                 fg=self.C["dim"], font=("Segoe UI", 8)).pack(side="left")
        exit_btn = tk.Label(self.title_bar, text="×", bg=self.C["bg"],
                            fg=self.C["dim"], font=("Segoe UI", 11), cursor="hand2")
        exit_btn.pack(side="right")
        exit_btn.bind("<Button-1>", lambda e: self.hide())
        self.title_bar.bind("<Button-1>", lambda e: setattr(self, "_d", (e.x, e.y)))
        self.title_bar.bind("<B1-Motion>", self._drag)

        # ── GPU graph ──
        self.gpu_frame = tk.Frame(self.root, bg=self.C["bg"])
        gpu_hdr = tk.Frame(self.gpu_frame, bg=self.C["bg"])
        gpu_hdr.pack(fill="x", padx=14, pady=(4, 2))
        tk.Label(gpu_hdr, text=app.backend.gpu_label, bg=self.C["bg"],
                 fg=self.C["dim"], font=("Segoe UI", 8, "bold")).pack(side="left")
        self.gpu_pct = tk.Label(gpu_hdr, text="--%", bg=self.C["bg"],
                                fg=self.C["bar_lo"], font=("Consolas", 9, "bold"))
        self.gpu_pct.pack(side="right")
        self.gpu_cv = tk.Canvas(self.gpu_frame, bg=self.C["bar_bg"],
                                width=cw, height=40, highlightthickness=0)
        self.gpu_cv.pack(padx=14, pady=(0, 4))

        # ── Recording ──
        self.rec_frame = tk.Frame(self.root, bg=self.C["bg"])
        self.rec_sep = tk.Frame(self.rec_frame, bg=self.C["sep"], height=1)
        self.rec_sep.pack(fill="x", padx=14, pady=(2, 6))

        hdr = tk.Frame(self.rec_frame, bg=self.C["bg"])
        hdr.pack(fill="x", padx=14, pady=(0, 4))
        self.dot = tk.Label(hdr, text="●", bg=self.C["bg"], fg=self.C["rec"],
                            font=("Segoe UI", 11))
        self.dot.pack(side="left")
        self.state_lbl = tk.Label(hdr, text="Recording", bg=self.C["bg"],
                                  fg=self.C["text"], font=("Segoe UI", 11, "bold"))
        self.state_lbl.pack(side="left", padx=(4, 0))
        self.timer = tk.Label(hdr, text="0:00", bg=self.C["bg"], fg=self.C["dim"],
                              font=("Consolas", 11))
        self.timer.pack(side="right")

        info_row = tk.Frame(self.rec_frame, bg=self.C["bg"])
        info_row.pack(fill="x", padx=14)
        self.model_lbl = tk.Label(info_row, text="", bg=self.C["bg"],
                                  fg=self.C["dim"], font=("Segoe UI", 8))
        self.model_lbl.pack(side="left")
        self.target_lbl = tk.Label(info_row, text="", bg=self.C["bg"],
                                   fg=self.C["dim"], font=("Segoe UI", 8))
        self.target_lbl.pack(side="right")

        self.level_cv = tk.Canvas(self.rec_frame, bg=self.C["bg"], width=cw,
                                  height=24, highlightthickness=0)
        self.level_cv.pack(padx=14, pady=(4, 2))

        self.hint = tk.Label(self.rec_frame, text="", bg=self.C["bg"],
                             fg=self.C["dim"], font=("Segoe UI", 8))
        self.hint.pack(fill="x", padx=14, pady=(2, 4))

        hdr.bind("<Button-1>", lambda e: setattr(self, "_d", (e.x, e.y)))
        hdr.bind("<B1-Motion>", self._drag)

        # ── Queue ──
        self.queue_frame = tk.Frame(self.root, bg=self.C["bg"])
        self.queue_sep = tk.Frame(self.queue_frame, bg=self.C["sep"], height=1)
        self.queue_sep.pack(fill="x", padx=14, pady=(2, 4))
        self.queue_hdr = tk.Frame(self.queue_frame, bg=self.C["bg"])
        self.queue_hdr.pack(fill="x", padx=14)
        _qf = ("Consolas", 8)
        for i, (txt, w) in enumerate([("TIME", 9), ("DUR", 5), ("APP", 8),
                                      ("WINDOW", 20), ("", 2)]):
            tk.Label(self.queue_hdr, text=txt, bg=self.C["bg"], fg=self.C["dim"],
                     font=_qf, width=w, anchor="w").grid(row=0, column=i, sticky="w")
        self.queue_items_frame = tk.Frame(self.queue_frame, bg=self.C["bg"])
        self.queue_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.queue_item_labels = []

        # ── History ──
        self.history_frame = tk.Frame(self.root, bg=self.C["bg"])
        tk.Frame(self.history_frame, bg=self.C["sep"], height=1).pack(
            fill="x", padx=14, pady=(2, 4))
        history_hdr_row = tk.Frame(self.history_frame, bg=self.C["bg"])
        history_hdr_row.pack(fill="x", padx=14)
        history_hdr_row.columnconfigure(3, weight=1)
        for i, (txt, w) in enumerate([("TIME", 9), ("DUR", 5), ("APP", 8)]):
            tk.Label(history_hdr_row, text=txt, bg=self.C["bg"], fg=self.C["dim"],
                     font=_qf, width=w, anchor="w").grid(row=0, column=i, sticky="w")
        tk.Label(history_hdr_row, text="WINDOW", bg=self.C["bg"], fg=self.C["dim"],
                 font=_qf, anchor="w").grid(row=0, column=3, sticky="we")

        history_scroll_container = tk.Frame(self.history_frame, bg=self.C["bg"])
        history_scroll_container.pack(fill="x", padx=14, pady=(0, 6))
        self._history_canvas = tk.Canvas(
            history_scroll_container, bg=self.C["bg"], highlightthickness=0,
            height=VISIBLE_HISTORY * HISTORY_ITEM_H)
        self._history_scrollbar = tk.Scrollbar(
            history_scroll_container, orient="vertical",
            command=self._history_canvas.yview)
        self.history_items_frame = tk.Frame(self._history_canvas, bg=self.C["bg"])
        self.history_items_frame.bind(
            "<Configure>",
            lambda e: self._history_canvas.configure(
                scrollregion=self._history_canvas.bbox("all")))
        self._history_canvas_win = self._history_canvas.create_window(
            (0, 0), window=self.history_items_frame, anchor="nw")
        self._history_canvas.configure(yscrollcommand=self._history_scrollbar.set)
        self._history_canvas.pack(side="left", fill="x", expand=True)
        self._history_canvas.bind("<Configure>", lambda e: self._history_canvas.itemconfig(
            self._history_canvas_win, width=e.width))

        def _on_history_mousewheel(event):
            self._history_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._on_history_mousewheel = _on_history_mousewheel
        self._history_canvas.bind("<MouseWheel>", _on_history_mousewheel)

        self.history_item_widgets = []

        # ── Benchmark panel (Windows/Tk only) ──
        self.benchmark_frame = tk.Frame(self.root, bg=self.C["bg"])
        tk.Frame(self.benchmark_frame, bg=self.C["sep"], height=1).pack(
            fill="x", padx=14, pady=(2, 4))
        bench_hdr = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        bench_hdr.pack(fill="x", padx=14)
        bench_hdr.columnconfigure(2, weight=1)
        _bf = ("Consolas", 8)
        tk.Label(bench_hdr, text="BENCHMARK", bg=self.C["bg"], fg=self.C["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").grid(
                     row=0, column=0, sticky="w")
        self.bench_title_lbl = tk.Label(bench_hdr, text="", bg=self.C["bg"],
                                        fg=self.C["dim"], font=_bf, anchor="w")
        self.bench_title_lbl.grid(row=0, column=2, sticky="we", padx=(8, 0))
        bench_close = tk.Label(bench_hdr, text="×", bg=self.C["bg"], fg="#ef4444",
                               font=("Segoe UI", 11, "bold"), cursor="hand2")
        bench_close.grid(row=0, column=3, sticky="e", padx=(4, 0))
        bench_close.bind("<Button-1>", lambda e: self._close_benchmark())

        bench_cols = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        bench_cols.pack(fill="x", padx=14, pady=(2, 0))
        bench_cols.columnconfigure(2, weight=1)
        for txt, w, col in (("MODEL", 16, 0), ("TIME", 8, 1)):
            tk.Label(bench_cols, text=txt, bg=self.C["bg"], fg=self.C["dim"],
                     font=_bf, width=w, anchor="w").grid(row=0, column=col, sticky="w")
        tk.Label(bench_cols, text="TEXT", bg=self.C["bg"], fg=self.C["dim"],
                 font=_bf, anchor="w").grid(row=0, column=2, sticky="we", padx=(4, 0))

        # Not scrollable: at most one row per downloaded model.
        self.benchmark_items_frame = tk.Frame(self.benchmark_frame, bg=self.C["bg"])
        self.benchmark_items_frame.pack(fill="x", padx=14, pady=(0, 6))
        self.benchmark_item_widgets = []
        self._benchmark_view_job = None
        self.benchmark_mode = False

        self._tooltip = None

        self.root.geometry(f"{OV_W}x120")
        self.root.update_idletasks()
        self.root.geometry(OFF_SCREEN)

        self._sw = self.root.winfo_screenwidth()
        self._timer_job = None
        self._blink_job = None
        self._level_job = None
        self._gpu_refresh_job = None
        self._blink_on = True
        self._pos = None
        self._t0 = time.time()
        self._level = 0.0

        #: Win32 HWNDs of our own windows, cached as plain ints. Read from the
        #: keyboard thread by the backend's capture_target(), which must not
        #: touch Tcl. Refreshed on the main thread whenever we show ourselves.
        self._own_hwnds = ()
        self._refresh_own_hwnds()

        self.tray_icon = None
        self._tray_thread = None
        self._tray_state = "idle"

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
        for getter in (lambda: int(self.root.wm_frame(), 16),
                       lambda: self.root.winfo_id()):
            try:
                value = getter()
            except Exception:
                continue
            if value and value not in ids:
                ids.append(value)
        self._own_hwnds = tuple(ids)

    def own_window_ids(self):
        return self._own_hwnds

    # ── Layout helpers ──

    def _drag(self, e):
        dx, dy = self._d
        x = self.root.winfo_x() + e.x - dx
        y = self.root.winfo_y() + e.y - dy
        self._pos = (x, y)
        self.root.geometry(f"+{x}+{y}")

    def _get_pos(self):
        if self._pos:
            return f"+{self._pos[0]}+{self._pos[1]}"
        return f"+{self._sw // 2 - OV_W // 2}+20"

    def _repack(self):
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.history_frame.pack_forget()
        self.benchmark_frame.pack_forget()
        if self.app.backend.gpu_available:
            self.gpu_frame.pack(fill="x")
        self.rec_frame.pack(fill="x")
        if self.benchmark_mode:
            self.benchmark_frame.pack(fill="x")
        elif self.history_mode:
            self.history_frame.pack(fill="x")
        elif self.app.jobs.active_count() > 0:
            self.queue_frame.pack(fill="x")
        self._update_state_display()

    def _update_state_display(self):
        k = self.app.ptt_label
        secs = self.app.cfg.silence_duration
        if self.app.recording:
            armed = self.app.benchmark_next
            self.dot.config(fg=self.C["bar_mid"] if armed else self.C["rec"])
            self.state_lbl.config(
                text="Recording (benchmark)" if armed else "Recording",
                fg=self.C["bar_mid"] if armed else self.C["text"])
            stop = f"{secs:.0f}s silence" if secs > 0 else "no auto-stop"
            self.hint.config(
                text=f"Transcribe: {k} / Enter↵ / {stop}"
                     f"  |  History: Space  |  Esc: discard")
        elif self.benchmark_mode:
            running = self.app.benchmark_job is not None
            self.dot.config(fg=self.C["bar_mid"] if running else self.C["bar_lo"])
            self.state_lbl.config(
                text="Benchmarking…" if running else "Benchmark results",
                fg=self.C["bar_mid"] if running else self.C["bar_lo"])
            self.timer.config(text="")
            self.hint.config(text="Close: ×  |  Hide: Esc")
        elif self.history_mode:
            self.dot.config(fg=self.C["bar_lo"])
            self.state_lbl.config(text="History", fg=self.C["bar_lo"])
            self.timer.config(text="")
            self.hint.config(text="Back: Space  |  Hide: Esc")
        elif self.app.last_error:
            self.dot.config(fg="#ef4444")
            self.state_lbl.config(text="Failed", fg="#ef4444")
            self.timer.config(text="")
            self.hint.config(text=str(self.app.last_error))
        elif self._message:
            self.dot.config(fg=self.C["dim"])
            self.state_lbl.config(text="Ready", fg=self.C["dim"])
            self.timer.config(text="")
            self.hint.config(text=self._message)
        elif self.app.jobs.busy():
            self.dot.config(fg=self.C["trans"])
            self.state_lbl.config(text="Transcribing", fg=self.C["trans"])
            self.timer.config(text="")
            self.hint.config(text=f"Record: Double {k}  |  Hide: Esc")
        else:
            self.dot.config(fg=self.C["dim"])
            self.state_lbl.config(text="Ready", fg=self.C["dim"])
            self.timer.config(text="")
            self.hint.config(text=f"Record: Double {k}  |  Hide: Esc")

    def _calc_height(self):
        h = 20
        if self.app.backend.gpu_available:
            h += 62
        h += 130
        if self.benchmark_mode:
            # sep + BENCHMARK row + column headers, then one row per model. A
            # run only covers downloaded models, so sizing off the full
            # catalogue would leave dead space.
            job = self._benchmark_view_job
            rows = len(job.results) if job and job.results else 1
            h += 48 + rows * HISTORY_ITEM_H
        elif self.history_mode:
            n = self.app.jobs.history_count()
            visible = min(n, VISIBLE_HISTORY) if n > 0 else 1
            h += 34 + visible * HISTORY_ITEM_H
        else:
            n = self.app.jobs.active_count()
            if n > 0:
                h += 34 + min(n, MAX_QUEUE_VISIBLE) * 22
                if n > MAX_QUEUE_VISIBLE:
                    h += 18
        return max(h, 60)

    def _show_overlay(self):
        self._repack()
        self._rebuild_queue()
        h = self._calc_height()
        self.root.geometry(f"{OV_W}x{h}{self._get_pos()}")
        self.root.update_idletasks()
        self.root.attributes("-alpha", 0.93)
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
        self._t0 = time.time()
        self.model_lbl.config(text=f"Model: {self.app.model_name}")
        self.target_lbl.config(text=f"→ {target_name}" if target_name else "")
        self._draw_level(0)
        self._show_overlay()
        self._level_tick()
        self._tick()
        self._blink()

    def _show_rec_idle(self):
        self._cancel_timer_blink()
        self.model_lbl.config(text=f"Model: {self.app.model_name}")
        self.target_lbl.config(text="")
        self._draw_level(0)

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
        else:
            self.hide()

    def check_hide(self):
        # An unacknowledged error keeps the overlay up — it is the only place
        # the reason is written down.
        if (not self.app.jobs.busy() and not self.app.recording
                and not self.history_mode and not self.benchmark_mode
                and not self.app.last_error and not self._message):
            self.hide()

    # ── Transient states (mirrors the macOS UI's contract) ──

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
        self._repack()
        h = self._calc_height()
        self.root.geometry(f"{OV_W}x{h}{self._get_pos()}")
        self.root.update_idletasks()
        # History mode is exactly the state in which the overlay holds focus,
        # so the backend must be able to recognise it as ours.
        self._refresh_own_hwnds()
        self.visible = True

    def hide(self):
        self._cancel_timer_blink()
        self._stop_gpu_refresh()
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.history_frame.pack_forget()
        self.benchmark_frame.pack_forget()
        self.history_mode = False
        self.benchmark_mode = False
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)
        self.visible = False

    def push_level(self, rms):
        """Called from the audio thread ~16x/s. Store only — Tcl is not
        thread-safe, so _level_tick() does the redraw on the main loop."""
        self._level = rms

    # ── Queue / history rendering ──

    def _rebuild_queue(self):
        for w in self.queue_item_labels:
            w.destroy()
        self.queue_item_labels = []

        jobs = self.app.jobs.active()
        if not jobs:
            return

        _qf = ("Consolas", 8)
        _qbg = self.C["bg"]

        for job in jobs[:MAX_QUEUE_VISIBLE]:
            color = self.C["trans"] if job.status == JobStatus.TRANSCRIBING else self.C["dim"]
            ts = time.strftime("%H:%M:%S", time.localtime(job.created_at))
            dur = f"{job.audio_duration:.1f}s"
            app_name = (job.app_name[:8] if job.app_name else "?")

            row = tk.Frame(self.queue_items_frame, bg=_qbg)
            row.pack(fill="x")
            row.columnconfigure(4, weight=1)
            self.queue_item_labels.append(row)

            for i, (txt, w) in enumerate([(ts, 9), (dur, 5), (app_name, 8),
                                          (job.window_name, 20)]):
                tk.Label(row, text=txt, bg=_qbg, fg=color, font=_qf,
                         width=w, anchor="w").grid(row=0, column=i, sticky="w")

            xbtn = tk.Label(row, text="×", bg=_qbg, fg="#ef4444",
                            font=("Consolas", 9, "bold"), cursor="hand2")
            xbtn.grid(row=0, column=4, sticky="e", padx=(0, 2))
            xbtn.bind("<Button-1>", lambda e, j=job: self.app.cancel_job(j))

        if len(jobs) > MAX_QUEUE_VISIBLE:
            extra = tk.Label(self.queue_items_frame,
                             text=f"  +{len(jobs) - MAX_QUEUE_VISIBLE} more…",
                             bg=_qbg, fg=self.C["dim"], font=_qf)
            extra.pack(anchor="w")
            self.queue_item_labels.append(extra)

    def _rebuild_history(self):
        for w in self.history_item_widgets:
            w.destroy()
        self.history_item_widgets = []

        entries = self.app.jobs.history()
        items = list(reversed(entries))

        if not items:
            lbl = tk.Label(self.history_items_frame, text="  No transcriptions yet",
                           bg=self.C["bg"], fg=self.C["dim"], font=("Segoe UI", 8))
            lbl.pack(anchor="w")
            lbl.bind("<MouseWheel>", self._on_history_mousewheel)
            self.history_item_widgets.append(lbl)
            self._history_scrollbar.pack_forget()
            self._history_canvas.configure(height=HISTORY_ITEM_H)
            return

        _qf = ("Consolas", 8)
        _qbg = self.C["bg"]

        for idx, entry in enumerate(items):
            row = tk.Frame(self.history_items_frame, bg=_qbg)
            row.pack(fill="x")
            row.columnconfigure(3, weight=1)
            self.history_item_widgets.append(row)

            for i, (txt, w) in enumerate([(entry["ts"], 9), (entry["dur"], 5),
                                          (entry["app"], 8)]):
                tk.Label(row, text=txt, bg=_qbg, fg=self.C["dim"],
                         font=_qf, width=w, anchor="w").grid(row=0, column=i, sticky="w")

            tk.Label(row, text=entry["window"], bg=_qbg, fg=self.C["dim"],
                     font=_qf, anchor="w").grid(row=0, column=3, sticky="we")

            btn_frame = tk.Frame(row, bg=_qbg)
            btn_frame.grid(row=0, column=4, sticky="e", padx=(4, 0))

            copy_btn = tk.Label(btn_frame, text="\U0001f4cb", bg=_qbg,
                                fg=self.C["bar_lo"], font=("Segoe UI", 11), cursor="hand2")
            copy_btn.pack(side="left", padx=(0, 6))
            copy_btn.bind("<Button-1>", lambda e, t=entry["text"]: self._copy_text(t))
            preview = entry["text"][:500] + ("…" if len(entry["text"]) > 500 else "")
            copy_btn.bind("<Enter>", lambda e, p=preview: self._show_tooltip(e, p))
            copy_btn.bind("<Leave>", lambda e: self._hide_tooltip())

            del_btn = tk.Label(btn_frame, text="×", bg=_qbg, fg="#ef4444",
                               font=("Segoe UI", 11, "bold"), cursor="hand2")
            del_btn.pack(side="left")
            real_idx = len(items) - 1 - idx
            del_btn.bind("<Button-1>", lambda e, ri=real_idx: self._delete_history(ri))

            row.bind("<MouseWheel>", self._on_history_mousewheel)
            for child in row.winfo_children():
                child.bind("<MouseWheel>", self._on_history_mousewheel)
                for grandchild in child.winfo_children():
                    grandchild.bind("<MouseWheel>", self._on_history_mousewheel)

        if len(items) > VISIBLE_HISTORY:
            self._history_scrollbar.pack(side="right", fill="y")
            self._history_canvas.configure(height=VISIBLE_HISTORY * HISTORY_ITEM_H)
        else:
            self._history_scrollbar.pack_forget()
            self._history_canvas.configure(height=len(items) * HISTORY_ITEM_H)

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
        _bg = self.C["bg"]
        icons = {"waiting": ("○", self.C["dim"]),
                 "loading": ("↻", self.C["bar_mid"]),
                 "running": ("▶", self.C["trans"]),
                 "done":    ("✓", self.C["bar_lo"]),
                 "error":   ("×", "#ef4444")}

        for r in job.results:
            row = tk.Frame(self.benchmark_items_frame, bg=_bg)
            row.pack(fill="x")
            row.columnconfigure(2, weight=1)
            self.benchmark_item_widgets.append(row)

            icon, icon_col = icons.get(r.status, ("?", self.C["dim"]))
            tk.Label(row, text=f"{icon} {r.model}", bg=_bg, fg=icon_col,
                     font=_f, width=18, anchor="w").grid(row=0, column=0, sticky="w")

            if r.transcribe_secs is not None:
                time_txt, time_fg = f"{r.transcribe_secs:.2f}s", self.C["bar_lo"]
            elif r.status in ("running", "loading"):
                time_txt, time_fg = "…", self.C["bar_mid"]
            elif r.status == "error":
                time_txt, time_fg = "ERR", "#ef4444"
            else:
                time_txt, time_fg = "—", self.C["dim"]
            tk.Label(row, text=time_txt, bg=_bg, fg=time_fg, font=_f,
                     width=8, anchor="w").grid(row=0, column=1, sticky="w")

            if r.status == "done" and r.text is not None:
                preview, preview_fg = (r.text[:40] + ("…" if len(r.text) > 40 else ""),
                                       self.C["text"])
            elif r.status == "error":
                preview, preview_fg = (r.error or "error")[:40], "#ef4444"
            elif r.status == "loading":
                preview, preview_fg = "loading model…", self.C["bar_mid"]
            elif r.status == "running":
                preview, preview_fg = "transcribing…", self.C["bar_mid"]
            else:
                preview, preview_fg = "", self.C["dim"]

            lbl = tk.Label(row, text=preview, bg=_bg, fg=preview_fg,
                           font=_f, anchor="w")
            lbl.grid(row=0, column=2, sticky="we", padx=(4, 0))

            if r.status == "done" and r.text:
                tip = r.text[:500] + ("…" if len(r.text) > 500 else "")
                lbl.bind("<Enter>", lambda e, p=tip: self._show_tooltip(e, p))
                lbl.bind("<Leave>", lambda e: self._hide_tooltip())
                copy_btn = tk.Label(row, text="\U0001f4cb", bg=_bg,
                                    fg=self.C["bar_lo"], font=("Segoe UI", 10),
                                    cursor="hand2")
                copy_btn.grid(row=0, column=3, sticky="e", padx=(4, 0))
                copy_btn.bind("<Button-1>", lambda e, t=r.text: self._copy_text(t))

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    def _delete_history(self, idx):
        self.app.jobs.delete_history(idx)
        self._rebuild_history()
        self._show_overlay()

    def _show_tooltip(self, event, text):
        self._hide_tooltip()
        x = event.widget.winfo_rootx()
        self._tooltip = tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tw.configure(bg="#1e293b")
        tk.Label(tw, text=text, bg="#1e293b", fg="#f1f5f9",
                 font=("Segoe UI", 10), wraplength=360, justify="left",
                 padx=8, pady=4).pack()
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
            self._tooltip.destroy()
            self._tooltip = None

    # ── Animation ──

    def _cancel_timer_blink(self):
        for j in (self._timer_job, self._blink_job, self._level_job):
            if j:
                self.root.after_cancel(j)
        self._timer_job = self._blink_job = self._level_job = None
        self._level = 0.0

    def _level_tick(self):
        self._draw_level(self._level)
        self._level_job = self.root.after(60, self._level_tick)

    def _tick(self):
        e = time.time() - self._t0
        self.timer.config(text=f"{int(e) // 60}:{int(e) % 60:02d}")
        self._timer_job = self.root.after(500, self._tick)

    def _blink(self):
        self._blink_on = not self._blink_on
        self.dot.config(fg=self.C["rec"] if self._blink_on else self.C["bg"])
        self._blink_job = self.root.after(500, self._blink)

    def _draw_level(self, rms, mx=4000):
        c = self.level_cv
        c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, W, H, fill=self.C["bar_bg"], outline="")
        r = min(rms / mx, 1.0)
        fw = int(W * r)
        clr = self.C["bar_lo"] if r < 0.4 else (self.C["bar_mid"] if r < 0.75 else self.C["bar_hi"])
        if fw > 0:
            c.create_rectangle(0, 0, fw, H, fill=clr, outline="")
        sx = int(W * min(self.app.cfg.silence_threshold / mx, 1.0))
        c.create_line(sx, 0, sx, H, fill="#ef4444", width=2)
        for p in (0.25, 0.5, 0.75):
            c.create_line(int(W * p), 0, int(W * p), H, fill="#0f172a")

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
        history = self.app.gpu_series()
        if history:
            pct = int(history[-1] * 100)
            color = self.C["bar_lo"] if pct < 50 else (
                self.C["bar_mid"] if pct < 80 else self.C["bar_hi"])
            self.gpu_pct.config(text=f"{pct}%", fg=color)
        self._draw_gpu_graph(history)
        self._gpu_refresh_job = self.root.after(1000, self._refresh_gpu)

    def _draw_gpu_graph(self, history):
        c = self.gpu_cv
        c.delete("all")
        W, H = int(c["width"]), int(c["height"])
        c.create_rectangle(0, 0, W, H, fill=self.C["bar_bg"], outline="")
        for p in (0.25, 0.5, 0.75):
            y = int(H * (1.0 - p))
            c.create_line(0, y, W, y, fill="#1e293b")

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
            c.create_line(x1, y1, x2, y2, fill=self.C["bar_lo"], width=2)

    # ── Tray ──

    def _build_tray_menu(self):
        items = []
        for info in self.app.model_catalog():
            label = f"{info.name}\t{info.size_label}"
            if not info.downloaded:
                label = "↓ " + label

            def make_act(n):
                def act(icon, item):
                    self.app.request_model(n)
                return act

            def make_checked(n):
                return lambda item: n == self.app.model_name

            items.append(pystray.MenuItem(label, make_act(info.name),
                                          checked=make_checked(info.name)))

        return pystray.Menu(
            pystray.MenuItem("WhisperType", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Model", pystray.Menu(*items)),
            pystray.MenuItem(
                "Download all models",
                lambda icon, item: self.app.download_all_models(),
                enabled=lambda item: (not self.app.downloading_all
                                      and any(not i.downloaded
                                              for i in self.app.model_catalog())),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Benchmark next recording",
                lambda icon, item: self.app.toggle_benchmark_next(),
                checked=lambda item: self.app.benchmark_next,
            ),
            pystray.MenuItem(
                "Open last benchmark",
                lambda icon, item: self.app.open_last_benchmark(),
                enabled=lambda item: self.app.last_benchmark is not None,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.app.quit()),
        )

    def set_tray_state(self, state):
        self._tray_state = state
        if self.tray_icon:
            self.tray_icon.icon = make_tray_icon(state)

    def refresh_tray(self):
        if self.tray_icon:
            self.tray_icon.menu = self._build_tray_menu()
            # Not a hardcoded "idle": rebuilding the menu during a download or
            # a benchmark must not reset the icon that says so.
            self.tray_icon.icon = make_tray_icon(self._tray_state or "idle")

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
