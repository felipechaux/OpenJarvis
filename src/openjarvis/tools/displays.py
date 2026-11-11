"""Every connected screen in one screenshot, for ``look_at_screen`` and the
computer-control MCP server.

``screencapture`` writes one file per active display.  The shots are tiled
into a single JPEG (side by side or stacked, whichever keeps the long edge
shorter, so text stays readable) with a thin grey line between screens.  A
:class:`Layout` remembers where each screen landed, so image coordinates map
back to global screen points (top-left origin, the space of
``CGDisplayBounds`` and mouse events).

The layout math is pure; only :func:`active_displays` and :func:`capture`
touch macOS.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Sequence, Tuple

Which = Literal["all", "main", "external", "cursor"]
WHICH = ("all", "main", "external", "cursor")
# Grey line between screens, in image pixels.
GAP = 6


@dataclass(frozen=True)
class Display:
    """One active display, in global points (top-left origin)."""

    id: int
    x: float
    y: float
    width: float
    height: float
    main: bool = False
    builtin: bool = False

    def contains(self, px: float, py: float) -> bool:
        return (
            self.x <= px < self.x + self.width and self.y <= py < self.y + self.height
        )


@dataclass(frozen=True)
class Tile:
    """Where a display sits in the composite image (pixels)."""

    display: Display
    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class Layout:
    tiles: Tuple[Tile, ...]
    scale: float  # image pixels per screen point
    width: int
    height: int

    def to_screen(self, x: float, y: float) -> Tuple[float, float]:
        """Image pixel → global screen point."""
        for tile in self.tiles:
            if (
                tile.left <= x < tile.left + tile.width
                and tile.top <= y < tile.top + tile.height
            ):
                d = tile.display
                return (
                    d.x + (x - tile.left) / self.scale,
                    d.y + (y - tile.top) / self.scale,
                )
        raise ValueError(
            f"({x:.0f}, {y:.0f}) is not on any screen in the screenshot "
            f"({self.width}x{self.height})."
        )

    def describe(self) -> str:
        """English description of what the image shows, for the vision prompt."""
        if len(self.tiles) == 1:
            return f"The image shows one screen: {_name(self.tiles[0].display)}."
        stacked = self.tiles[1].top > self.tiles[0].top
        where = ("top", "bottom") if stacked else ("left", "right")
        lines = [
            f"The image shows {len(self.tiles)} screens "
            f"{'stacked top to bottom' if stacked else 'side by side'}, "
            "separated by a grey line (the image layout is not the physical one):"
        ]
        others = [t.display for t in self.tiles]
        for i, tile in enumerate(self.tiles):
            if i == 0:
                slot = where[0]
            elif i == len(self.tiles) - 1:
                slot = where[1]
            else:
                slot = f"number {i + 1}"
            lines.append(
                f"- {slot}: {_name(tile.display)}, "
                f"{_physical(tile.display, others)}."
            )
        return "\n".join(lines)


def _name(d: Display) -> str:
    kind = "the Mac's built-in screen" if d.builtin else "an external monitor"
    return f"{kind}{' (main display)' if d.main else ''}"


def _physical(d: Display, displays: Sequence[Display]) -> str:
    """Where *d* physically sits relative to the other screens."""
    others = [o for o in displays if o is not d]
    if not others:
        return "the only screen"
    cx, cy = d.x + d.width / 2, d.y + d.height / 2
    ox = sum(o.x + o.width / 2 for o in others) / len(others)
    oy = sum(o.y + o.height / 2 for o in others) / len(others)
    dx, dy = cx - ox, cy - oy
    if abs(dx) >= abs(dy):
        side = "right" if dx > 0 else "left"
    else:
        side = "below" if dy > 0 else "above"
    return f"physically on the {side}" if side in ("left", "right") else (
        f"physically {side} the other{'s' if len(others) > 1 else ''}"
    )


def select(displays: Sequence[Display], which: str,
           cursor: Optional[Tuple[float, float]] = None) -> List[Display]:
    """The displays *which* asks for; all of them when none match."""
    if which == "main":
        chosen = [d for d in displays if d.main]
    elif which == "external":
        chosen = [d for d in displays if not d.builtin]
    elif which == "cursor" and cursor is not None:
        chosen = [d for d in displays if d.contains(*cursor)]
    else:
        chosen = []
    return chosen or list(displays)


def layout(displays: Sequence[Display], max_edge: int) -> Layout:
    """Tile *displays* (in physical left-to-right order) into one image whose
    long edge is *max_edge*, choosing the arrangement that keeps them biggest."""
    if not displays:
        raise ValueError("no displays")
    ordered = sorted(displays, key=lambda d: (d.x, d.y))
    gaps = GAP * (len(ordered) - 1)
    row = (sum(d.width for d in ordered), max(d.height for d in ordered))
    col = (max(d.width for d in ordered), sum(d.height for d in ordered))
    # Scale that fits each arrangement into max_edge (gaps are fixed pixels).
    s_row = min((max_edge - gaps) / row[0], max_edge / row[1])
    s_col = min(max_edge / col[0], (max_edge - gaps) / col[1])
    stacked = s_col > s_row
    scale = s_col if stacked else s_row
    tiles = []
    offset = 0.0
    for d in ordered:
        w, h = round(d.width * scale), round(d.height * scale)
        if stacked:
            tiles.append(Tile(d, 0, offset, w, h))
            offset += h + GAP
        else:
            tiles.append(Tile(d, offset, 0, w, h))
            offset += w + GAP
    width = int(max(t.left + t.width for t in tiles))
    height = int(max(t.top + t.height for t in tiles))
    return Layout(tuple(tiles), scale, width, height)


# ── macOS ──────────────────────────────────────────────────────────────────


def active_displays() -> List[Display]:
    """Active displays, main first (the order ``screencapture`` writes them)."""
    import Quartz

    err, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if err:
        raise OSError(f"CGGetActiveDisplayList failed ({err})")
    main = Quartz.CGMainDisplayID()
    out = []
    for did in ids[:count]:
        b = Quartz.CGDisplayBounds(did)
        out.append(Display(
            id=int(did), x=b.origin.x, y=b.origin.y,
            width=b.size.width, height=b.size.height,
            main=did == main, builtin=bool(Quartz.CGDisplayIsBuiltin(did)),
        ))
    return out


def cursor_position() -> Optional[Tuple[float, float]]:
    try:
        import Quartz

        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return p.x, p.y
    except Exception:  # noqa: BLE001
        return None


def _load(path: Path):
    import Quartz
    from Foundation import NSURL

    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    image = source and Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if not image:
        raise OSError(f"could not read screenshot {path.name}")
    return image


def _pair(displays: Sequence[Display], images: list) -> dict:
    """display id → image.  ``screencapture`` follows the active display list;
    if an image's shape disagrees with its display, match by aspect ratio."""
    import Quartz

    if len(images) != len(displays):
        raise OSError(
            f"screencapture returned {len(images)} images for {len(displays)} screens"
        )

    def aspect(img) -> float:
        return Quartz.CGImageGetWidth(img) / max(1, Quartz.CGImageGetHeight(img))

    def fits(d: Display, img) -> bool:
        return abs(aspect(img) - d.width / d.height) < 0.02

    if all(fits(d, img) for d, img in zip(displays, images)):
        return {d.id: img for d, img in zip(displays, images)}
    left = list(images)
    out = {}
    for d in displays:
        best = min(left, key=lambda img: abs(aspect(img) - d.width / d.height))
        out[d.id] = best
        left.remove(best)
    return out


def compose(images: dict, lay: Layout, out: Path, quality: float = 0.8) -> None:
    """Draw each display's image into its tile and save *out* as JPEG."""
    import Quartz
    from Foundation import NSURL

    space = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, lay.width, lay.height, 8, 0, space, Quartz.kCGImageAlphaNoneSkipLast
    )
    if ctx is None:
        raise OSError("could not create the screenshot canvas")
    Quartz.CGContextSetRGBFillColor(ctx, 0.5, 0.5, 0.5, 1.0)  # the grey gaps
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, lay.width, lay.height))
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    for tile in lay.tiles:
        # Core Graphics puts the origin bottom-left.
        rect = Quartz.CGRectMake(
            tile.left, lay.height - tile.top - tile.height, tile.width, tile.height
        )
        Quartz.CGContextDrawImage(ctx, rect, images[tile.display.id])
    image = Quartz.CGBitmapContextCreateImage(ctx)
    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(str(out)), "public.jpeg", 1, None
    )
    Quartz.CGImageDestinationAddImage(
        dest, image, {Quartz.kCGImageDestinationLossyCompressionQuality: quality}
    )
    if not Quartz.CGImageDestinationFinalize(dest):
        raise OSError("could not write the screenshot")


def capture(out: Path, max_edge: int, which: str = "all") -> Layout:
    """Screenshot the displays *which* asks for into *out* (one JPEG)."""
    displays = active_displays()
    wanted = select(
        displays, which, cursor_position() if which == "cursor" else None
    )
    lay = layout(wanted, max_edge)
    with tempfile.TemporaryDirectory(prefix="jarvis-displays-") as tmp:
        shots = [Path(tmp) / f"display{i}.png" for i in range(len(displays))]
        subprocess.run(
            ["screencapture", "-x", "-t", "png", *map(str, shots)],
            check=True, capture_output=True, timeout=20,
        )
        images = _pair(displays, [_load(p) for p in shots if p.exists()])
        compose(images, lay, out)
    return lay


__all__ = [
    "Display", "Layout", "Tile", "WHICH", "active_displays", "capture",
    "compose", "layout", "select",
]
