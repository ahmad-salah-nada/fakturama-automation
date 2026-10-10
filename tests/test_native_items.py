"""Visible item occurrence boundaries; no native desktop actions or live claims."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tjm.desktop import DesktopError
from tjm.native_items import visible_row_bands


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "tests/fixtures/gui/selected-order-rows.png"
TRACE_HEADERS = {"Qty.": (705, 606, 744, 626), "Item No.": (822, 606, 906, 622)}


def test_recorded_selected_order_row_has_four_complete_visible_bands():
    with Image.open(TRACE) as image:
        assert visible_row_bands(image, TRACE_HEADERS, 773) == [
            (630, 658), (660, 688), (690, 718), (720, 748),
        ]


def simple_grid():
    """Known drawn boundaries plus a blue selected row and partial last row."""
    image = Image.new("RGB", (200, 165), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 46, 180, 73), fill=(65, 141, 227))
    # These independently specified rules enclose three rows, then a partial row.
    for top in (44, 74, 104, 134):
        draw.rectangle((10, top, 180, top + 1), fill=(178, 178, 178))
    boxes = {"Qty.": (20, 24, 60, 40), "Item No.": (90, 24, 160, 40)}
    return image, boxes


@pytest.mark.parametrize(("scale", "offset"), [(1, (27, 19)), (2, (53, 37))])
def test_moved_and_scaled_grid_preserves_occurrences_without_fixed_row_spacing(scale, offset):
    original, boxes = simple_grid()
    scaled = original.resize((original.width * scale, original.height * scale), Image.Resampling.NEAREST)
    dx, dy = offset
    image = Image.new("RGB", (scaled.width + dx + 20, scaled.height + dy + 20), "white")
    image.paste(scaled, offset)
    moved_boxes = {name: (left * scale + dx, top * scale + dy,
                          right * scale + dx, bottom * scale + dy)
                   for name, (left, top, right, bottom) in boxes.items()}
    expected = [(46 * scale + dy, 74 * scale + dy),
                (76 * scale + dy, 104 * scale + dy),
                (106 * scale + dy, 134 * scale + dy)]
    assert visible_row_bands(image, moved_boxes, 150 * scale + dy) == expected


def test_missing_rule_in_one_identity_column_does_not_merge_two_occurrences():
    image, boxes = simple_grid()
    ImageDraw.Draw(image).rectangle((90, 74, 159, 75), fill="white")
    with pytest.raises(DesktopError, match="boundaries are not established"):
        visible_row_bands(image, boxes, 150)


def test_grid_without_shared_rules_cannot_prove_an_item_occurrence():
    _, boxes = simple_grid()
    with pytest.raises(DesktopError, match="boundaries are not established"):
        visible_row_bands(Image.new("RGB", (200, 165), "white"), boxes, 150)


def test_partially_visible_first_row_has_no_complete_occurrence():
    image, boxes = simple_grid()
    with pytest.raises(DesktopError, match="boundaries are not established"):
        visible_row_bands(image, boxes, 60)
