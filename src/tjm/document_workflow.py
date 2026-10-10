"""Document controls; input/readback here does not establish persistence."""

from decimal import Decimal
import re

from .desktop import DesktopError, wait_for
from .fields import find_phrases
from .models import Payment
from .ocr import ocr_tokens, token_evidence
from .product_workflow import euro_display
from .workflow import editor_scope, read_field


def address_control(ui, capture, tab, *, title):
    """Find the multiline field directly below the observed address tab.

    Its first line can scroll out of view after editing. The current tab label
    and exposed field bounds locate it without relying on source text visibility.
    """
    scope = editor_scope(ui, capture, title, after="Addresses", before="Items")
    box = ui.label(capture, tab, scope=scope)
    height = box[3] - box[1]
    point = capture.screen_point((box[0] + box[2]) / 2, box[3] + height)
    control = ui.desktop.hit_test(capture, *point, expected_role="AXTextArea")
    ui.require_inside_scope(capture, control, scope)
    if not isinstance(control.value, str):
        raise DesktopError("The document address is not readable text.")
    return control


def source_address(address):
    return "\n".join([address.recipient, address.street,
                      address.postal_code + " " + address.city, address.country])


def read_addresses(ui, *, title):
    result = {}
    for tab, key in (("Invoice address", "billing"), ("Delivery address", "delivery")):
        ui.click_label(tab, scope=title)
        capture = ui.snapshot("document-" + key + "-address")
        result[key] = address_control(ui, capture, tab, title=title).value
    return result


def candidate_delivery_text(ui, customer, *, title):
    """Test source delivery text for a newly created contact only.

    This returns pre-save readback, not association or persistence acceptance.
    The caller must prove saved Order and copied/reopened Invoice outcomes.
    """
    ui.click_label("Delivery address", scope=title)
    capture = ui.snapshot("before-source-delivery-candidate")
    control = address_control(ui, capture, "Delivery address", title=title)
    ui.set_control(capture, control, source_address(customer.delivery),
                   label="source Delivery address")
    actual = read_addresses(ui, title=title)
    expected = {"billing": source_address(customer.billing),
                "delivery": source_address(customer.delivery)}
    if actual != expected:
        raise DesktopError("The document addresses differ from the source after delivery input.")
    return actual


def _price_mode(ui, capture, scope):
    tokens = [t for t in capture.tokens() if scope[0] <= t.cx < scope[2]
              and scope[1] <= t.cy < scope[3]]
    controls = []
    for value in ("Net", "Gross"):
        for box in find_phrases(tokens, value):
            point = capture.screen_point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            control = ui.desktop.hit_test(capture, *point)
            if control.role == "AXPopUpButton" and control.value == value:
                ui.require_inside_scope(capture, control, scope)
                if not any(ui.desktop.cf.CFEqual(control.element, old.element) for old in controls):
                    controls.append(control)
    if len(controls) != 1:
        raise DesktopError("Document header needs one visible native Net/Gross popup.")
    return controls[0].value


def read_document_header(ui, *, title, invoice=False):
    """Read displayed dates verbatim; never overwrite proposed Invoice dates."""
    capture = ui.snapshot("document-header-readback")
    header = editor_scope(ui, capture, title, before="Addresses")
    settings = editor_scope(ui, capture, title, before="Items")
    result = {"number": read_field(ui, capture, "No.", scope=header),
              "date": read_field(ui, capture, "Date", scope=header),
              "reference": read_field(ui, capture, "Cust.Ref.", scope=header),
              "price_mode": _price_mode(ui, capture, header),
              "vat_mode": read_field(ui, capture, "VAT", scope=settings,
                                     roles=("AXPopUpButton",))}
    if invoice:
        result.update(order_date=read_field(ui, capture, "Order Date", scope=settings),
                      service_date=read_field(ui, capture, "Service date", scope=settings))
    return result


def parse_display_money(text, *, currency="EUR"):
    """Parse the supported EUR UI display, without guessing separators/currency."""
    if currency != "EUR" or not isinstance(text, str):
        raise DesktopError("Only a readable EUR money display is supported.")
    match = re.fullmatch(r"(?:€|EUR)\s*([0-9]+\.[0-9]{2})", text.strip())
    if match is None:
        raise DesktopError(f"Unrecognized EUR money display: {text!r}.")
    return Decimal(match[1])


def parse_display_percent(text):
    """Parse an explicit percentage; a bare number is not a verified percentage."""
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]{1,6})?)\s*%", text.strip()) if isinstance(text, str) else None
    if match is None or Decimal(match[1]) > 100:
        raise DesktopError("Unreadable or out-of-range document percentage.")
    return Decimal(match[1])


def _money_candidates(tokens, *, currency):
    ordered = sorted(tokens, key=lambda token: token.left)
    result, index = [], 0
    while index < len(ordered):
        token = ordered[index]
        selected = [token]
        if token.text in {"EUR", "€"} and index + 1 < len(ordered):
            following = ordered[index + 1]
            if (0 <= following.left - token.right <= max(token.height, following.height) * 2
                    and abs(following.cy - token.cy) <= max(token.height, following.height) / 2):
                selected.append(following)
        text = " ".join(part.text for part in selected)
        if text.startswith(("€", "EUR")):
            value = parse_display_money(text, currency=currency)
            result.append({"value": value, "text": text,
                           "box": (selected[0].left, min(t.top for t in selected),
                                   selected[-1].right, max(t.bottom for t in selected)),
                           "confidence": min(t.confidence for t in selected)})
            index += len(selected)
        else:
            index += 1
    return result


def _observe_summary_money(ui, capture, label, scope, *, currency):
    # "Total" also occurs inside "Total Net". The displayed net row bounds
    # its lower gross counterpart without accepting either OCR match at random.
    label_scope = scope
    if label == "Total":
        net_box = ui.label(capture, "Total Net", scope=scope)
        label_scope = (scope[0], net_box[3], scope[2], scope[3])
    # A shipping-method value can contain the word "shipping" itself. The
    # shared reader distinguishes the native static caption from popup text.
    box = ui.label(capture, label, scope=label_scope, exclude_inputs=label == "Shipping")
    height, center = box[3] - box[1], (box[1] + box[3]) / 2
    row = (box[2] + height / 2, max(scope[1], center - height),
           scope[2], min(scope[3], center + height))
    tokens = [t for t in capture.tokens() if row[0] <= t.cx < row[2]
              and abs(t.cy - center) <= height / 2]
    # Currency-shaped pixels locate a control; their OCR spelling is not its
    # value. In particular, a displayed zero can be recognized as letter O.
    native = {}
    for token in tokens:
        if not token.text.startswith(("EUR", "€")):
            continue
        control = ui.desktop.hit_test(capture, *capture.screen_point(token.cx, token.cy))
        if control.role in {"AXTextField", "AXStaticText"} and isinstance(control.value, str):
            ui.require_inside_scope(capture, control, scope)
            try:
                value = parse_display_money(control.value, currency=currency)
            except DesktopError:
                continue
            native[control.bounds.bbox] = (value, control)
    if len(native) == 1:
        value, control = next(iter(native.values()))
        return value, {"label": label, "method": "native value", "observed": control.value,
                       "bounds": control.bounds.as_dict()}
    if len(native) > 1:
        raise DesktopError(f"Document {label} has multiple native currency amounts.")
    try:
        candidates = _money_candidates(tokens, currency=currency)
    except DesktopError:
        candidates = []  # Cropped OCR is a separate reading of these same fresh pixels.
    if len(candidates) == 1:
        candidate = candidates[0]
        value_box = candidate["box"]
        control = ui.desktop.hit_test(capture, *capture.screen_point(
            (value_box[0] + value_box[2]) / 2, (value_box[1] + value_box[3]) / 2))
        if control.role in {"AXTextField", "AXStaticText"} and isinstance(control.value, str):
            ui.require_inside_scope(capture, control, scope)
            value = parse_display_money(control.value, currency=currency)
            return value, {"label": label, "method": "native value", "observed": control.value,
                           "bounds": control.bounds.as_dict()}
        if (candidate["confidence"] >= 85 and row[0] < value_box[0] < value_box[2] < row[2]
                and row[1] < value_box[1] < value_box[3] < row[3]):
            return candidate["value"], {"label": label, "method": "fresh scoped OCR",
                                        "observed": candidate["text"], "row_scope": row,
                                        "tokens": token_evidence(tokens)}
    return None, {"label": label, "crop_box": tuple(map(round, row))}


def _crop_summary_money(ui, capture, observation, *, currency):
    label, crop_box = observation["label"], observation["crop_box"]
    crop = capture.image.crop(crop_box)
    path = ui.evidence_dir / f"summary-{ui.sequence:04d}-{label.replace(' ', '-')}.png"
    crop.save(path)
    tokens = ocr_tokens(crop, psm=7)
    candidates = _money_candidates(tokens, currency=currency)
    if len(candidates) != 1 or candidates[0]["confidence"] < 85:
        raise DesktopError(f"Document {label} requires one complete high-confidence EUR amount.")
    candidate = candidates[0]
    if candidate["box"][0] <= 0 or candidate["box"][2] >= crop.width:
        raise DesktopError(f"Document {label} amount touches its crop boundary.")
    return candidate["value"], {"label": label, "method": "fresh cropped OCR",
                                "observed": candidate["text"], "crop": str(path),
                                "crop_box": crop_box, "tokens": token_evidence(tokens)}


def read_document_totals(ui, *, title, currency="EUR"):
    """Read one fresh summary observation; monetary output uses decimal strings."""
    capture = ui.snapshot("document-summary-readback")
    scope = editor_scope(ui, capture, title, after="Remarks")
    discount_control = ui.field(capture, "Discount", scope=scope, roles=("AXTextField",))
    discount = parse_display_percent(discount_control.value)
    shipping = ui.field(capture, "Shipping", scope=scope, roles=("AXPopUpButton", "AXComboBox"))
    if not isinstance(shipping.value, str) or not shipping.value.strip():
        raise DesktopError("The document shipping method is unreadable.")
    result = {"overall_discount_percent": format(discount.normalize(), "f"),
              "shipping_method": shipping.value}
    observations = []
    for label, key in (("Total Net", "net"), ("VAT", "vat"), ("Total", "gross"),
                       ("Shipping", "shipping_net")):
        value, evidence = _observe_summary_money(ui, capture, label, scope, currency=currency)
        observations.append((key, value, evidence))
    readings = []
    for key, value, evidence in observations:
        if value is None:
            value, evidence = _crop_summary_money(ui, capture, evidence, currency=currency)
        result[key] = f"{value:.2f}"
        readings.append(evidence)
    result["evidence"] = {"capture": capture.metadata(), "fields": readings,
                           "discount_display": discount_control.value,
                           "shipping_display": shipping.value}
    ui.discoveries.append({"method": "document summary", **result["evidence"]})
    return result


def create_invoice_followup(ui, *, saved_title, number, reference):
    """Create once from an exact clean Order; this does not save the Invoice."""
    if not isinstance(saved_title, str) or not saved_title or saved_title.startswith("*") or saved_title == "New Order":
        raise ValueError("Follow-up creation requires the observed clean saved Order title.")
    if not isinstance(number, str) or not number.strip() or not isinstance(reference, str) or not reference.strip():
        raise ValueError("Follow-up creation requires exact nonempty Order number/reference strings.")
    capture = ui.snapshot("before-saved-order-followup")
    d = ui.desktop
    group = d.find_ax(capture, role="AXTabGroup", title=saved_title)
    selected = d._read(group.element, "AXValue", required=True)
    selected_control = d._control(selected)
    if selected_control.role != "AXRadioButton" or selected_control.title != saved_title:
        raise DesktopError("The exact clean saved Order tab is not selected.")
    header = editor_scope(ui, capture, saved_title, before="Addresses")
    number_control = ui.field(capture, "No.", scope=header)
    reference_control = ui.field(capture, "Cust.Ref.", scope=header)
    if number_control.value != number or reference_control.value != reference:
        raise DesktopError("The saved Order differs from its expected number/reference.")
    d.click(capture, number_control.bounds.x + number_control.bounds.width / 2,
            number_control.bounds.y + number_control.bounds.height / 2)

    def same_saved_order():
        d._require_focus()
        tab = d._read(group.element, "AXValue", required=True)
        current_number, current_reference = d._control(number_control.element), d._control(reference_control.element)
        current_tab = d._control(tab)
        return (d._read(group.element, "AXTitle") == saved_title
                and d.cf.CFEqual(tab, selected)
                and current_tab.role == "AXRadioButton" and current_tab.title == saved_title
                and current_number.bounds == number_control.bounds
                and current_reference.bounds == reference_control.bounds
                and current_number.value == number and current_reference.value == reference
                and d._read(number_control.element, "AXFocused") is True)

    wait_for(same_saved_order, "exact clean saved Order context", timeout=2)
    scope = ui.scope(capture, saved_title)
    heading = ui.label(capture, "Create a follow-up document", scope=scope)
    bottom = ui.label(capture, "Items", scope=scope)[1]
    margin = heading[3] - heading[1]
    followup = (max(scope[0], heading[0] - margin), heading[3], scope[2], bottom)
    if followup[1] >= followup[3]:
        raise DesktopError("The follow-up group has no observed area before Items.")
    proof = {"source_title": saved_title, "order_number": number, "reference": reference,
             "scope": followup, "tooltip": "Create: New Invoice", "requested": False}
    action_record = {"action": "create_invoice_followup", **proof}
    ui.actions.append(action_record)
    ui.icon("invoice_new.png", size=32, scope=followup, expected_help=proof["tooltip"],
            action_guard=same_saved_order)
    proof["requested"] = True
    action_record["requested"] = True
    ui.wait_tab("New Invoice")
    return {"title": "New Invoice", "proposed_header": read_document_header(ui, title="New Invoice", invoice=True),
            "action_proof": proof}


def close_saved_document(ui, *, title, number, reference):
    """Close one exact clean saved document; never accept a Save Parts dialog."""
    capture = ui.snapshot("before-clean-document-close")
    d = ui.desktop
    group = d.find_ax(capture, role="AXTabGroup", title=title)
    selected = d._read(group.element, "AXValue", required=True)
    tab = d._control(selected)
    if tab.role != "AXRadioButton" or tab.title != title or title.startswith("*"):
        raise DesktopError("The exact clean saved document tab must be selected before Close.")
    scope = editor_scope(ui, capture, title, before="Addresses")
    identity = ui.field(capture, "No.", scope=scope)
    source_reference = ui.field(capture, "Cust.Ref.", scope=scope)
    if identity.value != number or source_reference.value != reference:
        raise DesktopError("The saved document to close has a different identity/reference.")
    d.click(capture, identity.bounds.x + identity.bounds.width / 2,
            identity.bounds.y + identity.bounds.height / 2)

    def same_document():
        return (d._read(group.element, "AXTitle") == title
                and d.cf.CFEqual(d._read(group.element, "AXValue", required=True), selected)
                and d._control(identity.element).value == number
                and d._control(source_reference.element).value == reference
                and d._read(identity.element, "AXFocused") is True)

    wait_for(same_document, "exact saved document focused before Close", timeout=2)
    d.key(capture, "cmd+w", focus_guard=same_document)

    def closed():
        fresh = ui.snapshot("after-clean-document-close", ocr=False)
        current = d._control(group.element)
        if current.title in {title, "*" + title}:
            return None
        remaining = list(d._read(current.element, "AXChildren", required=True))
        if any(d._read(element, "AXTitle") in {title, "*" + title} for element in remaining):
            raise DesktopError("The saved document tab remains after Close.")
        return {"passed": True, "closed": True, "number": number, "reference": reference,
                "remaining_editor": current.title, "save_requests": 0,
                "capture": fresh.metadata()}

    evidence = wait_for(closed, "exact saved document closed without another Save", timeout=8)
    ui.actions.append({"action": "close_saved_document", **evidence})
    return evidence


def _invoice_payment_controls(ui, capture, title):
    scope = editor_scope(ui, capture, title, after="Remarks")
    box = ui.label(capture, "paid", scope=scope)
    paid = ui.desktop.hit_test(capture, *capture.screen_point(
        (box[0] + box[2]) / 2, (box[1] + box[3]) / 2), expected_role="AXCheckBox")
    ui.require_inside_scope(capture, paid, scope)
    if paid.title != "paid" or paid.value not in (0, 1) or paid.enabled is not True:
        raise DesktopError("Invoice paid checkbox must have one readable enabled binary state.")
    method = ui.field(capture, "paid", scope=scope, roles=("AXPopUpButton",))
    if not isinstance(method.value, str) or not method.value.strip():
        raise DesktopError("Invoice payment method is unreadable.")
    return scope, paid, method


def read_invoice_payment(ui, *, title, currency="EUR"):
    """Read the current UI; the caller must retrieve a saved Invoice for persistence."""
    capture = ui.snapshot("invoice-payment-readback")
    scope, paid, method = _invoice_payment_controls(ui, capture, title)
    result = {"method": method.value, "paid": bool(paid.value), "date": None, "amount": None}
    if paid.value:
        result["date"] = read_field(ui, capture, "at", scope=scope)
        if not isinstance(result["date"], str) or not result["date"].strip():
            raise DesktopError("Paid Invoice has no readable payment date.")
        amount = read_field(ui, capture, "Value", scope=scope)
        result["amount"] = f"{parse_display_money(amount, currency=currency):.2f}"
    result["evidence"] = {"capture": capture.metadata(), "paid_state": paid.value,
                          "paid_fields_visible": bool(paid.value),
                          "limitation": "Hidden unpaid date/value storage is not observed." if not paid.value else None}
    return result


def set_invoice_payment(ui, payment, *, title="New Invoice", currency="EUR"):
    """Choose method first, then set paid/source date/full value and read back."""
    if not isinstance(payment, Payment) or not payment.method.strip() or currency != "EUR":
        raise ValueError("Invoice input requires a checked Payment and supported EUR currency.")
    if payment.paid and (payment.date is None or payment.amount is None
                         or not payment.amount.is_finite() or payment.amount < 0
                         or payment.amount != payment.amount.quantize(Decimal("0.01"))):
        raise ValueError("Paid Invoice requires a checked source date and exact full EUR amount.")
    capture = ui.snapshot("before-invoice-payment")
    _, _, method = _invoice_payment_controls(ui, capture, title)
    if method.value != payment.method:
        ui.choose_control(capture, method, payment.method, label="Invoice payment method")
    capture = ui.snapshot("before-invoice-paid-state")
    _, paid, method = _invoice_payment_controls(ui, capture, title)
    if method.value != payment.method:
        raise DesktopError("Invoice payment method differs after selection.")
    wanted = int(payment.paid)
    if paid.value != wanted:
        current = ui.desktop._control(paid.element)
        if current.bounds != paid.bounds or current.title != "paid" or current.value != paid.value:
            raise DesktopError("Invoice paid checkbox changed before its observed click.")
        ui.desktop.click(capture, paid.bounds.x + paid.bounds.width / 2,
                         paid.bounds.y + paid.bounds.height / 2)
        wait_for(lambda: ui.desktop._read(paid.element, "AXValue") == wanted,
                 "Invoice paid checkbox readback", timeout=2)
    if payment.paid:
        capture = ui.snapshot("before-source-payment-date")
        scope, _, _ = _invoice_payment_controls(ui, capture, title)
        ui.set_text("at", payment.date.strftime("%b %d, %Y"), scope=scope)
        capture = ui.snapshot("before-full-payment-value")
        scope, _, _ = _invoice_payment_controls(ui, capture, title)
        ui.set_text("Value", f"{payment.amount:.2f}", scope=scope,
                    expected=euro_display(payment.amount, currency=currency))
    result = read_invoice_payment(ui, title=title, currency=currency)
    expected = {"method": payment.method, "paid": payment.paid,
                "date": payment.date.strftime("%b %d, %Y") if payment.paid else None,
                "amount": f"{payment.amount:.2f}" if payment.paid else None}
    if {key: result[key] for key in expected} != expected:
        raise DesktopError("Invoice payment fields differ from the source after input.")
    return result


def open_product_selector(ui, *, title):
    """Open the scoped selector once, then observe its owned focused modal.

    Setup disables automatic acceptance of one filtered result. Waiting here
    only observes modal readiness and never repeats the icon input.
    """
    capture = ui.snapshot("before-order-product-selector")
    scope = editor_scope(ui, capture, title, after="Items", before="Remarks")
    ui.icon("product_list.png", scope=scope,
            expected_help="Pick an item from the list of all products")
    d = ui.desktop

    def modal_ready():
        d._require_focus()
        windows = d.windows()
        matches = [window for window in windows if window.title == 'Select a product']
        if len(matches) > 1 or any(window.title not in {'', ui.title, 'Select a product'} for window in windows):
            raise DesktopError('Product selection has ambiguous or unexpected owned windows.')
        if not matches:
            return None
        code, pid = d.ax.AXUIElementGetPid(matches[0].element, None)
        if code != d.ax.kAXErrorSuccess or int(pid) != d.pid:
            raise DesktopError('The Product selector window is not owned by the retained Fakturama process.')
        d._focused_window(matches[0])
        return matches[0]

    modal = wait_for(modal_ready, 'exact owned focused Select a product window after one icon click', timeout=8)
    proof = {'action': 'product_selector_ready', 'modal_verified': True,
             'modal_title': modal.title, 'modal_bounds': modal.bounds.as_dict(),
             'process_identity': list(d.identity)}
    ui.actions.append(proof)
    return proof
