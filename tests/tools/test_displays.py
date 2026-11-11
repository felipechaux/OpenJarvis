"""displays.py: tiling every monitor into one screenshot and mapping back."""

from __future__ import annotations

import sys

import pytest

from openjarvis.tools import displays
from openjarvis.tools.displays import GAP, Display, layout, select

LAPTOP = Display(1, 0, 0, 1470, 956, main=True, builtin=True)
MONITOR = Display(2, 1470, 0, 1920, 1080)


def test_two_screens_are_stacked_when_that_keeps_them_bigger():
    lay = layout([MONITOR, LAPTOP], 1568)
    # Side by side would be 3390 points wide; stacked is 2036 tall.
    assert [t.display for t in lay.tiles] == [LAPTOP, MONITOR]  # left to right
    assert lay.tiles[1].top == lay.tiles[0].height + GAP
    assert max(lay.width, lay.height) <= 1568
    assert lay.scale > 1568 / 3390 + 0.1


def test_portrait_screens_go_side_by_side():
    a, b = Display(1, 0, 0, 600, 1200), Display(2, 600, 0, 600, 1200)  # portrait
    lay = layout([a, b], 1568)
    assert lay.tiles[1].left == lay.tiles[0].width + GAP and lay.tiles[1].top == 0


def test_single_screen_fills_the_long_edge():
    lay = layout([LAPTOP], 1568)
    assert lay.width == 1568 and len(lay.tiles) == 1


@pytest.mark.parametrize("display", [LAPTOP, MONITOR])
def test_image_corners_map_back_to_each_screen(display):
    lay = layout([LAPTOP, MONITOR], 1568)
    tile = next(t for t in lay.tiles if t.display == display)
    assert lay.to_screen(tile.left, tile.top) == (display.x, display.y)


def test_screens_left_of_and_above_the_main_one():
    left = Display(3, -1920, -200, 1920, 1080)
    lay = layout([LAPTOP, left], 1568)
    tile = next(t for t in lay.tiles if t.display == left)
    x, y = lay.to_screen(tile.left, tile.top)
    assert (round(x), round(y)) == (-1920, -200)


def test_the_gap_is_not_a_screen():
    lay = layout([LAPTOP, MONITOR], 1568)
    with pytest.raises(ValueError):
        lay.to_screen(5, lay.tiles[0].height + GAP / 2)


def test_description_names_both_screens_and_where_they_are():
    text = layout([LAPTOP, MONITOR], 1568).describe()
    assert "2 screens" in text
    assert (
        "top: the Mac's built-in screen (main display), physically on the left"
        in text
    )
    assert "bottom: an external monitor, physically on the right" in text


@pytest.mark.parametrize(
    "which, cursor, expected",
    [
        ("all", None, [LAPTOP, MONITOR]),
        ("main", None, [LAPTOP]),
        ("external", None, [MONITOR]),
        ("cursor", (2000, 500), [MONITOR]),
        ("cursor", None, [LAPTOP, MONITOR]),
    ],
)
def test_select(which, cursor, expected):
    assert select([LAPTOP, MONITOR], which, cursor) == expected


def test_no_external_monitor_falls_back_to_all():
    assert select([LAPTOP], "external") == [LAPTOP]


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Graphics")
def test_capture_tiles_one_image_per_display(monkeypatch, tmp_path):
    """Real Core Graphics compositing; screencapture replaced by solid PNGs."""
    import Quartz
    from Foundation import NSURL

    def solid(path, w, h, rgb):
        ctx = Quartz.CGBitmapContextCreate(
            None, w, h, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaNoneSkipLast,
        )
        Quartz.CGContextSetRGBFillColor(ctx, *rgb, 1.0)
        Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
        dest = Quartz.CGImageDestinationCreateWithURL(
            NSURL.fileURLWithPath_(str(path)), "public.png", 1, None
        )
        image = Quartz.CGBitmapContextCreateImage(ctx)
        Quartz.CGImageDestinationAddImage(dest, image, None)
        assert Quartz.CGImageDestinationFinalize(dest)

    def fake_screencapture(cmd, **kwargs):
        assert "-m" not in cmd
        first, second = cmd[-2:]  # main display first, like screencapture
        solid(first, 2940, 1912, (1, 0, 0))   # Retina laptop: red
        solid(second, 1920, 1080, (0, 0, 1))  # monitor: blue

    monkeypatch.setattr(displays, "active_displays", lambda: [LAPTOP, MONITOR])
    monkeypatch.setattr(displays.subprocess, "run", fake_screencapture)
    out = tmp_path / "screen.jpg"
    lay = displays.capture(out, 1568)

    image = displays._load(out)
    assert (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)) == (
        lay.width, lay.height,
    )
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
    row = Quartz.CGImageGetBytesPerRow(image)
    bpp = Quartz.CGImageGetBitsPerPixel(image) // 8

    def pixel(x, y):
        i = int(y) * row + int(x) * bpp
        return tuple(bytes(data[i:i + 3]))

    top, bottom = lay.tiles
    r, g, b = pixel(top.width / 2, top.height / 2)
    assert r > 200 and b < 60  # laptop on top
    r, g, b = pixel(bottom.width / 2, bottom.top + bottom.height / 2)
    assert b > 200 and r < 60  # monitor below
