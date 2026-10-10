"""Reference/state navigation joins actual rows with native numbers, without GUI."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm import document_sequence as module
from tjm.desktop import DesktopError
from tjm.workflow_rules import WorkflowReview


@pytest.fixture
def route(monkeypatch, tmp_path):
    monkeypatch.setattr("tjm.document_states.state_cell_reader", lambda desktop: object())
    monkeypatch.setattr(module, '_dismiss_customer_history', lambda ui: None)
    reference = "WEB-2026-0714-A17"
    invoice_row = {"values": {"Cust.Ref.": reference, "State": "paid", "Total": "EUR678.30"}}
    order_row = {"values": {"Cust.Ref.": reference, "State": "open", "Total": "EUR678.30"}}
    initial = SimpleNamespace(rows=[invoice_row], evidence={"table": "Invoices", "complete_native_view": True})
    transaction = SimpleNamespace(rows=[order_row, invoice_row],
                                  evidence={"table": "This transaction", "complete_native_view": True})
    state = SimpleNamespace(
        reference=reference, invoice_row=invoice_row, order_row=order_row,
        tables=[initial, transaction, transaction], actions=[],
        headers=[{"number": "INV000001", "reference": reference, "date": "Oct 8, 2026"},
                 {"number": "PO000001", "reference": reference, "date": "Jul 14, 2026"},
                 {"number": "INV000001", "reference": reference, "date": "Oct 8, 2026"}])
    state.ui = SimpleNamespace(
        desktop=object(),
        evidence_dir=tmp_path,
        set_text=lambda label, value, **kwargs: state.actions.append(("search", value)),
        wait_tab=lambda title: state.actions.append(("wait", title)),
        snapshot=lambda *args, **kwargs: SimpleNamespace(metadata=lambda: {"native_capture": True}))
    monkeypatch.setattr(module, "open_documents", lambda *args, **kwargs:
                        state.actions.append(("type", kwargs["category"])) or {"category": kwargs["category"], "passed": True})
    monkeypatch.setattr(module, "_category", lambda ui, label:
                        state.actions.append(("category", label)) or {"category": label, "passed": True})

    def table(ui, headers, **kwargs):
        assert headers == ["State", "Total"]
        state.actions.append(("read", kwargs["search_value"]))
        return state.tables.pop(0)

    def selected(ui, actual_table, row, *, clicks):
        assert clicks == 2 and any(row is candidate for candidate in actual_table.rows)
        state.actions.append(("open", row["values"]["State"]))

    def header(ui, *, title, invoice):
        state.actions.append(("native", title, invoice))
        return state.headers.pop(0)

    monkeypatch.setattr(module, "read_complete_table", table)
    monkeypatch.setattr(module, "rowselect", selected)
    monkeypatch.setattr(module, "read_document_header", header)
    monkeypatch.setattr(module, "fit_columns", lambda *args, **kwargs: pytest.fail("No column resizing is authorized."))
    return state


def execute(state):
    return module.read_transaction_documents_by_reference(
        state.ui, invoice_number="INV000001", order_number="PO000001",
        external_reference=state.reference, current_title="INV000001",
        invoice_title="INV000001", order_title="PO000001")


def test_actual_native_numbers_are_joined_to_actual_state_and_total_without_number_ocr(route):
    # Unreadable painted numbers are deliberately irrelevant to this route.
    route.invoice_row["values"]["Document"] = "INVOOO001"
    route.order_row["values"]["Document"] = "POOOOOO1"
    result = execute(route)
    assert result["order_observation"] == {
        "number": "PO000001", "date": "2026-07-14", "external_reference": route.reference,
        "state": "Open", "total": "678.30"}
    assert result["invoice_observation"] == {
        "number": "INV000001", "external_reference": route.reference, "paid": True, "total": "678.30"}
    assert result["order_number"] == "PO000001" and result["invoice_number"] == "INV000001"
    assert result["passed"] is True and result["save_requests"] == 0
    assert route.actions == [
        ("type", "Invoices"), ("search", route.reference), ("read", route.reference),
        ("open", "paid"), ("wait", "INV000001"), ("native", "INV000001", True),
        ("search", ""), ("category", "This transaction"), ("read", ""),
        ("open", "open"), ("wait", "PO000001"), ("native", "PO000001", False),
        ("category", "This transaction"), ("read", ""),
        ("open", "paid"), ("wait", "INV000001"), ("native", "INV000001", True)]
    assert json.loads(Path(result["report"]).read_text())["passed"] is True


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_wrong_actual_native_number_stops_without_replacing_it_with_retained_number(route, stage):
    route.headers[stage]["number"] = "ACTUAL-WRONG-NUMBER"
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "transaction_native_identity_mismatch"
    evidence = json.loads(Path(error.value.reference_state_evidence).read_text())
    assert evidence["passed"] is False
    assert evidence["evidence"]["observed"]["number"] == "ACTUAL-WRONG-NUMBER"
    assert "order_observation" not in evidence


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_wrong_actual_native_reference_stops(route, stage):
    route.headers[stage]["reference"] = "ACTUAL-OTHER-REFERENCE"
    with pytest.raises(WorkflowReview, match="actual native number/reference"):
        execute(route)


@pytest.mark.parametrize("rows", [[], ["paid", "paid"], ["open"], ["unknown"]])
def test_initial_invoice_reference_candidate_must_be_exact_and_unique(route, rows):
    route.tables[0].rows = [{"values": {"Cust.Ref.": route.reference, "State": state, "Total": "EUR678.30"}}
                            for state in rows]
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "invoice_reference_not_unique"
    assert not any(action[0] == "open" for action in route.actions)


@pytest.mark.parametrize("states", [("paid", "paid"), ("open", "open"), ("open", "unknown")])
def test_ambiguous_or_unknown_transaction_states_stop_before_order_candidate_open(route, states):
    route.tables[1].rows = [{"values": {"Cust.Ref.": route.reference, "State": state, "Total": "EUR678.30"}}
                           for state in states]
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "transaction_state_candidates_ambiguous"
    assert [action for action in route.actions if action[0] == "open"] == [("open", "paid")]


def test_other_reference_in_this_transaction_stops(route):
    route.headers[1]["reference"] = "OTHER-REFERENCE"
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "transaction_native_identity_mismatch"


def test_extra_transaction_row_is_not_ignored(route):
    route.tables[1].rows.append({"values": dict(route.order_row["values"])})
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "transaction_reference_scope_mismatch"


def test_changed_transaction_rows_after_order_open_stop_before_final_invoice_click(route):
    changed_invoice = {"values": dict(route.invoice_row["values"]) | {"Total": "EUR679.30"}}
    route.tables[2] = SimpleNamespace(rows=[route.order_row, changed_invoice], evidence={"table": "changed"})
    with pytest.raises(WorkflowReview) as error:
        execute(route)
    assert error.value.code == "transaction_rows_changed"
    assert [action for action in route.actions if action[0] == "open"] == [("open", "paid"), ("open", "open")]


def test_actual_unpaid_state_remains_false_in_observation_instead_of_assuming_source_paid(route):
    route.invoice_row["values"]["State"] = "unpaid"
    assert execute(route)["invoice_observation"]["paid"] is False


def test_complete_table_read_failure_is_retained_and_never_retried(route, monkeypatch):
    calls = []

    def unreadable(*args, **kwargs):
        calls.append(1)
        error = DesktopError("Low-confidence State cell")
        error.table_cell_evidence = {"raw_text": "uncertain state"}
        raise error

    monkeypatch.setattr(module, "read_complete_table", unreadable)
    with pytest.raises(DesktopError) as error:
        execute(route)
    evidence = json.loads(Path(error.value.reference_state_evidence).read_text())
    assert evidence["table_cell_evidence"]["raw_text"] == "uncertain state"
    assert calls == [1] and not any(action[0] == "open" for action in route.actions)
