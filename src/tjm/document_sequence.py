"""Read saved Documents and their transaction through the visible application.

These helpers do not save, create, or infer document types from number prefixes.
The caller owns SaveLedger and the separate evidence of the follow-up action.
The first live use must establish the supported tree and display formats.
"""

from dataclasses import replace
from datetime import date
import json
import re
from uuid import uuid4

import numpy as np

from .contact_sequence import _left_scope, _write_evidence
from .desktop import DesktopError, wait_for
from .document_workflow import parse_display_money, read_document_header
from .fields import find_phrases
from .ui import Ui
from .ui_tables import fit_columns, read_complete_table, rowselect
from .workflow_rules import WorkflowReview


DOCUMENT_HEADERS = ["Document", "Date", "Cust.Ref.", "State", "Total"]
_MONTHS = {name: month for month, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_CHECKS = {"order_persistence", "invoice_persistence", "order_open"}
_TYPE_CATEGORIES = {"Orders", "Invoices"}


def parse_document_date(text, *, date_format="english_medium"):
    """Normalize only the display format explicitly established by the caller.

    English medium matches the observed ``Jul 14, 2026``. ISO is opt-in for a
    separately verified display setting; numeric locale guesses are rejected.
    """
    if not isinstance(text, str):
        raise DesktopError("Saved document Date must be readable text.")
    text = text.strip()
    if date_format == "english_medium":
        match = re.fullmatch(r"([A-Z][a-z]{2}) ([1-9]|[12][0-9]|3[01]), ([0-9]{4})", text)
        if match is None or match[1] not in _MONTHS:
            raise DesktopError(f"Unrecognized English medium document Date: {text!r}.")
        values = int(match[3]), _MONTHS[match[1]], int(match[2])
    elif date_format == "iso":
        match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", text)
        if match is None:
            raise DesktopError(f"Unrecognized ISO document Date: {text!r}.")
        values = tuple(int(value) for value in match.groups())
    else:
        raise ValueError("Choose a previously verified english_medium or iso date display.")
    try:
        return date(*values).isoformat()
    except ValueError as error:
        raise DesktopError(f"Invalid saved document Date: {text!r}.") from error


def _cell(values, header):
    value = values.get(header)
    if not isinstance(value, str) or not value.strip():
        raise DesktopError(f"Saved document {header!r} must be readable nonblank text.")
    return value


def normalize_saved_document(values, *, check, currency="EUR", date_format="english_medium"):
    """Build the report observation entirely from actual requested row cells.

    Lowercase ``open``, ``paid`` and ``unpaid`` are the supported Fakturama
    labels. Unknown states stop instead of being treated as open or unpaid.
    The raw row and type icon remain in the caller's table evidence.
    """
    if check not in _CHECKS:
        raise ValueError("Unsupported saved-document report check.")
    if not isinstance(values, dict):
        raise DesktopError("Saved document observation requires actual table cell values.")
    result = {"number": _cell(values, "Document"),
              "external_reference": _cell(values, "Cust.Ref."),
              "total": f"{parse_display_money(_cell(values, 'Total'), currency=currency):.2f}"}
    state = _cell(values, "State")
    if check == "invoice_persistence":
        if state not in {"paid", "unpaid"}:
            raise DesktopError(f"Unrecognized saved Invoice state: {state!r}.")
        result["paid"] = state == "paid"
    else:
        if state != "open":
            raise DesktopError(f"Saved Order is not in the supported open state: {state!r}.")
        result["state"] = "Open"
        if check == "order_persistence":
            result["date"] = parse_document_date(_cell(values, "Date"), date_format=date_format)
    return result


def _selected_row_boxes(image, scope):
    """Locate blue selection pixels; native role/selection checks give them meaning."""
    left, top, right, bottom = map(round, scope)
    pixels = np.asarray(image.crop((left, top, right, bottom)).convert("RGB")).astype(int)
    blue = ((pixels[:, :, 2] > pixels[:, :, 0] + 60)
            & (pixels[:, :, 2] > pixels[:, :, 1] + 40) & (pixels[:, :, 2] > 150))
    ys = np.flatnonzero(blue.sum(axis=1) >= 50)
    boxes = []
    for rows in np.split(ys, np.flatnonzero(np.diff(ys) > 1) + 1):
        if len(rows) < 10:
            continue
        xs = np.flatnonzero(blue[rows[0]:rows[-1] + 1].any(axis=0))
        for columns in np.split(xs, np.flatnonzero(np.diff(xs) > 1) + 1):
            if len(columns) >= 50:
                boxes.append((left + int(columns[0]), top + int(rows[0]),
                              left + int(columns[-1]) + 1, top + int(rows[-1]) + 1))
    return boxes


def _tree_scope(ui, label):
    """Resolve one bounded tree from a category caption or selected native row."""
    capture = ui.snapshot("documents-category-scope")
    scope = ui.scope(capture, "Documents")
    d = ui.desktop
    tokens = [t for t in capture.tokens() if scope[0] <= t.cx < scope[2]
              and scope[1] <= t.cy < scope[3]]
    candidates = [(box, "caption") for box in find_phrases(tokens, label)]
    selected_boxes = _selected_row_boxes(capture.image, scope)
    candidates += [(box, "selection pixels") for box in selected_boxes]
    ui.discoveries.append({"method": "Documents caption or selected-row pixels",
                           "label": label, "scope": scope, "selection_boxes": selected_boxes})
    matches = []
    for box, method in candidates:
        control = d.hit_test(capture, *capture.screen_point(
            (box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
        if method == "caption" and (control.role != "AXStaticText" or control.value != label):
            continue
        selected_row = False
        for hops in range(5):
            bounds = control.bounds
            region = ((bounds.x - capture.screen_rect.x) * capture.scale_x,
                      (bounds.y - capture.screen_rect.y) * capture.scale_y,
                      (bounds.x + bounds.width - capture.screen_rect.x) * capture.scale_x,
                      (bounds.y + bounds.height - capture.screen_rect.y) * capture.scale_y)
            if not (scope[0] <= region[0] < region[2] <= scope[2]
                    and scope[1] <= region[1] < region[3] <= scope[3]):
                break
            if control.role == "AXRow" and d._read(control.element, "AXSelected") is True:
                _row_caption(ui, capture, control, scope)
                selected_row = True
            if control.role == "AXOutline":
                if method == "selection pixels" and not selected_row:
                    break
                if control.enabled is False:
                    raise DesktopError("Documents category tree is explicitly disabled.")
                if not any(d.cf.CFEqual(control.element, prior[1].element) for prior in matches):
                    matches.append((region, control, hops, method))
                break
            parent = d._read(control.element, "AXParent")
            if parent is None:
                break
            control = d._control(parent)
    if len(matches) != 1:
        raise DesktopError(f"Documents category needs one caption-owned native tree; found {len(matches)}.")
    region, control, hops, method = matches[0]
    ui.actions.append({"action": "documents_tree_scope", "label": label,
                       "role": control.role, "bounds": control.bounds.as_dict(),
                       "parent_hops": hops, "scope": list(region), "discovery": method})
    return region, control


def _row_caption(ui, capture, row, scope):
    """Read the observed AXRow -> AXGroup -> AXStaticText caption shape."""
    ui.require_inside_scope(capture, row, scope)
    if row.role != "AXRow":
        raise DesktopError("Documents category needs a native AXRow.")
    d = ui.desktop
    captions = []
    for element in d._read(row.element, "AXChildren", required=True):
        child = d._control(element)
        if not row.bounds.contains_rect(child.bounds):
            raise DesktopError("A category child extends outside its row.")
        if child.role == "AXStaticText":
            captions.append(child)
        elif child.role == "AXGroup":
            for nested_element in d._read(child.element, "AXChildren", required=True):
                nested = d._control(nested_element)
                if not child.bounds.contains_rect(nested.bounds):
                    raise DesktopError("A category caption extends outside its group.")
                if nested.role == "AXStaticText":
                    captions.append(nested)
                elif nested.role != "AXDisclosureTriangle":
                    raise DesktopError("Unsupported category group child.")
        else:
            raise DesktopError("Unsupported category row child.")
    if len(captions) != 1 or not isinstance(captions[0].value, str) or not captions[0].value.strip():
        raise DesktopError("Documents category requires one readable native caption.")
    return captions[0]


def _category(ui, label):
    scope, tree = _tree_scope(ui, label)
    d = ui.desktop
    capture = ui.snapshot("before-native-category-click", ocr=False)
    current = d._control(tree.element)
    if current.role != "AXOutline" or current.bounds != tree.bounds:
        raise DesktopError("Documents category tree changed before its click.")
    ui.require_inside_scope(capture, current, ui.scope(capture, "Documents"))
    candidates = []
    for element in d._read(current.element, "AXChildren", required=True):
        row = d._control(element)
        if row.role == "AXRow":
            caption = _row_caption(ui, capture, row, scope)
            if caption.value == label:
                candidates.append(caption)
    if len(candidates) != 1:
        raise DesktopError("Documents category requires one exact visible native caption.")
    caption = candidates[0]
    d.click(capture, caption.bounds.x + caption.bounds.width / 2,
            caption.bounds.y + caption.bounds.height / 2)

    def selected():
        fresh_capture = ui.snapshot("documents-category-selected", ocr=False)
        fresh = d._control(tree.element)
        ui.require_inside_scope(fresh_capture, fresh, ui.scope(fresh_capture, "Documents"))
        if fresh.role != "AXOutline" or fresh.bounds != tree.bounds:
            raise DesktopError("Documents category tree changed after its observed click.")
        rows = d._read(fresh.element, "AXSelectedRows", required=True)
        if rows is None or len(rows) == 0:
            return None
        if len(rows) != 1:
            raise DesktopError("Documents category requires exactly one native selected row.")
        row = d._control(rows[0])
        actual = _row_caption(ui, fresh_capture, row, scope)
        if actual.value != label:
            return None
        result = {"category": actual.value, "passed": True,
                  "tree_role": fresh.role, "tree_bounds": fresh.bounds.as_dict(),
                  "row_role": row.role, "row_bounds": row.bounds.as_dict(),
                  "raw_value": row.value, "raw_title": row.title,
                  "caption_role": actual.role, "caption_value": actual.value,
                  "caption_bounds": actual.bounds.as_dict(),
                  "capture": fresh_capture.metadata()}
        ui.actions.append({"action": "documents_category_verified", **result})
        return result

    return wait_for(selected, f"native selected Documents category {label}", timeout=2)


def open_documents(ui: Ui, *, current_title, category):
    """Open Documents and verify the requested supported document-type parent."""
    if not isinstance(category, str) or category not in _TYPE_CATEGORIES:
        raise ValueError("Choose the explicit Orders or Invoices document-type parent.")
    ui.click_label("Documents", scope=_left_scope(ui, current_title))
    ui.wait_tab("Documents")
    return _category(ui, category)


def _read_documents(ui, query, column_values, category_evidence):
    if column_values:
        fit_columns(ui, column_values, scope="Documents", headers=DOCUMENT_HEADERS)
    table = read_complete_table(ui, DOCUMENT_HEADERS, scope="Documents",
                                search_field="Search:", search_value=query, timeout=25)
    name = "documents-view-" + uuid4().hex[:12] + ".json"
    evidence = {**table.evidence, "complete_table_report": table.evidence["report"],
                "documents_category": category_evidence,
                "report": str(ui.evidence_dir / name)}
    _write_evidence(ui, name, evidence)
    return replace(table, evidence=evidence)


def documents_rows(ui: Ui, query, *, current_title, category, column_values=None):
    """Return a complete type-parent table with the exact visible query.

    ``column_values`` only sets presentation widths. These expected strings are
    never used to populate the observed cells or infer record presence.
    """
    if not isinstance(query, str):
        raise ValueError("Documents query must be text.")
    category_evidence = open_documents(ui, current_title=current_title, category=category)
    ui.set_text("Search:", query, scope="Documents")
    return _read_documents(ui, query, column_values, category_evidence)


def reopen_saved_document_by_reference(ui: Ui, retained_number, external_reference, *,
                                       category, current_title, saved_title):
    """Open one exact saved reference row and read its native editor identity.

    Only Cust.Ref. is read for navigation. A demonstrated clipped cell permits
    one presentation-only fit of that column and one fresh complete read; all
    other read errors stop. The retained number is never substituted into the
    observed row. The caller must separately close the old editor for a cold
    reopen proof. This helper does not prove saved State, totals, or content.
    """
    for name, value in (("retained_number", retained_number),
                        ("external_reference", external_reference),
                        ("current_title", current_title), ("saved_title", saved_title)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Supply a nonblank {name}.")
    category_evidence = open_documents(ui, current_title=current_title, category=category)
    ui.set_text("Search:", external_reference, scope="Documents")

    def read_reference_table():
        return read_complete_table(ui, ["Cust.Ref."], scope="Documents",
                                   search_field="Search:", search_value=external_reference,
                                   timeout=25)

    initial_clip = None
    try:
        table = read_reference_table()
    except DesktopError as error:
        cell = getattr(error, "table_cell_evidence", {})
        raw = cell.get("raw_text")
        if (not str(error).startswith("A table cell is clipped:")
                or not isinstance(raw, str) or not re.search(r"\.{2,}|…", raw)):
            raise
        initial_clip = {"error": str(error), "cell": cell,
                        "report": getattr(error, "table_adapter_evidence_path", None)}
        fit_columns(ui, {"Cust.Ref.": external_reference},
                    scope="Documents", headers=["Cust.Ref."])
        table = read_reference_table()

    references = [_cell(row["values"], "Cust.Ref.") for row in table.rows]
    if any(re.search(r"\.{2,}|…", reference) for reference in references):
        raise DesktopError("A complete Cust.Ref. observation must not contain clipped text.")
    matches = [row for row, reference in zip(table.rows, references)
               if reference == external_reference]
    if len(matches) != 1:
        raise WorkflowReview(
            "saved_document_reference_not_unique",
            "The complete type-parent table must contain one exact source reference row.",
            external_reference=external_reference, category=category,
            exact_count=len(matches), observed_references=references,
            table_evidence=table.evidence,
        )
    row = matches[0]
    rowselect(ui, table, row, clicks=2)
    ui.wait_tab(saved_title)
    header = read_document_header(ui, title=saved_title)
    expected_identity = {"number": retained_number, "reference": external_reference}
    observed_identity = {"number": header.get("number"), "reference": header.get("reference")}
    evidence = {
        "passed": observed_identity == expected_identity,
        "stage": "saved_reference_row_opened",
        "identity_source": "reopened native No. and Cust.Ref. fields",
        "expected_identity": expected_identity,
        "observed_header": dict(header), "raw_row": dict(row["values"]),
        "documents_category": category_evidence, "table_evidence": table.evidence,
        "initial_clipped_read": initial_clip,
        "limitations": "Caller owns prior editor close and separate saved State/content checks.",
    }
    name = "saved-document-reference-" + uuid4().hex[:12] + ".json"
    evidence["report"] = str(ui.evidence_dir / name)
    _write_evidence(ui, name, evidence)
    if not evidence["passed"]:
        raise WorkflowReview(
            "saved_document_identity_mismatch",
            "The reopened native document identity differs from the retained number or reference.",
            expected=expected_identity, observed=observed_identity, report=evidence["report"],
        )
    return header, evidence


def check_prepared_demo_query(ui: Ui, external_reference, *, current_title):
    """Read the visible query; fresh data is an explicit environment precondition.

    This does not prove cross-type absence when the category tree is hidden.
    It must only be called for the expressly selected prepared-empty demo route.
    """
    if not isinstance(external_reference, str) or not external_reference.strip():
        raise ValueError('A nonblank source reference is required before demo input.')
    ui.click_label('Documents', scope=_left_scope(ui, current_title))
    ui.set_text('Search:', external_reference, scope='Documents')
    table = read_complete_table(ui, ['Total'], scope='Documents', search_field='Search:',
                                search_value=external_reference, timeout=25)
    if table.rows:
        raise WorkflowReview('document_reference_exists',
                             'The prepared demo view has query results; stop before business input.',
                             count=len(table.rows), table_evidence=table.evidence)
    evidence = {'visible_query_empty_verified': True,
                'cross_type_document_absence_verified': False,
                'query': external_reference, 'save_requests': 0,
                'environment_precondition': 'Operator prepared an empty disposable demo workspace before this command.',
                'limitation': 'The hidden category tree does not establish the complete cross-type search scope.',
                'visible_empty_table': table.evidence}
    evidence['report'] = _write_evidence(ui, 'prepared-demo-query-' + uuid4().hex[:12] + '.json', evidence)
    return evidence


def require_no_documents(ui: Ui, external_reference, *, current_title, fresh_launch=None):
    """Reject query results in both type parents, or verify an owned fresh launch.

    A fresh launch means this invocation created a previously absent data
    directory and launched Fakturama there before any business input. Its empty
    visible list is supporting evidence; a hidden type tree alone proves nothing.
    """
    if not isinstance(external_reference, str) or not external_reference.strip():
        raise ValueError("A nonblank source external reference is required before writes.")
    evidence = {'passed': False, 'query': external_reference, 'save_requests': 0}
    if fresh_launch is not None:
        if (fresh_launch.get('previously_absent') is not True
                or fresh_launch.get('window_title') != ui.title
                or fresh_launch.get('process_identity') != list(ui.desktop.identity)):
            raise DesktopError('Fresh launch does not identify this newly owned workspace/process.')
        ui.click_label('Documents', scope=_left_scope(ui, current_title))
        ui.set_text('Search:', external_reference, scope='Documents')
        table = read_complete_table(ui, ['Total'], scope='Documents', search_field='Search:',
                                    search_value=external_reference, timeout=25)
        if table.rows:
            raise WorkflowReview('document_reference_exists', 'The fresh workspace has query results; stop before business input.')
        evidence.update(passed=True, method='new directory owned and launched by this invocation',
                        fresh_launch=fresh_launch, visible_empty_table=table.evidence,
                        limitation='This bootstrap route is restricted to a newly created demo workspace.')
    else:
        categories = []
        for category in ('Orders', 'Invoices'):
            try:
                selected = open_documents(ui, current_title=current_title, category=category)
            except DesktopError as error:
                raise WorkflowReview('document_absence_scope_unsupported',
                                     'Both type parents must be available for cross-type repeat protection; missing parents do not prove absence.',
                                     category=category, error=str(error)) from error
            ui.set_text('Search:', external_reference, scope='Documents')
            table = read_complete_table(ui, ['Total'], scope='Documents', search_field='Search:',
                                        search_value=external_reference, timeout=25)
            if table.rows:
                raise WorkflowReview('document_reference_exists',
                                     'Documents contains source-reference query results; inspect before starting another transaction.',
                                     category=category, count=len(table.rows), table_evidence=table.evidence)
            categories.append({'category': selected, 'empty_table': table.evidence})
        evidence.update(passed=True, method='both complete type-parent queries', categories=categories)
    evidence['report'] = _write_evidence(ui, 'document-absence-' + uuid4().hex[:12] + '.json', evidence)
    return evidence


def reopen_saved_document_by_native_identity(ui, *, retained_number, external_reference,
                                              category, current_title, saved_title):
    """Use a unique exact-query candidate, then prove its native number/reference.

    The row's icon/text State and Total are read independently. A retained
    number is only a comparison target; no painted number/reference is repaired.
    Caller must close the old editor first for a cold-reopen claim.
    """
    selected = open_documents(ui, current_title=current_title, category=category)
    ui.set_text('Search:', external_reference, scope='Documents')
    table = _reference_state_table(ui, external_reference)
    if len(table.rows) != 1:
        raise WorkflowReview('saved_document_query_not_unique',
                             'The exact reference query must return one complete candidate.',
                             category=category, count=len(table.rows), table_evidence=table.evidence)
    invoice = category == 'Invoices'
    header, identity = _open_reference_state_candidate(
        ui, table, table.rows[0], saved_title=saved_title, retained_number=retained_number,
        external_reference=external_reference, invoice=invoice)
    row = dict(table.rows[0]['values']) | {'Document': header['number'],
                                         'Cust.Ref.': header['reference'], 'Date': header['date']}
    observation = normalize_saved_document(row, check='invoice_persistence' if invoice else 'order_persistence')
    result = {'passed': True, 'header': header, 'observation': observation,
              'category': selected, 'native_identity': identity, 'save_requests': 0}
    result['report'] = _write_evidence(ui, 'native-document-retrieval-' + uuid4().hex[:12] + '.json', result)
    return result


def _number_row(table, number):
    if not isinstance(number, str) or not number.strip():
        raise ValueError("Supply the retained nonblank generated document number.")
    matches = [row for row in table.rows if _cell(row["values"], "Document") == number]
    if len(matches) != 1:
        raise WorkflowReview("saved_document_not_unique",
                             "The complete Documents table must contain exactly one retained number.",
                             number=number, exact_count=len(matches),
                             observed_numbers=[row["values"]["Document"] for row in table.rows])
    return matches[0]


def _column_values(number, expected, date_format):
    values = {"Document": number, "Cust.Ref.": expected["external_reference"],
              "Total": "EUR " + expected["total"]}
    if "date" in expected:
        parsed = date.fromisoformat(expected["date"])
        if date_format == "english_medium":
            month = next(name for name, index in _MONTHS.items() if index == parsed.month)
            values["Date"] = f"{month} {parsed.day}, {parsed.year}"
        elif date_format == "iso":
            values["Date"] = parsed.isoformat()
        else:
            raise ValueError("Choose a previously verified date display.")
    values["State"] = ("paid" if expected["paid"] else "unpaid") if "paid" in expected else "open"
    return values


def verify_saved_document(ui: Ui, number, *, check, expected, current_title,
                          currency="EUR", date_format="english_medium"):
    """Read a unique saved-number row, normalize it, and compare report fields.

    Returns the observed dictionary and its evidence separately. No document
    editor is opened and no Save is dispatched. This proves the saved list's
    fields, not every line/address or the Invoice's payment detail fields.
    """
    if check not in _CHECKS or not isinstance(expected, dict):
        raise ValueError("Supply a supported report check and its expected dictionary.")
    if currency != "EUR":
        raise DesktopError("Only the established EUR Documents display is supported.")
    category = "Invoices" if check == "invoice_persistence" else "Orders"
    table = documents_rows(ui, number, current_title=current_title, category=category,
                           column_values=_column_values(number, expected, date_format))
    row = _number_row(table, number)
    observed = normalize_saved_document(dict(row["values"]), check=check,
                                        currency=currency, date_format=date_format)
    if json.dumps(observed, sort_keys=True, allow_nan=False) != json.dumps(
            expected, sort_keys=True, allow_nan=False):
        raise WorkflowReview("saved_document_mismatch",
                             "The actual saved Documents row differs from the required report fields.",
                             check=check, expected=expected, observed=observed)
    return observed, {
        "passed": True, "stage": "saved_list", "raw_row": dict(row["values"]),
        "table_evidence": table.evidence,
        "limitations": "Document type icon remains raw screenshot evidence; no type is inferred from its number.",
    }


def read_transaction_documents(ui: Ui, *, invoice_number, order_number, current_title,
                               column_values=None):
    """Read both saved numbers in the selected Invoice's This transaction view.

    This returns only persisted-linkage evidence. It cannot supply the separate
    proof of actually choosing Invoice under Create a follow-up document.
    """
    if invoice_number == order_number:
        raise ValueError("Order and Invoice must have distinct retained generated numbers.")
    invoice_table = documents_rows(ui, invoice_number, current_title=current_title,
                                   category="Invoices",
                                   column_values=column_values)
    invoice_row = _number_row(invoice_table, invoice_number)
    rowselect(ui, invoice_table, invoice_row)
    ui.set_text("Search:", "", scope="Documents")
    category = _category(ui, "This transaction")
    transaction = _read_documents(ui, "", column_values, category)
    order_row = _number_row(transaction, order_number)
    result_invoice = _number_row(transaction, invoice_number)
    return {"order_number": order_row["values"]["Document"],
            "invoice_number": result_invoice["values"]["Document"],
            "method": "This transaction", "passed": True,
            "ui_evidence": {"selected_invoice_row": dict(invoice_row["values"]),
                            "selection_table": invoice_table.evidence,
                            "query": "", "category": "This transaction",
                            "rows": [dict(row["values"]) for row in transaction.rows],
                            "transaction_table": transaction.evidence}}


def _reference_state_table(ui, query):
    """Read actual state/total cells; native headers establish identity/reference."""
    from .document_states import state_cell_reader
    return read_complete_table(ui, ["State", "Total"], scope="Documents",
                               search_field="Search:", search_value=query, timeout=25,
                               cell_readers={"State": state_cell_reader(ui.desktop)})


def _reference_state_values(table):
    return [{key: _cell(row["values"], key) for key in ("State", "Total")}
            for row in table.rows]


def _reference_transaction_candidates(table, external_reference):
    values = _reference_state_values(table)
    if len(values) != 2:
        raise WorkflowReview("transaction_reference_scope_mismatch",
                             "This transaction must contain exactly two complete candidates; native references must then be checked.",
                             external_reference=external_reference, observed_rows=values)
    orders = [row for row, value in zip(table.rows, values) if value["State"] == "open"]
    invoices = [row for row, value in zip(table.rows, values) if value["State"] in {"paid", "unpaid"}]
    if len(orders) != 1 or len(invoices) != 1:
        raise WorkflowReview("transaction_state_candidates_ambiguous",
                             "Actual states must identify one open candidate and one paid/unpaid candidate; native identities still require verification.",
                             observed_rows=values)
    return orders[0], invoices[0], values


def _open_reference_state_candidate(ui, table, row, *, saved_title, retained_number,
                                    external_reference, invoice):
    rowselect(ui, table, row, clicks=2)
    history = _dismiss_customer_history(ui) if not invoice else None
    ui.wait_tab(saved_title)
    header = read_document_header(ui, title=saved_title, invoice=invoice)
    actual = {"number": header.get("number"), "reference": header.get("reference")}
    wanted = {"number": retained_number, "reference": external_reference}
    if actual != wanted:
        raise WorkflowReview("transaction_native_identity_mismatch",
                             "The opened candidate's actual native number/reference differs from the retained identity.",
                             expected=wanted, observed=actual, native_header=header)
    capture = ui.snapshot("transaction-native-identity", ocr=False)
    return header, {"passed": True, "identity_source": "actual native No. and Cust.Ref.",
                    "native_header": dict(header), "raw_row": dict(row["values"]),
                    "table_evidence": table.evidence, "capture": capture.metadata(),
                    "customer_history": history}


def _dismiss_customer_history(ui):
    """Dismiss only the observed, harmless Order customer-statistics message."""
    if not any(window.title == 'Information' for window in ui.desktop.windows()):
        return None
    dialog = Ui(ui.desktop, 'Information', ui.evidence_dir / ('customer-history-' + uuid4().hex[:12]),
                text_input=ui.text_input)
    capture = dialog.snapshot('customer-history-information')
    text = ' '.join(token.text for token in capture.tokens())
    if not all(phrase in text for phrase in ('has already ordered', 'times in the past.',
                                             'Last time:', 'Invoice numbers:', 'Total volume:')):
        raise DesktopError('Unrecognized Information dialog; it was left untouched.')
    ok = ui.desktop.find_ax(capture, role='AXButton', title='OK')
    ui.desktop.press(capture, ok)
    proof = {'passed': True, 'message': text, 'capture': capture.metadata(),
             'scope': 'customer statistics only; native document identity still checked afterwards',
             'save_requests': 0}
    dialog.actions.append({'action': 'dismiss_customer_statistics', **proof})
    dialog.write_actions()
    return proof


def read_transaction_documents_by_reference(ui: Ui, *, invoice_number, order_number,
                                            external_reference, current_title,
                                            invoice_title, order_title,
                                            currency="EUR", date_format="english_medium"):
    """Join actual row State/Total to native number/reference in This transaction.

    States choose readonly candidates; they do not establish document type.
    Each candidate is opened and its actual native number/reference is checked.
    Number pixels are never read or repaired. This proves saved state/linkage,
    not cold content, payment detail, or the earlier follow-up action.
    """
    for name, value in (("invoice_number", invoice_number), ("order_number", order_number),
                        ("external_reference", external_reference), ("current_title", current_title),
                        ("invoice_title", invoice_title), ("order_title", order_title)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Supply a nonblank {name}.")
    if invoice_number == order_number:
        raise ValueError("Order and Invoice must have distinct retained native numbers.")
    if currency != "EUR" or date_format != "english_medium":
        raise ValueError("Only the demonstrated English medium dates and EUR display are supported.")
    if any(title.startswith("*") or title in {"New Order", "New Invoice"}
           for title in (invoice_title, order_title)):
        raise ValueError("Saved transaction candidates require their observed clean editor titles.")
    name = "reference-state-transaction-" + uuid4().hex[:12] + ".json"
    result = {"passed": False, "method": "This transaction", "save_requests": 0,
              "scope": "actual native identities joined with actual saved reference/state/total rows",
              "report": str(ui.evidence_dir / name), "ui_evidence": {}}
    proof = result["ui_evidence"]
    try:
        proof["invoice_type_category"] = open_documents(ui, current_title=current_title, category="Invoices")
        ui.set_text("Search:", external_reference, scope="Documents")
        invoices = _reference_state_table(ui, external_reference)
        invoice_values = _reference_state_values(invoices)
        proof["invoice_selection_table"] = invoices.evidence
        proof["invoice_selection_rows"] = invoice_values
        if (len(invoice_values) != 1 or invoice_values[0]["State"] not in {"paid", "unpaid"}):
            raise WorkflowReview("invoice_reference_not_unique",
                                 "The exact native reference query must return one complete paid/unpaid candidate; its native identity is checked next.",
                                 observed_rows=invoice_values, external_reference=external_reference)
        invoice_header, proof["selected_invoice_native_identity"] = _open_reference_state_candidate(
            ui, invoices, invoices.rows[0], saved_title=invoice_title,
            retained_number=invoice_number, external_reference=external_reference, invoice=True)
        proof["initial_invoice_header"] = invoice_header
        ui.set_text("Search:", "", scope="Documents")
        proof["initial_transaction_category"] = _category(ui, "This transaction")
        transaction = _reference_state_table(ui, "")
        order_candidate, invoice_candidate, initial_values = _reference_transaction_candidates(
            transaction, external_reference)
        proof["initial_transaction_table"] = transaction.evidence
        proof["initial_transaction_rows"] = initial_values
        if {key: invoice_candidate["values"][key] for key in ("State", "Total")} != invoice_values[0]:
            raise WorkflowReview("invoice_row_changed",
                                 "The Invoice candidate's actual reference/state/total changed between views.",
                                 initial_row=invoice_values[0], observed_row=dict(invoice_candidate["values"]))
        order_header, proof["order_native_identity"] = _open_reference_state_candidate(
            ui, transaction, order_candidate, saved_title=order_title,
            retained_number=order_number, external_reference=external_reference, invoice=False)
        proof["rechecked_transaction_category"] = _category(ui, "This transaction")
        rechecked = _reference_state_table(ui, "")
        _, final_invoice_candidate, final_values = _reference_transaction_candidates(rechecked, external_reference)
        proof["rechecked_transaction_table"] = rechecked.evidence
        proof["rechecked_transaction_rows"] = final_values
        if final_values != initial_values:
            raise WorkflowReview("transaction_rows_changed",
                                 "This transaction's actual reference/state/total rows changed after opening the Order.",
                                 initial_rows=initial_values, observed_rows=final_values)
        final_invoice_header, proof["invoice_native_identity"] = _open_reference_state_candidate(
            ui, rechecked, final_invoice_candidate, saved_title=invoice_title,
            retained_number=invoice_number, external_reference=external_reference, invoice=True)
        # These identity fields come from actual native headers, never from the
        # retained-number arguments or a correction of ambiguous painted text.
        order_row = dict(order_candidate["values"]) | {
            "Document": order_header["number"], "Date": order_header["date"],
            "Cust.Ref.": order_header["reference"]}
        invoice_row = dict(final_invoice_candidate["values"]) | {
            "Document": final_invoice_header["number"], "Date": final_invoice_header["date"],
            "Cust.Ref.": final_invoice_header["reference"]}
        result["order_observation"] = normalize_saved_document(
            order_row, check="order_persistence", currency=currency, date_format=date_format)
        result["invoice_observation"] = normalize_saved_document(
            invoice_row, check="invoice_persistence", currency=currency, date_format=date_format)
        result.update(order_number=order_header["number"], invoice_number=final_invoice_header["number"], passed=True)
        proof.update(category="This transaction", query="", rows=final_values,
                     limitation="The earlier saved Order's follow-up action, cold content and Invoice payment-detail proofs remain separate.")
        return result
    except Exception as error:
        result["error"] = str(error)
        result["error_type"] = type(error).__name__
        for attribute in ("table_cell_evidence", "table_adapter_evidence_path", "evidence"):
            if hasattr(error, attribute):
                result[attribute] = getattr(error, attribute)
        error.reference_state_evidence = result["report"]
        raise
    finally:
        _write_evidence(ui, name, result)
