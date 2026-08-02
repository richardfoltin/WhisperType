"""Tray icon bitmaps — identical artwork on both platforms."""
import io

from PIL import Image, ImageDraw

_COLORS = {
    "idle":         ("#1e293b", "#6ee7b7"),
    "recording":    ("#1e293b", "#f87171"),
    "transcribing": ("#1e293b", "#fbbf24"),
    "downloading":  ("#1e293b", "#64748b"),
}


def make_tray_icon(state="idle"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg, fg = _COLORS.get(state, _COLORS["idle"])
    d.ellipse([2, 2, 62, 62], fill=bg)
    if state == "downloading":
        d.line([32, 12, 32, 44], fill=fg, width=4)
        d.polygon([(20, 36), (44, 36), (32, 52)], fill=fg)
        d.line([18, 56, 46, 56], fill=fg, width=3)
    else:
        d.rounded_rectangle([22, 10, 42, 38], radius=10, fill=fg)
        d.arc([14, 26, 50, 50], 0, 180, fill=fg, width=4)
        d.line([32, 50, 32, 58], fill=fg, width=4)
        d.line([24, 58, 40, 58], fill=fg, width=4)
    return img


def png_bytes(state="idle"):
    buf = io.BytesIO()
    make_tray_icon(state).save(buf, format="PNG")
    return buf.getvalue()
