"""Button icons drawn with Pillow, for systems without the Windows icon fonts (Segoe Fluent Icons / MDL2 Assets).

On Linux there is no font every desktop is guaranteed to have that contains play / pause / skip / power / speaker
glyphs, so text symbols show up as empty boxes on some machines. Drawing the shapes removes that dependency.
Kept free of GUI code so it can be unit tested: every function returns a plain PIL RGBA image.
"""
import math

from PIL import Image, ImageDraw

NAMES = ("play", "pause", "prev", "next", "settings", "power", "volume", "muted", "back")
_K = 4  # supersampling factor: shapes are drawn 4x too large and scaled down for smooth edges


def _rgb(color):
    h = color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _speaker(d, s, c):
    """The speaker body shared by the volume and muted icons."""
    d.rectangle((0.14 * s, 0.38 * s, 0.30 * s, 0.62 * s), fill=c)
    d.polygon([(0.26 * s, 0.38 * s), (0.50 * s, 0.18 * s), (0.50 * s, 0.82 * s), (0.26 * s, 0.62 * s)], fill=c)


def _draw(name, d, s, c):
    w = max(1, round(0.085 * s))  # stroke width
    if name == "play":
        d.polygon([(0.30 * s, 0.16 * s), (0.30 * s, 0.84 * s), (0.86 * s, 0.50 * s)], fill=c)
    elif name == "pause":
        d.rounded_rectangle((0.24 * s, 0.17 * s, 0.42 * s, 0.83 * s), radius=0.04 * s, fill=c)
        d.rounded_rectangle((0.58 * s, 0.17 * s, 0.76 * s, 0.83 * s), radius=0.04 * s, fill=c)
    elif name in ("prev", "next"):
        pts = [(0.82 * s, 0.20 * s), (0.82 * s, 0.80 * s), (0.34 * s, 0.50 * s)]
        bar = (0.18 * s, 0.20 * s, 0.29 * s, 0.80 * s)
        if name == "next":
            pts = [(s - x, y) for x, y in pts]
            bar = (s - bar[2], bar[1], s - bar[0], bar[3])
        d.polygon(pts, fill=c)
        d.rectangle(bar, fill=c)
    elif name == "settings":
        cx = cy = s / 2
        teeth, outer, inner = 8, 0.44 * s, 0.33 * s
        pts = []
        for i in range(teeth * 4):
            r = outer if i % 4 in (0, 1) else inner
            a = 2 * math.pi * (i - 0.5) / (teeth * 4)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=c)
        hole = 0.15 * s
        d.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=c[:3] + (0,))
    elif name == "power":
        d.arc((0.16 * s, 0.20 * s, 0.84 * s, 0.88 * s), start=-60, end=240, fill=c, width=w)
        d.line((0.50 * s, 0.10 * s, 0.50 * s, 0.50 * s), fill=c, width=w)
    elif name == "volume":
        _speaker(d, s, c)
        d.arc((0.36 * s, 0.32 * s, 0.70 * s, 0.68 * s), start=-50, end=50, fill=c, width=w)
        d.arc((0.30 * s, 0.16 * s, 0.90 * s, 0.84 * s), start=-48, end=48, fill=c, width=w)
    elif name == "muted":
        _speaker(d, s, c)
        d.line((0.62 * s, 0.38 * s, 0.86 * s, 0.62 * s), fill=c, width=w)
        d.line((0.86 * s, 0.38 * s, 0.62 * s, 0.62 * s), fill=c, width=w)
    elif name == "back":
        d.line((0.20 * s, 0.50 * s, 0.82 * s, 0.50 * s), fill=c, width=w)
        d.line((0.46 * s, 0.24 * s, 0.20 * s, 0.50 * s), fill=c, width=w)
        d.line((0.46 * s, 0.76 * s, 0.20 * s, 0.50 * s), fill=c, width=w)
    else:
        raise ValueError(f"unknown icon {name!r}")


def draw(name, size, color):
    """`name` (one of NAMES) as a `size` x `size` RGBA image in the given "#rrggbb" colour."""
    s = size * _K
    rgb = _rgb(color)
    img = Image.new("RGBA", (s, s), rgb + (0,))
    _draw(name, ImageDraw.Draw(img), s, rgb + (255,))
    # Only the scaled-down coverage (alpha) is kept and laid over the flat colour. Scaling the colour channels too
    # would blend the edges with the transparent background and leave a faint fringe around every shape.
    out = Image.new("RGBA", (size, size), rgb + (0,))
    out.putalpha(img.getchannel("A").resize((size, size), Image.LANCZOS))
    return out
