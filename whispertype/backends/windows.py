"""Windows backend — Win32 SendInput + foreground window handling.

Lifted from the original single-file whispertype.pyw with no behavioural
changes; only the overlay-skipping hook is now injected instead of reaching
for a global `overlay` object.
"""
import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

from ..log import log
from .base import Backend

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
VK_RETURN = 0x0D


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [("type", wt.DWORD), ("_u", _INPUT_UNION)]


_extra = ctypes.pointer(ctypes.c_ulong(0))

#: One SendInput call per this many characters. A 300 s dictation is tens of
#: thousands of INPUT structs, and a number of applications drop the tail of a
#: single huge batch rather than queueing it.
TYPE_CHUNK_CHARS = 200

#: Separate handle so ctypes preserves GetLastError for the SendInput checks
#: (ctypes.windll does not).
_user32_le = ctypes.WinDLL("user32", use_last_error=True)


def _send_inputs(events):
    """Send one batch, reporting short writes instead of losing them silently."""
    n = len(events)
    if n == 0:
        return 0
    sent = _user32_le.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))
    if sent != n:
        log(f"SendInput: {sent}/{n} events accepted (GetLastError={ctypes.get_last_error()})")
    return sent


class WindowsBackend(Backend):
    name = "windows"
    gpu_label = "GPU"

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._own_windows = lambda: ()
        self._nvml = None
        self._gpu_handle = None
        try:
            import pynvml
            pynvml.nvmlInit()
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml = pynvml
            self.gpu_available = True
            log(f"NVML initialized: {pynvml.nvmlDeviceGetName(self._gpu_handle)}")
        except Exception as e:
            log(f"NVML not available (GPU graph disabled): {e}")

    def set_own_window_provider(self, fn):
        self._own_windows = fn

    # ── Target window ──

    def capture_target(self):
        """Foreground window, skipping our own overlay (which may hold focus
        in history mode) by walking down the Z-order.

        Returns None when the overlay is in front and nothing behind it
        qualifies — the caller must treat that as "no target" rather than
        typing into whatever happens to be there.
        """
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        own = tuple(self._own_windows())
        if hwnd in own:
            GW_HWNDNEXT = 2
            candidate = user32.GetWindow(hwnd, GW_HWNDNEXT)
            while candidate:
                if (candidate not in own
                        and user32.GetParent(candidate) == 0
                        and user32.IsWindowVisible(candidate)
                        and user32.GetWindowTextLengthW(candidate) > 0):
                    return candidate
                candidate = user32.GetWindow(candidate, GW_HWNDNEXT)
            log("capture_target: overlay is in front and no window behind it")
            return None
        return hwnd

    def target_title(self, target):
        buf = ctypes.create_unicode_buffer(256)
        self._user32.GetWindowTextW(target, buf, 256)
        title = buf.value.strip()
        return (title[:20] + "…") if len(title) > 20 else (title or "(untitled)")

    def target_app(self, target):
        try:
            pid = wt.DWORD()
            self._user32.GetWindowThreadProcessId(target, ctypes.byref(pid))
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid.value)
            if h:
                buf = ctypes.create_unicode_buffer(260)
                size = wt.DWORD(260)
                ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                ctypes.windll.kernel32.CloseHandle(h)
                name = buf.value.strip()
                if name:
                    return Path(name).stem
        except Exception:
            pass
        return "?"

    def activate(self, target):
        user32 = self._user32
        if not user32.IsWindow(target):
            log(f"activate: HWND {target} is no longer valid")
            return False
        SW_RESTORE = 9
        if user32.IsIconic(target):
            user32.ShowWindow(target, SW_RESTORE)
        elif user32.GetForegroundWindow() == target:
            # Already there. Skipping the synthetic Alt below matters: a bare
            # Alt press activates the target application's menu bar.
            return True
        # Press+release Alt so SetForegroundWindow is allowed from background
        alt_down = INPUT()
        alt_down.type = INPUT_KEYBOARD
        alt_down.ki.wVk = VK_MENU
        alt_down.ki.dwFlags = 0
        alt_up = INPUT()
        alt_up.type = INPUT_KEYBOARD
        alt_up.ki.wVk = VK_MENU
        alt_up.ki.dwFlags = KEYEVENTF_KEYUP
        arr = (INPUT * 2)(alt_down, alt_up)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        for attempt in range(2):
            user32.SetForegroundWindow(target)
            for _ in range(10):  # poll up to ~500 ms
                time.sleep(0.05)
                if user32.GetForegroundWindow() == target:
                    return True
            if attempt == 0:
                user32.BringWindowToTop(target)
                log(f"activate: retry with BringWindowToTop for HWND {target}")
        log(f"activate: FAILED to activate HWND {target} after retries")
        return False

    # ── Input synthesis ──

    def type_text(self, text):
        sent = expected = 0
        for start in range(0, len(text), TYPE_CHUNK_CHARS):
            # Iterate UTF-16 code units, not characters: wScan is a WORD, so a
            # non-BMP character has to go out as its two surrogate halves
            # rather than being truncated by ord().
            units = text[start:start + TYPE_CHUNK_CHARS].encode("utf-16-le")
            events = []
            for i in range(0, len(units), 2):
                code = units[i] | (units[i + 1] << 8)
                for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                    inp = INPUT()
                    inp.type = INPUT_KEYBOARD
                    inp.ki.wVk = 0
                    inp.ki.wScan = code
                    inp.ki.dwFlags = flags
                    inp.ki.time = 0
                    inp.ki.dwExtraInfo = _extra
                    events.append(inp)
            expected += len(events)
            sent += _send_inputs(events)
            if start + TYPE_CHUNK_CHARS < len(text):
                time.sleep(0.005)   # let the target's message pump drain
        if sent != expected:
            raise RuntimeError(
                f"only {sent}/{expected} keystroke events were delivered")

    def send_enter(self):
        events = []
        for flags in (0, KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki.wVk = VK_RETURN
            inp.ki.wScan = 0
            inp.ki.dwFlags = flags
            inp.ki.time = 0
            inp.ki.dwExtraInfo = _extra
            events.append(inp)
        _send_inputs(events)

    # ── Metrics ──

    def gpu_percent(self):
        if not self._nvml:
            return None
        try:
            return float(self._nvml.nvmlDeviceGetUtilizationRates(self._gpu_handle).gpu)
        except Exception:
            return None
