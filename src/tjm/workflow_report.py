"""Preflight checks and run reports for the desktop workflow.

The UI adapter supplies observations, and this module checks them against the
input and SaveLedger. Reports retain input provenance, document numbers and
comparison results. The desktop adapter runs preflight before entering data.
"""

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .models import RawOrder, ValidatedOrder
from .validation import validate_order
from .workflow_rules import SaveLedger, WorkflowReview


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
STAGES = {"pre_save", "saved_list", "reopened"}
EMAIL_PROBE_EVIDENCE = {
    "after_save": "demo/email-probe.json",
    "reopened": "demo/email-probe.json",
    "context": "docs/LIMITATIONS.md",
    "probe_email": "ava.stone@example.test",
    "reopened_email": "",
    "customer_id": "CUST000001",
    "scope": "Fakturama 2.2.0; a separate fictitious contact with the same .test domain",
}


def _json_copy(value: Any) -> Any:
    """Reject non-JSON evidence and non-finite numbers; copy mutable values."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise WorkflowReview("invalid_report_data", "Report evidence must be finite JSON data.") from error


def _same(expected: Any, observed: Any) -> bool:
    # Python considers True == 1. JSON comparison keeps those types distinct.
    return json.dumps(expected, sort_keys=True, allow_nan=False) == json.dumps(
        observed, sort_keys=True, allow_nan=False)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowReview("missing_report_metadata", f"Supply nonblank {field}.", field=field)
    return value


def _validated_order(extraction: Mapping) -> ValidatedOrder:
    # JSON-mode strict parsing accepts the dates/decimal strings produced by
    # extraction, while retaining the models' rejection of unknown fields.
    return ValidatedOrder.model_validate_json(json.dumps(extraction["order"], allow_nan=False))


def _two_decimal_places(value: Decimal) -> bool:
    """Accept trailing zeroes without rounding or using the caller's context."""
    if not value:
        return True
    digits = value.as_tuple().digits
    trailing_zeroes = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    return value.as_tuple().exponent + trailing_zeroes >= -2


def _desktop_input_issues(order: ValidatedOrder) -> list[dict]:
    """Refuse source shapes the current narrow desktop runner cannot preserve.

    These are input limits, not proof of a ready window or readable layout.
    Current UI guards must still check the actual application before input.
    """
    issues = []

    def unsupported(field, observed, supported):
        issues.append({"code": "unsupported_desktop_input", "field": field,
                       "message": "The current desktop workflow cannot preserve this input; review it before any UI mutations.",
                       "observed": observed, "supported": supported})

    if len(order.items) != 2:
        unsupported("items", len(order.items), "Exactly two source item rows.")
    for index, item in enumerate(order.items):
        for field in ("unit_net", "quantity", "vat_percent", "discount_percent"):
            value = getattr(item, field)
            if not _two_decimal_places(value):
                unsupported(f"items[{index}].{field}", str(value),
                            "At most two effective decimal places; trailing zeroes are allowed.")
    for kind in ("billing", "delivery"):
        address = getattr(order.customer, kind)
        for field in ("additional_name", "address_specification", "district"):
            value = getattr(address, field)
            if value is not None:
                unsupported(f"customer.{kind}.{field}", value,
                            "No optional address lines; the reader supports four-line addresses.")
    for field in ("shipping_net", "overall_discount_percent"):
        value = getattr(order, field)
        if value != 0:
            unsupported(field, str(value), "Zero; document-level charges are not entered by this runner.")
    if order.shipping_vat_percent is not None:
        unsupported("shipping_vat_percent", str(order.shipping_vat_percent),
                    "Absent; Shipping VAT has no demonstrated reader.")
    return issues


def preflight(extraction_report: Mapping, *, fakturama_version: str = "2.2.0") -> dict:
    """Check source integrity, known email incompatibility and desktop input limits.

    ``ready`` permits the caller to begin UI checks, not to assume that the
    application's other fields or email domains will persist. A .test address
    is preserved exactly and produces review_required on 2.2.0. No value is
    substituted, and no general domain-validity rule is inferred from this probe.
    """
    result = {"status": "review_required", "checks": [], "issues": [],
              "desktop_mutations_performed": 0}
    if not isinstance(extraction_report, Mapping):
        result["issues"].append({"code": "invalid_extraction_report",
                                 "message": "Supply the extraction report before UI input."})
        return result
    status = extraction_report.get("status")
    if status != "validated":
        result["status"] = "failed" if status == "failed" else "review_required"
        result["issues"] = _json_copy(extraction_report.get("issues") or [])
        result["issues"].append({"code": "extraction_not_validated",
                                 "message": "Extraction must validate before UI input.",
                                 "observed": status})
        return result
    extraction_checks = extraction_report.get("checks")
    if (extraction_report.get("issues") or not isinstance(extraction_checks, list)
            or not extraction_checks
            or any(not isinstance(check, Mapping) or check.get("passed") is not True
                   for check in extraction_checks)):
        result["issues"].append({"code": "conflicting_extraction_report",
                                 "message": "Validated extraction requires explicit passing checks and no issues."})
        return result
    try:
        order = _validated_order(extraction_report)
    except (KeyError, TypeError, ValueError, ValidationError):
        result["issues"].append({"code": "invalid_validated_order",
                                 "message": "The reported order does not satisfy the validated-order schema."})
        return result
    evidence = extraction_report.get("evidence")
    try:
        raw = RawOrder.model_validate(extraction_report["raw"])
        policy = evidence.get("name_order") if isinstance(evidence, Mapping) else None
        rechecked = validate_order(raw, name_order=policy)
    except (KeyError, TypeError, ValueError, ValidationError):
        result["issues"].append({"code": "invalid_extraction_source",
                                 "message": "The raw transcription must satisfy the source schema."})
        return result
    if (rechecked.status != "validated" or not _same(order.model_dump(mode="json"),
                                                     rechecked.order.model_dump(mode="json"))):
        result["issues"].append({"code": "conflicting_extraction_order",
                                 "message": "The validated Order must match fresh validation of the unchanged raw transcription and selected name policy.",
                                 "validation_issues": rechecked.issues})
        return result
    source = evidence.get("input") if isinstance(evidence, Mapping) else None
    if (not isinstance(source, Mapping) or not isinstance(source.get("sha256"), str)
            or not SHA256.fullmatch(source["sha256"])
            or not isinstance(source.get("path"), str) or not source["path"].strip()):
        result["status"] = "failed"
        result["issues"].append({"code": "missing_input_provenance",
                                 "message": "The extraction report must identify the input path and SHA-256."})
        return result
    result["checks"].append({"name": "validated_extraction", "passed": True,
                              "evidence": {"input_sha256": source["sha256"],
                                           "extraction_report": evidence.get("report")}})
    domain = order.customer.email.rsplit("@", 1)[-1].casefold()
    if fakturama_version == "2.2.0" and domain.endswith(".test"):
        result["issues"].append({
            "code": "fakturama_test_email_incompatibility", "field": "customer.email",
            "message": "Fakturama 2.2.0 did not preserve a .test email in the saved-and-reopened probe. Review the original source before UI mutations; do not substitute its email.",
            "source_value": order.customer.email,
            "evidence": deepcopy(EMAIL_PROBE_EVIDENCE),
        })
        return result
    unsupported = _desktop_input_issues(order)
    if unsupported:
        result["issues"].extend(unsupported)
        return result
    result["checks"].append({
        "name": "desktop_input_capability", "passed": True,
        "observed": "Two rows, supported decimal precision, four-line addresses and no document-level charges.",
        "limitation": "Input compatibility does not establish UI readiness, table geometry or persistence.",
    })
    result["status"] = "ready"
    result["checks"].append({
        "name": "known_email_compatibility_stop", "passed": True,
        "observed": "The documented 2.2.0 .test stop does not apply to this input/version.",
        "limitation": "Email acceptance and persistence still require native UI readback.",
    })
    return result


def new_run_report(extraction_report: Mapping, *, workspace: str, input_kind: str,
                   fakturama_version: str = "2.2.0", demo_label: str | None = None,
                   original_input_sha256: str | None = None) -> dict:
    """Build a JSON-safe run report before any desktop mutations.

    ``input_kind`` identifies the original image or a demonstration fixture.
    A demonstration records its label and the original image's SHA-256.
    The caller supplies the file hashes.
    """
    _text(workspace, "workspace")
    _text(fakturama_version, "fakturama_version")
    if input_kind not in {"original", "demonstration"}:
        raise WorkflowReview("invalid_input_kind", "Choose original or demonstration explicitly.")
    if input_kind == "demonstration":
        _text(demo_label, "demo_label")
        if not isinstance(original_input_sha256, str) or not SHA256.fullmatch(original_input_sha256):
            raise WorkflowReview("missing_original_provenance", "A demonstration must identify the original input SHA-256.")
    elif demo_label is not None or original_input_sha256 is not None:
        raise WorkflowReview("conflicting_provenance", "Original input must not carry demonstration metadata.")
    checked = preflight(extraction_report, fakturama_version=fakturama_version)
    extraction = _json_copy(dict(extraction_report))
    extraction_evidence = extraction.get("evidence")
    if not isinstance(extraction_evidence, Mapping):
        extraction_evidence = {}
    source = extraction_evidence.get("input")
    if not isinstance(source, Mapping):
        source = {}
    if input_kind == "demonstration" and source.get("sha256") == original_input_sha256:
        raise WorkflowReview("conflicting_provenance", "The demonstration and original must have distinct input hashes.")
    return {
        "status": "running" if checked["status"] == "ready" else checked["status"],
        "workspace": workspace,
        "fakturama_version": fakturama_version,
        "input": source,
        "provenance": {"input_kind": input_kind, "demo_label": demo_label,
                       "original_input_sha256": original_input_sha256},
        "order": extraction.get("order"),
        "extraction_report": extraction_evidence.get("report"),
        "preflight": checked,
        "generated_numbers": {"order": None, "invoice": None},
        "checks": [], "issues": deepcopy(checked["issues"]),
        "save_ledger": [], "save_counts": {"save_intents": 0, "with_persistence_evidence": 0},
        "completion": None,
    }


def _running(report: dict) -> None:
    if report.get("status") != "running":
        raise WorkflowReview("run_not_running", "A stopped or verified report cannot accept further workflow observations.",
                             status=report.get("status"))


def set_generated_number(report: dict, kind: str, number: str) -> None:
    """Retain Fakturama's proposed document number, never replace it later."""
    _running(report)
    if kind not in {"order", "invoice"}:
        raise WorkflowReview("invalid_document_kind", "Generated numbers identify Order or Invoice.")
    _text(number, f"generated {kind} number")
    if report["generated_numbers"][kind] is not None:
        raise WorkflowReview("repeated_generated_number", "The proposed document number is already recorded.", kind=kind)
    report["generated_numbers"][kind] = number


def record_check(report: dict, name: str, *, expected: Any, observed: Any,
                 passed: bool, evidence: Mapping, stage: str = "pre_save") -> None:
    """Record one exact comparison with evidence; booleans must be real bools.

    A failed/uncertain observation remains in the history and prevents completion;
    callers cannot overwrite it with a later successful check of the same name.
    Amounts should use the validated decimal strings, not binary floating point.
    """
    _running(report)
    _text(name, "check name")
    if type(passed) is not bool:
        raise WorkflowReview("invalid_check_result", "passed must be the boolean True or False.")
    if stage not in STAGES:
        raise WorkflowReview("invalid_check_stage", "Choose pre_save, saved_list or reopened.")
    if not isinstance(evidence, Mapping) or not evidence:
        raise WorkflowReview("missing_check_evidence", "Each check needs nonempty UI observation evidence.")
    if any(check["name"] == name for check in report["checks"]):
        raise WorkflowReview("repeated_check", "A check already exists; retain the initial observation.", name=name)
    entry = _json_copy({"name": name, "expected": expected, "observed": observed,
                        "passed": passed, "stage": stage, "evidence": dict(evidence)})
    if passed and not _same(entry["expected"], entry["observed"]):
        raise WorkflowReview("contradictory_check", "A passing check must have exact matching expected/observed JSON values.",
                             name=name)
    report["checks"].append(entry)


def sync_ledger(report: dict, ledger: SaveLedger) -> None:
    """Copy Save intentions/evidence, including the last uncertain Save on a stop.

    Updating evidence does not authorize more UI input. Verified reports remain
    immutable through this API; failed/review_required reports can retain the
    ledger when an exception is reported before this final evidence sync.
    """
    if report.get("status") not in {"running", "review_required", "failed"}:
        raise WorkflowReview("run_not_running", "A verified or unknown report cannot change its ledger.")
    report["save_ledger"] = ledger.as_json()
    report["save_counts"] = ledger.counts()


def stop_run(report: dict, *, status: str, code: str, message: str,
             evidence: Mapping | None = None) -> None:
    """Stop after ambiguity or failure without claiming completion or retrying Save."""
    _running(report)
    if status not in {"review_required", "failed"}:
        raise WorkflowReview("invalid_stop_status", "A stop requires review_required or failed.")
    issue = {"code": _text(code, "issue code"), "message": _text(message, "issue message")}
    if evidence is not None:
        issue["evidence"] = _json_copy(dict(evidence))
    report["issues"].append(issue)
    report["status"] = status


def _plain_decimal(value: str) -> str:
    number = Decimal(value)
    if not number:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cent_decimal(value: str) -> str:
    number = Decimal(value)
    if not number:
        return "0.00"
    if not _two_decimal_places(number):
        raise WorkflowReview("unsupported_desktop_input",
                             "Expected money must be exact cents; do not round a source value for comparison.",
                             observed=value)
    return f"{number:.2f}"


def content_expectations(report: Mapping) -> dict:
    """Project the full source content required in each reopened document.

    Shapes are returned for recorded document numbers only, allowing the saved
    Order to be reopened and checked before creating its follow-up Invoice.
    Numeric comparisons use the same display representation as the actual
    reader: quantities/percentages are plain decimals and item money/totals
    have two decimal places. Raw/source model spellings remain unchanged.
    Line order is preserved for repeated SKUs. Optional source address fields
    are checked only when supplied. Source item units stay in extraction
    evidence; the user approved checking the prescribed GUI line fields only.
    Invoice ``order_date`` means
    the copied Order date; its newly proposed Invoice/service dates require
    separate caller checks. Prices and VAT modes are the prescribed modes.
    """
    order = report["order"]
    customer = order["customer"]
    content = {
        "order_date": order["order_date"],
        "external_reference": order["external_reference"],
        "billing": {key: value for key, value in customer["billing"].items() if value is not None},
        "delivery": {key: value for key, value in customer["delivery"].items() if value is not None},
        "items": [{"sku": item["sku"], "description": item["description"],
                   "quantity": _plain_decimal(item["quantity"]),
                   "unit_net": _cent_decimal(item["unit_net"]),
                   "vat_percent": _plain_decimal(item["vat_percent"]),
                   "discount_percent": _plain_decimal(item["discount_percent"]),
                   "line_net": _cent_decimal(item["line_net"])}
                  for item in order["items"]],
        "totals": {key: _cent_decimal(value) for key, value in order["totals"].items()},
        "currency": order["currency"],
        "overall_discount_percent": _plain_decimal(order["overall_discount_percent"]),
        "shipping_net": _plain_decimal(order["shipping_net"]),
        "price_mode": "Net", "vat_mode": "With VAT",
    }
    if order.get("shipping_vat_percent") is not None:
        content["shipping_vat_percent"] = _plain_decimal(order["shipping_vat_percent"])
    return {
        f"{kind}_content": {"number": _text(report["generated_numbers"][kind],
                                            f"generated {kind} number"), **deepcopy(content)}
        for kind in ("order", "invoice")
        if report["generated_numbers"].get(kind) is not None
    }


def completion_expectations(report: Mapping) -> dict:
    """Return the seven required content and saved-record comparisons.

    UI adapters normalize only known display formats to these source values.
    Dates use source ISO strings, totals use decimal strings, and the Invoice's
    visible saved state is normalized to the source paid boolean. The adapter
    must establish that state mapping from the actual UI, not assume a label.
    ``order_open`` is checked after the Invoice save, not in the earlier draft.
    """
    order_no = _text(report["generated_numbers"]["order"], "generated Order number")
    invoice_no = _text(report["generated_numbers"]["invoice"], "generated Invoice number")
    order = report["order"]
    payment = order["payment"]
    reference = order["external_reference"]
    total = _cent_decimal(order["totals"]["gross"])
    payment_amount = _cent_decimal(payment["amount"]) if payment["amount"] is not None else None
    return {
        "order_persistence": {"number": order_no, "date": order["order_date"],
                              "external_reference": reference, "state": "Open", "total": total},
        "invoice_persistence": {"number": invoice_no, "external_reference": reference,
                                "paid": payment["paid"], "total": total},
        "order_open": {"number": order_no, "state": "Open",
                       "external_reference": reference, "total": total},
        "invoice_payment": {"number": invoice_no, "method": payment["method"],
                            "paid": payment["paid"], "date": payment["date"],
                            "amount": payment_amount},
        "invoice_order_linkage": {"invoice_number": invoice_no, "order_number": order_no,
                                  "order_date": order["order_date"],
                                  "external_reference": order["external_reference"]},
        **content_expectations(report),
    }


def _linkage_evidence(check: Mapping, expected: Mapping) -> bool:
    """Require the follow-up action and a persisted transaction view.

    Neither matching Cust.Ref. nor copied Order date establishes a relationship.
    The caller supplies actual UI evidence for the saved Order's Invoice action,
    the resulting proposed Invoice number, and both saved documents in This
    transaction. This is the source-backed UI route selected for this version;
    another route needs live proof before extending this policy. This pure gate
    checks the supplied evidence shape, not the authenticity of screenshots.
    """
    evidence = check.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    identities = {key: expected[key] for key in ("order_number", "invoice_number")}
    for key, details in (
        ("followup_action", {**identities, "area": "Create a follow-up document", "action": "Invoice"}),
        ("persisted_linkage", {**identities, "method": "This transaction"}),
    ):
        observation = evidence.get(key)
        if (not isinstance(observation, Mapping) or observation.get("passed") is not True
                or not _same({name: observation.get(name) for name in details}, details)
                or not isinstance(observation.get("ui_evidence"), Mapping)
                or not observation["ui_evidence"]):
            return False
    return True


def complete_run(report: dict, ledger: SaveLedger) -> dict:
    """Complete the report when comparisons and persistence checks pass.

    Both document numbers require matching one-Save ledger entries. Every created
    master also requires successful retrieval, and all recorded checks must pass.

    PDF §§4.1-4.5 and 5.1/5.4-5.6 allow content checked before Save, followed by
    the Documents row; reopened content is also accepted. Invoice payment needs
    reopening because the list omits those fields. The Order's follow-up action
    and transaction view establish the Order-Invoice relationship.
    """
    if report.get("status") == "verified":
        return report
    sync_ledger(report, ledger)
    if report.get("status") != "running":
        return report
    problems = []
    try:
        expected = completion_expectations(report)
    except (KeyError, TypeError, WorkflowReview):
        expected = {}
        problems.append({"code": "missing_generated_numbers",
                         "message": "Both proposed document numbers are needed for completion."})
    checks = {check["name"]: check for check in report["checks"]}
    for name, wanted in expected.items():
        check = checks.get(name)
        if name in {"order_content", "invoice_content"}:
            stages = {"pre_save", "reopened"}
        elif name == "invoice_payment":
            stages = {"reopened"}
        else:
            stages = {"saved_list", "reopened"}
        if (check is None or check.get("passed") is not True
                or check.get("stage") not in stages
                or not isinstance(check.get("evidence"), Mapping) or not check["evidence"]
                or not _same(check.get("expected"), wanted)
                or not _same(check.get("observed"), wanted)):
            problems.append({"code": "missing_completion_check", "check": name,
                             "message": "An exact passing comparison at an allowed proof stage is required.",
                             "expected": wanted, "allowed_stages": sorted(stages)})
        elif name == "invoice_order_linkage" and not _linkage_evidence(check, wanted):
            problems.append({"code": "missing_linkage_evidence", "check": name,
                             "message": "Supply passing UI evidence for the saved Order's follow-up Invoice action and both saved document numbers in This transaction."})
    if any(check.get("passed") is not True for check in report["checks"]):
        problems.append({"code": "failed_workflow_check", "message": "A recorded workflow check did not pass."})
    for kind, key in (("Order", "order"), ("Invoice", "invoice")):
        number = report["generated_numbers"].get(key)
        entries = [entry for entry in report["save_ledger"] if entry["kind"] == kind]
        if (len(entries) != 1 or entries[0]["identity"] != number
                or entries[0].get("save_intent") is not True
                or not isinstance(entries[0].get("persistence_evidence"), Mapping)
                or entries[0]["persistence_evidence"].get("passed") is not True):
            problems.append({"code": "missing_document_persistence", "kind": kind,
                             "message": "The generated document needs one matching Save intention and passing retrieval evidence."})
    for entry in report["save_ledger"]:
        evidence = entry.get("persistence_evidence")
        if not isinstance(evidence, Mapping) or evidence.get("passed") is not True:
            problems.append({"code": "unverified_save", "kind": entry["kind"],
                             "identity": entry["identity"],
                             "message": "A Save intention has no passing persistence evidence; do not retry blindly."})
    if report["issues"]:
        problems.append({"code": "unresolved_workflow_issue", "message": "Run issues remain unresolved."})
    if problems:
        report["issues"].extend(problems)
        report["status"] = "review_required"
        return report
    report["status"] = "verified"
    report["completion"] = {"required_checks": list(expected)}
    return report


def write_run_report(report: Mapping, path: str | Path) -> None:
    """Atomically replace one report file; never write desktop/application data."""
    target = Path(path)
    data = json.dumps(dict(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
