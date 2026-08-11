"""Application icons for the history rows.

The macOS overlay asks NSWorkspace for the target app's icon and hands the
history list a data URI. Windows has no such one-liner and pywin32 is not a
dependency, so the icon is dug out of the executable by hand:

    ExtractIconEx -> HICON -> GetIconInfo -> GetDIBits -> PIL -> PNG -> Tk

Every step has a failure mode that is not an exception — a store app with no
extractable icon, a 24-bit icon whose alpha channel is all zeroes, a path that
no longer exists — so anything unresolvable returns None and the caller draws
a monogram plate instead, exactly like the macOS fallback.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import io
import tkinter as tk

from ..log import log

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

DIB_RGB_COLORS = 0
BI_RGB = 0
SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                ("yHotspot", wt.DWORD), ("hbmMask", wt.HANDLE),
                ("hbmColor", wt.HANDLE)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wt.LONG), ("bmWidth", wt.LONG),
                ("bmHeight", wt.LONG), ("bmWidthBytes", wt.LONG),
                ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wt.HANDLE), ("iIcon", ctypes.c_int),
                ("dwAttributes", wt.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80)]


_user32.GetIconInfo.argtypes = [wt.HANDLE, ctypes.POINTER(ICONINFO)]
_user32.GetIconInfo.restype = wt.BOOL
_user32.DestroyIcon.argtypes = [wt.HANDLE]
_gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.DeleteObject.argtypes = [wt.HANDLE]
_gdi32.GetDIBits.argtypes = [wt.HDC, wt.HANDLE, wt.UINT, wt.UINT,
                             ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                             wt.UINT]
_shell32.ExtractIconExW.argtypes = [wt.LPCWSTR, ctypes.c_int,
                                    ctypes.POINTER(wt.HANDLE),
                                    ctypes.POINTER(wt.HANDLE), wt.UINT]


def _bitmap_pixels(hbitmap):
    """(width, height, BGRA bytes) for a bitmap handle, or None."""
    info = BITMAP()
    if not _gdi32.GetObjectW(hbitmap, ctypes.sizeof(BITMAP), ctypes.byref(info)):
        return None
    width, height = int(info.bmWidth), int(info.bmHeight)
    if width <= 0 or height <= 0:
        return None

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    # Negative: ask for a top-down DIB, or every icon comes out upside down.
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    buf = ctypes.create_string_buffer(width * height * 4)
    hdc = _user32.GetDC(None)
    try:
        got = _gdi32.GetDIBits(hdc, hbitmap, 0, height, buf,
                               ctypes.byref(bmi), DIB_RGB_COLORS)
    finally:
        _user32.ReleaseDC(None, hdc)
    if not got:
        return None
    return width, height, buf.raw


def _image_from_icon(hicon):
    """HICON -> RGBA PIL image, or None."""
    from PIL import Image

    info = ICONINFO()
    if not _user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        colour = _bitmap_pixels(info.hbmColor) if info.hbmColor else None
        if colour is None:
            return None
        width, height, raw = colour
        image = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1)

        if not image.getchannel("A").getbbox():
            # A pre-XP icon carries no alpha at all; the AND mask is what says
            # which pixels are transparent (1 = transparent there).
            mask = _bitmap_pixels(info.hbmMask) if info.hbmMask else None
            if mask is not None and mask[0] == width:
                mono = Image.frombuffer("RGBA", (width, mask[1]), mask[2],
                                        "raw", "BGRA", 0, 1).crop(
                                            (0, 0, width, height))
                alpha = mono.convert("L").point(lambda v: 0 if v > 127 else 255)
                image.putalpha(alpha)
            else:
                image.putalpha(255)
        return image
    finally:
        for handle in (info.hbmColor, info.hbmMask):
            if handle:
                _gdi32.DeleteObject(handle)


def _extract(path):
    """The best icon available for `path`, as a PIL image, or None."""
    large, small = wt.HANDLE(), wt.HANDLE()
    count = _shell32.ExtractIconExW(path, 0, ctypes.byref(large),
                                    ctypes.byref(small), 1)
    for handle in (large, small):
        if handle and handle.value:
            try:
                image = _image_from_icon(handle.value)
                if image is not None:
                    return image
            finally:
                _user32.DestroyIcon(handle.value)
    if count:
        return None

    # Store apps ship their icons as PNG assets beside the executable rather
    # than as resources inside it, so ExtractIconEx finds nothing. The shell
    # knows where they live.
    info = SHFILEINFOW()
    if _shell32.SHGetFileInfoW(path, 0, ctypes.byref(info),
                               ctypes.sizeof(info),
                               SHGFI_ICON | SHGFI_LARGEICON) and info.hIcon:
        try:
            return _image_from_icon(info.hIcon)
        finally:
            _user32.DestroyIcon(info.hIcon)
    return None


_cache = {}


def _png(path, size):
    """base64 PNG of `path`'s icon at `size`, or None.

    This is what gets cached — bytes, not a Tk image. A `tk.PhotoImage`
    belongs to the interpreter that created it and is dead the moment that
    interpreter goes, so a cache of them is a cache of handles that can turn
    invalid; the extraction is the expensive half anyway.
    """
    key = (path.lower(), size)
    if key in _cache:
        return _cache[key]

    data = None
    try:
        source = _extract(path)
        if source is not None:
            from PIL import Image
            if source.size != (size, size):
                source = source.resize((size, size), Image.LANCZOS)
            buf = io.BytesIO()
            source.save(buf, format="PNG")
            data = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        log(f"Could not read the icon for {path}: {e}")
    # Misses are cached too, so a store app with no extractable icon is not
    # re-probed on every history redraw.
    _cache[key] = data
    return data


def photo(path, size=16, master=None):
    """A `tk.PhotoImage` of `path`'s icon, or None. Tk thread only.

    Built per call and bound to `master`: Tk drops an image as soon as the
    last Python reference to it goes, and a widget's `image=` option is not
    one — so the caller has to keep the returned object alive for as long as
    the widget showing it.
    """
    if not path:
        return None
    data = _png(path, size)
    if data is None:
        return None
    try:
        # Through base64 rather than PIL.ImageTk: Tk 8.6 reads PNG itself, and
        # ImageTk adds a second image-lifetime owner that is even easier to
        # get wrong than this one.
        return tk.PhotoImage(master=master, data=data)
    except tk.TclError as e:
        log(f"Could not build the icon image for {path}: {e}")
        return None
