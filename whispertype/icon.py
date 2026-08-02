"""Tray icon bitmaps — identical artwork on both platforms.

The icon is the only thing the app can say when no overlay is on screen, so
every state the user can end up in has its own colour and glyph. In particular
`error` exists so a transcript that could not be delivered does not look
exactly like one that was.
"""
import io
from functools import lru_cache

from PIL import Image, ImageDraw

_COLORS = {
    "idle":         ("#1e293b", "#6ee7b7"),   # green mic
    "recording":    ("#1e293b", "#f87171"),   # red mic
    "transcribing": ("#1e293b", "#fbbf24"),   # amber mic
    "downloading":  ("#1e293b", "#64748b"),   # grey download arrow
    "loading":      ("#1e293b", "#64748b"),   # grey download arrow
    "error":        ("#1e293b", "#ef4444"),   # red exclamation
    "paused":       ("#1e293b", "#475569"),   # struck-through mic
}


def _draw_mic(d, fg):
    d.rounded_rectangle([22, 10, 42, 38], radius=10, fill=fg)
    d.arc([14, 26, 50, 50], 0, 180, fill=fg, width=4)
    d.line([32, 50, 32, 58], fill=fg, width=4)
    d.line([24, 58, 40, 58], fill=fg, width=4)


@lru_cache(maxsize=None)
def make_tray_icon(state="idle"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg, fg = _COLORS.get(state, _COLORS["idle"])
    d.ellipse([2, 2, 62, 62], fill=bg)

    if state in ("downloading", "loading"):
        d.line([32, 12, 32, 44], fill=fg, width=4)
        d.polygon([(20, 36), (44, 36), (32, 52)], fill=fg)
        d.line([18, 56, 46, 56], fill=fg, width=3)
    elif state == "error":
        d.rounded_rectangle([28, 12, 36, 40], radius=4, fill=fg)
        d.ellipse([27, 46, 37, 56], fill=fg)
    else:
        _draw_mic(d, fg)
        if state == "paused":
            # Diagonal strike, in the background colour so it reads as a cut.
            d.line([14, 54, 50, 14], fill=bg, width=10)
            d.line([14, 54, 50, 14], fill=fg, width=4)
    return img


@lru_cache(maxsize=None)
def png_bytes(state="idle"):
    buf = io.BytesIO()
    make_tray_icon(state).save(buf, format="PNG")
    return buf.getvalue()
