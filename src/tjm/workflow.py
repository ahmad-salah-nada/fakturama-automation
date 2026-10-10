"""Sequential Fakturama desktop workflow stages.

The live form functions below take validated source values. No transaction value
or screen coordinate is embedded here. A stage's successful input is not proof
of a saved record: the caller must retrieve and compare it before acceptance.
"""

from dataclasses import dataclass

from .desktop import DesktopError, wait_for
from .ui import Ui
from .models import Customer, ValidatedOrder


@dataclass(frozen=True)
class DocumentEditor:
    title: str
    proposed_number: str
    native_content: object


def editor_scope(ui: Ui, capture, title: str, *, before=None, after=None):
    scope = ui.scope(capture, title)
    top, bottom = scope[1], scope[3]
    if after is not None:
        box = ui.label(capture, after, scope=scope)
        top = box[1]
        if after == "Items":
            # The table frame begins slightly above its adjacent Items label.
            # Keep that observed header rule using a glyph-derived margin.
            top = max(scope[1], top - (box[3] - box[1]) / 2)
    if before is not None:
        bottom = ui.label(capture, before, scope=scope)[1]
    if top >= bottom:
        raise DesktopError("The editor section is not in its expected order.")
    return scope[0], top, scope[2], bottom


def read_field(ui: Ui, capture, label, *, scope, roles=("AXTextField", "AXTextArea")):
    control = ui.field(capture, label, scope=scope, roles=roles)
    return control.value


def begin_order(ui: Ui, order: ValidatedOrder) -> DocumentEditor:
    ui.icon("order_new.png", size=32)
    capture = ui.wait_tab("New Order")
    header = editor_scope(ui, capture, "New Order", before="Addresses")
    number = read_field(ui, capture, "No.", scope=header)
    if not isinstance(number, str) or not number.strip():
        raise DesktopError("The new Order has no readable proposed number.")
    group = ui.desktop.find_ax(capture, role="AXTabGroup", title="New Order")
    content = ui.desktop._read(group.element, "AXValue", required=True)
    ui.set_text("Date", order.order_date.strftime("%b %d, %Y"), scope=header)
    ui.set_text("Cust.Ref.", order.external_reference, scope=header)
    capture = ui.snapshot("order-header-readback")
    header = editor_scope(ui, capture, "New Order", before="Addresses")
    settings_scope = editor_scope(ui, capture, "New Order", before="Items")
    modes = [ui.field(capture, "VAT", scope=settings_scope, roles=("AXPopUpButton",)).value]
    # Net has no adjacent label; locate its exact visible native popup value.
    try:
        ui.choose_current("Gross", "Net", scope=header)
    except DesktopError as error:
        if "found 0" not in str(error):
            raise
        ui.choose_current("Net", "Net", scope=header)
    if modes != ["With VAT"]:
        ui.choose("VAT", "With VAT", scope=settings_scope)
    return DocumentEditor("New Order", number, content)


def open_contact_selector(ui: Ui, editor_title="New Order"):
    capture = ui.snapshot("before-order-contact-selector")
    scope = editor_scope(ui, capture, editor_title, after="Addresses", before="Items")
    ui.icon("contact_list.png", scope=scope,
            expected_help="Pick an address from the list of all contacts")


def fill_debtor_main(ui: Ui, customer: Customer, *, title="New Debtor") -> str:
    capture = ui.snapshot("generated-debtor-identity")
    customer_id = read_field(ui, capture, "Customer ID", scope=title)
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise DesktopError("The new Debtor has no readable generated Customer ID.")
    ui.set_text("Company", customer.company, scope=title)
    ui.set_pair("First Name Last Name", customer.first_name, customer.last_name, scope=title)
    capture = ui.snapshot("debtor-identity-filled")
    main = editor_scope(ui, capture, title, after="Main address")
    for label, value in (("additional name", customer.billing.additional_name),
                         ("Address specification", customer.billing.address_specification),
                         ("district", customer.billing.district)):
        if value is not None:
            ui.set_text(label, value, scope=main)
        elif read_field(ui, capture, label, scope=main) != "":
            raise DesktopError(f"Unexpected unsourced {label} in the new Debtor.")
    ui.set_text("Street", customer.billing.street, scope=main)
    ui.set_pair("ZIP - City", customer.billing.postal_code, customer.billing.city, scope=main)
    ui.choose("Country", customer.billing.country, scope=main)
    ui.set_text("E-Mail", customer.email, scope=main)
    ui.set_text("Telephone", customer.telephone, scope=main)
    shared = customer.billing == customer.delivery
    ui.address_roles({"Invoice address", "Delivery address"} if shared else {"Invoice address"},
                     scope=main)
    ui.snapshot("debtor-billing-filled")
    return customer_id


def fill_separate_delivery(ui: Ui, customer: Customer, *, title="New Debtor"):
    if customer.billing == customer.delivery:
        return
    delivery = customer.delivery
    ui.click_heading_button("Main address", "+", scope=title)
    capture = ui.snapshot("new-additional-address")
    scope = editor_scope(ui, capture, title, after="Main address")
    # This address's name retains the explicitly sourced warehouse recipient.
    # It does not establish that Fakturama will render that recipient correctly.
    ui.set_text("additional name", delivery.additional_name or delivery.recipient, scope=scope)
    ui.set_text("Street", delivery.street, scope=scope)
    for label, value in (("Address specification", delivery.address_specification),
                         ("district", delivery.district)):
        if value is not None:
            ui.set_text(label, value, scope=scope)
    ui.set_pair("ZIP - City", delivery.postal_code, delivery.city, scope=scope)
    ui.choose("Country", delivery.country, scope=scope)
    ui.address_roles({"Delivery address"}, scope=scope)
    ui.snapshot("delivery-address-master-filled")


def fill_debtor_defaults(ui: Ui, customer: Customer, *, title="New Debtor"):
    ui.click_label("Miscellaneous", scope=title)
    ui.set_text("Alias name", customer.alias, scope=title)
    ui.set_text("Discount", "0", expected="0%", scope=title)
    ui.choose("Net or Gross", "Net", scope=title)
    ui.snapshot("debtor-defaults-filled")


PAYMENT_FIELDS = {
    "name": "Name", "description": "Description", "account": "Account",
    "cash_discount": "Cash discount", "discount_days": "Discount Days",
    "net_days": "Net Days", "text_unpaid": "Text 'unpaid'",
    "text_deposit": "Text 'deposit'", "text_paid": "Text 'paid'",
    "code": "!editorPaymentPaymentcode!",
}


def read_payment_definition(ui: Ui, *, title="New Term of Payment"):
    capture = ui.snapshot("payment-definition-readback")
    values = {key: read_field(ui, capture, label, scope=title,
                             roles=("AXTextField", "AXTextArea", "AXPopUpButton"))
              for key, label in PAYMENT_FIELDS.items()}
    # Installed English payment-code labels contain a trailing display space.
    # This normalization is limited to that fixed enum; master names stay exact.
    if isinstance(values["code"], str):
        values["code"] = values["code"].strip()
    standard = read_field(ui, capture, "Standard", scope=title, roles=("AXStaticText",))
    return values, standard


def fill_payment_definition(ui: Ui, method: str, *, title="New Term of Payment"):
    from .workflow_rules import payment_definition, require_matching_payment

    expected = payment_definition(method)
    _, standard_before = read_payment_definition(ui, title=title)
    ui.set_text("Name", expected["name"], scope=title)
    ui.set_text("Description", expected["description"], scope=title)
    ui.choose(PAYMENT_FIELDS["code"], expected["code"], scope=title, display_whitespace=True)
    values, standard_after = read_payment_definition(ui, title=title)
    require_matching_payment(values, method)
    if standard_after != standard_before:
        raise DesktopError("The standard payment method changed during creation.")
    return values, standard_before


def save_once(ui: Ui, ledger, *, kind: str, identity: str, title: str,
              identity_label: str, journal_directory, identity_before=None):
    """Record durable intent, verify the active editor, then dispatch one Save.

    A successful return establishes one dispatch only. Retrieval is a separate
    caller obligation. Even an unknown dispatch result keeps the intent file.
    """
    import hashlib
    import json
    from pathlib import Path

    capture = ui.snapshot("before-single-save")
    scope = (editor_scope(ui, capture, title, before=identity_before)
             if identity_before is not None else title)
    control = ui.field(capture, identity_label, scope=scope,
                       roles=("AXTextField", "AXTextArea"))
    if control.value != identity:
        raise DesktopError("The active record identity does not match this Save request.")
    # The selected editor and Eclipse's active part can differ when a Data view
    # has focus. A harmless identity-field click makes this editor the active part.
    ui.desktop.click(capture, control.bounds.x + control.bounds.width / 2,
                     control.bounds.y + control.bounds.height / 2)
    wait_for(lambda: ui.desktop._read(control.element, "AXFocused") is True,
             "exact record identity field focused before Save", timeout=2)
    group = None
    for name in (title, "*" + title):
        try:
            group = ui.desktop.find_ax(capture, role="AXTabGroup", title=name)
            break
        except DesktopError as error:
            if not str(error).endswith("found 0."):
                raise
    if group is None:
        raise DesktopError("The intended Save editor is not active.")
    content = ui.desktop._read(group.element, "AXValue", required=True)

    def same_record():
        d = ui.desktop
        selected = d._read(group.element, "AXValue", required=True)
        return (d._read(group.element, "AXTitle") in {title, "*" + title}
                and d.cf.CFEqual(selected, content)
                and d._read(control.element, "AXFocused") is True
                and d._control(control.element).value == identity)

    if not same_record():
        raise DesktopError("The intended Save editor changed before journaling.")
    intent = ledger.record_intent(kind, identity)
    folder = Path(journal_directory)
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((kind + "\0" + identity).encode("utf-8")).hexdigest()
    with (folder / (digest + ".json")).open("x", encoding="utf-8") as stream:
        json.dump({"intent": intent, "editor_title": title,
                   "before_save_capture": capture.metadata()}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        import os
        os.fsync(stream.fileno())
    ui.press("Save", action_guard=same_record)
    return intent


def activate_editor(ui: Ui, title: str, *, current_title: str):
    capture = ui.snapshot("before-editor-activation", ocr=False)
    d = ui.desktop
    try:
        group = d.find_ax(capture, role="AXTabGroup", title=current_title)
    except DesktopError as error:
        if not str(error).endswith("found 0."):
            raise
        group = d.find_ax(capture, role="AXTabGroup", title="*" + current_title)
    try:
        tab = d.find_ax(capture, role="AXRadioButton", title=title)
    except DesktopError as error:
        if not str(error).endswith("found 0."):
            raise
        tab = d.find_ax(capture, role="AXRadioButton", title="*" + title)
    selected = d._read(group.element, "AXValue", required=True)
    selected_tab = d._control(selected)
    if (selected_tab.role != "AXRadioButton" or not group.bounds.contains_rect(tab.bounds)
            or tab.bounds.y != selected_tab.bounds.y):
        raise DesktopError("The requested editor is not a tab in the observed editor group.")
    # SWT exposes a synthetic radio tab whose AXPress can return success without
    # switching editors. Use its freshly observed native bounds for the click.
    if tab.enabled is not True:
        raise DesktopError("The requested editor tab is disabled.")
    d.click(capture, tab.bounds.x + tab.bounds.width / 2,
            tab.bounds.y + tab.bounds.height / 2)
    ui.actions.append({"action": "activate_editor", "native_tab_title": tab.title})
    return ui.wait_tab(title)


def run_image(image_path, *, workspace, output_dir="runs", extractor="ocr", name_order=None,
              model=None, demonstration=False, original_image=None,
              allow_demo_payment_sequence=False, fresh_demo=False, prepared_empty_demo=False):
    """Extract, preflight, execute one transaction and retain every stop point."""
    import hashlib
    import json
    from pathlib import Path
    from .extract import extract_order, write_json
    from .workflow_report import new_run_report, stop_run

    if (demonstration and not original_image) or (original_image and not demonstration):
        raise ValueError("Demonstration provenance requires both flags.")
    if allow_demo_payment_sequence and not demonstration:
        raise ValueError("The sequence variation is restricted to a labelled demonstration.")
    if fresh_demo and not demonstration:
        raise ValueError('Fresh disposable setup is restricted to a labelled demonstration.')
    if prepared_empty_demo and not demonstration:
        raise ValueError('A prepared empty disposable workspace is restricted to a labelled demonstration.')
    if prepared_empty_demo and fresh_demo:
        raise ValueError('Choose either fresh demo setup or a prepared empty demo workspace.')
    extraction = extract_order(image_path, output_dir=Path(output_dir) / "extraction",
                               extractor=extractor, name_order=name_order, model=model)
    digest = hashlib.sha256(Path(original_image).read_bytes()).hexdigest() if demonstration else None
    report = new_run_report(extraction, workspace=str(Path(workspace).expanduser().resolve()),
                            input_kind="demonstration" if demonstration else "original",
                            demo_label="Compatibility input with original-image comparison" if demonstration else None,
                            original_input_sha256=digest)
    folder = Path(extraction["evidence"]["run_directory"])
    report["evidence"] = {"run_directory": str(folder), "report": str(folder / "workflow-report.json")}
    report["declared_payment_sequence_variation"] = allow_demo_payment_sequence
    if prepared_empty_demo:
        report['declared_prepared_empty_demo'] = True
    write_json(folder / "workflow-report.json", report)
    if report['status'] != 'running':
        return report
    from .live_workflow import execute_transaction
    from .readiness import _select_workspace_desktop, connect_readiness, expected_workspace_title
    from .workflow_rules import SaveLedger, WorkflowReview
    from .workflow_report import sync_ledger, write_run_report
    ledger, ui = SaveLedger(), None
    try:
        launch = None
        if fresh_demo:
            from .demo_setup import launch_fresh_demo
            report['phase'] = 'launch owned fresh demonstration workspace'
            write_run_report(report, folder / 'workflow-report.json')
            launch = launch_fresh_demo(workspace, folder)
        _, title = expected_workspace_title(workspace)
        desktop, _ = _select_workspace_desktop(title)
        desktop.activate()
        ui, ready = connect_readiness(workspace, folder / 'ui', text_input='unicode')
        order = ValidatedOrder.model_validate_json(json.dumps(report['order']))
        environment = {'prepared_empty_demo': True} if prepared_empty_demo else {}
        execute_transaction(ui, order, report, ledger, folder, readiness=ready,
                            allow_demo_payment_sequence=allow_demo_payment_sequence, fresh_launch=launch, **environment)
    except Exception as error:
        proof = {'phase': report.get('phase'), 'error_type': type(error).__name__,
                 'save_ledger': ledger.as_json()}
        for attribute in ('document_content_evidence', 'document_cleanup_evidence',
                          'document_presentation_evidence', 'reference_state_evidence',
                          'table_adapter_evidence_path', 'table_cell_evidence', 'evidence'):
            value = getattr(error, attribute, None)
            if value is not None:
                proof[attribute] = value
        if report['status'] == 'running':
            error_code = getattr(error, 'code', None)
            if not isinstance(error_code, str) or not error_code.strip():
                error_code = 'workflow_stopped'
            stop_run(report, status='review_required' if isinstance(error, (DesktopError, WorkflowReview)) else 'failed',
                     code=error_code, message=str(error), evidence=proof)
    finally:
        # Completion already synchronized and sealed the verified ledger.
        if report['status'] != 'verified':
            sync_ledger(report, ledger)
        if ui is not None:
            ui.write_actions()
        write_run_report(report, folder / 'workflow-report.json')
    return report
