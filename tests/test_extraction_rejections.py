"""OCR regressions for malformed source data and literal source-value words.

These are fresh fictional forms built with the independent fixture generator.
Coordinates below describe the test form's drawing, never a production locator.
"""

from copy import deepcopy
from pathlib import Path
import runpy
import shutil

from PIL import Image, ImageDraw, ImageFont
import pytest

from tjm.extract import extract_order
from tjm.fields import decimal_text


FIXTURE_GENERATOR = Path(__file__).parent / "fixtures" / "generate_orders.py"
GENERATOR = runpy.run_path(str(FIXTURE_GENERATOR))
requires_ocr = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="Image rejection regressions require the documented Tesseract executable",
)


def source_values():
    return deepcopy(GENERATOR["cases"]()["one_row_unpaid"]["raw"])


def extract_image(image, tmp_path):
    path = tmp_path / "generated-source.png"
    image.save(path)
    return extract_order(path, name_order="given-family", output_dir=tmp_path / "runs")


def assert_review(report, field):
    assert report["status"] == "review_required", report["issues"]
    assert report["order"] is None
    assert any(issue.get("field") == field for issue in report["issues"]), report["issues"]


@requires_ocr
def test_wrapped_company_requires_review_without_silently_losing_second_line(tmp_path):
    raw = source_values()
    raw["customer"]["company"] = "Harbor Studio\nLtd"
    report = extract_image(GENERATOR["render"](raw), tmp_path)
    assert_review(report, "customer.company")
    evidence = report["evidence"]["partial_extraction"]["customer.company"]
    assert "Harbor Studio" in evidence["raw_text"]
    assert "Ltd" in evidence["raw_text"]
    assert Path(evidence["crop"]).is_file()


@requires_ocr
def test_present_blank_shipping_label_requires_review_instead_of_zero(tmp_path):
    base = GENERATOR["render"](source_values())
    # Insert a fresh blank value region before the existing ITEMS heading. The
    # generator places that heading at y=1360; all later content moves together.
    split, gap = 1360, 70
    image = Image.new("RGB", (base.width, base.height + gap), "white")
    image.paste(base.crop((0, 0, base.width, split)), (0, 0))
    image.paste(base.crop((0, split, base.width, base.height)), (0, split + gap))
    label_font = ImageFont.truetype(str(GENERATOR["font_path"](True)), 20)
    ImageDraw.Draw(image).text((140, 1340), "SHIPPING NET", font=label_font, fill="#596778")
    report = extract_image(image, tmp_path)
    assert_review(report, "shipping_net")
    assert report["raw"] is None
    evidence = report["evidence"]["partial_extraction"]["shipping_net"]
    assert evidence["present"] is True
    assert evidence["raw_text"] == ""
    assert "default" not in evidence


@requires_ocr
def test_discount_word_inside_company_is_preserved_as_source_value(tmp_path):
    raw = source_values()
    raw["customer"]["company"] = "Discount House Ltd"
    report = extract_image(GENERATOR["render"](raw), tmp_path)
    assert report["status"] == "validated", report["issues"]
    assert report["issues"] == []
    assert report["raw"]["customer"]["company"] == "Discount House Ltd"
    assert report["order"]["customer"]["company"] == "Discount House Ltd"
    assert report["order"]["overall_discount_percent"] == "0"


@requires_ocr
def test_percent_marker_on_source_money_requires_review_instead_of_being_removed(tmp_path):
    raw = source_values()
    raw["totals"]["net"] = "100.00%"
    report = extract_image(GENERATOR["render"](raw), tmp_path)
    assert_review(report, "totals.net")
    assert report["raw"]["totals"]["net"] == "100.00%"
    assert any(issue["code"] == "invalid_decimal" and issue["field"] == "totals.net"
               for issue in report["issues"])


@requires_ocr
def test_multiword_postal_code_requires_review_with_full_address_evidence(tmp_path):
    raw = source_values()
    for role in ("billing", "delivery"):
        raw["customer"][role].update(
            street="Tavistock Place 9", postal_code="SW1A 1AA",
            city="London", country="United Kingdom",
        )
    report = extract_image(GENERATOR["render"](raw), tmp_path)
    assert_review(report, "customer.billing.postal_code")
    assert report["raw"] is None
    address = report["evidence"]["partial_extraction"]["customer.billing"]
    assert address["raw_text"].splitlines() == [
        "Harbor Studio Ltd", "Tavistock Place 9", "SW1A 1AA London", "United Kingdom",
    ]
    assert "parsed_components" not in address
    assert Path(address["crop"]).is_file()
    assert address["value_box"]


@pytest.mark.parametrize("text,kwargs,expected", [
    ("EUR 100.00%", {"currency": True}, "100.00%"),
    ("EUR EUR 100.00", {"currency": True}, "EUR 100.00"),
    ("1%9%", {"percent": True}, "1%9"),
    ("19%%", {"percent": True}, "19%"),
    ("EUR 19%", {"percent": True}, "EUR 19"),
])
def test_field_marker_normalization_leaves_wrong_or_repeated_markers_for_review(
    text, kwargs, expected
):
    assert decimal_text(text, **kwargs) == expected
