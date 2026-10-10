"""Painted cells use observed rules and guarded SKU identity, never target input."""

from types import SimpleNamespace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import tjm.native_items as items
import tjm.ui_table as cells
from tjm.desktop import AXControl, DesktopError, Rect
from tjm.ocr import Token


def header_tokens(*, split_unit=False, extra_price=False):
    values = [("Qty.", 20, 40), ("Item", 75, 100), ("No.", 104, 125)]
    values += [("U.", 150, 162), ("Price", 165, 200)] if split_unit else [("U.Price", 150, 200)]
    values += [("Discount", 225, 265), ("Price", 310, 350)]
    if extra_price:
        values.append(("Price", 375, 410))
    return [Token(text, left, 10, right-left, 16, 95) for text, left, right in values]


@pytest.mark.parametrize("split_unit", [False, True])
def test_standalone_price_excludes_only_price_inside_complete_unit_caption(split_unit):
    boxes, _ = items._header_boxes(header_tokens(split_unit=split_unit), "Price")
    assert boxes["Price"] == (310, 10, 350, 26)
    assert boxes["U.Price"] == (150, 10, 200, 26)


@pytest.mark.parametrize("changed", [
    lambda tokens: [token for token in tokens if token.text not in {"U.Price", "U."}],
    lambda tokens: tokens + [Token("Price", 375, 10, 35, 16, 95)],
    lambda tokens: tokens + [Token("U.Price", 420, 10, 60, 16, 95)],
])
def test_missing_or_ambiguous_price_identity_is_refused(changed):
    with pytest.raises(DesktopError):
        items._header_boxes(changed(header_tokens()), "Price")


def grid():
    image = Image.new("RGB", (420, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 46, 408, 73), fill=(65, 141, 227))
    for x in (10, 140, 220, 300, 410):
        draw.rectangle((x, 44, x+1, 135), fill=(178, 178, 178))
    return image


@pytest.mark.parametrize("scale,dx,dy", [(1, 0, 0), (2, 23, 17)])
def test_rules_follow_current_moved_and_scaled_row(scale, dx, dy):
    original = grid()
    expanded = original.resize((420*scale, 160*scale), Image.Resampling.NEAREST)
    image = Image.new("RGB", (expanded.width+dx, expanded.height+dy), "white")
    image.paste(expanded, (dx, dy))
    header = (310*scale+dx, 10*scale+dy, 350*scale+dx, 26*scale+dy)
    row = (46*scale+dy, 74*scale+dy)
    box, evidence = items._painted_cell_box(image, header, row,
        (10*scale+dx, 40*scale+dy, 412*scale+dx, 140*scale+dy),
        (dx, dy, image.width, image.height))
    assert box == (302*scale+dx, row[0]+2, 410*scale+dx, row[1]-2)
    assert evidence["right_rule_span"] == [410*scale+dx, 412*scale+dx]


def test_missing_terminal_rule_is_not_replaced_by_native_table_edge():
    image = grid()
    ImageDraw.Draw(image).rectangle((410, 44, 411, 135), fill="white")
    with pytest.raises(DesktopError, match="two unique shared column rules"):
        items._painted_cell_box(image, (310, 10, 350, 26), (46, 74),
                                (10, 40, 412, 140), (0, 0, 420, 160))


def test_missing_internal_rule_cannot_merge_discount_into_price():
    image = grid()
    ImageDraw.Draw(image).rectangle((300, 44, 301, 135), fill=(65, 141, 227))
    with pytest.raises(DesktopError, match="merge another header"):
        items._painted_cell_box(image, (310, 10, 350, 26), (46, 74),
            (10, 40, 412, 140), (0, 0, 420, 160),
            header_tokens=({"text": "Discount", "box": [225, 10, 265, 26]},))


def test_rule_inside_header_makes_column_boundaries_ambiguous():
    image = grid()
    ImageDraw.Draw(image).rectangle((330, 44, 331, 135), fill=(178, 178, 178))
    with pytest.raises(DesktopError, match="two unique shared column rules"):
        items._painted_cell_box(image, (310, 10, 350, 26), (46, 74),
                                (10, 40, 412, 140), (0, 0, 420, 160))


@pytest.fixture
def reader(monkeypatch, tmp_path):
    native = SimpleNamespace(cf=SimpleNamespace(CFEqual=lambda a, b: a is b), keys=[])
    table_element, sku_element, window_element = object(), object(), object()
    table = AXControl("AXScrollArea", None, None, Rect(10, 40, 402, 100), True, table_element)
    sku = AXControl("AXTextField", None, "EXACT-SKU", Rect(75, 46, 50, 28), True, sku_element)
    capture = SimpleNamespace(image=grid(), window_title="Main", window_bounds=Rect(0, 0, 420, 160),
                              window_element=window_element, process_identity=(1,),
                              screen_rect=Rect(0, 0, 420, 160), metadata=lambda: {"pid": 1})
    native._control = lambda element: table if element is table_element else sku
    native.guard = lambda current: True

    def key(current, name, *, focus_guard):
        assert focus_guard() is True
        native.keys.append(name)

    native.key = key
    ui = SimpleNamespace(desktop=native, actions=[], sequence=1, evidence_dir=tmp_path,
                         snapshot=lambda *args, **kwargs: capture)
    monkeypatch.setattr(items, "_vat_source", lambda *args: (
        lambda: True, {"document_number": "PO1", "reference": "SOURCE-REF"}))
    observation = items.ItemObservation(capture, table, (10, 40, 412, 140), (0, 0, 420, 160),
        {"Price": (310, 10, 350, 26), "VAT": (150, 10, 200, 26)}, (46, 74), (330, 60), "capture.png")
    monkeypatch.setattr(items, "open_item_cell", lambda *args, **kwargs: items.NativeCell(
        capture, sku, lambda: True, "Item No.", "EXACT-SKU", table, observation=observation))
    monkeypatch.setattr(items, "_observe_item", lambda *args: observation)
    reads = []

    def read_cell(image, box, path):
        reads.append(box)
        return "EUR 450.00", {"crop": str(path), "box": list(box), "raw_text": "EUR 450.00"}

    monkeypatch.setattr(cells, "_cell", read_cell)
    return native, ui, capture, table, sku, reads


def test_price_read_uses_observed_cell_ocr_without_target_click_or_value_derivation(reader):
    native, ui, _, _, _, reads = reader
    # No click/double_click/input API exists on this desktop fixture. Only the
    # previously opened SKU's guarded Escape is allowed during this read.
    result = items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="Price")
    assert result["text"] == "EUR 450.00"
    assert result["evidence"]["native_sku"] == "EXACT-SKU"
    assert result["evidence"]["target_input_requests"] == 0
    assert native.keys == ["escape"]
    assert reads == [(302, 48, 410, 72)]
    assert ui.actions[-1]["verified"] is True


def test_changed_native_sku_is_refused_before_any_target_read(reader):
    native, ui, _, _, sku, reads = reader
    native._control = lambda element: AXControl(sku.role, sku.title, "OTHER", sku.bounds,
                                               sku.enabled, sku.element)
    with pytest.raises(DesktopError, match="native SKU changed"):
        items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="Price")
    assert not reads and not native.keys


def test_different_native_table_stops_before_painted_read(reader, monkeypatch):
    native, ui, capture, table, sku, reads = reader
    other = AXControl(table.role, None, None, table.bounds, True, object())
    monkeypatch.setattr(items, "open_item_cell", lambda *args, **kwargs: items.NativeCell(
        capture, sku, lambda: True, "Item No.", "EXACT-SKU", other))
    with pytest.raises(DesktopError, match="verified source SKU"):
        items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="Price")
    assert not reads and native.keys == ["escape"]


def test_clipped_cell_preserves_crop_and_geometry_without_retry(reader, monkeypatch):
    native, ui, _, _, _, reads = reader

    def clipped(image, box, path):
        reads.append(box)
        error = DesktopError("A table cell is clipped")
        error.table_cell_evidence = {"crop": str(path), "raw_text": "VAT 19..."}
        raise error

    monkeypatch.setattr(cells, "_cell", clipped)
    monkeypatch.setattr(items, "_vat_text_box", lambda desktop, capture, box: (
        box, {"slot_box": [202, 48, 220, 72], "fixture": "arrow shape separately tested"}))
    with pytest.raises(DesktopError, match="clipped") as stopped:
        items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="VAT")
    evidence = stopped.value.painted_item_evidence
    assert evidence["cell_box"] == [142, 48, 220, 72]
    assert evidence["cell_readback"]["raw_text"] == "VAT 19..."
    assert len(reads) == 1 and native.keys == ["escape"]
    assert ui.actions[-1]["verified"] is False


def test_post_read_pixel_change_rejects_the_observation(reader, monkeypatch):
    _, ui, capture, _, _, reads = reader
    changed = SimpleNamespace(**vars(capture))
    changed.image = capture.image.copy()
    changed.image.putpixel((320, 60), (0, 0, 0))
    ui.snapshot = lambda label, **kwargs: changed if label == "after-painted-item-read" else capture
    with pytest.raises(DesktopError, match="pixels changed"):
        items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="Price")
    assert len(reads) == 1
    assert ui.actions[-1]["verified"] is False


@pytest.mark.parametrize("row_index", [-1, True, "0"])
def test_invalid_row_index_stops_before_observation(reader, row_index):
    native, ui, _, _, _, reads = reader
    ui.snapshot = lambda *args, **kwargs: pytest.fail("Invalid row must not inspect the UI")
    with pytest.raises(ValueError, match="source row index"):
        items.read_painted_item_cell(ui, expected_sku="EXACT-SKU", header="Price", row_index=row_index)
    assert not native.keys and not reads


def test_recorded_price_focus_outline_is_excluded_before_cell_ocr(tmp_path):
    trace = (Path(__file__).resolve().parents[1] / "tests/fixtures/gui"
             / "price-cell-focus.png")
    # Independently recorded header/table/row geometry is a regression fixture,
    # never a source of production coordinates or expected transaction values.
    image = Image.open(trace)
    box, _ = items._painted_cell_box(image, (1964, 605, 2014, 622), (630, 658),
                                    (694, 600, 2978, 816), (606, 596, 3000, 790))
    assert box == (1954, 632, 2102, 656)
    text, evidence = cells._cell(image, box, tmp_path / "recorded-price.png")
    assert text == "EUR450.00"
    assert all(token["confidence"] >= 70 for token in evidence["tokens"])


def arrow_capture(widths=(6, 8, 8, 6, 4, 2), *, background=(186, 214, 251), shift=0):
    image = Image.new("RGB", (80, 24), background)
    draw = ImageDraw.Draw(image)
    for index, width in enumerate(widths):
        left = 62+11-width//2+shift
        draw.rectangle((left, 9+index, left+width-1, 9+index), fill=(40, 40, 40))
    return SimpleNamespace(image=image, scale_x=2, scale_y=2)


@pytest.fixture
def arrow_resource(monkeypatch):
    resource = {"logical_dimensions": [9, 5], "resource": "fixture/down_2.png"}
    monkeypatch.setattr(items, "_vat_arrow_resource", lambda desktop: resource)
    return resource


def test_only_verified_source_sized_arrow_slot_is_excluded(arrow_resource):
    box, evidence = items._vat_text_box(None, arrow_capture(), (0, 0, 80, 24))
    assert box == (0, 0, 62, 24)
    assert evidence["slot_box"] == [62, 0, 80, 24]
    assert evidence["arrow_row_widths"] == [6, 8, 8, 6, 4, 2]
    assert evidence["arrow_glyph_box"] == [69, 9, 77, 15]


@pytest.mark.parametrize("capture", [
    arrow_capture((), background=(186, 214, 251)),
    arrow_capture(background=(255, 255, 255)),
    arrow_capture((8, 8, 8, 8, 8, 8)),
    arrow_capture((2, 4, 6, 8, 8, 6)),
    arrow_capture((6, 8, 6, 8, 4, 2)),
    arrow_capture(shift=3),
])
def test_unknown_absent_clipped_or_nontriangle_arrow_stops_without_exclusion(arrow_resource, capture):
    with pytest.raises(DesktopError):
        items._vat_text_box(None, capture, (0, 0, 80, 24))


def test_triangle_with_disconnected_dark_content_is_not_accepted(arrow_resource):
    capture = arrow_capture()
    capture.image.putpixel((72, 10), (186, 214, 251))
    with pytest.raises(DesktopError, match="contiguous triangle"):
        items._vat_text_box(None, capture, (0, 0, 80, 24))


def test_arrow_resource_requires_the_connected_apps_unique_core_jar(tmp_path):
    native = SimpleNamespace(identity=(1, "test", str(tmp_path), 1))
    plugins = tmp_path / "Contents/Eclipse/plugins"
    plugins.mkdir(parents=True)
    for name in ("org.eclipse.nebula.widgets.nattable.core_1.jar",
                 "org.eclipse.nebula.widgets.nattable.core_2.jar"):
        (plugins / name).write_bytes(b"not a jar")
    with pytest.raises(DesktopError, match="one NatTable core jar"):
        items._vat_arrow_resource(native)


def test_recorded_full_vat_text_has_high_confidence_after_proven_arrow_exclusion(tmp_path):
    root = Path(__file__).resolve().parents[1]
    trace = (root / "tests/fixtures/gui"
             / "vat-cell-arrow.png")
    capture = SimpleNamespace(image=Image.open(trace), scale_x=2, scale_y=2)
    # Reproduce the exact icon resource in an isolated fake bundle; the test
    # must not depend on an installed application or its business data.
    from zipfile import ZipFile
    bundle = tmp_path / "Fixture.app"
    plugins = bundle / "Contents/Eclipse/plugins"
    plugins.mkdir(parents=True)
    with ZipFile(plugins / "org.eclipse.nebula.widgets.nattable.core_fixture.jar", "w") as archive:
        archive.write(root / "tests/fixtures/nattable/down_2.png",
                      "org/eclipse/nebula/widgets/nattable/images/down_2.png")
    native = SimpleNamespace(identity=(1, "test", str(bundle), 1))
    box, evidence = items._vat_text_box(native, capture, (1504, 632, 1802, 656))
    assert box == (1504, 632, 1784, 656)
    assert evidence["resource"]["logical_dimensions"] == [9, 5]
    assert evidence["arrow_row_widths"] == [6, 8, 8, 6, 4, 2]
    text, reading = cells._cell(capture.image, box, tmp_path / "recorded-vat-text.png")
    assert text == "VAT 19% (19.0%)"
    assert all(token["confidence"] >= 90 for token in reading["tokens"])
