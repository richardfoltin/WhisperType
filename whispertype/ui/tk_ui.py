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

        self._tooltip = None

        self.root.geometry(f"{OV_W}x120")
        self.root.update_idletasks()
        self.root.geometry(OFF_SCREEN)

        self._sw = self.root.winfo_screenwidth()
        self._timer_job = None
        self._blink_job = None
        self._gpu_refresh_job = None
        self._blink_on = True
        self._pos = None
        self._t0 = time.time()

        self.tray_icon = None
        self._tray_thread = None

    # ── UI interface: thread marshalling ──

    def call_soon(self, fn):
        self.root.after(0, fn)

    def call_later(self, ms, fn):
        self.root.after(ms, fn)

    def own_window_ids(self):
        try:
            return (self.root.winfo_id(),)
        except Exception:
            return ()

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
        if self.app.backend.gpu_available:
            self.gpu_frame.pack(fill="x")
        self.rec_frame.pack(fill="x")
        if self.history_mode:
            self.history_frame.pack(fill="x")
        elif self.app.jobs.active_count() > 0:
            self.queue_frame.pack(fill="x")
        self._update_state_display()

    def _update_state_display(self):
        k = self.app.ptt_label
        secs = self.app.cfg.silence_duration
        if self.app.recording:
            self.dot.config(fg=self.C["rec"])
            self.state_lbl.config(text="Recording", fg=self.C["text"])
            self.hint.config(
                text=f"Transcribe: {k} / Enter↵ / {secs:.0f}s silence"
                     f"  |  History: Space  |  Hide: Esc")
        elif self.history_mode:
            self.dot.config(fg=self.C["bar_lo"])
            self.state_lbl.config(text="History", fg=self.C["bar_lo"])
            self.timer.config(text="")
            self.hint.config(text="Record: Space  |  Hide: Esc")
        elif self.app.jobs.busy():
            self.dot.config(fg=self.C["trans"])
            self.state_lbl.config(text="Transcribing", fg=self.C["trans"])
            self.timer.config(text="")
            self.hint.config(text=f"Record: Double {k}  |  History: Space  |  Hide: Esc")
        else:
            self.dot.config(fg=self.C["dim"])
            self.state_lbl.config(text="Ready", fg=self.C["dim"])
            self.timer.config(text="")
            self.hint.config(text=f"Record: Double {k}  |  History: Space  |  Hide: Esc")

    def _calc_height(self):
        h = 20
        if self.app.backend.gpu_available:
            h += 62
        h += 130
        if self.history_mode:
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
        self._start_gpu_refresh()

    # ── UI interface: state ──

    def show_recording(self, target_name=""):
        self.history_mode = False
        self._t0 = time.time()
        self.model_lbl.config(text=f"Model: {self.app.model_name}")
        self.target_lbl.config(text=f"→ {target_name}" if target_name else "")
        self._draw_level(0)
        self._show_overlay()
        self._tick()
        self._blink()

    def _show_rec_idle(self):
        self._cancel_timer_blink()
        self.model_lbl.config(text=f"Model: {self.app.model_name}")
        self.target_lbl.config(text="")
        self._draw_level(0)

    def on_recording_stopped(self):
        self._cancel_timer_blink()
        if self.app.jobs.busy():
            self._show_rec_idle()
            self._show_overlay()
        else:
            self.hide()

    def refresh(self):
        if self.app.recording or self.app.jobs.busy() or self.history_mode:
            if not self.app.recording:
                self._show_rec_idle()
            if self.history_mode:
                self._rebuild_history()
            self._show_overlay()
        else:
            self.hide()

    def check_hide(self):
        if not self.app.jobs.busy() and not self.app.recording and not self.history_mode:
            self.hide()

    def set_history_mode(self, on):
        self.history_mode = on
        if on:
            self._cancel_timer_blink()
            self._rebuild_history()
        self._repack()
        h = self._calc_height()
        self.root.geometry(f"{OV_W}x{h}{self._get_pos()}")
        self.root.update_idletasks()
        self.visible = True

    def hide(self):
        self._cancel_timer_blink()
        self._stop_gpu_refresh()
        self.gpu_frame.pack_forget()
        self.rec_frame.pack_forget()
        self.queue_frame.pack_forget()
        self.history_frame.pack_forget()
        self.history_mode = False
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(OFF_SCREEN)
        self.visible = False

    def push_level(self, rms):
        self.root.after(0, lambda: self._draw_level(rms))

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
        for j in (self._timer_job, self._blink_job):
            if j:
                self.root.after_cancel(j)
        self._timer_job = self._blink_job = None

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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda icon, item: self.app.quit()),
        )

    def set_tray_state(self, state):
        if self.tray_icon:
            self.tray_icon.icon = make_tray_icon(state)

    def refresh_tray(self):
        if self.tray_icon:
            self.tray_icon.menu = self._build_tray_menu()
            self.tray_icon.icon = make_tray_icon("idle")

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
