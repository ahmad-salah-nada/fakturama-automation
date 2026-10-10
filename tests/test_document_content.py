"""Actual-value adaptation and completeness failures; no live desktop claim."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from tjm.desktop import DesktopError
from tjm.document_content import (
    _column_rules, _row_coverage, _row_has_content, adapt_document_content, collect_document_content,
    parse_four_line_address, parse_item_discount, parse_item_money, parse_painted_vat,
)
from tjm.models import ValidatedOrder
from tjm.workflow_report import content_expectations
from tjm.workflow_rules import WorkflowReview


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/fixtures/gui/validated-demo-order.json"


def actual_reads():
    return {
        "header": {"number": "PO000001", "date": "Jul 14, 2026",
                   "reference": "WEB-2026-0714-A17", "price_mode": "Net", "vat_mode": "With VAT"},
        "addresses": {
            "billing": "Northstar Office GmbH\nFriedrichstrasse 88\n10117 Berlin\nGermany",
            "delivery": "Northstar Office Warehouse\nBeusselstrasse 44\n10553 Berlin\nGermany"},
        "items": [
            {"Item No.": "CHR-ERG-01", "Qty.": "2.00", "Name": "Ergonomic Desk Chair",
             "Description": "Ergonomic Desk Chair", "U.Price": "EUR 250", "Discount": "-10.00 %",
             "VAT": {"text": "VAT 19% (19.0%)"}, "Price": {"text": "EUR450.00"}},
            {"Item No.": "MAT-DESK-02", "Qty.": "3.00", "Name": "Anti-Fatigue Desk Mat",
             "Description": "Anti-Fatigue Desk Mat", "U.Price": "€40.00", "Discount": "0.00 %",
             "VAT": {"text": "VAT 19% (19.0%)"}, "Price": {"text": "EUR 120.00"}}],
        "summary": {"net": "570.00", "vat": "108.30", "gross": "678.30",
                    "overall_discount_percent": "0", "shipping_net": "0.00"},
    }


def source_order():
    return ValidatedOrder.model_validate_json(SOURCE.read_text())


def test_actual_read_formats_match_the_current_demonstration_content_contract():
    order = source_order()
    expected = content_expectations({"order": order.model_dump(mode="json"),
                                     "generated_numbers": {"order": "PO000001"}})["order_content"]
    assert adapt_document_content(actual_reads()) == expected


def test_an_actual_price_discrepancy_is_retained_even_when_summary_remains_source_total():
    raw = actual_reads()
    raw["items"][0]["Price"]["text"] = "EUR 449.00"
    observed = adapt_document_content(raw)
    assert observed["items"][0]["line_net"] == "449.00"
    assert observed["totals"]["net"] == "570.00"


def test_addresses_and_reference_come_from_the_actual_reads_without_source_substitution():
    raw = actual_reads()
    raw["header"]["reference"] = "ACTUAL-OTHER-REFERENCE"
    raw["addresses"]["delivery"] = "Actual recipient\nActual street\n12345 Actual City\nActual Country"
    observed = adapt_document_content(raw)
    assert observed["external_reference"] == "ACTUAL-OTHER-REFERENCE"
    assert observed["delivery"] == {"recipient": "Actual recipient", "street": "Actual street",
                                     "postal_code": "12345", "city": "Actual City", "country": "Actual Country"}


def test_invoice_uses_observed_copied_order_date_and_keeps_proposed_dates_in_raw_evidence():
    raw = actual_reads()
    raw["header"].update(number="INV000001", date="Oct 8, 2026",
                         service_date="Oct 8, 2026", order_date="Jul 14, 2026")
    before = deepcopy(raw)
    observed = adapt_document_content(raw, invoice=True)
    assert observed["number"] == "INV000001" and observed["order_date"] == "2026-07-14"
    assert raw == before


@pytest.mark.parametrize("text", ["10.00 %", "+10 %", "-100.01%", "-10,0%", "10", "-1e1%", None])
def test_discount_refuses_a_surcharge_or_guessed_format(text):
    with pytest.raises(DesktopError):
        parse_item_discount(text)


@pytest.mark.parametrize("text,wanted", [("-10.00 %", "10"), ("0.00 %", "0"), ("-0.00%", "0")])
def test_observed_signed_discount_becomes_a_nonnegative_reduction(text, wanted):
    assert parse_item_discount(text) == wanted


@pytest.mark.parametrize("text", ["VAT 19% (...", "VAT 19%", "VAT 19% (19,0%)", "(19.0%)",
                                  "VAT 19% (101%)", None])
def test_vat_requires_the_complete_painted_converter_display(text):
    with pytest.raises(DesktopError):
        parse_painted_vat(text)


def test_vat_uses_actual_converter_percentage_and_retains_the_actual_name():
    assert parse_painted_vat("Actual VAT name (7.0%)") == {"name": "Actual VAT name", "vat_percent": "7"}


@pytest.mark.parametrize("text", ["USD250.00", "EUR 1,250.00", "250.00", "EUR 250,00",
                                  "EUR 250.001", "EUR -1.00", "EUR 2e2", None])
def test_item_money_never_guesses_currency_or_decimal_separators(text):
    with pytest.raises(DesktopError):
        parse_item_money(text)


@pytest.mark.parametrize("text", ["Recipient\nStreet\n10117 Berlin", "Recipient\nStreet\n10117\nGermany",
                                  "Recipient\nStreet\n10117 Berlin\nGermany\nExtra",
                                  "Recipient \nStreet\n10117 Berlin\nGermany", None])
def test_address_refuses_partial_or_extra_lines(text):
    with pytest.raises(DesktopError):
        parse_four_line_address(text)


def test_blank_grid_rules_are_not_data_but_an_extra_unreadable_mark_is_data():
    image = Image.new("RGB", (280, 35), "white")
    draw = ImageDraw.Draw(image)
    for x in (0, 60, 160, 279):
        draw.line((x, 0, x, 34), fill=(178, 178, 178), width=2)
    draw.line((0, 0, 279, 0), fill=(178, 178, 178), width=2)
    assert _row_has_content(image, (0, 0, 280, 35)) is False
    draw.rectangle((100, 10, 108, 19), fill="black")
    assert _row_has_content(image, (0, 0, 280, 35)) is True


def test_a_partial_row_with_text_cannot_be_declared_blank():
    image = Image.new("RGB", (280, 12), "white")
    ImageDraw.Draw(image).text((80, 0), "partial", fill="black")
    assert _row_has_content(image, (0, 0, 280, 12)) is True


def test_partial_row_rules_that_end_at_a_closing_frame_are_projected_from_actual_full_row():
    image = Image.new("RGB", (280, 75), "white")
    draw = ImageDraw.Draw(image)
    for x in (0, 60, 160, 279):
        draw.line((x, 0, x, 59), fill=(178, 178, 178), width=2)
    draw.rectangle((0, 60, 279, 64), fill=(170, 195, 220))
    rules = _column_rules(image, (0, 0, 280, 35))
    assert _row_has_content(image, (0, 35, 280, 75), column_rules=rules) is False
    draw.text((90, 39), "extra", fill="black")
    assert _row_has_content(image, (0, 35, 280, 75), column_rules=rules) is True


@pytest.mark.parametrize("occupied,partial,expected_error", [
    ((0, 1), False, "Actual occupied document rows"),
    ((0, 2), False, "Nonempty content follows a blank"),
    ((0, 1, 2), False, "All complete visible item rows"),
    ((0,), True, "Content remains in the partial item row"),
])
def test_complete_row_coverage_stops_on_extra_gap_overflow_or_partial_content(
        monkeypatch, tmp_path, occupied, partial, expected_error):
    import tjm.document_content as module

    image = Image.new("RGB", (220, 170), "white")
    draw = ImageDraw.Draw(image)
    for top in (44, 74, 104, 134):
        draw.rectangle((10, top, 180, top + 1), fill=(178, 178, 178))
    for index in occupied:
        draw.text((100, 50 + index * 30), "occupied", fill="black")
    if partial:
        draw.text((100, 138), "clipped", fill="black")
    observation = SimpleNamespace(
        capture=SimpleNamespace(image=image),
        boxes={"Qty.": (20, 24, 60, 40), "Item No.": (90, 24, 160, 40)},
        scope=(0, 0, 220, 156), table_box=(10, 20, 181, 156), table=object())
    monkeypatch.setattr(module, "_observe_item", lambda *args: observation)
    monkeypatch.setattr(module, "_no_scrollbars", lambda *args: {"no_vertical_scrollbar": True})
    ui = SimpleNamespace(desktop=object(), evidence_dir=tmp_path, sequence=1)
    with pytest.raises((DesktopError, WorkflowReview), match=expected_error):
        _row_coverage(ui, "New Order", 1)


class NoUi:
    def __getattr__(self, name):
        pytest.fail("Unsupported source must stop before UI access: " + name)


def test_extra_address_fields_stop_before_any_ui_access():
    order = source_order().model_copy(deep=True)
    order.customer.delivery.additional_name = "Unsafely omitted extra line"
    with pytest.raises(WorkflowReview, match="Additional address lines"):
        collect_document_content(NoUi(), order, title="New Order", retained_number="PO000001")


def test_collector_preserves_partial_raw_reads_and_does_not_accept_another_identity(monkeypatch, tmp_path):
    import tjm.document_content as module

    order = source_order()
    header = actual_reads()["header"] | {"number": "OTHER001"}
    monkeypatch.setattr(module, "read_document_header", lambda *args, **kwargs: header)
    ui = type("EvidenceUi", (), {"evidence_dir": tmp_path, "actions": [], "discoveries": []})()
    with pytest.raises(DesktopError) as error:
        collect_document_content(ui, order, title="New Order", retained_number="PO000001")
    retained = json.loads(Path(error.value.document_content_evidence).read_text())
    assert retained["passed"] is False and retained["raw"]["header"]["number"] == "OTHER001"


def test_collector_rejects_actual_line_price_discrepancy_without_filling_it_from_source(monkeypatch, tmp_path):
    import tjm.document_content as module

    raw = actual_reads()
    raw["items"][0]["Price"]["text"] = "EUR449.00"
    monkeypatch.setattr(module, "read_document_header", lambda *args, **kwargs: raw["header"])
    monkeypatch.setattr(module, "read_addresses", lambda *args, **kwargs: raw["addresses"])
    monkeypatch.setattr(module, "read_document_totals", lambda *args, **kwargs: raw["summary"])
    monkeypatch.setattr(module, "_row_coverage", lambda *args: {"passed": True, "observed_count": 2})
    monkeypatch.setattr(module, "read_item_cell", lambda *args, row_index, header, **kwargs:
                        raw["items"][row_index][header])
    monkeypatch.setattr(module, "read_painted_item_cell", lambda *args, row_index, header, **kwargs:
                        raw["items"][row_index][header])
    ui = SimpleNamespace(evidence_dir=tmp_path, actions=[], discoveries=[])
    with pytest.raises(WorkflowReview) as error:
        collect_document_content(ui, source_order(), title="New Order", retained_number="PO000001")
    retained = json.loads(Path(error.value.document_content_evidence).read_text())
    assert retained["passed"] is False
    assert retained["observed"]["items"][0]["line_net"] == "449.00"
    assert retained["expected"]["items"][0]["line_net"] == "450.00"
