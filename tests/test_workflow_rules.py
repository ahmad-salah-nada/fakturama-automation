"""Pure workflow decisions; no UI actions and no claims of live persistence."""

from copy import deepcopy
from decimal import Decimal
import json

import pytest

from tjm.models import Customer, Item
from tjm.workflow_rules import (
    SaveLedger, WorkflowReview, payment_definition, require_matching_payment, require_matching_vat, require_unique_customer,
    require_unique_payment, require_unique_product, require_unique_vat, required_vat_definition,
)


@pytest.fixture
def customer():
    address = {"recipient": "Atelier GmbH", "street": "Example 1", "postal_code": "00123",
               "city": "Bonn", "country": "Germany"}
    return Customer(company="Atelier GmbH", first_name="Zoë", last_name="O’Neil",
                    contact_name="Zoë O’Neil", alias="ATELIER", billing=address, delivery=address,
                    email="zoe@example.test", telephone="123")


@pytest.fixture
def customer_row():
    return {"company": "Atelier GmbH", "first_name": "Zoë", "last_name": "O’Neil",
            "postal_code": "00123", "city": "Bonn", "customer_id": "D-17"}


def test_customer_exact_row_preserves_identifier_and_leading_zero(customer, customer_row):
    other = dict(customer_row, first_name="Ada", customer_id="D-8")
    assert require_unique_customer([other, customer_row], customer) is customer_row
    assert require_unique_customer([other], customer) is None
    assert require_unique_customer([], customer) is None
    assert require_unique_customer([customer_row], customer.model_dump()) is customer_row


@pytest.mark.parametrize("changed", [
    {"company": "Atelier GmbH "}, {"company": "atelier gmbh"},
    {"first_name": "Zoe"}, {"last_name": "O'Neil"},
])
def test_customer_does_not_fuzzy_match(customer, customer_row, changed):
    assert require_unique_customer([dict(customer_row, **changed)], customer) is None


def test_customer_duplicate_exact_or_same_named_other_address_stops(customer, customer_row):
    for rows in ([customer_row, deepcopy(customer_row)],
                 [dict(customer_row, postal_code="123")],
                 [dict(customer_row, city="Berlin")]):
        with pytest.raises(WorkflowReview) as raised:
            require_unique_customer(rows, customer)
        assert raised.value.code == "conflicting_customer"


def test_customer_unique_exact_billing_row_survives_other_delivery_location(customer, customer_row):
    delivery = dict(customer_row, postal_code="00987", city="Berlin")
    assert require_unique_customer([customer_row, delivery], customer) is customer_row
    assert require_unique_customer([delivery, customer_row], customer) is customer_row
    with pytest.raises(WorkflowReview) as raised:
        require_unique_customer([customer_row, delivery, deepcopy(customer_row)], customer)
    assert raised.value.code == "conflicting_customer"


@pytest.mark.parametrize("field", ["company", "first_name", "last_name", "postal_code", "city"])
def test_customer_unreadable_candidate_never_establishes_absence(customer, customer_row, field):
    row = dict(customer_row)
    del row[field]
    with pytest.raises(WorkflowReview) as raised:
        require_unique_customer([row], customer)
    assert raised.value.code == "incomplete_identity"


def test_product_exact_sku_ignores_master_price_and_source_row_occurrence():
    source = {"sku": "001-a.B"}
    row = {"sku": "001-a.B", "gross_price": "999.00", "description": "Old catalog description"}
    other = {"sku": "001-a.b"}
    assert require_unique_product([other, row], source) is row
    assert require_unique_product([other, row], source) is row  # repeated source SKU remains a row
    assert require_unique_product([other], source) is None
    assert require_unique_product([], source) is None
    item = Item(sku=source["sku"], description="Current", quantity=Decimal("2"),
                unit_net=Decimal("3"), vat_percent=Decimal("19"), discount_percent=Decimal("0"),
                line_net=Decimal("6"), product_gross=Decimal("3.57"))
    assert require_unique_product([row], item) is row


def test_product_duplicate_sku_and_unreadable_sku_stop():
    with pytest.raises(WorkflowReview, match="Multiple Products"):
        require_unique_product([{"sku": "SKU"}, {"sku": "SKU"}], {"sku": "SKU"})
    with pytest.raises(WorkflowReview, match="unreadable"):
        require_unique_product([{"sku": None}], {"sku": "SKU"})


@pytest.mark.parametrize(("rate", "name", "value"), [
    (Decimal("19.000"), "VAT 19%", "19"), ("7.5", "VAT 7.5%", "7.5"),
    ("0", "VAT 0%", "0"), ("100.00", "VAT 100%", "100"),
])
def test_required_vat_definition_from_source_percentage(rate, name, value):
    assert required_vat_definition(rate) == {
        "name": name, "description": name, "value": value, "code": "S"}
    assert "standard_vat" not in required_vat_definition(rate)


@pytest.mark.parametrize("rate", ["19%", "19,0", "1e1", "NaN", Decimal("Infinity"),
                                  Decimal("-1"), "101", 19.0, True])
def test_required_vat_rejects_ambiguous_or_invalid_percentages(rate):
    with pytest.raises(WorkflowReview):
        required_vat_definition(rate)


def test_existing_vat_matches_only_mandatory_reuse_fields():
    row = {"name": "VAT 19%", "value": "19.00 %", "code": "S (Standard rate)",
           "description": "Existing wording", "standard_vat": "VAT 7%"}
    assert require_unique_vat([row], Decimal("19")) is row
    assert require_unique_vat([dict(row, name="VAT 19.00%")], Decimal("19")) is None
    assert require_unique_vat([], Decimal("19")) is None
    assert row["standard_vat"] == "VAT 7%"


@pytest.mark.parametrize("change", [{"value": "7"}, {"code": "E"}, {"value": None}])
def test_exact_vat_name_with_wrong_or_unreadable_definition_stops(change):
    row = {"name": "VAT 19%", "value": "19", "code": "S"}
    with pytest.raises(WorkflowReview):
        require_unique_vat([dict(row, **change)], Decimal("19"))


def test_duplicate_vat_name_stops_even_if_definition_is_identical():
    row = required_vat_definition("19")
    with pytest.raises(WorkflowReview, match="Multiple VAT"):
        require_unique_vat([row, deepcopy(row)], "19")


@pytest.mark.parametrize(("method", "code"), [
    ("Bank Transfer", "Credit transfer"), ("Credit Card", "Credit card"),
    ("SEPA Direct Debit", "SEPA direct debit"),
])
def test_all_assignment_payment_mappings_and_neutral_creation_fields(method, code):
    definition = payment_definition(method)
    assert definition["name"] == definition["description"] == method
    assert definition["code"] == code
    assert all(definition[field] == "0" for field in ("cash_discount", "discount_days", "net_days"))
    assert all(definition[field] == "" for field in ("account", "text_unpaid", "text_deposit", "text_paid"))
    assert "set_as_standard" not in definition
    assert require_unique_payment([definition], method) is definition


def test_unsupported_payment_mapping_stops_without_inventing_a_code():
    for method in ("Cash", "bank transfer", "Bank Transfer "):
        with pytest.raises(WorkflowReview) as raised:
            payment_definition(method)
        assert raised.value.code == "unsupported_payment_code"


def test_payment_absence_and_numeric_display_equivalence():
    row = payment_definition("Credit Card")
    row.update(cash_discount="0.00%", net_days="0.0", discount_days="0")
    assert require_unique_payment([row], "Credit Card") is row
    assert require_unique_payment([row], "Bank Transfer") is None
    assert require_unique_payment([], "Bank Transfer") is None


@pytest.mark.parametrize(("field", "wrong"), [
    ("description", "Wire"), ("account", "DE123"), ("code", "Credit card"),
    ("cash_discount", "1"), ("discount_days", "30"), ("net_days", "10"),
    ("text_paid", "Paid"), ("text_unpaid", "Due"), ("text_deposit", "Deposit"),
])
def test_payment_definition_conflicts_stop(field, wrong):
    row = payment_definition("Bank Transfer")
    row[field] = wrong
    with pytest.raises(WorkflowReview) as raised:
        require_unique_payment([row], "Bank Transfer")
    assert raised.value.code == "conflicting_payment"
    assert field in raised.value.evidence["fields"]


def test_payment_duplicate_exact_rows_and_missing_definition_stop():
    row = payment_definition("Bank Transfer")
    with pytest.raises(WorkflowReview, match="Multiple Payment"):
        require_unique_payment([row, deepcopy(row)], "Bank Transfer")
    del row["net_days"]
    with pytest.raises(WorkflowReview):
        require_unique_payment([row], "Bank Transfer")


def test_save_intent_is_recorded_before_dispatch_and_blocks_uncertain_repeat():
    ledger = SaveLedger()
    intent = ledger.record_intent("Order", "O-17")
    assert intent["persistence_evidence"] is None
    assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 0}
    with pytest.raises(WorkflowReview) as raised:
        ledger.record_intent("Order", "O-17")
    assert raised.value.code == "repeated_save"
    assert ledger.counts()["save_intents"] == 1


def test_one_record_definition_checks_also_reject_wrong_exact_name():
    vat = required_vat_definition("19")
    assert require_matching_vat(vat, "19") is vat
    with pytest.raises(WorkflowReview):
        require_matching_vat(dict(vat, name="VAT 19.0%"), "19")
    payment = payment_definition("Bank Transfer")
    assert require_matching_payment(payment, "Bank Transfer") is payment
    with pytest.raises(WorkflowReview):
        require_matching_payment(dict(payment, name="bank transfer"), "Bank Transfer")


@pytest.mark.parametrize("marker", ["?", "[UNREADABLE]", "[ILLEGIBLE]"])
def test_unreadable_result_identity_stops_before_absence_is_reported(marker):
    with pytest.raises(WorkflowReview):
        require_unique_product([{"sku": marker}], {"sku": "SKU"})


@pytest.mark.parametrize("rate", [Decimal("1E-999999"), Decimal("1E+999999"), "1.0000001"])
def test_vat_precision_limits_reject_pathological_formatting(rate):
    with pytest.raises(WorkflowReview):
        required_vat_definition(rate)


def test_negative_zero_vat_has_canonical_zero_name():
    assert required_vat_definition(Decimal("-0.00"))["name"] == "VAT 0%"


def test_save_verification_is_separate_evidence_and_does_not_permit_resave():
    ledger = SaveLedger()
    ledger.record_intent("Order", "O-17")
    evidence = {"passed": True, "retrieved_no": "O-17", "total": "119.00", "screenshot": "documents.png"}
    row = ledger.record_verified("Order", "O-17", evidence=evidence)
    assert row["persistence_evidence"] == evidence
    assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 1}
    with pytest.raises(WorkflowReview, match="Save intention already"):
        ledger.record_intent("Order", "O-17")
    with pytest.raises(WorkflowReview, match="already recorded"):
        ledger.record_verified("Order", "O-17", evidence=evidence)


def test_save_ledger_identity_includes_kind_and_all_records_remain_independent():
    ledger = SaveLedger()
    for kind, identity in [("VAT", "VAT 19%"), ("Product", "SKU"), ("Order", "17"), ("Invoice", "17")]:
        ledger.record_intent(kind, identity)
    ledger.record_verified("Product", "SKU", evidence={"passed": True, "retrieved_sku": "SKU"})
    assert ledger.counts() == {"save_intents": 4, "with_persistence_evidence": 1}
    assert len(json.loads(json.dumps(ledger.as_json()))) == 4


def test_save_ledger_copies_reports_and_evidence_instead_of_exposing_mutable_state():
    ledger = SaveLedger()
    intent = ledger.record_intent("Order", "17")
    intent["identity"] = "18"
    evidence = {"passed": True, "check": {"number": "17"}}
    ledger.record_verified("Order", "17", evidence=evidence)
    evidence["check"]["number"] = "18"
    report = ledger.as_json()
    report[0]["identity"] = "18"
    report[0]["persistence_evidence"]["check"]["number"] = "19"
    assert ledger.as_json()[0]["identity"] == "17"
    assert ledger.as_json()[0]["persistence_evidence"]["check"]["number"] == "17"


@pytest.mark.parametrize("evidence", [{}, None, {"passed": True, "amount": Decimal("1")},
                                      {"passed": True, "number": float("nan")}])
def test_save_ledger_rejects_missing_or_non_json_verification(evidence):
    ledger = SaveLedger()
    ledger.record_intent("Order", "17")
    with pytest.raises(WorkflowReview):
        ledger.record_verified("Order", "17", evidence=evidence)
    assert ledger.counts()["with_persistence_evidence"] == 0


def test_save_ledger_verification_without_intent_or_blank_identity_stops():
    ledger = SaveLedger()
    with pytest.raises(WorkflowReview, match="no preceding"):
        ledger.record_verified("Order", "17", evidence={"passed": True, "retrieved_no": "17"})
    with pytest.raises(WorkflowReview):
        ledger.record_intent("Order", " ")
    assert ledger.as_json() == []


@pytest.mark.parametrize("passed", [False, None, "true", 1])
def test_failed_or_ambiguous_readback_never_records_persistence_verification(passed):
    ledger = SaveLedger()
    ledger.record_intent("Order", "17")
    with pytest.raises(WorkflowReview) as raised:
        ledger.record_verified("Order", "17", evidence={"passed": passed, "retrieved_no": "wrong"})
    assert raised.value.code == "failed_persistence_verification"
    assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 0}
    assert ledger.as_json()[0]["persistence_evidence"] is None
    with pytest.raises(WorkflowReview, match="Save intention already"):
        ledger.record_intent("Order", "17")
