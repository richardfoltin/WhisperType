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
        in history mode) by walking down the Z-order."""
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        if hwnd in tuple(self._own_windows()):
            GW_HWNDNEXT = 2
            candidate = user32.GetWindow(hwnd, GW_HWNDNEXT)
            while candidate:
                if (user32.GetParent(candidate) == 0
                        and user32.IsWindowVisible(candidate)
                        and user32.GetWindowTextLengthW(candidate) > 0):
                    return candidate
                candidate = user32.GetWindow(candidate, GW_HWNDNEXT)
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
        events = []
        for ch in text:
            code = ord(ch)
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = INPUT()
                inp.type = INPUT_KEYBOARD
                inp.ki.wVk = 0
                inp.ki.wScan = code
                inp.ki.dwFlags = flags
                inp.ki.time = 0
                inp.ki.dwExtraInfo = _extra
                events.append(inp)
        n = len(events)
        self._user32.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))

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
        n = len(events)
        self._user32.SendInput(n, (INPUT * n)(*events), ctypes.sizeof(INPUT))

    # ── Metrics ──

    def gpu_percent(self):
        if not self._nvml:
            return None
        try:
            return float(self._nvml.nvmlDeviceGetUtilizationRates(self._gpu_handle).gpu)
        except Exception:
            return None
