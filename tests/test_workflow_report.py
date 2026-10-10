"""Evidence gates are pure; these tests do not prove live Fakturama behavior."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest

from tjm.models import RawOrder
from tjm.validation import validate_order
from tjm.workflow_report import (
    complete_run, completion_expectations, content_expectations, new_run_report, preflight, record_check,
    set_generated_number, stop_run, sync_ledger, write_run_report,
)
from tjm.workflow_rules import SaveLedger, WorkflowReview


@pytest.fixture
def extraction():
    raw = json.loads((Path(__file__).parent / "fixtures/sample_expected.json").read_text())["raw"]
    raw["customer"]["email"] = "marta.klein@example.com"
    validation = validate_order(RawOrder.model_validate(raw), name_order="given-family")
    assert validation.status == "validated"
    return {**validation.model_dump(mode="json"), "raw": raw,
            "evidence": {"input": {"path": "/fixtures/demo.png", "sha256": "a" * 64},
                         "name_order": "given-family",
                         "report": "/fixtures/extraction/report.json"}}


def run(extraction, *, demo=True):
    return new_run_report(extraction, workspace="/fixtures/fakturama-demo",
                          input_kind="demonstration" if demo else "original",
                          demo_label="Approved example.com demonstration" if demo else None,
                          original_input_sha256="b" * 64 if demo else None)


def linkage_evidence():
    return {
        "followup_action": {"order_number": "PO000001", "invoice_number": "RE000001",
                            "area": "Create a follow-up document", "action": "Invoice",
                            "passed": True, "ui_evidence": {"screenshot": "followup-invoice.png"}},
        "persisted_linkage": {"order_number": "PO000001", "invoice_number": "RE000001",
                              "method": "This transaction", "passed": True,
                              "ui_evidence": {"screenshot": "saved-transaction.png"}},
    }


def passing_run(extraction, *, demo=True):
    report = run(extraction, demo=demo)
    set_generated_number(report, "order", "PO000001")
    set_generated_number(report, "invoice", "RE000001")
    ledger = SaveLedger()
    for kind, number in (("Order", "PO000001"), ("Invoice", "RE000001")):
        ledger.record_intent(kind, number)
        ledger.record_verified(kind, number, evidence={"passed": True, "retrieved_no": number,
                                                       "screenshot": f"{kind}-reopened.png"})
    for name, expected in completion_expectations(report).items():
        evidence = linkage_evidence() if name == "invoice_order_linkage" else {"screenshot": f"{name}.png"}
        record_check(report, name, expected=expected, observed=expected,
                     passed=True, evidence=evidence, stage="reopened")
    return report, ledger


def test_known_test_domain_stops_before_mutations_and_retains_source(extraction):
    extraction["order"]["customer"]["email"] = "marta.klein@example.test"
    extraction["raw"]["customer"]["email"] = "marta.klein@example.test"
    before = deepcopy(extraction)
    report = preflight(extraction)
    assert report["status"] == "review_required"
    assert report["desktop_mutations_performed"] == 0
    issue = report["issues"][0]
    assert issue["code"] == "fakturama_test_email_incompatibility"
    assert issue["source_value"] == "marta.klein@example.test"
    assert issue["evidence"]["probe_email"] == "ava.stone@example.test"
    assert issue["evidence"]["reopened_email"] == ""
    assert extraction == before


@pytest.mark.parametrize("email", ["a@EXAMPLE.TEST", "a@sub.example.test"])
def test_known_guard_handles_case_and_subdomains_without_changing_email(extraction, email):
    extraction["order"]["customer"]["email"] = email
    extraction["raw"]["customer"]["email"] = email
    result = preflight(extraction)
    assert result["status"] == "review_required"
    assert result["issues"][0]["source_value"] == email


@pytest.mark.parametrize("email", ["a@example.com", "a@example.invalid", "a@example.testing"])
def test_preflight_does_not_guess_other_domains_are_valid(extraction, email):
    extraction["order"]["customer"]["email"] = email
    extraction["raw"]["customer"]["email"] = email
    result = preflight(extraction)
    assert result["status"] == "ready"
    assert "persistence still require native UI readback" in result["checks"][-1]["limitation"]


def test_known_version_guard_does_not_claim_other_versions_pass(extraction):
    extraction["order"]["customer"]["email"] = "a@example.test"
    extraction["raw"]["customer"]["email"] = "a@example.test"
    result = preflight(extraction, fakturama_version="2.1.3c")
    assert result["status"] == "ready"
    assert "native UI readback" in result["checks"][-1]["limitation"]


@pytest.mark.parametrize(("status", "expected"), [
    ("failed", "failed"), ("review_required", "review_required"), ("running", "review_required"),
])
def test_unvalidated_extraction_cannot_begin_desktop_work(extraction, status, expected):
    extraction["status"] = status
    extraction["issues"] = [{"code": "source_error", "message": "Review source"}]
    result = preflight(extraction)
    assert result["status"] == expected
    assert result["desktop_mutations_performed"] == 0
    assert result["issues"][0]["code"] == "source_error"
    assert result["issues"][-1]["code"] == "extraction_not_validated"


@pytest.mark.parametrize("change", [
    {"issues": [{"code": "unresolved"}]}, {"checks": []},
    {"checks": [{"passed": 1}]}, {"checks": [{"passed": "true"}]},
    {"checks": [{"passed": False}]}, {"checks": [None]},
])
def test_validated_label_does_not_override_conflicting_checks(extraction, change):
    extraction.update(change)
    assert preflight(extraction)["issues"][0]["code"] == "conflicting_extraction_report"


@pytest.mark.parametrize("changed", [None, "wrong", {"unexpected": "field"}])
def test_malformed_validated_order_stops_before_desktop(extraction, changed):
    extraction["order"] = changed
    assert preflight(extraction)["issues"][0]["code"] == "invalid_validated_order"


def test_raw_source_email_cannot_be_replaced_only_in_validated_order(extraction):
    extraction["raw"]["customer"]["email"] = "marta.klein@example.test"
    assert extraction["order"]["customer"]["email"] == "marta.klein@example.com"
    result = preflight(extraction)
    assert result["status"] == "review_required"
    assert result["issues"][0]["code"] == "conflicting_extraction_order"
    assert result["desktop_mutations_performed"] == 0


def test_revalidation_detects_forged_totals_and_changed_name_policy(extraction):
    extraction["order"]["totals"]["gross"] = "999.00"
    assert preflight(extraction)["issues"][0]["code"] == "conflicting_extraction_order"
    extraction["order"]["totals"]["gross"] = "678.30"
    extraction["evidence"]["name_order"] = None
    assert preflight(extraction)["issues"][0]["code"] == "conflicting_extraction_order"


@pytest.mark.parametrize("raw", [None, {}, {"unknown": "field"}])
def test_missing_or_invalid_raw_transcription_stops(extraction, raw):
    extraction["raw"] = raw
    assert preflight(extraction)["issues"][0]["code"] == "invalid_extraction_source"


@pytest.mark.parametrize("source", [None, {}, {"path": "/x", "sha256": "not-a-hash"},
                                    {"path": "", "sha256": "a" * 64}])
def test_missing_input_provenance_is_a_failed_preflight(extraction, source):
    extraction["evidence"]["input"] = source
    assert preflight(extraction)["status"] == "failed"
    assert preflight(extraction)["issues"][0]["code"] == "missing_input_provenance"


def test_demo_provenance_is_retained_and_copied(extraction):
    report = run(extraction)
    assert report["status"] == "running"
    assert report["input"]["sha256"] == "a" * 64
    assert report["workspace"] == "/fixtures/fakturama-demo"
    assert report["provenance"]["input_kind"] == "demonstration"
    assert report["provenance"]["original_input_sha256"] == "b" * 64
    assert "acceptance_scope" not in report
    assert "original_sample_acceptance" not in report
    extraction["order"]["customer"]["email"] = "changed@example.com"
    assert report["order"]["customer"]["email"] == "marta.klein@example.com"


@pytest.mark.parametrize("kwargs", [
    {"input_kind": "guess"}, {"input_kind": "demonstration"},
    {"input_kind": "demonstration", "demo_label": "Demo", "original_input_sha256": "bad"},
    {"input_kind": "demonstration", "demo_label": "Demo", "original_input_sha256": "a" * 64},
    {"input_kind": "original", "demo_label": "Demo"},
    {"input_kind": "original", "original_input_sha256": "b" * 64},
])
def test_ambiguous_original_demo_provenance_is_rejected(extraction, kwargs):
    with pytest.raises(WorkflowReview):
        new_run_report(extraction, workspace="/fixtures", **kwargs)


def test_known_original_source_builds_stopped_report_with_no_save_intents(extraction):
    extraction["order"]["customer"]["email"] = "marta.klein@example.test"
    extraction["raw"]["customer"]["email"] = "marta.klein@example.test"
    report = run(extraction, demo=False)
    assert report["status"] == "review_required"
    assert report["save_counts"] == {"save_intents": 0, "with_persistence_evidence": 0}
    assert report["order"]["customer"]["email"] == "marta.klein@example.test"
    assert complete_run(report, SaveLedger()) is report
    assert report["status"] == "review_required"
    with pytest.raises(WorkflowReview, match="stopped"):
        set_generated_number(report, "order", "PO1")


def test_generated_number_retains_first_native_proposal(extraction):
    report = run(extraction)
    set_generated_number(report, "order", "PO000007")
    with pytest.raises(WorkflowReview, match="already recorded"):
        set_generated_number(report, "order", "PO000008")
    assert report["generated_numbers"]["order"] == "PO000007"


@pytest.mark.parametrize("passed", [1, 0, None, "true"])
def test_check_passed_requires_exact_bool(extraction, passed):
    with pytest.raises(WorkflowReview, match="boolean"):
        record_check(run(extraction), "paid", expected=True, observed=True,
                     passed=passed, evidence={"screenshot": "x.png"})


@pytest.mark.parametrize(("expected", "observed"), [(True, 1), (False, 0), ("678.30", 678.30)])
def test_passing_check_cannot_hide_type_or_value_mismatch(extraction, expected, observed):
    with pytest.raises(WorkflowReview, match="exact matching"):
        record_check(run(extraction), "payment", expected=expected, observed=observed,
                     passed=True, evidence={"screenshot": "x.png"})


@pytest.mark.parametrize("evidence", [{}, None, {"number": Decimal("1")}, {"number": float("nan")}])
def test_check_needs_nonempty_json_evidence(extraction, evidence):
    with pytest.raises(WorkflowReview):
        record_check(run(extraction), "payment", expected=True, observed=True,
                     passed=True, evidence=evidence)


def test_failed_check_cannot_be_overwritten_and_evidence_is_copied(extraction):
    report = run(extraction)
    evidence = {"screenshot": "x.png", "native": {"value": "wrong"}}
    record_check(report, "email", expected="right", observed="wrong", passed=False, evidence=evidence)
    evidence["native"]["value"] = "right"
    assert report["checks"][0]["evidence"]["native"]["value"] == "wrong"
    with pytest.raises(WorkflowReview, match="already exists"):
        record_check(report, "email", expected="right", observed="right", passed=True,
                     evidence={"screenshot": "y.png"})


def test_completed_demo_retains_input_provenance_and_required_checks(extraction):
    report, ledger = passing_run(extraction)
    assert complete_run(report, ledger)["status"] == "verified"
    assert report["provenance"]["input_kind"] == "demonstration"
    assert set(report["completion"]) == {"required_checks"}
    assert report["save_counts"] == {"save_intents": 2, "with_persistence_evidence": 2}
    assert len(report["completion"]["required_checks"]) == 7
    assert json.loads(json.dumps(report))["status"] == "verified"


def test_completed_original_retains_input_provenance(extraction):
    report, ledger = passing_run(extraction, demo=False)
    assert complete_run(report, ledger)["status"] == "verified"
    assert report["provenance"]["input_kind"] == "original"


@pytest.mark.parametrize("name", ["order_persistence", "invoice_persistence", "order_open",
                                  "invoice_payment", "invoice_order_linkage", "order_content", "invoice_content"])
def test_missing_required_comparison_prevents_completion(extraction, name):
    report, ledger = passing_run(extraction)
    report["checks"] = [check for check in report["checks"] if check["name"] != name]
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == name for issue in report["issues"])
    assert report["completion"] is None


@pytest.mark.parametrize("name", ["invoice_payment", "order_content", "invoice_content"])
def test_list_does_not_prove_hidden_payment_or_document_content(extraction, name):
    report, ledger = passing_run(extraction)
    next(check for check in report["checks"] if check["name"] == name)["stage"] = "saved_list"
    assert complete_run(report, ledger)["status"] == "review_required"


def test_prescribed_pre_save_content_plus_saved_rows_can_complete_without_content_reopen(extraction):
    report, _ = passing_run(extraction)
    for check in report["checks"]:
        if check["name"] in {"order_content", "invoice_content"}:
            check["stage"] = "pre_save"
        elif check["name"] != "invoice_payment":
            check["stage"] = "saved_list"
    ledger = SaveLedger()
    for kind, number in (("Order", "PO000001"), ("Invoice", "RE000001")):
        ledger.record_intent(kind, number)
        ledger.record_verified(kind, number, evidence={"passed": True, "retrieved_no": number,
                                                       "screenshot": f"{kind}-saved-row.png"})
    assert complete_run(report, ledger)["status"] == "verified"
    assert report["provenance"]["input_kind"] == "demonstration"


@pytest.mark.parametrize("name", ["order_persistence", "invoice_persistence", "order_open",
                                  "invoice_payment", "invoice_order_linkage"])
def test_draft_cannot_prove_persistence_payment_or_relationship(extraction, name):
    report, ledger = passing_run(extraction)
    next(check for check in report["checks"] if check["name"] == name)["stage"] = "pre_save"
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == name for issue in report["issues"])


@pytest.mark.parametrize("key", ["followup_action", "persisted_linkage"])
def test_shared_reference_without_both_action_and_persisted_link_proof_cannot_complete(extraction, key):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == "invoice_order_linkage")
    del check["evidence"][key]
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue["code"] == "missing_linkage_evidence" for issue in report["issues"])


@pytest.mark.parametrize(("key", "field", "value"), [
    ("followup_action", "order_number", "PO999999"),
    ("followup_action", "invoice_number", "RE999999"),
    ("followup_action", "area", "Top toolbar"),
    ("followup_action", "action", "Delivery"),
    ("followup_action", "passed", 1),
    ("followup_action", "ui_evidence", {}),
    ("persisted_linkage", "order_number", "PO999999"),
    ("persisted_linkage", "invoice_number", "RE999999"),
    ("persisted_linkage", "method", "Same Cust.Ref."),
    ("persisted_linkage", "passed", False),
    ("persisted_linkage", "ui_evidence", None),
])
def test_linkage_proof_must_identify_real_followup_and_saved_transaction(extraction, key, field, value):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == "invoice_order_linkage")
    check["evidence"][key][field] = value
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue["code"] == "missing_linkage_evidence" for issue in report["issues"])


@pytest.mark.parametrize("name", ["order_persistence", "invoice_persistence", "order_open"])
def test_generated_number_alone_cannot_prove_required_documents_row(extraction, name):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == name)
    check["expected"] = check["observed"] = {"number": check["expected"]["number"]}
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == name for issue in report["issues"])


@pytest.mark.parametrize(("name", "field", "value"), [
    ("order_persistence", "date", "2026-07-15"),
    ("order_persistence", "external_reference", "other-ref"),
    ("order_persistence", "state", "Closed"),
    ("order_persistence", "total", "999.00"),
    ("invoice_persistence", "external_reference", "other-ref"),
    ("invoice_persistence", "paid", False),
    ("invoice_persistence", "total", "999.00"),
    ("order_open", "external_reference", "other-ref"),
    ("order_open", "total", "999.00"),
])
def test_passing_saved_rows_must_match_source_values(extraction, name, field, value):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == name)
    check["expected"][field] = check["observed"][field] = value
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == name for issue in report["issues"])


def test_arbitrary_passing_values_do_not_override_source_paid_expectation(extraction):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == "invoice_payment")
    check["expected"]["paid"] = check["observed"]["paid"] = False
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == "invoice_payment" for issue in report["issues"])


def test_unpaid_source_requires_clear_payment_values(extraction):
    extraction["raw"]["payment"].update(status="UNPAID", date=None)
    extraction.update(validate_order(RawOrder.model_validate(extraction["raw"]),
                                     name_order="given-family").model_dump(mode="json"))
    report, ledger = passing_run(extraction)
    expected = completion_expectations(report)["invoice_payment"]
    assert expected["paid"] is False
    assert expected["date"] is expected["amount"] is None
    assert complete_run(report, ledger)["status"] == "verified"


def test_order_must_remain_open_and_linkage_retains_source_date_reference(extraction):
    report, ledger = passing_run(extraction)
    expected = completion_expectations(report)
    assert expected["order_persistence"] == {
        "number": "PO000001", "date": "2026-07-14", "external_reference": "WEB-2026-0714-A17",
        "state": "Open", "total": "678.30"}
    assert expected["invoice_persistence"] == {
        "number": "RE000001", "external_reference": "WEB-2026-0714-A17", "paid": True, "total": "678.30"}
    assert expected["order_open"] == {
        "number": "PO000001", "state": "Open", "external_reference": "WEB-2026-0714-A17", "total": "678.30"}
    assert expected["invoice_order_linkage"] == {
        "invoice_number": "RE000001", "order_number": "PO000001",
        "order_date": "2026-07-14", "external_reference": "WEB-2026-0714-A17"}
    check = next(check for check in report["checks"] if check["name"] == "order_open")
    check["observed"]["state"] = "Closed"
    assert complete_run(report, ledger)["status"] == "review_required"


def test_content_expectations_cover_source_addresses_ordered_lines_and_totals(extraction):
    report, _ = passing_run(extraction)
    content = content_expectations(report)
    expected = content["order_content"]
    assert expected["number"] == "PO000001"
    assert expected["order_date"] == "2026-07-14"
    assert expected["external_reference"] == "WEB-2026-0714-A17"
    assert expected["billing"] == {
        "recipient": "Northstar Office GmbH", "street": "Friedrichstrasse 88",
        "postal_code": "10117", "city": "Berlin", "country": "Germany"}
    assert expected["delivery"]["recipient"] == "Northstar Office Warehouse"
    assert expected["items"][0] == {
        "sku": "CHR-ERG-01", "description": "Ergonomic Desk Chair", "quantity": "2",
        "unit_net": "250.00", "vat_percent": "19", "discount_percent": "10",
        "line_net": "450.00"}
    assert report["order"]["items"][0]["unit"] == "pcs"
    assert expected["totals"] == {"net": "570.00", "vat": "108.30", "gross": "678.30"}
    assert expected["currency"] == "EUR"
    assert expected["overall_discount_percent"] == expected["shipping_net"] == "0"
    assert expected["price_mode"] == "Net"
    assert expected["vat_mode"] == "With VAT"
    assert "shipping_vat_percent" not in expected
    assert content["invoice_content"] == {**expected, "number": "RE000001"}
    expected["items"][0]["description"] = "changed"
    assert content["invoice_content"]["items"][0]["description"] == "Ergonomic Desk Chair"


def test_order_content_can_be_checked_before_followup_invoice_number_exists(extraction):
    report = run(extraction)
    set_generated_number(report, "order", "PO000001")
    content = content_expectations(report)
    assert set(content) == {"order_content"}
    record_check(report, "order_content", expected=content["order_content"],
                 observed=content["order_content"], passed=True,
                 evidence={"screenshot": "saved-order-reopened-before-followup.png"}, stage="reopened")
    set_generated_number(report, "invoice", "RE000001")
    assert set(content_expectations(report)) == {"order_content", "invoice_content"}


def test_content_preserves_repeated_skus_and_checks_only_supplied_optional_fields(extraction):
    report, _ = passing_run(extraction)
    report["order"]["items"].append(deepcopy(report["order"]["items"][0]))
    report["order"]["items"][0]["unit"] = None
    report["order"]["customer"]["delivery"]["district"] = "Moabit"
    report["order"]["customer"]["billing"]["additional_name"] = "Accounts"
    report["order"]["shipping_vat_percent"] = "19"
    content = content_expectations(report)["order_content"]
    assert [item["sku"] for item in content["items"]] == ["CHR-ERG-01", "MAT-DESK-02", "CHR-ERG-01"]
    assert "unit" not in content["items"][0]
    assert "unit" not in content["items"][2]
    assert report["order"]["items"][2]["unit"] == "pcs"
    assert content["delivery"]["district"] == "Moabit"
    assert content["billing"]["additional_name"] == "Accounts"
    assert content["shipping_vat_percent"] == "19"


@pytest.mark.parametrize(("kind", "field", "wrong"), [
    ("order_content", "delivery", {"recipient": "Wrong company"}),
    ("invoice_content", "items", []), ("invoice_content", "totals", {"gross": "999.00"}),
    ("order_content", "price_mode", "Gross"), ("invoice_content", "vat_mode", "No VAT"),
])
def test_numbers_and_payment_passes_cannot_mask_wrong_document_content(extraction, kind, field, wrong):
    report, ledger = passing_run(extraction)
    check = next(check for check in report["checks"] if check["name"] == kind)
    check["expected"][field] = check["observed"][field] = wrong
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue.get("check") == kind for issue in report["issues"])


@pytest.mark.parametrize("kind", ["Order", "Invoice"])
def test_save_intent_without_persistence_prevents_completion(extraction, kind):
    report, _ = passing_run(extraction)
    ledger = SaveLedger()
    for current, number in (("Order", "PO000001"), ("Invoice", "RE000001")):
        ledger.record_intent(current, number)
        if current != kind:
            ledger.record_verified(current, number, evidence={"passed": True, "retrieved_no": number})
    assert complete_run(report, ledger)["status"] == "review_required"
    assert any(issue["code"] == "missing_document_persistence" and issue["kind"] == kind
               for issue in report["issues"])


def test_other_generated_identity_cannot_prove_this_documents_persistence(extraction):
    report, _ = passing_run(extraction)
    ledger = SaveLedger()
    for kind, number in (("Order", "PO999999"), ("Invoice", "RE000001")):
        ledger.record_intent(kind, number)
        ledger.record_verified(kind, number, evidence={"passed": True, "retrieved_no": number})
    assert complete_run(report, ledger)["status"] == "review_required"


def test_failed_extra_check_or_unverified_master_save_blocks_green_result(extraction):
    report, ledger = passing_run(extraction)
    record_check(report, "customer_email", expected="a", observed="b", passed=False,
                 evidence={"screenshot": "email.png"})
    ledger.record_intent("Product", "SKU-17")
    assert complete_run(report, ledger)["status"] == "review_required"
    assert {issue["code"] for issue in report["issues"]} >= {"failed_workflow_check", "unverified_save"}


def test_ledger_sync_reports_intents_separately_and_copies_entries(extraction):
    report = run(extraction)
    ledger = SaveLedger()
    ledger.record_intent("Order", "PO1")
    sync_ledger(report, ledger)
    assert report["save_counts"] == {"save_intents": 1, "with_persistence_evidence": 0}
    report["save_ledger"][0]["identity"] = "changed"
    assert ledger.as_json()[0]["identity"] == "PO1"


@pytest.mark.parametrize("status", ["failed", "review_required"])
def test_stopped_runs_retain_their_status_and_never_complete(extraction, status):
    report, ledger = passing_run(extraction)
    stop_run(report, status=status, code="unknown_save_outcome", message="Review without retry.",
             evidence={"last_screenshot": "save.png"})
    assert complete_run(report, ledger)["status"] == status
    assert report["completion"] is None
    assert report["provenance"]["input_kind"] == "demonstration"
    assert report["save_counts"]["save_intents"] == 2
    with pytest.raises(WorkflowReview, match="stopped"):
        record_check(report, "later", expected=True, observed=True, passed=True,
                     evidence={"screen": "later.png"})


def test_stopped_report_still_retains_last_uncertain_save_intention(extraction):
    report = run(extraction)
    ledger = SaveLedger()
    ledger.record_intent("Order", "PO000001")
    stop_run(report, status="review_required", code="unknown_save_outcome",
             message="Review without retry.")
    sync_ledger(report, ledger)
    assert report["status"] == "review_required"
    assert report["save_ledger"][0]["persistence_evidence"] is None
    assert report["save_counts"] == {"save_intents": 1, "with_persistence_evidence": 0}


def test_atomic_report_write_roundtrips_unicode_and_leaves_no_temporary_files(extraction, tmp_path):
    report = run(extraction)
    report["provenance"]["demo_label"] = "Approved demonstration — no original acceptance"
    target = tmp_path / "workflow-report.json"
    target.write_text("prior incomplete content")
    write_run_report(report, target)
    assert json.loads(target.read_text()) == report
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_write_does_not_replace_previous_evidence(extraction, tmp_path):
    report = run(extraction)
    target = tmp_path / "workflow-report.json"
    target.write_text("prior evidence")
    report["invalid"] = float("nan")
    with pytest.raises(ValueError):
        write_run_report(report, target)
    assert target.read_text() == "prior evidence"
