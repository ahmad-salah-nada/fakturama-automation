"""Real OCR checks for table geometry, column identity and retained rows."""

import copy
import json
import runpy
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tjm.errors import ExtractionError
from tjm.ocr import ocr_tokens
from tjm.table import extract_items

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = json.loads((FIXTURES / "synthetic_expected.json").read_text())
SAMPLE = json.loads((FIXTURES / "sample_expected.json").read_text())
pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None,
                                reason="Table integration requires Tesseract")


@pytest.mark.parametrize("name", ["sample", "one_row_unpaid", "three_rows_mixed_vat",
                                 "scaled_moved", "repeated_sku"])
def test_every_table_row_and_field_matches_independent_source(name, tmp_path):
    case = SAMPLE if name == "sample" else SYNTHETIC[name]
    path = PROJECT / SAMPLE["source"] if name == "sample" else FIXTURES / f"{name}.png"
    with Image.open(path) as image:
        items, evidence = extract_items(image, ocr_tokens(image), tmp_path)
    assert items == case["raw"]["items"]
    assert evidence["row_count"] == len(items)
    assert len(evidence["rows"]) == len(items)
    for row in evidence["rows"]:
        for cell in row["cells"].values():
            assert Path(cell["crop"]).is_file()
            assert cell["source_box"]
            assert cell["ocr_tokens"]


@pytest.mark.parametrize("name,field", [
    ("missing_vat", "items[0].vat_percent"),
    ("unreadable_row", "items[0].description"),
])
def test_unreadable_cells_stop_instead_of_guessing_or_dropping_rows(name, field, tmp_path):
    with Image.open(FIXTURES / f"{name}.png") as image:
        with pytest.raises(ExtractionError) as error:
            extract_items(image, ocr_tokens(image), tmp_path)
    assert error.value.field == field
    assert error.value.evidence["rows"]


def test_swapped_percentage_columns_are_identified_by_their_headings(tmp_path):
    # These positions are the independent fixture generator's drawing geometry,
    # never production locator coordinates. Exchange two complete unequal-width
    # columns while retaining the source's heading and values together.
    with Image.open(FIXTURES / "three_rows_mixed_vat.png") as source:
        image = source.copy()
    table_top, table_bottom = 1405, 1704
    discount = image.crop((1235, table_top, 1340, table_bottom + 1))
    vat = image.crop((1340, table_top, 1440, table_bottom + 1))
    image.paste(vat, (1235, table_top))
    image.paste(discount, (1335, table_top))
    items, evidence = extract_items(image, ocr_tokens(image), tmp_path)
    assert items == SYNTHETIC["three_rows_mixed_vat"]["raw"]["items"]
    headers = [cell["field"] for cell in evidence["headers"]]
    assert headers.index("vat_percent") < headers.index("discount_percent")


def test_unlabelled_table_is_not_inferred_from_values(tmp_path):
    with Image.open(FIXTURES / "one_row_unpaid.png") as image:
        tokens = [token for token in ocr_tokens(image) if token.text != "ITEMS"]
        with pytest.raises(ExtractionError) as error:
            extract_items(image, tokens, tmp_path)
    assert error.value.code == "unsupported_table"
    assert error.value.field == "items"


def test_visible_row_without_ordinal_cannot_be_silently_absorbed(tmp_path):
    with Image.open(FIXTURES / "three_rows_mixed_vat.png") as source:
        image = source.copy()
    # Clear just the second ordinal using the independent drawing geometry.
    ImageDraw.Draw(image).rectangle((114, 1551, 166, 1621), fill="#F3F7FA")
    with pytest.raises(ExtractionError) as error:
        extract_items(image, ocr_tokens(image), tmp_path)
    assert error.value.field == "items"
    assert "ordinal" in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("vat_percent", "1%9"),     # Renderer prints 1%9%; never turn this into 19.
    ("discount_percent", "1%0"),
    ("quantity", "4%"),
    ("quantity", "EUR 4"),
    ("unit_net", "25.00%"),
    ("unit_net", "25.00 EUR"),
])
def test_source_numeric_markers_cannot_be_deleted_to_make_a_valid_number(field, value, tmp_path):
    # The independent fixture renderer prints the malformed source value. The
    # test exercises real crop OCR, rather than supplying an already bad string
    # directly to the implementation's cleaning rule.
    render = runpy.run_path(str(FIXTURES / "generate_orders.py"))["render"]
    raw = copy.deepcopy(SYNTHETIC["one_row_unpaid"]["raw"])
    raw["items"][0][field] = value
    image = render(raw)
    with pytest.raises(ExtractionError) as error:
        extract_items(image, ocr_tokens(image), tmp_path)
    assert error.value.field == f"items[0].{field}"
    cell = error.value.evidence["rows"][0]["cells"][field]
    assert "%" in cell["raw_text"] or "EUR" in cell["raw_text"]
    assert "selected_reading" not in cell
