"""Table structure uses controlled OCR; integration tests read the image pixels."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from tjm.desktop import DesktopError
from tjm.errors import ExtractionError
from tjm.ocr import Token
from tjm import ui_table
from tjm.ui_table import read_table


ROOT = Path(__file__).resolve().parents[1]


def test_unassigned_selector_pixels_refuse_with_locatable_evidence(monkeypatch):
    image = Image.new("RGB", (80, 90), "white")
    ImageDraw.Draw(image).rectangle((25, 30, 34, 39), fill="black")
    monkeypatch.setattr(ui_table, "_read_tokens", lambda *args, **kwargs: [])
    header = {"font": 12, "left": 10, "right": 60, "top": 0,
              "bottom": 10, "columns": [(10, 60)]}
    with pytest.raises(DesktopError, match="Unassigned visible content") as caught:
        ui_table._row_boxes(image, header, (10, 10, 60, 70))
    assert caught.value.evidence == {"selector_unassigned_content": {
        "pixel_count": 100, "threshold": 6,
        "body_bounds": [10, 10, 60, 70], "content_bounds": [25, 30, 35, 40],
    }}


def font():
    for name in ("/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, 18)
        except OSError:
            pass
    pytest.skip("A TrueType font is needed to render OCR table fixtures.")


def grid(headers, rows, *, offset=(70, 115), widths=None, blanks=3, ruled=True):
    widths = widths or [180] * len(headers)
    left, top = offset
    width, height = sum(widths), 36
    bottom = top + height * (1 + len(rows) + blanks)
    image = Image.new("RGB", (left + width + 65, bottom + 50), (238, 238, 238))
    draw = ImageDraw.Draw(image)
    draw.text((left + 5, top - 50), "Search: this must never become a table row", font=font(), fill="black")
    draw.rectangle((left, top + height, left + width, bottom), fill="white")
    draw.rectangle((left, top, left + width, top + height), fill=(238, 238, 238))
    edges = [left]
    for column_width in widths:
        edges.append(edges[-1] + column_width)
    for x in edges:
        draw.line((x, top, x, top + height), fill=(160, 160, 160), width=2)
        if ruled:
            draw.line((x, top + height, x, bottom), fill=(190, 190, 190), width=1)
    draw.line((left, top + height, left + width, top + height), fill=(170, 170, 170), width=2)
    for index, label in enumerate(headers):
        draw.text((edges[index] + 8, top + 7), label, font=font(), fill="black")
    for row_index, row in enumerate(rows):
        for index, value in enumerate(row):
            draw.text((edges[index] + 8, top + height * (row_index + 1) + 7),
                      value, font=font(), fill="black")
    if ruled:
        for index in range(2, len(rows) + blanks + 2):
            draw.line((left, top + height * index, left + width, top + height * index),
                      fill=(190, 190, 190), width=1)
    draw.line((left, bottom, left + width, bottom), fill=(170, 170, 170), width=2)
    return image


def read(image, headers, tmp_path):
    return read_table(image, (0, 0, *image.size), headers, tmp_path)


def controlled_grid_ocr(monkeypatch, headers, rows, *, offset=(70, 115), widths=None):
    """Supply transcription separately from the pixel geometry under test."""
    widths = widths or [180] * len(headers)
    left, top = offset
    header_tokens = []
    for label, width in zip(headers, widths, strict=True):
        header_tokens.append(Token(label, left + 8, top + 11, len(label) * 8, 15, 99))
        left += width
    cells = iter(headers + [value for row in rows for value in row])

    def tokens(image, *, psm):
        if psm == 11:
            return header_tokens
        assert psm == 7
        value = next(cells)
        return [Token(value, 15, 15, min(len(value) * 12, image.width - 30), 45, 99)]

    monkeypatch.setattr(ui_table, "ocr_tokens", tokens)


@pytest.mark.parametrize("offset,widths", [((70, 115), [180, 270, 170, 110]),
                                          ((130, 190), [230, 310, 190, 125])])
def test_changed_layout_preserves_order_identity_and_decimal_text(
    tmp_path, monkeypatch, offset, widths
):
    headers = ["No.", "Name", "SKU", "Price"]
    rows = [["CUST0042", "Maple Studio", "MAT-AX-12", "EUR 129.50"],
            ["CUST0043", "River Works", "CAB-Q7-09", "EUR 46.20"]]
    controlled_grid_ocr(monkeypatch, headers, rows, offset=offset, widths=widths)
    image = grid(headers, rows, offset=offset, widths=widths)
    result = read(image, headers, tmp_path)
    assert [row["values"] for row in result["rows"]] == [dict(zip(headers, row)) for row in rows]
    assert result["evidence"]["bounded_body"] is True
    assert result["evidence"]["visible_only"] is True
    assert result["evidence"]["not_scrolled_requires_caller_check"] is True
    evidence = json.loads(Path(result["evidence"]["report"]).read_text())
    assert all(Path(cell["crop"]).is_file() and cell["tokens"] for cell in evidence["evidence"]["cells"])
    assert len(result["headers"]) == 4


@pytest.mark.parametrize("offset,widths", [((70, 115), [180, 270, 170, 110]),
                                          ((130, 190), [230, 310, 190, 125])])
def test_real_ocr_changed_layout_reads_values_or_requests_review(tmp_path, offset, widths):
    headers = ["No.", "Name", "SKU", "Price"]
    rows = [["CUST0042", "Maple Studio", "MAT-AX-12", "EUR 129.50"],
            ["CUST0043", "River Works", "CAB-Q7-09", "EUR 46.20"]]
    image = grid(headers, rows, offset=offset, widths=widths)
    try:
        result = read(image, headers, tmp_path)
    except DesktopError:
        # OCR engines/fonts can differ. Uncertain transcription must retain a
        # review report; an accepted read must preserve every identifier/amount.
        evidence = json.loads(next(tmp_path.glob("table-*/readback.json")).read_text())
        assert evidence["status"] == "review_required"
        assert evidence["error"]
        assert Path(evidence["scope_crop"]).is_file()
    else:
        assert [row["values"] for row in result["rows"]] == [
            dict(zip(headers, row)) for row in rows
        ]


def test_single_sku_identity_is_read_when_optional_name_is_clipped(tmp_path):
    image = grid(["SKU", "Name", "Price"], [["PRD-12", "Cab...", "EUR 90.00"],
                                            ["PRD-13", "Desk...", "EUR 70.00"]])
    result = read(image, ["SKU"], tmp_path)
    assert [row["values"] for row in result["rows"]] == [{"SKU": "PRD-12"}, {"SKU": "PRD-13"}]
    assert [header["text"] for header in result["headers"]] == ["SKU"]
    assert result["evidence"]["bounded_body"] is True
    assert result["evidence"]["not_scrolled_requires_caller_check"] is True


def test_single_sku_request_still_rejects_clipped_identity(tmp_path):
    image = grid(["SKU", "Name"], [["PRD-...", "Cabinet"]])
    with pytest.raises(DesktopError, match="clipped|Low-confidence"):
        read(image, ["SKU"], tmp_path)


def test_single_sku_request_still_rejects_duplicate_identity_headers(tmp_path):
    image = grid(["SKU", "SKU", "Name"], [["PRD-12", "PRD-13", "Cabinet"]])
    with pytest.raises(DesktopError, match="occurs more than once"):
        read(image, ["SKU"], tmp_path)


def test_recorded_product_selector_can_read_only_exact_sku(tmp_path):
    trace = ROOT / "tests/fixtures/gui/product-selector"
    if not trace.is_dir():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    observation = json.loads((trace / "observation.json").read_text())["observations"][0]
    with Image.open(trace / "table.png") as image:
        result = read_table(image, (0, 0, *image.size), ["Item No."], tmp_path,
                            observed_table_box=observation["table_box"])
    assert [row["values"] for row in result["rows"]] == [{"Item No.": "CHR-ERG-01"}]
    assert result["evidence"]["native_boundary_supplied"] is True
    assert result["evidence"]["bounded_body"] is True


def test_empty_bounded_grid_does_not_read_search_query(tmp_path):
    headers = ["SKU", "Name", "Price"]
    result = read(grid(headers, []), headers, tmp_path)
    assert result["rows"] == []


def test_selected_blue_row_with_white_text_is_not_reported_as_absent(tmp_path):
    headers = ["SKU", "Name", "Price"]
    image = grid(headers, [["", "", ""]])
    draw = ImageDraw.Draw(image)
    draw.rectangle((72, 154, 609, 185), fill=(42, 117, 218))
    for x, value in zip((78, 258, 438), ("SKU-12", "Cabinet", "EUR 90.00")):
        draw.text((x, 158), value, font=font(), fill="white")
    result = read(image, headers, tmp_path)
    assert [r["values"] for r in result["rows"]] == [dict(zip(headers, ["SKU-12", "Cabinet", "EUR 90.00"]))]
    assert all(cell["preparation"] == "solid color background contrast"
               for cell in result["evidence"]["cells"])


def test_unruled_selector_reads_two_full_rows_in_visual_order(tmp_path):
    headers = ["No.", "First Name", "Name", "Company"]
    rows = [["CUST0081", "Lena", "Weber", "Harbor Studio"],
            ["CUST0082", "Jonas", "Meyer", "Grove Workshop"]]
    result = read(grid(headers, rows, widths=[160, 170, 160, 280], ruled=False), headers, tmp_path)
    assert [r["values"] for r in result["rows"]] == [dict(zip(headers, row)) for row in rows]
    assert result["evidence"]["row_geometry"] == "observed row baselines"


def test_clipped_required_identity_rejects_instead_of_matching_prefix(tmp_path, monkeypatch):
    headers = ["No.", "Company", "City"]
    rows = [["CUST0091", "Harbor St...", "Berlin"]]
    controlled_grid_ocr(monkeypatch, headers, rows)
    with pytest.raises(DesktopError, match="clipped"):
        read(grid(headers, rows), headers, tmp_path)
    evidence = json.loads(next(tmp_path.glob("table-*/readback.json")).read_text())
    assert evidence["status"] == "review_required"
    assert evidence["cells"][-1]["raw_text"] == "Harbor St..."
    assert evidence["cells"][-1]["tokens"]


def test_clipped_required_header_rejects(tmp_path):
    image = grid(["No.", "Comp...", "City"], [])
    with pytest.raises(DesktopError, match="headers"):
        read(image, ["No.", "Company", "City"], tmp_path)


def test_table_cut_off_at_scope_bottom_rejects_absence(tmp_path):
    headers = ["SKU", "Name", "Price"]
    image = grid(headers, [])
    image = image.crop((0, 0, image.width, image.height - 70))
    with pytest.raises(DesktopError, match="scope edge"):
        read(image, headers, tmp_path)


def test_occupied_final_grid_row_rejects_hidden_overflow(tmp_path):
    headers = ["SKU", "Name", "Price"]
    image = grid(headers, [["SKU-12", "Cabinet", "EUR 90.00"],
                           ["SKU-13", "Desk", "EUR 70.00"]], blanks=0)
    with pytest.raises(DesktopError, match="occupied"):
        read(image, headers, tmp_path)


def test_nonempty_row_after_blank_row_rejects_row_order(tmp_path):
    headers = ["SKU", "Name", "Price"]
    image = grid(headers, [["SKU-12", "Cabinet", "EUR 90.00"], ["", "", ""],
                           ["SKU-13", "Desk", "EUR 70.00"]])
    with pytest.raises(DesktopError, match="row order"):
        read(image, headers, tmp_path)


def test_low_confidence_identity_is_not_used(tmp_path, monkeypatch):
    actual_ocr = ui_table.ocr_tokens
    def uncertain(image, *, psm):
        return [replace(t, confidence=40) if t.text == "Cabinet" else t
                for t in actual_ocr(image, psm=psm)]
    monkeypatch.setattr(ui_table, "ocr_tokens", uncertain)
    headers = ["SKU", "Name", "Price"]
    with pytest.raises(DesktopError, match="Low-confidence"):
        read(grid(headers, [["SKU-12", "Cabinet", "EUR 90.00"]]), headers, tmp_path)


@pytest.mark.parametrize("token", [Token("Name", -1, 2, 10, 10, 99),
                                   Token("Name", 2, 2, 10, 10, float("nan")),
                                   Token("Name", 2, 2, 10, 10, 101)])
def test_invalid_ocr_token_stops_before_claiming_absence(tmp_path, monkeypatch, token):
    monkeypatch.setattr(ui_table, "ocr_tokens", lambda image, psm: [token])
    with pytest.raises(DesktopError, match="invalid table token"):
        read(Image.new("RGB", (100, 100), "white"), ["Name", "Price"], tmp_path)


def test_ocr_runtime_error_preserves_evidence_and_becomes_desktop_error(tmp_path, monkeypatch):
    def failed(image, psm):
        raise ExtractionError("Tesseract unavailable", code="ocr_runtime")
    monkeypatch.setattr(ui_table, "ocr_tokens", failed)
    with pytest.raises(DesktopError, match="OCR did not complete"):
        read(Image.new("RGB", (100, 100), "white"), ["Name", "Price"], tmp_path)
    assert json.loads(next(tmp_path.glob("table-*/readback.json")).read_text())["status"] == "review_required"


@pytest.mark.parametrize("scope", [(-1, 0, 100, 100), (0, 0, 150, 100),
                                   (0, 0, float("nan"), 100), (True, 0, 100, 100)])
def test_invalid_scope_stops_before_evidence_or_ocr(tmp_path, scope):
    with pytest.raises(DesktopError, match="scope"):
        read_table(Image.new("RGB", (100, 100)), scope, ["Name", "Price"], tmp_path)
    assert not list(tmp_path.iterdir())


def test_current_empty_selector_is_bounded_and_empty(tmp_path):
    path = ROOT / "tests/fixtures/gui/empty-contact-selector.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    result = read(image, ["No.", "First Name", "Name", "Company", "ZIP", "City"], tmp_path)
    assert result["rows"] == []


def test_actual_empty_selector_descenders_do_not_become_column_rules(tmp_path):
    path = ROOT / "tests/fixtures/gui/empty-contact-query.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    headers = ["Company", "First Name", "Name", "ZIP", "City"]
    result = read(Image.open(path), headers, tmp_path)
    assert result["rows"] == []
    assert {header["text"] for header in result["headers"]} == set(headers)
    assert {record["raw_text"] for record in result["evidence"]["headers"]}.issuperset(set(headers))
    # This actual glyph joined to the bottom bar previously invented columns
    # through Company and address type. Only the full-height separators remain.
    rules = result["evidence"]["geometry"]["column_rules"]
    assert not any(left <= 584 < right or left <= 1077 < right for left, right in rules)


def test_actual_populated_selector_after_resize_keeps_full_height_rules(tmp_path):
    path = ROOT / "tests/fixtures/gui/clipped-contact-after-resize.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path).convert("RGB")
    expected = ["No.", "First Name", "Name", "Company", "ZIP", "City"]
    # Captured header observations for this PNG include OCR boxes that absorb
    # rules. Replaying them isolates separator discovery from OCR-version drift.
    tokens = [Token("No.", 118, 128, 30, 30, 96.509979),
              Token("First", 228, 128, 45, 30, 95.820168),
              Token("Name", 282, 133, 55, 15, 13.967676),
              Token("Name", 396, 133, 55, 15, 96.788826),
              Token("Company", 515, 133, 92, 21, 95.227264),
              Token("ZIP.", 695, 128, 36, 30, 70.186333),
              Token("City", 843, 128, 37, 30, 96.629555)]
    geometry = ui_table._header_geometry(image, tokens, expected)
    # Geometry from this trace must retain all actual boundaries after the Name
    # column was shortened; many OCR word boxes absorb the header/row rule.
    assert geometry["font"] == 15
    assert (geometry["top"], geometry["bottom"]) == (125, 154)
    assert geometry["column_rules"] == [(56, 59), (206, 209), (356, 359), (484, 487),
                                        (634, 637), (784, 787), (934, 937), (1084, 1087),
                                        (1234, 1237), (1384, 1387)]


def test_real_populated_selector_after_resize_requests_review(tmp_path):
    path = ROOT / "tests/fixtures/gui/clipped-contact-after-resize.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    # The visible Company is clipped. Whether OCR first reports clipping or
    # uncertainty, it must not return an accepted identity from this image.
    with pytest.raises(DesktopError):
        read(Image.open(path).convert("RGB"),
             ["No.", "First Name", "Name", "Company", "ZIP", "City"], tmp_path)
    evidence = json.loads(next(tmp_path.glob("table-*/readback.json")).read_text())
    assert evidence["status"] == "review_required"


@pytest.mark.parametrize("filename,scope", [
    ("clipped-contact-query.png", None),
    ("clipped-debtor-list.png", (550, 1090, 2704, 1686)),
])
def test_actual_clipped_contact_traces_fail_readback(tmp_path, filename, scope):
    path = ROOT / "tests/fixtures/gui" / filename
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    with pytest.raises(DesktopError, match="clipped"):
        read_table(image, scope or (0, 0, *image.size),
                   ["No.", "First Name", "Name", "Company", "ZIP", "City"], tmp_path)


def test_actual_payment_table_preserves_all_requested_terms(tmp_path):
    path = ROOT / "tests/fixtures/gui/payment-table.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    headers = ["Name", "Description", "Discount", "Disc. Days", "Net Days"]
    result = read_table(Image.open(path), (550, 1090, 2704, 1686), headers, tmp_path)
    assert [r["values"] for r in result["rows"]] == [dict(zip(headers, ["Cash", "Pay Cash", "0%", "0", "0"]))]


def test_current_documents_native_boundary_closes_short_footer_without_waiving_checks(tmp_path):
    path = ROOT / "tests/fixtures/gui/empty-document-table.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    scope = (606, 1110, 3000, 1800)
    headers = ["Document", "Date", "Name", "Cust.Ref.", "State", "Total", "Printed"]
    with pytest.raises(DesktopError, match="scope edge"):
        read_table(image, scope, headers, tmp_path)
    # This box is independently derived from the trace's captured AXScrollArea:
    # screen Rect(532,641,1164,280), screen origin(200,25), image scale2.
    native = (664, 1232, 2992, 1792)
    result = read_table(image, scope, headers, tmp_path, observed_table_box=native)
    assert result["rows"] == []
    assert result["evidence"]["native_boundary_supplied"] is True
    assert result["evidence"]["native_boundary_used"] is True
    assert result["evidence"]["observed_table_box"] == list(native)
    assert result["evidence"]["read_scope"] == list(native)
    assert result["evidence"]["visible_only"] is True
    assert result["evidence"]["not_scrolled_requires_caller_check"] is True
    native_crop = image.crop(native)
    with pytest.raises(DesktopError, match="scope edge"):
        read(native_crop, headers, tmp_path)


@pytest.mark.parametrize("native", [(-1, 0, 100, 100), (0, 0, 101, 100),
                                    (0, 0, 100, float("nan")), (True, 0, 100, 100),
                                    (10, 10, 10, 20)])
def test_invalid_native_boundary_stops_before_evidence_or_ocr(tmp_path, native):
    with pytest.raises(DesktopError, match="native table boundary"):
        read_table(Image.new("RGB", (100, 100)), (0, 0, 100, 100),
                   ["Name", "Price"], tmp_path, observed_table_box=native)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("native", [(650, 1200, 2992, 1792), (664, 1232, 2940, 1792)])
def test_native_boundary_contradicting_current_header_or_frame_rejects(tmp_path, native):
    path = ROOT / "tests/fixtures/gui/empty-document-table.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    with pytest.raises(DesktopError, match="contradicts"):
        read_table(Image.open(path), (606, 1110, 3000, 1800),
                   ["Document", "Date", "Name", "Cust.Ref.", "State", "Total", "Printed"],
                   tmp_path, observed_table_box=native)


@pytest.mark.parametrize("rows,blanks,error", [
    ([["SKU-12", "Cab...", "EUR 90.00"]], 3, "clipped"),
    ([["SKU-12", "Cabinet", "EUR 90.00"], ["SKU-13", "Desk", "EUR 70.00"]], 0, "occupied"),
])
def test_native_boundary_does_not_waive_clipped_cells_or_occupied_final_row(tmp_path, rows, blanks, error):
    headers = ["SKU", "Name", "Price"]
    image = grid(headers, rows, blanks=blanks)
    # Bounds include the observed one-pixel frame around this synthetic table.
    native = (69, 114, 612, 115 + 36 * (1 + len(rows) + blanks) + 2)
    with pytest.raises(DesktopError, match=error):
        read_table(image, (0, 0, *image.size), headers, tmp_path, observed_table_box=native)


def test_actual_fitted_selector_native_white_filler_keeps_complete_identity(tmp_path):
    path = ROOT / "tests/fixtures/gui/fitted-contact-table.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    headers = ["No.", "First Name", "Name", "Company", "ZIP", "City"]
    result = read_table(Image.open(path), (0, 0, 1600, 1100), headers, tmp_path,
                        observed_table_box=(0, 124, 1586, 1002))
    assert [row["values"] for row in result["rows"]] == [dict(zip(headers, [
        "CUST000001", "Marta", "Klein", "Northstar Office GmbH", "10117", "Berlin",
    ]))]
    checks = result["evidence"]["native_header_alignment"]
    assert checks["gray_right_frame"] is False
    assert checks["white_filler_after_complete_final_separator"] is True


def test_actual_empty_product_selector_native_blank_header_filler_is_complete(tmp_path):
    path = ROOT / "tests/fixtures/gui/empty-product-table.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    headers = ["Item No.", "Name", "Description", "Stock", "Price", "VAT"]
    result = read_table(Image.open(path), (0, 0, 1602, 1102), headers, tmp_path,
                        observed_table_box=(52, 126, 1600, 1002))
    assert result["rows"] == []
    checks = result["evidence"]["native_header_alignment"]
    assert checks["gray_right_frame"] is False
    assert checks["blank_white_header_filler_after_final_separator"] is True
    assert {header["text"] for header in result["headers"]} == set(headers)


def test_actual_order_grid_neutral_dark_header_has_observed_full_height_rules(tmp_path):
    path = ROOT / "tests/fixtures/gui/dark-order-grid.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    headers = ["Qty.", "Item No.", "Name", "Description", "VAT", "U.Price", "Discount", "Price"]
    # Items label starts604 and is16px tall. Including half a glyph of margin
    # preserves the complete header frame rather than cropping its top4 pixels.
    region = (550, 596, 2704, 773)
    crop = image.crop(region)
    geometry = ui_table._header_geometry(crop, ui_table._read_tokens(crop, psm=11), headers)
    assert geometry["dark_neutral_header"] is True
    assert geometry["header_background"] == 159
    assert (geometry["top"], geometry["bottom"]) == (5, 34)
    assert geometry["column_rules"] == [(84, 86), (144, 146), (294, 296), (444, 446),
                                        (594, 596), (744, 746), (894, 896), (1044, 1046),
                                        (1194, 1196), (1344, 1346), (1494, 1496)]
    clipped = image.crop((550, 604, 2704, 773))
    with pytest.raises(DesktopError):
        ui_table._header_geometry(clipped, ui_table._read_tokens(clipped, psm=11), headers)
    text, evidence = ui_table._cell(crop, (147, 7, 292, 31), tmp_path / "quantity-header.png", header=True)
    assert text == "Qty."
    assert evidence["preparation"] == "solid neutral dark background contrast"
