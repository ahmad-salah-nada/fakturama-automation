from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError
from tjm import document_sequence
from tjm.document_sequence import _number_row, normalize_saved_document, parse_document_date
from tjm.workflow_rules import WorkflowReview


def saved_row(**changes):
    values = {"Document": "PO000001", "Date": "Jul 14, 2026",
              "Cust.Ref.": "WEB-2026-0714-A17", "State": "open", "Total": "€678.30"}
    values.update(changes)
    return values


def test_order_saved_fields_are_normalized_from_the_actual_row():
    values = saved_row()
    assert normalize_saved_document(values, check="order_persistence") == {
        "number": "PO000001", "date": "2026-07-14",
        "external_reference": "WEB-2026-0714-A17", "state": "Open", "total": "678.30",
    }
    assert values["State"] == "open"
    assert values["Total"] == "€678.30"


def test_post_invoice_order_open_check_uses_only_its_required_saved_fields():
    values = saved_row(Date="Unneeded raw date")
    assert normalize_saved_document(values, check="order_open") == {
        "number": "PO000001", "external_reference": "WEB-2026-0714-A17",
        "state": "Open", "total": "678.30",
    }


@pytest.mark.parametrize("state,paid", [("paid", True), ("unpaid", False)])
def test_invoice_paid_flag_comes_only_from_the_known_actual_state(state, paid):
    values = saved_row(Document="Unrecognized-prefix-37", State=state, Total="EUR 678.30")
    assert normalize_saved_document(values, check="invoice_persistence") == {
        "number": "Unrecognized-prefix-37", "external_reference": "WEB-2026-0714-A17",
        "paid": paid, "total": "678.30",
    }


@pytest.mark.parametrize("check,state", [
    ("order_persistence", "paid"), ("order_open", "in work"),
    ("order_open", "Open"), ("invoice_persistence", "open"),
    ("invoice_persistence", "in work"), ("invoice_persistence", "Paid"),
])
def test_unestablished_state_is_never_guessed_as_open_or_paid(check, state):
    with pytest.raises(DesktopError, match="state"):
        normalize_saved_document(saved_row(State=state), check=check)


@pytest.mark.parametrize("field,value", [("Document", ""), ("Cust.Ref.", "  "),
                                         ("State", None), ("Total", "")])
def test_missing_required_actual_cell_stops(field, value):
    with pytest.raises(DesktopError, match="readable nonblank"):
        normalize_saved_document(saved_row(**{field: value}), check="order_persistence")


@pytest.mark.parametrize("total", ["678.30", "USD 678.30", "€678,30", "€1,678.30", "€-1.00"])
def test_unknown_currency_or_separators_are_not_silently_normalized(total):
    with pytest.raises(DesktopError, match="EUR money"):
        normalize_saved_document(saved_row(Total=total), check="order_persistence")


@pytest.mark.parametrize("text,expected", [("Jul 14, 2026", "2026-07-14"),
                                          ("Feb 29, 2024", "2024-02-29"),
                                          ("Jan 1, 2027", "2027-01-01")])
def test_observed_english_medium_date_has_unambiguous_calendar_value(text, expected):
    assert parse_document_date(text) == expected


@pytest.mark.parametrize("text", ["14/07/2026", "07/14/2026", "14 Jul 2026",
                                  "July 14, 2026", "Jul 014, 2026", "2026-07-14",
                                  "Jul 32, 2026", "Feb 29, 2025", "Foo 14, 2026"])
def test_unestablished_or_invalid_date_stops(text):
    with pytest.raises(DesktopError, match="Date"):
        parse_document_date(text)


def test_iso_dates_require_the_caller_to_choose_its_verified_display_format():
    assert parse_document_date("2026-07-14", date_format="iso") == "2026-07-14"
    with pytest.raises(DesktopError, match="ISO"):
        parse_document_date("2026-7-14", date_format="iso")
    with pytest.raises(DesktopError, match="Invalid"):
        parse_document_date("2026-02-29", date_format="iso")


def test_retained_document_number_requires_exact_unique_actual_row():
    partial = {"values": saved_row(Document="PO0000010")}
    exact = {"values": saved_row()}
    table = SimpleNamespace(rows=[partial, exact])
    assert _number_row(table, "PO000001") is exact
    with pytest.raises(WorkflowReview) as missing:
        _number_row(table, "PO00001")
    assert missing.value.evidence["exact_count"] == 0
    table.rows.append({"values": saved_row()})
    with pytest.raises(WorkflowReview) as duplicate:
        _number_row(table, "PO000001")
    assert duplicate.value.evidence["exact_count"] == 2


@pytest.mark.parametrize("category", ["all", "paid", "not shipped", "This transaction", None])
def test_unsupported_type_scope_stops_before_any_ui_navigation(category):
    with pytest.raises(ValueError, match="Orders or Invoices"):
        document_sequence.documents_rows(
            object(), "PO000001", current_title="PO000001", category=category,
        )


@pytest.mark.parametrize("category", ["Orders", "Invoices"])
def test_type_parent_is_verified_before_search_and_complete_table_read(monkeypatch, category):
    actions = []
    ui = SimpleNamespace(
        click_label=lambda label, **kwargs: actions.append(("click", label, kwargs)),
        wait_tab=lambda title: actions.append(("tab", title)),
        set_text=lambda label, value, **kwargs: actions.append(("search", label, value, kwargs)),
    )
    evidence = {"category": category, "passed": True}
    result = SimpleNamespace(rows=[], evidence={"documents_category": evidence})
    monkeypatch.setattr(document_sequence, "_left_scope", lambda *_: "left navigation")

    def select_category(actual_ui, label):
        assert actual_ui is ui
        actions.append(("category", label))
        return evidence

    def read(actual_ui, query, column_values, category_evidence):
        assert actual_ui is ui
        assert category_evidence is evidence
        actions.append(("read", query, column_values))
        return result

    monkeypatch.setattr(document_sequence, "_category", select_category)
    monkeypatch.setattr(document_sequence, "_read_documents", read)
    assert document_sequence.documents_rows(
        ui, "retained-number", current_title="saved editor", category=category,
    ) is result
    assert actions == [
        ("click", "Documents", {"scope": "left navigation"}),
        ("tab", "Documents"), ("category", category),
        ("search", "Search:", "retained-number", {"scope": "Documents"}),
        ("read", "retained-number", None),
    ]


@pytest.mark.parametrize("check,category,state", [
    ("order_persistence", "Orders", "open"),
    ("order_open", "Orders", "open"),
    ("invoice_persistence", "Invoices", "paid"),
])
def test_saved_document_check_queries_the_type_parent_without_using_number_prefix(
        monkeypatch, check, category, state):
    number = "opaque-generated-number"
    values = saved_row(Document=number, State=state)
    table = SimpleNamespace(rows=[{"values": values}], evidence={"scope": category})
    expected = normalize_saved_document(values, check=check)
    requests = []

    def read(_ui, query, **kwargs):
        requests.append((query, kwargs["category"]))
        return table

    monkeypatch.setattr(document_sequence, "documents_rows", read)
    observed, evidence = document_sequence.verify_saved_document(
        object(), number, check=check, expected=expected, current_title="saved editor",
    )
    assert requests == [(number, category)]
    assert observed == expected
    assert evidence["table_evidence"] is table.evidence


def test_missing_type_parent_cannot_establish_cross_type_absence(monkeypatch):
    def missing(*args, **kwargs):
        raise DesktopError('Native Orders parent absent')
    monkeypatch.setattr(document_sequence, 'open_documents', missing)
    with pytest.raises(WorkflowReview) as error:
        document_sequence.require_no_documents(
            object(), "WEB-2026-0714-A17", current_title="New Order",
        )
    assert error.value.code == "document_absence_scope_unsupported"
    assert error.value.evidence['category'] == 'Orders'


def test_transaction_view_starts_from_exact_invoice_and_clears_search_before_category(monkeypatch):
    order_number, invoice_number = "opaque-order-number", "opaque-invoice-number"
    partial = {"values": saved_row(Document=invoice_number + "0", State="paid")}
    invoice_row = {"values": saved_row(Document=invoice_number, State="paid")}
    order_row = {"values": saved_row(Document=order_number)}
    invoice_table = SimpleNamespace(rows=[partial, invoice_row], evidence={"view": "Invoices"})
    transaction_table = SimpleNamespace(
        rows=[invoice_row, order_row], evidence={"view": "This transaction"},
    )
    actions = []
    ui = SimpleNamespace(
        set_text=lambda label, value, **kwargs: actions.append(("search", label, value, kwargs)),
    )

    def invoices(actual_ui, query, **kwargs):
        assert actual_ui is ui
        actions.append(("read-invoice", query, kwargs["category"]))
        return invoice_table

    def select(actual_ui, actual_table, row):
        assert actual_ui is ui and actual_table is invoice_table and row is invoice_row
        actions.append(("select-invoice", row["values"]["Document"]))

    def category(actual_ui, label):
        assert actual_ui is ui
        actions.append(("category", label))
        return {"category": label, "passed": True}

    def transaction(actual_ui, query, column_values, category_evidence):
        assert actual_ui is ui
        assert category_evidence == {"category": "This transaction", "passed": True}
        actions.append(("read-transaction", query))
        return transaction_table

    monkeypatch.setattr(document_sequence, "documents_rows", invoices)
    monkeypatch.setattr(document_sequence, "rowselect", select)
    monkeypatch.setattr(document_sequence, "_category", category)
    monkeypatch.setattr(document_sequence, "_read_documents", transaction)
    result = document_sequence.read_transaction_documents(
        ui, invoice_number=invoice_number, order_number=order_number, current_title="saved Invoice",
    )
    assert actions == [
        ("read-invoice", invoice_number, "Invoices"),
        ("select-invoice", invoice_number),
        ("search", "Search:", "", {"scope": "Documents"}),
        ("category", "This transaction"), ("read-transaction", ""),
    ]
    assert result["order_number"] == order_number
    assert result["invoice_number"] == invoice_number
    assert result["passed"] is True


@pytest.mark.parametrize("invoice_rows", [[], ["INV", "INV"]])
def test_transaction_never_selects_a_category_without_one_exact_invoice(monkeypatch, invoice_rows):
    table = SimpleNamespace(
        rows=[{"values": saved_row(Document=number)} for number in invoice_rows], evidence={},
    )
    monkeypatch.setattr(document_sequence, "documents_rows", lambda *_args, **_kwargs: table)
    with pytest.raises(WorkflowReview) as error:
        document_sequence.read_transaction_documents(
            object(), invoice_number="INV", order_number="ORD", current_title="saved Invoice",
        )
    assert error.value.code == "saved_document_not_unique"
    assert error.value.evidence["exact_count"] == len(invoice_rows)


@pytest.fixture
def reference_navigation(monkeypatch, tmp_path):
    reference = "WEB-2026-0714-A17"
    row = {"values": {"Cust.Ref.": reference}}
    table = SimpleNamespace(rows=[row], evidence={"complete_native_view": True})
    state = SimpleNamespace(
        reference=reference, row=row, table=table, reads=[table], actions=[],
        header={"number": "PO000001", "reference": reference, "date": "Jul 14, 2026"},
    )
    ui = SimpleNamespace(
        evidence_dir=tmp_path,
        click_label=lambda label, **_kwargs: state.actions.append(("click", label)),
        wait_tab=lambda title: state.actions.append(("wait", title)),
        set_text=lambda label, value, **kwargs: state.actions.append(("search", label, value, kwargs)),
    )
    state.ui = ui
    monkeypatch.setattr(document_sequence, "_left_scope", lambda *_args: "left")

    def category(_ui, label):
        state.actions.append(("category", label))
        return {"category": label, "passed": True}

    def read(_ui, headers, **kwargs):
        assert headers == ["Cust.Ref."]
        assert kwargs["scope"] == "Documents"
        assert kwargs["search_field"] == "Search:"
        assert kwargs["search_value"] == reference
        state.actions.append(("read", tuple(headers)))
        result = state.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def select(_ui, actual_table, actual_row, *, clicks):
        assert actual_table is table and actual_row is row
        assert clicks == 2
        state.actions.append(("doubleclick", dict(actual_row["values"])))

    def header(_ui, *, title):
        state.actions.append(("native-header", title))
        return state.header

    def fit(_ui, values, **kwargs):
        assert values == {"Cust.Ref.": reference}
        assert kwargs == {"scope": "Documents", "headers": ["Cust.Ref."]}
        state.actions.append(("fit", tuple(values)))

    monkeypatch.setattr(document_sequence, "_category", category)
    monkeypatch.setattr(document_sequence, "read_complete_table", read)
    monkeypatch.setattr(document_sequence, "rowselect", select)
    monkeypatch.setattr(document_sequence, "read_document_header", header)
    monkeypatch.setattr(document_sequence, "fit_columns", fit)
    return state


def reopen_reference(state, *, category="Orders"):
    return document_sequence.reopen_saved_document_by_reference(
        state.ui, "PO000001", state.reference, category=category,
        current_title="saved editor", saved_title="PO000001",
    )


@pytest.mark.parametrize("category", ["Orders", "Invoices"])
def test_reference_navigation_returns_actual_native_identity_without_populating_row_number(
        reference_navigation, category):
    state = reference_navigation
    header, evidence = reopen_reference(state, category=category)
    assert header is state.header
    assert evidence["passed"] is True
    assert evidence["observed_header"] == state.header
    assert evidence["raw_row"] == {"Cust.Ref.": state.reference}
    assert state.row["values"] == {"Cust.Ref.": state.reference}
    assert ("category", category) in state.actions
    assert state.actions[-3:] == [
        ("doubleclick", {"Cust.Ref.": state.reference}),
        ("wait", "PO000001"), ("native-header", "PO000001"),
    ]
    assert not any(action[0] == "fit" for action in state.actions)


@pytest.mark.parametrize("references,exact_count", [
    ([], 0), (["WEB-2026-0714-A170"], 0),
    (["WEB-2026-0714-A17", "WEB-2026-0714-A17"], 2),
])
def test_missing_partial_or_duplicate_reference_stops_before_opening(
        reference_navigation, references, exact_count):
    state = reference_navigation
    state.table.rows = [{"values": {"Cust.Ref.": reference}} for reference in references]
    with pytest.raises(WorkflowReview) as error:
        reopen_reference(state)
    assert error.value.code == "saved_document_reference_not_unique"
    assert error.value.evidence["exact_count"] == exact_count
    assert not any(action[0] in {"doubleclick", "native-header"} for action in state.actions)


@pytest.mark.parametrize("clipped", ["WEB-2026...", "WEB-2026…"])
def test_clipped_reference_cannot_be_accepted_as_complete(reference_navigation, clipped):
    state = reference_navigation
    state.row["values"]["Cust.Ref."] = clipped
    with pytest.raises(DesktopError, match="clipped"):
        reopen_reference(state)
    assert not any(action[0] == "doubleclick" for action in state.actions)


def clipped_reference_error():
    error = DesktopError("A table cell is clipped: 'WEB-2026...'.")
    error.table_cell_evidence = {"raw_text": "WEB-2026...", "crop": "original-cell.png"}
    error.table_adapter_evidence_path = "original-read.json"
    return error


def test_one_evidenced_clipped_reference_fit_preserves_failed_read_and_observes_fresh_value(
        reference_navigation):
    state = reference_navigation
    state.reads = [clipped_reference_error(), state.table]
    _, evidence = reopen_reference(state)
    assert evidence["passed"] is True
    assert evidence["initial_clipped_read"]["cell"]["raw_text"] == "WEB-2026..."
    assert evidence["initial_clipped_read"]["report"] == "original-read.json"
    assert [action[0] for action in state.actions].count("fit") == 1
    assert [action[0] for action in state.actions].count("read") == 2


def test_a_second_clipped_read_stops_without_another_fit(reference_navigation):
    state = reference_navigation
    state.reads = [clipped_reference_error(), clipped_reference_error()]
    with pytest.raises(DesktopError, match="clipped"):
        reopen_reference(state)
    assert [action[0] for action in state.actions].count("fit") == 1
    assert not any(action[0] == "doubleclick" for action in state.actions)


def test_unrelated_table_error_never_triggers_column_fit(reference_navigation):
    state = reference_navigation
    state.reads = [DesktopError("Low-confidence table cell: 'WEB-2026-0714-A17'.")]
    with pytest.raises(DesktopError, match="Low-confidence"):
        reopen_reference(state)
    assert not any(action[0] in {"fit", "doubleclick"} for action in state.actions)


@pytest.mark.parametrize("changes", [
    {"number": "PO0000010"}, {"reference": "WEB-2026-0714-A170"}, {"number": None},
])
def test_reopened_native_identity_mismatch_is_not_replaced_with_expected_values(
        reference_navigation, changes):
    state = reference_navigation
    state.header.update(changes)
    original = dict(state.header)
    with pytest.raises(WorkflowReview) as error:
        reopen_reference(state)
    assert error.value.code == "saved_document_identity_mismatch"
    assert error.value.evidence["observed"] == {
        "number": original["number"], "reference": original["reference"],
    }
    assert state.header == original
    assert state.row["values"] == {"Cust.Ref.": state.reference}


def test_reference_reopen_rejects_wrong_type_before_desktop_navigation():
    with pytest.raises(ValueError, match="Orders or Invoices"):
        document_sequence.reopen_saved_document_by_reference(
            object(), "PO000001", "WEB-2026-0714-A17", category="all",
            current_title="saved editor", saved_title="PO000001",
        )
