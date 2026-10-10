from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError
from tjm.document_workflow import (
    _money_candidates, _observe_summary_money, create_invoice_followup,
    parse_display_money, parse_display_percent, set_invoice_payment,
)
from tjm.models import Payment
from tjm.ocr import Token
from tjm.ui import Ui


@pytest.mark.parametrize("display", ["€123.45", "EUR123.45", "EUR 123.45", "  € 123.45  "])
def test_money_display_retains_exact_decimal_value(display):
    assert parse_display_money(display) == Decimal("123.45")


@pytest.mark.parametrize("display", [
    "123.45", "USD123.45", "EUR123,45", "EUR1,123.45", "EUR1.123,45",
    "EUR123.4", "EUR123.450", "EUR-123.45", "EUR123.45 extra", "€", None,
])
def test_money_display_refuses_guessed_currency_separators_and_partial_values(display):
    with pytest.raises(DesktopError):
        parse_display_money(display)


def test_money_display_cannot_relabel_another_currency():
    with pytest.raises(DesktopError):
        parse_display_money("EUR123.45", currency="USD")


@pytest.mark.parametrize("display,expected", [
    ("0%", "0"), ("10.00%", "10.00"), ("100 %", "100"), ("12.3456%", "12.3456"),
])
def test_percentage_requires_explicit_unit_and_preserves_decimal(display, expected):
    assert parse_display_percent(display) == Decimal(expected)


@pytest.mark.parametrize("display", ["10", "-1%", "100.01%", "10,5%", "1e1%", "10% extra", None])
def test_percentage_rejects_unsupported_or_out_of_range_displays(display):
    with pytest.raises(DesktopError):
        parse_display_percent(display)


@pytest.mark.parametrize("unit", ["EUR", "€"])
def test_separately_recognized_currency_and_amount_must_be_adjacent(unit):
    tokens = [Token(unit, 100, 20, 24, 16, 97), Token("987.65", 132, 21, 70, 16, 91)]
    found = _money_candidates(tokens, currency="EUR")
    assert len(found) == 1
    assert found[0]["value"] == Decimal("987.65")
    assert found[0]["box"] == (100, 20, 202, 37)
    assert found[0]["confidence"] == 91


def test_currency_is_not_joined_to_a_distant_or_different_row_number():
    for amount in (Token("987.65", 300, 20, 70, 16, 95),
                   Token("987.65", 132, 50, 70, 16, 95)):
        with pytest.raises(DesktopError):
            _money_candidates([Token("EUR", 100, 20, 24, 16, 97), amount], currency="EUR")


def test_multiple_money_readings_remain_multiple_and_do_not_pick_a_source_match():
    tokens = [Token("€12.34", 100, 20, 70, 16, 98),
              Token("€56.78", 210, 20, 70, 16, 98)]
    assert [candidate["value"] for candidate in _money_candidates(tokens, currency="EUR")] == [
        Decimal("12.34"), Decimal("56.78")]


def test_gross_summary_label_is_confined_below_total_net():
    # Both labels really contain the same OCR word "Total". Exercise the
    # shared label reader rather than supplying a fake label-selection result.
    tokens = [Token("Total", 100, 30, 40, 16, 98), Token("Net", 148, 30, 25, 16, 98),
              Token("Total", 100, 100, 40, 16, 98)]
    capture = SimpleNamespace(tokens=lambda: tokens)
    ui = Ui.__new__(Ui)
    ui.sequence, ui.discoveries = 1, []
    value, pending = _observe_summary_money(ui, capture, "Total", (0, 0, 400, 200), currency="EUR")
    assert value is None
    assert pending["crop_box"] == (148, 92, 400, 124)
    assert ui.discoveries[-1]["pixel_box"] == [100, 100, 140, 116]


def test_shipping_caption_is_separate_from_shipping_method_text():
    tokens = [Token("Shipping", 10, 30, 70, 16, 98), Token("Free", 110, 30, 30, 16, 98),
              Token("shipping", 150, 30, 70, 16, 98)]
    capture = SimpleNamespace(tokens=lambda: tokens, screen_point=lambda x, y: (x, y))
    ui = Ui.__new__(Ui)
    ui.sequence, ui.discoveries = 1, []
    ui.desktop = SimpleNamespace(hit_test=lambda capture, x, y: SimpleNamespace(
        role="AXStaticText" if x < 100 else "AXPopUpButton"))
    value, pending = _observe_summary_money(ui, capture, "Shipping", (0, 0, 400, 200), currency="EUR")
    assert value is None
    assert pending["crop_box"][0] == 88
    assert ui.discoveries[-1]["pixel_box"] == [10, 30, 80, 46]


class NoUiActions:
    def snapshot(self, label):
        pytest.fail("Invalid input must be refused before observing or changing the UI.")


@pytest.mark.parametrize("title", ["", "*Order ORD99", "New Order", None])
def test_followup_rejects_unsaved_or_dirty_title_before_any_action(title):
    with pytest.raises(ValueError):
        create_invoice_followup(NoUiActions(), saved_title=title, number="ORD99", reference="REF99")


@pytest.mark.parametrize("amount,payment_date", [
    (None, date(2026, 4, 5)), (Decimal("123.45"), None),
    (Decimal("-1"), date(2026, 4, 5)), (Decimal("123.456"), date(2026, 4, 5)),
])
def test_paid_input_requires_complete_exact_amount_before_changing_method(amount, payment_date):
    payment = Payment(method="Bank Transfer", status="Paid", paid=True,
                      date=payment_date, amount=amount)
    with pytest.raises(ValueError):
        set_invoice_payment(NoUiActions(), payment)


def test_payment_requires_checked_model_and_supported_currency_before_action():
    payment = Payment(method="Bank Transfer", status="Unpaid", paid=False, date=None, amount=None)
    with pytest.raises(ValueError):
        set_invoice_payment(NoUiActions(), payment, currency="USD")
    with pytest.raises(ValueError):
        set_invoice_payment(NoUiActions(), SimpleNamespace(method="Bank Transfer", paid=False))
