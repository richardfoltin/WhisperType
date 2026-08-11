"""Overlay palette, and the Windows end of `config.theme`.

The macOS overlay gets this for free: every colour in `overlay.html` is a
system keyword, so the panel follows the user's appearance, accent colour and
contrast settings by itself. Tk has no such thing — it paints exactly the hex
values it is given — so the same behaviour has to be assembled here:

  * `theme: "auto"` reads the system light/dark setting (and is re-read on
    every appearance, so switching Windows to dark at 18:00 is picked up
    without a restart),
  * the accent colour comes from the user's Windows accent, corrected until it
    is actually legible on the panel it is drawn on — an accent picked to look
    good on the taskbar is not guaranteed to read on a dark HUD.

Everything here is pure: no Tk, no app state, so the tray thread may call it.
"""
import sys

IS_WINDOWS = sys.platform == "win32"

#: Slate, carried over from the original overlay so a dark-mode user sees the
#: panel they are used to.
DARK = {
    "bg": "#0f172a",
    "panel": "#16203a",
    "bar_bg": "#1e293b",
    "sep": "#334155",
    "hover": "#1c2942",
    "text": "#f1f5f9",
    "dim": "#94a3b8",
    "faint": "#64748b",
    "rec": "#f87171",
    "trans": "#fbbf24",
    "ok": "#6ee7b7",
    "danger": "#ef4444",
    "bar_lo": "#6ee7b7",
    "bar_mid": "#fbbf24",
    "bar_hi": "#f87171",
    "gpu_fill": "#1a3a2a",
    "key_bg": "#1e293b",
    "key_edge": "#3d4c68",
    "key_fg": "#cbd5e1",
    "accent": "#60a5fa",
}

#: The light panel is not the dark one inverted: on white, saturated greens and
#: ambers lose all contrast, so every status colour is darkened rather than
#: lightened, and the plate colours stay close together to keep the panel calm.
LIGHT = {
    "bg": "#f6f6f8",
    "panel": "#ffffff",
    "bar_bg": "#eaeaef",
    "sep": "#d5d5dc",
    "hover": "#ededf2",
    "text": "#15171c",
    "dim": "#54575e",
    "faint": "#84878f",
    "rec": "#c11a2b",
    "trans": "#96590a",
    "ok": "#0a7048",
    "danger": "#c11a2b",
    "bar_lo": "#0a7048",
    "bar_mid": "#96590a",
    "bar_hi": "#c11a2b",
    "gpu_fill": "#d6efe3",
    "key_bg": "#e7e7ec",
    "key_edge": "#c6c6cf",
    "key_fg": "#3a3d44",
    "accent": "#0a63c2",
}


# ── Colour maths ─────────────────────────────────────────────────────────────

def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _luminance(rgb):
    def channel(c):
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """WCAG contrast ratio between two hex colours (1.0 … 21.0)."""
    la, lb = _luminance(_rgb(a)), _luminance(_rgb(b))
    lo, hi = sorted((la, lb))
    return (hi + 0.05) / (lo + 0.05)


def mix(a, b, t):
    """`a` blended `t` of the way towards `b`."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ca + (cb - ca) * t for ca, cb in zip(ra, rb)))


def readable(colour, on, ratio=3.0):
    """Lighten or darken `colour` until it reads on `on`.

    A Windows accent colour is chosen against the taskbar, not against this
    panel: dark blue on the dark overlay, or the default light blue on the
    light one, is invisible. Rather than refusing to use the user's accent,
    walk it towards white or black until it carries.
    """
    try:
        if contrast(colour, on) >= ratio:
            return colour
        towards = "#ffffff" if _luminance(_rgb(on)) < 0.5 else "#000000"
        for step in range(1, 21):
            candidate = mix(colour, towards, step / 20.0)
            if contrast(candidate, on) >= ratio:
                return candidate
        return towards
    except Exception:
        return colour


# ── Windows appearance ───────────────────────────────────────────────────────

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_DWM = r"Software\Microsoft\Windows\CurrentVersion\DWM"
_ACCENT = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"


def _read_dword(subkey, name):
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            value, kind = winreg.QueryValueEx(key, name)
        if kind != winreg.REG_DWORD:
            return None
        return int(value)
    except OSError:
        return None
    except Exception:
        return None


def system_is_dark():
    """True when Windows is set to dark app mode. Defaults to dark when the
    value cannot be read — that is what the overlay has always looked like."""
    value = _read_dword(_PERSONALIZE, "AppsUseLightTheme")
    return True if value is None else value == 0


def _abgr(value):
    """AABBGGRR (the format every accent value is stored in) -> #rrggbb.

    Reading one of these as RGB gives a plausible-looking colour that is simply
    wrong, which is exactly the kind of bug nobody thinks to check.
    """
    return _hex((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))


def system_accent():
    """The user's accent colour as #rrggbb, or None.

    Three sources, because which of them exists depends on the Windows build
    and on whether the user ever opened Personalisation: this machine has no
    `DWM` key at all, only `Explorer\\Accent`.
    """
    value = _read_dword(_DWM, "AccentColor")
    if value is not None:
        return _abgr(value)
    value = _read_dword(_ACCENT, "AccentColorMenu")
    if value is not None:
        return _abgr(value)
    value = _read_dword(_DWM, "ColorizationColor")     # AARRGGBB
    if value is not None:
        return _hex(((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF))
    return None


# ── Palette ──────────────────────────────────────────────────────────────────

def resolve(theme):
    """"auto" | "dark" | "light" -> the palette to paint with.

    Returned fresh every time rather than cached: "auto" has to notice the
    system flipping, and building a dict of twenty strings is nothing next to
    the redraw it precedes.
    """
    dark = system_is_dark() if theme == "auto" else (theme != "light")
    palette = dict(DARK if dark else LIGHT)
    palette["dark"] = dark

    accent = system_accent()
    if accent:
        # 3.0 is the WCAG threshold for large text and UI components, which is
        # exactly what the accent is used for here: the waveform, the focus
        # ring, the section headings.
        palette["accent"] = readable(accent, palette["bg"], 3.0)
    return palette
