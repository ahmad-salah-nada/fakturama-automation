"""Desktop input limits and numeric equality, without native or network input.

The CLI tests use the real run_image/preflight path with a supplied validated
extraction. Native construction, launch and transaction dispatch are forbidden.
The collector tests double actual field reads; they do not prove live UI support.
"""

from copy import deepcopy
from decimal import Inexact, Rounded, localcontext
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm import cli, document_content
from tjm.desktop import Desktop
from tjm.models import RawOrder, ValidatedOrder
from tjm.validation import validate_order
from tjm.workflow_report import completion_expectations, content_expectations, preflight
from tjm.workflow_rules import WorkflowReview


def source():
    raw = json.loads((Path(__file__).parent / "fixtures/sample_expected.json").read_text())["raw"]
    raw["customer"]["email"] = "marta.klein@example.com"
    return raw


def extraction(raw, folder=None):
    validation = validate_order(RawOrder.model_validate(raw), name_order="given-family")
    assert validation.status == "validated", validation.issues
    evidence = {"input": {"path": "/independent-source.png", "sha256": "a" * 64},
                "name_order": "given-family", "report": "/independent-extraction/report.json"}
    if folder is not None:
        evidence["run_directory"] = str(folder)
    return {**validation.model_dump(mode="json"), "raw": raw, "evidence": evidence}


CASES = [
    ("one_row", "items"), ("three_rows", "items"),
    ("subcent_price", "items[0].unit_net"),
    ("quantity_precision", "items[0].quantity"),
    ("vat_precision", "items[0].vat_percent"),
    ("discount_precision", "items[0].discount_percent"),
    ("shipping", "shipping_net"), ("overall_discount", "overall_discount_percent"),
    ("shipping_vat", "shipping_vat_percent"),
] + [(f"{kind}.{field}", f"customer.{kind}.{field}")
     for kind in ("billing", "delivery")
     for field in ("additional_name", "address_specification", "district")]


def unsupported_source(case):
    raw = source()
    if case == "one_row":
        raw["items"] = raw["items"][:1]
        raw["totals"] = {"net": "450.00", "vat": "85.50", "gross": "535.50"}
    elif case == "three_rows":
        raw["items"].append(deepcopy(raw["items"][1]))
        raw["totals"] = {"net": "690.00", "vat": "131.10", "gross": "821.10"}
    elif case == "subcent_price":
        raw["items"][0].update(quantity="2", unit_net="0.125", discount_percent="20", line_net="0.20")
        raw["items"][1].update(quantity="1", unit_net="1", discount_percent="0", line_net="1.00")
        raw["totals"] = {"net": "1.20", "vat": "0.23", "gross": "1.43"}
    elif case in {"quantity_precision", "vat_precision", "discount_precision"}:
        raw["items"][0].update(quantity="1", unit_net="1000", vat_percent="0", discount_percent="0", line_net="1000")
        raw["items"][1].update(quantity="1", unit_net="1", vat_percent="0", discount_percent="0", line_net="1")
        if case == "quantity_precision":
            raw["items"][0].update(quantity="0.123", line_net="123")
            raw["totals"] = {"net": "124", "vat": "0", "gross": "124"}
        elif case == "vat_precision":
            raw["items"][0]["vat_percent"] = "0.123"
            raw["totals"] = {"net": "1001", "vat": "1.23", "gross": "1002.23"}
        else:
            raw["items"][0].update(discount_percent="0.123", line_net="998.77")
            raw["totals"] = {"net": "999.77", "vat": "0", "gross": "999.77"}
    elif case == "shipping":
        raw.update(shipping_net="1", shipping_vat_percent="0")
        raw["totals"] = {"net": "571", "vat": "108.30", "gross": "679.30"}
    elif case == "overall_discount":
        raw["overall_discount_percent"] = "10"
        raw["totals"] = {"net": "513", "vat": "97.47", "gross": "610.47"}
    elif case == "shipping_vat":
        raw["shipping_vat_percent"] = "0"
    else:
        kind, field = case.split(".")
        raw["customer"][kind][field] = "Source extra address text"
    return raw


@pytest.mark.parametrize("case,field", CASES)
def test_known_unsupported_source_stops_before_desktop_in_actual_cli_run(
        case, field, monkeypatch, tmp_path, capsys):
    folder = tmp_path / "extraction"
    folder.mkdir()
    report = extraction(unsupported_source(case), folder)
    before = deepcopy(report)
    extraction_calls = []

    def supplied_extraction(*args, **kwargs):
        extraction_calls.append((args, kwargs))
        return report

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported source must not construct or operate the desktop.")

    monkeypatch.setattr("tjm.extract.extract_order", supplied_extraction)
    monkeypatch.setattr(Desktop, "__init__", forbidden)
    monkeypatch.setattr("tjm.readiness._load_native", forbidden)
    monkeypatch.setattr("tjm.readiness._select_workspace_desktop", forbidden)
    monkeypatch.setattr("tjm.readiness.connect_readiness", forbidden)
    monkeypatch.setattr("tjm.demo_setup.launch_fresh_demo", forbidden)
    monkeypatch.setattr("tjm.live_workflow.execute_transaction", forbidden)
    workspace = tmp_path / "must-not-be-created"
    result = cli.main(["run", "/independent-source.png", "--workspace", str(workspace),
                       "--output", str(tmp_path), "--name-order", "given-family"])
    retained = json.loads(capsys.readouterr().out)
    assert result == 2 and retained["status"] == "review_required"
    assert any(issue["code"] == "unsupported_desktop_input" and issue["field"] == field
               for issue in retained["issues"])
    assert retained["preflight"]["desktop_mutations_performed"] == 0
    assert retained["save_ledger"] == []
    assert retained["save_counts"] == {"save_intents": 0, "with_persistence_evidence": 0}
    assert retained["generated_numbers"] == {"order": None, "invoice": None}
    assert retained["order"] == before["order"] and retained["input"] == before["evidence"]["input"]
    assert report == before and len(extraction_calls) == 1
    assert not workspace.exists()
    assert json.loads((folder / "workflow-report.json").read_text()) == retained


def equivalent_source():
    raw = source()
    raw["items"][0].update(quantity="2.000000", unit_net="250", vat_percent="19.0000",
                           discount_percent="10.00", line_net="450")
    raw["items"][1].update(quantity="3.000", unit_net="40.000000", vat_percent="19.00",
                           discount_percent="0.000", line_net="120.0")
    raw["totals"] = {"net": "570", "vat": "108.30000", "gross": "678.300"}
    raw.update(overall_discount_percent="0.0000", shipping_net="0.000000")
    return raw


def actual_reads():
    """Fixed UI-format values, supplied independently from the expected adapter."""
    return {
        "header": {"number": "PO000001", "date": "Jul 14, 2026",
                   "reference": "WEB-2026-0714-A17", "price_mode": "Net", "vat_mode": "With VAT"},
        "addresses": {
            "billing": "Northstar Office GmbH\nFriedrichstrasse 88\n10117 Berlin\nGermany",
            "delivery": "Northstar Office Warehouse\nBeusselstrasse 44\n10553 Berlin\nGermany"},
        "items": [
            {"Item No.": "CHR-ERG-01", "Qty.": "2.00", "Name": "Ergonomic Desk Chair",
             "Description": "Ergonomic Desk Chair", "U.Price": "EUR 250", "Discount": "-10.00 %",
             "VAT": {"text": "VAT 19% (19.0%)"}, "Price": {"text": "EUR 450.00"}},
            {"Item No.": "MAT-DESK-02", "Qty.": "3.00", "Name": "Anti-Fatigue Desk Mat",
             "Description": "Anti-Fatigue Desk Mat", "U.Price": "EUR40.00", "Discount": "0.00 %",
             "VAT": {"text": "VAT 19% (19.0%)"}, "Price": {"text": "EUR120.00"}}],
        "summary": {"net": "570.00", "vat": "108.30", "gross": "678.30",
                    "overall_discount_percent": "0", "shipping_net": "0.00"},
    }


def test_equivalent_spelling_passes_preflight_and_matches_independent_observation_without_source_changes():
    report = extraction(equivalent_source())
    before = deepcopy(report)
    assert preflight(report)["status"] == "ready"
    expected = content_expectations({"order": report["order"],
                                     "generated_numbers": {"order": "PO000001"}})["order_content"]
    assert expected == document_content.adapt_document_content(actual_reads())
    assert expected["items"][0] == {
        "sku": "CHR-ERG-01", "description": "Ergonomic Desk Chair", "quantity": "2",
        "unit_net": "250.00", "vat_percent": "19", "discount_percent": "10", "line_net": "450.00"}
    assert expected["totals"] == {"net": "570.00", "vat": "108.30", "gross": "678.30"}
    assert report == before


def test_capability_and_expected_numbers_ignore_hostile_caller_decimal_context():
    report = extraction(equivalent_source())
    request = {"order": report["order"], "generated_numbers": {"order": "PO000001"}}
    normal = content_expectations(request)
    with localcontext() as context:
        context.prec = 2
        context.Emax = 1
        context.Emin = -1
        context.traps[Inexact] = context.traps[Rounded] = True
        assert preflight(report)["status"] == "ready"
        assert content_expectations(request) == normal


@pytest.mark.parametrize("field", ["unit_net", "quantity", "vat_percent", "discount_percent"])
def test_effective_two_decimal_precision_is_supported_without_changing_source(field):
    raw = source()
    raw["items"][0].update(quantity="1", unit_net="1000", vat_percent="0", discount_percent="0", line_net="1000")
    raw["items"][1].update(quantity="1", unit_net="1", vat_percent="0", discount_percent="0", line_net="1")
    raw["items"][0][field] = "0.120000"
    if field == "unit_net":
        raw["items"][0]["line_net"] = "0.12"
        raw["totals"] = {"net": "1.12", "vat": "0", "gross": "1.12"}
    elif field == "quantity":
        raw["items"][0]["line_net"] = "120"
        raw["totals"] = {"net": "121", "vat": "0", "gross": "121"}
    elif field == "vat_percent":
        raw["totals"] = {"net": "1001", "vat": "1.20", "gross": "1002.20"}
    else:
        raw["items"][0]["line_net"] = "998.80"
        raw["totals"] = {"net": "999.80", "vat": "0", "gross": "999.80"}
    report = extraction(raw)
    before = deepcopy(report)
    assert preflight(report)["status"] == "ready"
    assert report == before


def test_completion_money_matches_ui_spelling_while_source_totals_remain_unchanged():
    extracted = extraction(equivalent_source())
    request = {"order": extracted["order"], "generated_numbers": {"order": "PO000001", "invoice": "INV000001"}}
    before = deepcopy(request)
    expected = completion_expectations(request)
    for check in ("order_persistence", "invoice_persistence", "order_open"):
        assert expected[check]["total"] == "678.30"
    assert expected["invoice_payment"]["amount"] == "678.30"
    assert request["order"]["totals"]["gross"] == "678.300"
    assert request == before


def test_expected_content_never_rounds_an_unsupported_subcent_source_price():
    report = extraction(unsupported_source("subcent_price"))
    before = deepcopy(report)
    assert report["order"]["items"][0]["unit_net"] == "0.125"
    with pytest.raises(WorkflowReview) as failure:
        content_expectations({"order": report["order"], "generated_numbers": {"order": "PO000001"}})
    assert failure.value.code == "unsupported_desktop_input"
    assert failure.value.evidence["observed"] == "0.125"
    assert report == before


def test_signed_zero_is_numeric_zero_without_changing_the_raw_spelling():
    raw = source()
    for item in raw["items"]:
        item["vat_percent"] = "-0.00"
    raw["totals"].update(vat="-0.0000", gross="570.00")
    raw.update(overall_discount_percent="-0.0000", shipping_net="-0.00")
    report = extraction(raw)
    before = deepcopy(report)
    assert preflight(report)["status"] == "ready"
    observed_raw = actual_reads()
    for item in observed_raw["items"]:
        item["VAT"]["text"] = "VAT 0% (0.0%)"
    observed_raw["summary"].update(vat="0.00", gross="570.00")
    expected = content_expectations({"order": report["order"], "generated_numbers": {"order": "PO000001"}})["order_content"]
    assert expected == document_content.adapt_document_content(observed_raw)
    assert expected["totals"]["vat"] == "0.00"
    assert report == before and report["raw"]["totals"]["vat"] == "-0.0000"


@pytest.mark.parametrize("changed", [None, ("Qty.", "2.01"), ("U.Price", "EUR250.01"),
                                      ("Discount", "-10.01 %"), ("VAT", "VAT 19.01% (19.01%)"),
                                      ("Price", "EUR450.01"), ("Item No.", "chr-erg-01"),
                                      ("Description", "ERGONOMIC DESK CHAIR")])
def test_real_content_collector_accepts_equal_numbers_and_refuses_unequal_values(
        changed, monkeypatch, tmp_path):
    report = extraction(equivalent_source())
    order = ValidatedOrder.model_validate_json(json.dumps(report["order"]))
    raw = actual_reads()
    if changed is not None:
        label, value = changed
        raw["items"][0][label] = {"text": value} if label in {"VAT", "Price"} else value
    monkeypatch.setattr(document_content, "read_document_header", lambda *a, **k: raw["header"])
    monkeypatch.setattr(document_content, "read_addresses", lambda *a, **k: raw["addresses"])
    monkeypatch.setattr(document_content, "read_document_totals", lambda *a, **k: raw["summary"])
    monkeypatch.setattr(document_content, "_row_coverage", lambda *a: {"passed": True, "observed_count": 2})
    monkeypatch.setattr(document_content, "read_item_cell", lambda *a, row_index, header, **k:
                        raw["items"][row_index][header])
    monkeypatch.setattr(document_content, "read_painted_item_cell", lambda *a, row_index, header, **k:
                        raw["items"][row_index][header])
    ui = SimpleNamespace(evidence_dir=tmp_path, actions=[], discoveries=[])
    if changed is None:
        result = document_content.collect_document_content(ui, order, title="New Order", retained_number="PO000001")
        assert result["passed"] is True and result["observed"] == result["expected"]
    else:
        with pytest.raises(WorkflowReview) as failure:
            document_content.collect_document_content(ui, order, title="New Order", retained_number="PO000001")
        assert failure.value.code == "document_content_mismatch"
        retained = json.loads(Path(failure.value.document_content_evidence).read_text())
        assert retained["passed"] is False and retained["expected"] != retained["observed"]
    assert ui.actions == []
