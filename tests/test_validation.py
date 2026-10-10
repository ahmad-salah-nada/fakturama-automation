"""Business-facing acceptance cases, independent of the desktop application."""

from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, Subnormal, ROUND_DOWN, localcontext
import json

import pytest
from pydantic import ValidationError

from tjm.models import RawOrder
from tjm.validation import validate_order


@pytest.fixture
def source():
    address = {"recipient": "Example GmbH", "street": "Teststrasse 8",
               "postal_code": "00123", "city": "Berlin", "country": "Germany"}
    return {
        "order_date": "2026-07-14", "external_reference": "Order-001", "currency": "EUR",
        "customer": {"company": "Example GmbH", "contact_name": "Marta Klein", "alias": "EXAMPLE",
                     "billing": address, "delivery": deepcopy(address),
                     "email": "marta@example.test", "telephone": "+49 30 12345"},
        "payment": {"method": "Bank Transfer", "status": "PAID", "date": "2026-07-18"},
        "items": [
            {"sku": "001-a.B", "description": "Test chair", "quantity": "2", "unit_net": "20.00",
             "vat_percent": "19", "discount_percent": "10", "line_net": "36.00", "unit": "pcs"},
            {"sku": "T-02", "description": "Test mat", "quantity": "3", "unit_net": "5.00",
             "vat_percent": "19", "discount_percent": "0", "line_net": "15.00"},
        ],
        "totals": {"net": "51.00", "vat": "9.69", "gross": "60.69"},
    }


def result(source, **kwargs):
    return validate_order(RawOrder.model_validate(source), **kwargs)


def codes(outcome):
    return {issue["code"] for issue in outcome.issues}


def test_exact_arithmetic_and_non_discounted_master_price(source):
    outcome = result(source, name_order="given-family")
    assert outcome.status == "validated", outcome.issues
    assert outcome.order.items[0].product_gross == Decimal("23.80")
    assert outcome.order.items[0].line_net == Decimal("36.00")
    assert outcome.order.payment.amount == Decimal("60.69")
    assert outcome.order.customer.billing.postal_code == "00123"
    assert outcome.order.items[0].sku == "001-a.B"
    assert outcome.order.items[1].unit is None
    assert any(check["rule"] == "name_interpretation" for check in outcome.checks)
    assert json.loads(json.dumps(outcome.model_dump(mode="json")))["order"]["totals"]["gross"] == "60.69"


def test_caller_decimal_traps_and_exponent_limits_do_not_change_validation(source):
    source["items"] = [{"sku": "SMALL", "description": "Fractional VAT", "quantity": "1",
                        "unit_net": "0.11", "vat_percent": "19", "discount_percent": "0",
                        "line_net": "0.11"}]
    source["totals"] = {"net": "0.11", "vat": "0.02", "gross": "0.13"}
    normal = result(source, name_order="given-family")
    assert normal.status == "validated"
    with localcontext() as caller:
        caller.prec = 2
        caller.Emax = 1
        caller.Emin = -1
        caller.rounding = ROUND_DOWN
        for signal in (Inexact, Rounded, Subnormal):
            caller.traps[signal] = True
        hostile = result(source, name_order="given-family")
        assert hostile.model_dump(mode="json") == normal.model_dump(mode="json")
        assert caller.prec == 2 and caller.Emax == 1 and caller.Emin == -1
        assert caller.rounding == ROUND_DOWN
        assert all(caller.traps[signal] for signal in (Inexact, Rounded, Subnormal))


def test_caller_tiny_emax_cannot_overflow_an_ordinary_order(source):
    normal = result(source, name_order="given-family")
    with localcontext() as caller:
        caller.Emax = 0
        hostile = result(source, name_order="given-family")
        assert hostile.model_dump(mode="json") == normal.model_dump(mode="json")
        assert caller.Emax == 0


def test_unknown_unsplit_name_never_silently_split(source):
    outcome = result(source)
    assert outcome.order is None
    assert "ambiguous_name" in codes(outcome)


@pytest.mark.parametrize("name", ["Marta", "Marta Maria Klein", "Dr Smith", "李 明", "M. Klein"])
def test_selected_name_convention_still_reviews_ambiguity(source, name):
    source["customer"]["contact_name"] = name
    assert "ambiguous_name" in codes(result(source, name_order="given-family"))


def test_explicit_name_parts_need_no_policy(source):
    source["customer"].update(contact_name="Klein, Marta Maria", first_name="Marta Maria", last_name="Klein")
    assert result(source).status == "validated"


def test_conflicting_explicit_name_parts_require_review(source):
    source["customer"].update(first_name="Bob", last_name="Smith")
    outcome = result(source)
    assert outcome.order is None
    assert "conflicting_name_parts" in codes(outcome)
    check = next(check for check in outcome.checks if check["rule"] == "explicit_name_parts")
    assert check["passed"] is False
    assert check["source_contact_name"] == "Marta Klein"


@pytest.mark.parametrize("contact_name", ["Marta Klein", "Klein Marta", "Klein,Marta", "Marta , Klein"])
def test_matching_explicit_parts_accept_only_explainable_presentation_forms(source, contact_name):
    source["customer"].update(contact_name=contact_name, first_name="Marta", last_name="Klein")
    outcome = result(source)
    assert outcome.status == "validated"
    assert outcome.order.customer.contact_name == contact_name
    check = next(check for check in outcome.checks if check["rule"] == "explicit_name_parts")
    assert check["passed"] is True and check["matched_form"] is not None


@pytest.mark.parametrize("contact_name", ["Marta Maria Klein", "Dr Marta Klein", "Marta Klein-Smith", "MARTA KLEIN"])
def test_explicit_parts_do_not_delete_unmatched_tokens_or_punctuation(source, contact_name):
    source["customer"].update(contact_name=contact_name, first_name="Marta", last_name="Klein")
    assert "conflicting_name_parts" in codes(result(source))


def test_canonically_equivalent_explicit_name_accents_preserve_source_strings(source):
    source["customer"].update(contact_name="Jose\u0301 Müller", first_name="José", last_name="Müller")
    outcome = result(source)
    assert outcome.status == "validated"
    assert outcome.order.customer.contact_name == "Jose\u0301 Müller"
    assert outcome.order.customer.first_name == "José"


def test_latin_name_convention_preserves_unicode_and_raw_name(source):
    source["customer"]["contact_name"] = "José Müller-Straße"
    order = result(source, name_order="given-family").order
    assert order.customer.first_name == "José"
    assert order.customer.last_name == "Müller-Straße"
    assert order.customer.contact_name == "José Müller-Straße"


@pytest.mark.parametrize("date", ["14/07/2026", "07/14/2026", "2026-02-30", "2026-7-14", "20260714"])
def test_ambiguous_and_impossible_dates_require_review(source, date):
    source["order_date"] = date
    assert "invalid_date" in codes(result(source, name_order="given-family"))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e2", "1,000.00", "EUR 20.00", "20,00"])
def test_numeric_ambiguity_and_nonfinite_values_require_review(source, value):
    source["items"][0]["unit_net"] = value
    assert "invalid_decimal" in codes(result(source, name_order="given-family"))


@pytest.mark.parametrize("field,value", [("quantity", "0"), ("quantity", "-1"),
                                          ("discount_percent", "101"), ("vat_percent", "-19"),
                                          ("unit_net", "-20")])
def test_out_of_range_values_require_review(source, field, value):
    source["items"][0][field] = value
    assert "out_of_range" in codes(result(source, name_order="given-family"))


def test_schema_rejects_implicit_numeric_conversion_and_unknown_fields(source):
    source["items"][0]["quantity"] = 2
    with pytest.raises(ValidationError):
        RawOrder.model_validate(source)
    source["items"][0]["quantity"] = "2"
    source["totals"]["guess"] = "60.69"
    with pytest.raises(ValidationError):
        RawOrder.model_validate(source)


def test_incorrect_line_and_document_totals_never_repaired(source):
    source["items"][0]["line_net"] = "35.00"
    outcome = result(source, name_order="given-family")
    assert outcome.order is None
    assert "arithmetic_mismatch" in codes(outcome)
    assert source["items"][0]["line_net"] == "35.00"


def test_half_cent_vat_not_accepted_even_with_agreeing_source(source):
    source["items"] = [{"sku": "TIE", "description": "Tie case", "quantity": "1", "unit_net": "0.10",
                        "vat_percent": "5", "discount_percent": "0", "line_net": "0.10"}]
    source["totals"] = {"net": "0.10", "vat": "0.01", "gross": "0.11"}
    outcome = result(source, name_order="given-family")
    assert outcome.order is None
    assert "uncalibrated_vat_tie" in codes(outcome)


def test_discount_rounding_stage_can_disagree_despite_cent_exact_line(source):
    source["items"] = [{"sku": "ROUND", "description": "Discount case", "quantity": "2", "unit_net": "0.03",
                        "vat_percent": "0", "discount_percent": "50", "line_net": "0.03"}]
    source["totals"] = {"net": "0.03", "vat": "0", "gross": "0.03"}
    assert "uncalibrated_discount_stage" in codes(result(source, name_order="given-family"))


def test_nonhalf_vat_rounding_stage_disagreement_requires_review(source):
    source["items"] = [{"sku": f"R-{i}", "description": "Small amount", "quantity": "1", "unit_net": "0.03",
                        "vat_percent": "19", "discount_percent": "0", "line_net": "0.03"} for i in range(2)]
    source["totals"] = {"net": "0.06", "vat": "0.02", "gross": "0.08"}
    assert "uncalibrated_vat_stage" in codes(result(source, name_order="given-family"))


def test_repeated_skus_remain_distinct_source_rows(source):
    source["items"][1]["sku"] = source["items"][0]["sku"]
    order = result(source, name_order="given-family").order
    assert len(order.items) == 2
    assert [row.quantity for row in order.items] == [Decimal("2"), Decimal("3")]
    assert [row.description for row in order.items] == ["Test chair", "Test mat"]


def test_paid_requires_source_date_and_matching_supplied_amount(source):
    source["payment"].pop("date")
    assert "missing_date" in codes(result(source, name_order="given-family"))
    source["payment"].update(date="2026-07-18", source_amount="60.68")
    assert "arithmetic_mismatch" in codes(result(source, name_order="given-family"))
    source["payment"]["source_amount"] = "60.69"
    assert result(source, name_order="given-family").status == "validated"


@pytest.mark.parametrize("status", ["UNPAID", "NOT PAID", "PENDING", "PARTIALLY PAID"])
def test_nonpaid_keeps_operational_payment_clear_and_source_data_visible(source, status):
    source["payment"].update(status=status, source_amount="12.00")
    order = result(source, name_order="given-family").order
    assert order.payment.status == status
    assert order.payment.paid is False
    assert order.payment.date is None and order.payment.amount is None
    assert order.payment.source_date.isoformat() == "2026-07-18"
    assert order.payment.source_amount == Decimal("12.00")


def test_unknown_payment_status_does_not_become_unpaid(source):
    source["payment"]["status"] = "PALD"
    assert "unsupported_payment_status" in codes(result(source, name_order="given-family"))


def test_shipping_requires_explicit_vat_and_totals(source):
    source["shipping_net"] = "10.00"
    assert "unknown_shipping_tax" in codes(result(source, name_order="given-family"))
    source["shipping_vat_percent"] = "19"
    source["totals"] = {"net": "61.00", "vat": "11.59", "gross": "72.59"}
    assert result(source, name_order="given-family").status == "validated"


def test_mixed_tax_overall_discount_requires_allocation_review(source):
    source["overall_discount_percent"] = "10"
    source["items"][1]["vat_percent"] = "7"
    assert "unknown_discount_tax" in codes(result(source, name_order="given-family"))


def test_single_tax_overall_discount_is_checkable_when_cent_exact(source):
    source["overall_discount_percent"] = "10"
    source["items"] = [source["items"][0]]
    source["totals"] = {"net": "32.40", "vat": "6.16", "gross": "38.56"}
    assert result(source, name_order="given-family").status == "validated"


def test_uniform_tax_overall_discount_still_reviews_line_vat_ties(source):
    source["overall_discount_percent"] = "10"
    source["totals"] = {"net": "45.90", "vat": "8.72", "gross": "54.62"}
    assert "uncalibrated_vat_tie" in codes(result(source, name_order="given-family"))


def test_blank_required_field_and_missing_rows_require_review(source):
    source["customer"]["billing"]["street"] = " "
    source["items"] = []
    assert {"missing_field", "missing_items"} <= codes(result(source, name_order="given-family"))


@pytest.mark.parametrize("placeholder", ["?", "???", "[UNREADABLE]", "[illegible]"])
def test_explicit_unreadable_placeholders_never_become_product_data(source, placeholder):
    source["items"][0]["description"] = placeholder
    assert "unreadable_field" in codes(result(source, name_order="given-family"))
