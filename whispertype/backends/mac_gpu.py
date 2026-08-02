"""
Apple Silicon GPU telemetry via IOKit IORegistry — drop-in replacement for pynvml
in WhisperType. Pure stdlib (ctypes). No sudo, no entitlements, no third-party deps.

Reads AGXAccelerator "PerformanceStatistics" -> "Device Utilization %".
Verified on macOS 26.5.1 (25F80), Apple M5, uid 501 (non-root).
"""
import ctypes
import ctypes.util

_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UTF8 = 0x08000100
_kCFNumberSInt64Type = 4


class AppleGPU:
    """Poll Apple Silicon (and Intel/AMD) GPU utilization from the IORegistry."""

    def __init__(self):
        self._ok = False
        self.name = "GPU"
        self.core_count = None
        self._iokit = ctypes.cdll.LoadLibrary(_IOKIT)
        self._cf = ctypes.cdll.LoadLibrary(_CF)
        self._bind()
        self._svc = 0
        self._open()

    # -- ctypes prototypes ------------------------------------------------
    def _bind(self):
        io, cf = self._iokit, self._cf
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong

        io.IOServiceMatching.restype = ctypes.c_void_p
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceGetMatchingServices.restype = ctypes.c_int
        io.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_uint32)]
        io.IOIteratorNext.restype = ctypes.c_uint32
        io.IOIteratorNext.argtypes = [ctypes.c_uint32]
        io.IOObjectRelease.restype = ctypes.c_int
        io.IOObjectRelease.argtypes = [ctypes.c_uint32]
        io.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
        io.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                                       ctypes.c_void_p, ctypes.c_uint32]

    # -- CF helpers -------------------------------------------------------
    def _s(self, text):
        return self._cf.CFStringCreateWithCString(None, text.encode(), _UTF8)

    def _cf_int(self, ref):
        if not ref or self._cf.CFGetTypeID(ref) != self._cf.CFNumberGetTypeID():
            return None
        out = ctypes.c_longlong(0)
        if self._cf.CFNumberGetValue(ref, _kCFNumberSInt64Type, ctypes.byref(out)):
            return out.value
        return None

    def _cf_str(self, ref):
        if not ref or self._cf.CFGetTypeID(ref) != self._cf.CFStringGetTypeID():
            return None
        buf = ctypes.create_string_buffer(256)
        if self._cf.CFStringGetCString(ref, buf, 256, _UTF8):
            return buf.value.decode(errors="replace")
        return None

    def _prop(self, svc, key_cfstr):
        """Copy a property; caller must CFRelease."""
        return self._iokit.IORegistryEntryCreateCFProperty(svc, key_cfstr, None, 0)

    # -- open -------------------------------------------------------------
    def _open(self):
        # Cache the CFStrings we reuse every second (they are constant).
        self._k_perf = self._s("PerformanceStatistics")
        self._k_util = self._s("Device Utilization %")
        self._k_mem = self._s("In use system memory")
        self._k_model = self._s("model")
        self._k_cores = self._s("gpu-core-count")

        it = ctypes.c_uint32(0)
        # kIOMainPortDefault == 0 (also valid as kIOMasterPortDefault on older macOS)
        if self._iokit.IOServiceGetMatchingServices(
                0, self._iokit.IOServiceMatching(b"IOAccelerator"), ctypes.byref(it)) != 0:
            return
        chosen = 0
        while True:
            svc = self._iokit.IOIteratorNext(it)
            if not svc:
                break
            if chosen:
                self._iokit.IOObjectRelease(svc)
                continue
            perf = self._prop(svc, self._k_perf)
            if perf:
                self._cf.CFRelease(perf)
                chosen = svc
                m = self._prop(svc, self._k_model)
                if m:
                    self.name = self._cf_str(m) or self.name
                    self._cf.CFRelease(m)
                c = self._prop(svc, self._k_cores)
                if c:
                    self.core_count = self._cf_int(c)
                    self._cf.CFRelease(c)
            else:
                self._iokit.IOObjectRelease(svc)
        self._iokit.IOObjectRelease(it)
        if chosen:
            self._svc = chosen
            self._ok = True

    @property
    def available(self):
        return self._ok

    # -- sample -----------------------------------------------------------
    def sample(self):
        """Return (utilization 0.0-1.0, in-use GPU memory in bytes) or (None, None)."""
        if not self._ok:
            return None, None
        perf = self._prop(self._svc, self._k_perf)
        if not perf:
            return None, None
        try:
            util = self._cf_int(self._cf.CFDictionaryGetValue(perf, self._k_util))
            mem = self._cf_int(self._cf.CFDictionaryGetValue(perf, self._k_mem))
        finally:
            self._cf.CFRelease(perf)
        if util is None:
            return None, mem
        return max(0.0, min(1.0, util / 100.0)), mem

    def close(self):
        if self._svc:
            self._iokit.IOObjectRelease(self._svc)
            self._svc = 0
        self._ok = False
