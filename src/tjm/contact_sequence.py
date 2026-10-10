"""Resolve a source customer through visible selectors and one-save masters.

The optional payment-before-contact order is an explicitly approved demo
variation. This module never adopts it automatically after an unavailable
payment choice. Document address text must still be checked by the caller.
"""

from pathlib import Path
import json

from .desktop import DesktopError, wait_for
from .models import Customer, ValidatedOrder
from .ui import Ui
from .ui_tables import fit_columns, read_complete_table, rowselect
from .workflow import (activate_editor, editor_scope, fill_debtor_defaults,
                       fill_debtor_main, fill_payment_definition,
                       fill_separate_delivery, open_contact_selector,
                       read_field, read_payment_definition, save_once)
from .workflow_rules import (WorkflowReview, payment_definition,
                             require_matching_payment, require_unique_customer)


CONTACT_HEADERS = ["No.", "First Name", "Name", "Company", "ZIP", "City"]
PAYMENT_HEADERS = ["Name", "Description", "Discount", "Disc. Days", "Net Days"]


def _write_evidence(ui, name, values):
    path = ui.evidence_dir / name
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(values, ensure_ascii=False, indent=2) + "\n")
    return str(path)


def _left_scope(ui, title):
    capture = ui.snapshot("left-panel-scope")
    editor = ui.scope(capture, title)
    if editor[0] <= 0:
        raise DesktopError("The active editor leaves no visible left panel.")
    return 0, editor[1], editor[0], capture.image.height


def _contact_values(customer):
    return {"First Name": customer.first_name, "Name": customer.last_name,
            "Company": customer.company, "ZIP": customer.billing.postal_code,
            "City": customer.billing.city}


def _customer_row(table, customer):
    mapped = [{"company": row["values"]["Company"],
               "first_name": row["values"]["First Name"],
               "last_name": row["values"]["Name"],
               "postal_code": row["values"]["ZIP"],
               "city": row["values"]["City"], "visible_row": row}
              for row in table.rows]
    matched = require_unique_customer(mapped, customer)
    return matched["visible_row"] if matched is not None else None


def _select_contact(ui, customer, *, expected_id=None):
    open_contact_selector(ui)
    stage = "initial" if expected_id is None else "created"
    selector = Ui(ui.desktop, "Select the address", ui.evidence_dir / f"contact-selector-{stage}",
                  text_input=ui.text_input)
    try:
        selector.set_text("Search:", customer.company)
        values = _contact_values(customer)
        if expected_id is not None:
            values["No."] = expected_id
        fit_columns(selector, values, scope=None, headers=CONTACT_HEADERS)
        table = read_complete_table(selector, CONTACT_HEADERS, search_field="Search:",
                                    search_value=customer.company, timeout=25)
        row = _customer_row(table, customer)
        if row is None:
            selector.press("Cancel")
            ui.wait_tab("New Order")
            return None, table.evidence
        customer_id = row["values"]["No."]
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise WorkflowReview("unreadable_customer_id", "Selected customer has no readable generated ID.")
        if expected_id is not None and customer_id != expected_id:
            raise WorkflowReview("changed_customer_id", "The reselected customer ID differs after creation.",
                                 expected=expected_id, observed=customer_id)
        rowselect(selector, table, row)
        selector.press("OK")
        ui.wait_tab("New Order")
        return customer_id, table.evidence
    finally:
        selector.write_actions()


def _group(ui, capture, title):
    try:
        return ui.desktop.find_ax(capture, role="AXTabGroup", title=title)
    except DesktopError as error:
        if not str(error).endswith("found 0."):
            raise
        return ui.desktop.find_ax(capture, role="AXTabGroup", title="*" + title)


def _save_new_master(ui, ledger, *, kind, identity, title, label, journal_directory,
                     identity_before=None):
    capture = ui.snapshot("master-before-save-title", ocr=False)
    group = _group(ui, capture, title)
    save_once(ui, ledger, kind=kind, identity=identity, title=title,
              identity_label=label, journal_directory=journal_directory,
              identity_before=identity_before)

    def clean_title():
        fresh = ui.snapshot("master-save-readiness", ocr=False)
        current = ui.desktop._control(group.element)
        name = current.title
        if (not isinstance(name, str) or not name.strip() or name.startswith("*")
                or name == title):
            return None
        scope = (editor_scope(ui, fresh, name, before=identity_before)
                 if identity_before is not None else name)
        value = read_field(ui, fresh, label, scope=scope)
        if value != identity:
            raise DesktopError("Master identity changed after its single Save.")
        return name

    return wait_for(clean_title, "clean saved master title and unchanged identity", timeout=10)


def _close_clean(ui, title, *, identity_label, identity):
    capture = ui.snapshot("before-clean-master-close")
    group = ui.desktop.find_ax(capture, role="AXTabGroup", title=title)
    if read_field(ui, capture, identity_label, scope=title) != identity:
        raise DesktopError("The master to close has a different identity.")
    tab = ui.desktop._read(group.element, "AXValue", required=True)
    if ui.desktop._control(tab).title != title:
        raise DesktopError("The clean master is not the selected editor.")
    # Focus this editor before Eclipse's Close command, just as Save does.
    field = ui.field(capture, identity_label, scope=title)
    ui.desktop.click(capture, field.bounds.x + field.bounds.width / 2,
                     field.bounds.y + field.bounds.height / 2)
    wait_for(lambda: ui.desktop._read(field.element, "AXFocused") is True,
             "clean master identity field focused before Close", timeout=2)
    ui.menu("Close")
    def remaining_editor():
        ui.snapshot("after-clean-master-close", ocr=False)
        current = ui.desktop._control(group.element)
        return current.title if current.title not in {title, "*" + title} else None
    current_title = wait_for(remaining_editor, "saved master closed", timeout=8)
    activate_editor(ui, "New Order", current_title=current_title)


def _payment_table(ui, method):
    ui.set_text("Search:", method, scope="terms of payment")
    fit_columns(ui, {"Name": method, "Description": method}, scope="terms of payment",
                headers=PAYMENT_HEADERS)
    table = read_complete_table(ui, PAYMENT_HEADERS, scope="terms of payment",
                                search_field="Search:", search_value=method, timeout=25)
    rows = [row for row in table.rows if row["values"]["Name"] == method]
    if len(rows) > 1:
        raise WorkflowReview("conflicting_payment", "Multiple Payment Methods have the exact Name.",
                             method=method, exact_count=len(rows))
    return table, rows[0] if rows else None


def _resolve_payment(ui, method, ledger, journal_directory):
    payment_definition(method)
    ui.click_label("terms of payment", scope=_left_scope(ui, "New Order"))
    table, row = _payment_table(ui, method)
    created = row is None
    standard_before = None
    if created:
        ui.icon("plus.png", size=16, scope="terms of payment",
                expected_help="Create a new term of payment")
        ui.wait_tab("New Term of Payment")
        _, standard_before = fill_payment_definition(ui, method)
        title = _save_new_master(ui, ledger, kind="payment", identity=method,
                                 title="New Term of Payment", label="Name",
                                 journal_directory=journal_directory)
        _close_clean(ui, title, identity_label="Name", identity=method)
        table, row = _payment_table(ui, method)
        if row is None:
            raise WorkflowReview("payment_not_retrieved", "Saved Payment Method is absent from its complete list.",
                                 method=method)
    rowselect(ui, table, row, clicks=2)
    ui.wait_tab(method)
    fields, standard = read_payment_definition(ui, title=method)
    require_matching_payment(fields, method)
    if created and standard != standard_before:
        raise DesktopError("The standard payment changed after saving and reopening the new master.")
    evidence = {"passed": True, "created": created, "closed_and_reopened": created,
                "fields": fields, "preserved_standard": standard,
                "table_evidence": table.evidence}
    evidence["report"] = _write_evidence(ui, "resolved-payment.json", evidence)
    if created:
        ledger.record_verified("payment", method, evidence=evidence)
    activate_editor(ui, "New Order", current_title=method)
    return evidence


def _read_pair(ui, capture, label, *, scope):
    region = ui.scope(capture, scope)
    first = ui.field(capture, label, scope=region)
    y = first.bounds.y + first.bounds.height / 2
    step = max(1, first.bounds.height / 2)
    x = first.bounds.x + first.bounds.width + step
    limit = capture.screen_rect.x + region[2] / capture.scale_x
    while x < limit:
        second = ui.desktop.hit_test(capture, x, y)
        if second.role == "AXTextField" and second.bounds.x > first.bounds.x + first.bounds.width:
            ui.require_inside_scope(capture, second, region)
            return [first.value, second.value]
        x += step
    raise DesktopError(f"No readable second adjacent field for {label!r}.")


def _read_address(ui, title):
    capture = ui.snapshot("persisted-contact-address")
    scope = editor_scope(ui, capture, title, after="Main address")
    fields = {name: read_field(ui, capture, name, scope=scope,
                               roles=("AXTextField", "AXTextArea", "AXPopUpButton"))
              for name in ("additional name", "Street", "Address specification", "district",
                           "Country", "E-Mail", "Telephone", "address type")}
    fields["ZIP - City"] = _read_pair(ui, capture, "ZIP - City", scope=scope)
    return fields


def _expected_address(address, *, email="", telephone="", roles, additional_name=None):
    return {"additional name": (address.additional_name if additional_name is None else additional_name) or "",
            "Street": address.street, "Address specification": address.address_specification or "",
            "district": address.district or "", "Country": address.country,
            "E-Mail": email, "Telephone": telephone, "address type": roles,
            "ZIP - City": [address.postal_code, address.city]}


def _verify_contact(ui, customer: Customer, customer_id, title, method):
    ui.click_label("Addresses", scope=title)
    capture = ui.snapshot("persisted-contact-identity")
    identity = {"Customer ID": read_field(ui, capture, "Customer ID", scope=title),
                "Company": read_field(ui, capture, "Company", scope=title),
                "names": _read_pair(ui, capture, "First Name Last Name", scope=title)}
    expected_identity = {"Customer ID": customer_id, "Company": customer.company,
                         "names": [customer.first_name, customer.last_name]}
    if identity != expected_identity:
        raise WorkflowReview("contact_persistence_mismatch", "Reopened contact identity differs from its source.",
                             expected=expected_identity, observed=identity)
    billing = _read_address(ui, title)
    shared = customer.billing == customer.delivery
    expected_billing = _expected_address(customer.billing, email=customer.email,
                                         telephone=customer.telephone,
                                         roles="Invoice address, Delivery address" if shared else "Invoice address")
    if billing != expected_billing:
        raise WorkflowReview("contact_persistence_mismatch", "Reopened main address differs from its source.",
                             expected=expected_billing, observed=billing)
    delivery = billing
    if not shared:
        ui.click_label("additional address #1", scope=title)
        delivery = _read_address(ui, title)
        expected_delivery = _expected_address(customer.delivery, roles="Delivery address",
                                              additional_name=customer.delivery.additional_name or customer.delivery.recipient)
        if delivery != expected_delivery:
            raise WorkflowReview("contact_persistence_mismatch", "Reopened delivery address differs from its source.",
                                 expected=expected_delivery, observed=delivery)
    ui.click_label("Miscellaneous", scope=title)
    capture = ui.snapshot("persisted-contact-defaults")
    defaults = {name: read_field(ui, capture, name, scope=title,
                                 roles=("AXTextField", "AXPopUpButton"))
                for name in ("Alias name", "Discount", "Net or Gross", "Payment")}
    expected_defaults = {"Alias name": customer.alias, "Discount": "0%",
                         "Net or Gross": "Net", "Payment": method}
    if defaults != expected_defaults:
        raise WorkflowReview("contact_persistence_mismatch", "Reopened contact defaults differ from the required values.",
                             expected=expected_defaults, observed=defaults)
    return {"passed": True, "closed_and_reopened": True, "identity": identity,
            "billing": billing, "delivery": delivery, "defaults": defaults}


def _reopen_contact(ui, customer, customer_id, saved_title):
    _close_clean(ui, saved_title, identity_label="Customer ID", identity=customer_id)
    ui.click_label("Debtors", scope=_left_scope(ui, "New Order"))
    ui.set_text("Search:", customer.company, scope="Debtors")
    values = _contact_values(customer) | {"No.": customer_id}
    # This overview can display the first address by database ID. Match the
    # generated contact ID; verify both address roles inside its reopened form.
    values["ZIP"] = max((customer.billing.postal_code, customer.delivery.postal_code), key=len)
    fit_columns(ui, values, scope="Debtors", headers=CONTACT_HEADERS)
    table = read_complete_table(ui, CONTACT_HEADERS, scope="Debtors", search_field="Search:",
                                search_value=customer.company, timeout=25)
    rows = [row for row in table.rows if row["values"]["No."] == customer_id]
    if len(rows) != 1:
        raise WorkflowReview("contact_not_retrieved", "Saved Customer ID is not unique in its complete list.",
                             customer_id=customer_id, exact_count=len(rows))
    rowselect(ui, table, rows[0], clicks=2)
    ui.wait_tab(saved_title)
    return table.evidence


def resolve_contact(ui, order: ValidatedOrder, ledger, journal_directory,
                    allow_demo_payment_sequence=False):
    """Select an exact existing billing row, or create and cold-check its master.

    Starts and finishes in the original New Order. The caller must independently
    verify document Invoice/Delivery text, modes and customer association before
    accepting this stage. A missing payment in the prescribed contact-first
    sequence stops for review; it does not trigger the demo variation.
    """
    if not isinstance(order, ValidatedOrder) or not isinstance(allow_demo_payment_sequence, bool):
        raise ValueError("Contact resolution requires a validated order and explicit boolean variation flag.")
    journal_directory = Path(journal_directory)
    customer = order.customer
    customer_id, selector_evidence = _select_contact(ui, customer)
    if customer_id is not None:
        return {"customer_id": customer_id, "created": False,
                "selector_evidence": selector_evidence, "document_addresses_verified": False}
    payment_evidence = None
    if allow_demo_payment_sequence:
        payment_evidence = _resolve_payment(ui, order.payment.method, ledger, journal_directory)
    ui.click_label("New Contact", scope=_left_scope(ui, "New Order"))
    ui.wait_tab("New Debtor")
    customer_id = fill_debtor_main(ui, customer)
    fill_separate_delivery(ui, customer)
    fill_debtor_defaults(ui, customer)
    try:
        ui.choose("Payment", order.payment.method, scope="New Debtor")
    except DesktopError as error:
        if str(error) != f"Timed out waiting for choice {order.payment.method}.":
            raise
        raise WorkflowReview("payment_unavailable_in_open_contact",
                             "The required payment is unavailable in the open contact. No refresh or sequence change was attempted.",
                             method=order.payment.method, demo_payment_sequence=allow_demo_payment_sequence) from error
    saved_title = _save_new_master(ui, ledger, kind="contact", identity=customer_id,
                                   title="New Debtor", label="Customer ID",
                                   journal_directory=journal_directory)
    table_evidence = _reopen_contact(ui, customer, customer_id, saved_title)
    readback = _verify_contact(ui, customer, customer_id, saved_title, order.payment.method)
    readback["table_evidence"] = table_evidence
    readback["report"] = _write_evidence(ui, "resolved-contact.json", readback)
    ledger.record_verified("contact", customer_id, evidence=readback)
    activate_editor(ui, "New Order", current_title=saved_title)
    selected_id, selector_evidence = _select_contact(ui, customer, expected_id=customer_id)
    if selected_id != customer_id:
        raise WorkflowReview("contact_not_reselected", "The created contact could not be reselected for the retained Order.")
    return {"customer_id": customer_id, "created": True, "contact_readback": readback,
            "payment_readback": payment_evidence, "selector_evidence": selector_evidence,
            "declared_demo_payment_sequence": allow_demo_payment_sequence,
            "document_addresses_verified": False}
