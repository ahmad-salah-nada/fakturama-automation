"""Real OCR integration: every source field, row and derived value is checked.

The fixtures are deliberately independent of the implementation. This suite
does not operate Fakturama or call a paid API. Tesseract is a stated dependency.
"""

import copy
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from tjm.extract import extract_order

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = json.loads((FIXTURES / "sample_expected.json").read_text(encoding="utf-8"))
SYNTHETIC = json.loads((FIXTURES / "synthetic_expected.json").read_text(encoding="utf-8"))
NUMERIC_KEYS = {
    "quantity", "unit_net", "vat_percent", "discount_percent", "line_net",
    "product_gross", "net", "vat", "gross", "overall_discount_percent",
    "shipping_net", "shipping_vat_percent", "amount", "source_amount",
}
pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None,
                                reason="OCR integration requires the documented Tesseract executable")


def canonical(value, key=None):
    """Compare decimal values, while retaining exact identity/date characters."""
    if isinstance(value, dict):
        return {name: canonical(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if key in NUMERIC_KEYS and value is not None:
        return Decimal(value)
    return value


def expected_order(case):
    expected = copy.deepcopy(case["raw"])
    customer = expected["customer"]
    customer["first_name"] = case["first_name"]
    customer["last_name"] = case["last_name"]
    for row, gross in zip(expected["items"], case["product_gross"], strict=True):
        row["product_gross"] = gross
    payment = expected["payment"]
    payment["paid"] = payment["status"] == "PAID"
    payment["source_date"] = payment["date"]
    payment["amount"] = case["paid_amount"]
    return expected


def assert_complete_result(report, case):
    assert report["status"] == "validated", report["issues"]
    assert report["issues"] == []
    assert canonical(report["raw"]) == canonical(case["raw"])
    assert canonical(report["order"]) == canonical(expected_order(case))
    assert report["checks"] and all(check["passed"] for check in report["checks"])
    assert report["evidence"]


def test_original_sample_all_fields_rows_and_derived_amounts(tmp_path):
    report = extract_order(PROJECT / SAMPLE["source"], name_order="given-family",
                           output_dir=tmp_path)
    assert_complete_result(report, SAMPLE)


@pytest.mark.parametrize("name", [name for name, case in SYNTHETIC.items()
                                 if case["expected_status"] == "validated"])
def test_changed_synthetic_images_all_fields(name, tmp_path):
    report = extract_order(FIXTURES / (name+".png"), name_order="given-family",
                           output_dir=tmp_path)
    assert_complete_result(report, SYNTHETIC[name])
    if name == "repeated_sku":
        rows = report["order"]["items"]
        assert len(rows) == 2
        assert rows[0]["sku"] == rows[1]["sku"] == "CAB-USB-09"
        assert [Decimal(row["quantity"]) for row in rows] == [Decimal("2"), Decimal("3")]
        assert [Decimal(row["discount_percent"]) for row in rows] == [Decimal("0"), Decimal("20")]
    elif name == "one_row_unpaid":
        assert report["order"]["payment"] == {
            "method": "Credit Card", "status": "UNPAID", "paid": False,
            "date": None, "amount": None, "source_date": None, "source_amount": None,
        }
        assert report["order"]["customer"]["billing"]["postal_code"] == "00421"


@pytest.mark.parametrize("name", [name for name, case in SYNTHETIC.items()
                                 if case["expected_status"] == "review_required"])
def test_incomplete_ambiguous_and_inconsistent_images_require_review(name, tmp_path):
    case = SYNTHETIC[name]
    report = extract_order(FIXTURES / (name+".png"), name_order="given-family",
                           output_dir=tmp_path)
    assert report["status"] == "review_required", report
    assert report["order"] is None
    assert any(issue.get("field") == case["issue_field"] for issue in report["issues"]), report["issues"]
    if name in {"ambiguous_name", "contradictory_total", "swapped_date_reference"}:
        # A valid transcription of a bad source still cannot become an Order.
        assert canonical(report["raw"]) == canonical(case["raw"])


def test_unsplit_sample_name_requires_explicit_convention(tmp_path):
    report = extract_order(PROJECT / SAMPLE["source"], output_dir=tmp_path)
    assert report["status"] == "review_required"
    assert report["order"] is None
    assert any(issue.get("code") == "ambiguous_name" for issue in report["issues"])
    assert canonical(report["raw"]) == canonical(SAMPLE["raw"])

