"""Read/edit a visible item occurrence through its temporary native cell editor.

This is the bounded method proved in M4, not a general table reader. It does
not resize columns, establish selector absence, scroll, or save.
Visible row rules/ordinal establish occurrence; the full native SKU proves identity.
"""

from dataclasses import dataclass
import re
import time

import numpy as np
from PIL import ImageChops, ImageOps

from .desktop import DesktopError, wait_for
from .fields import find_phrases
from .ocr import ocr_tokens, token_evidence
from .ui import Ui
from .ui_tables import _no_scrollbars, _pixel_box, _table_at
from .workflow import editor_scope


HEADERS = {"Qty.", "Item No.", "Name", "Description", "VAT", "U.Price", "Discount", "Price"}
EDITABLE_TEXT = {"Qty.", "Name", "Description", "U.Price", "Discount"}
EDITOR_ROLES = {"AXTextField", "AXTextArea", "AXPopUpButton", "AXComboBox"}


@dataclass
class NativeCell:
    capture: object
    control: object
    focused: object
    header: str
    expected_sku: str
    table: object
    modal_ui: object = None
    observation: object = None
    document_guard: object = None


@dataclass(frozen=True)
class ItemObservation:
    capture: object
    table: object
    table_box: tuple
    scope: tuple
    boxes: dict
    row_band: tuple
    point: tuple
    capture_path: str
    header_tokens: tuple = ()


@dataclass(frozen=True)
class ItemVatPopup:
    popup: object
    source_guard: object
    cancel: object
    evidence: dict


def _document_tab_bounds_match(original, current):
    if current.bounds == original.bounds:
        return True
    old, new = original.bounds, current.bounds
    # Fakturama widens the same selected tab when it adds its leading dirty marker.
    # Only that title transition permits a width change; its position/height stay exact.
    return ((new.x, new.y, new.height) == (old.x, old.y, old.height)
            and ((current.title == "*" + original.title and new.width > old.width)
                 or (original.title.startswith("*")
                     and current.title == original.title[1:] and new.width < old.width)))


def _vat_source(ui, capture, title):
    """Retain exact native document identity before its popup owns focus."""
    d = ui.desktop
    header = editor_scope(ui, capture, title, before="Addresses")
    fields = {label: ui.field(capture, label, scope=header)
              for label in ("No.", "Cust.Ref.")}
    if any(not isinstance(control.value, str) or not control.value.strip()
           for control in fields.values()):
        raise DesktopError("VAT read needs a complete native document number and reference.")
    try:
        group = d.find_ax(capture, role="AXTabGroup", title=title)
    except DesktopError as error:
        if not str(error).endswith("found 0."):
            raise
        group = d.find_ax(capture, role="AXTabGroup", title="*" + title)
    selected = d._read(group.element, "AXValue", required=True)
    tab = d._control(selected)
    if (tab.role != "AXRadioButton" or tab.title != group.title
            or tab.enabled is not True or not group.bounds.contains_rect(tab.bounds)):
        raise DesktopError("VAT read's exact document tab is not selected.")

    def guard():
        d._require_focus()
        window = d.window(ui.title)
        current_group, current_tab = d._control(group.element), d._control(tab.element)
        if (d.identity != capture.process_identity or window.bounds != capture.window_bounds
                or not d.cf.CFEqual(window.element, capture.window_element)
                or current_group.role != "AXTabGroup" or current_group.bounds != group.bounds
                or current_group.title not in {title, "*" + title}
                or current_tab.role != "AXRadioButton"
                or not _document_tab_bounds_match(tab, current_tab)
                or current_tab.title != current_group.title or current_tab.enabled is not True
                or not current_group.bounds.contains_rect(current_tab.bounds)
                or not d.cf.CFEqual(d._read(group.element, "AXValue", required=True), tab.element)):
            return False
        for original in fields.values():
            current = d._control(original.element)
            if (current.role != original.role or current.bounds != original.bounds
                    or current.title != original.title or current.value != original.value):
                return False
        return True

    if guard() is not True:
        raise DesktopError("VAT source document changed during identity discovery.")
    return guard, {"document_number": fields["No."].value,
                   "reference": fields["Cust.Ref."].value,
                   "selected_tab_title": tab.title,
                   "parent_window": capture.metadata()}


def _vat_popup(ui, capture, point, table, expected_sku, row_index, source):
    """Single-click the proved VAT occurrence; never choose or commit an option."""
    from .item_vat import _only_child, _popup_guard

    d = ui.desktop
    document_guard, evidence, sku_table = source
    if (not d.cf.CFEqual(table.element, sku_table.element) or table.bounds != sku_table.bounds):
        raise DesktopError("VAT popup candidate is not in the table of the verified source SKU.")
    before = d.windows()
    d.guard(capture)
    if document_guard() is not True:
        raise DesktopError("VAT source document changed before opening its popup.")
    d.click(capture, *point)

    def popup_ready():
        d._require_focus()
        focused = d._read(d.root, "AXFocusedWindow", required=True)
        if d.cf.CFEqual(focused, capture.window_element):
            return None
        windows = d.windows()
        matches = [window for window in windows if d.cf.CFEqual(window.element, focused)]
        if (len(matches) != 1 or matches[0].title != ""
                or any(d.cf.CFEqual(focused, old.element) for old in before)
                or not capture.window_bounds.contains_rect(matches[0].bounds)):
            raise DesktopError("VAT click did not open one new owned unnamed popup inside its parent.")
        popup = matches[0]
        context = ItemVatPopup(popup, source_guard, None, {})
        _popup_guard(d, context)
        scroll = _only_child(d, d._control(popup.element), "AXScrollArea")
        popup_table = _only_child(d, scroll, "AXTable")
        if popup_table.enabled is not True:
            raise DesktopError("VAT click's native popup table is not enabled.")
        return popup

    def source_guard():
        current = d._control(table.element)
        return (document_guard() is True and current.role == "AXScrollArea"
                and current.bounds == table.bounds)

    popup = wait_for(popup_ready, "exact VAT popup after one click", timeout=2)
    evidence = {**evidence, "sku": expected_sku, "row_index": row_index,
                "method": "visible row ordinal, exact native SKU and one VAT-cell click",
                "popup_bounds": popup.bounds.as_dict(), "item_table_bounds": table.bounds.as_dict()}
    cancelled = False
    context = None

    def cancel():
        nonlocal cancelled
        if cancelled:
            raise DesktopError("This exact VAT popup has already received its one Escape.")
        _popup_guard(d, context)
        # Set before dispatch: even an uncertain result must not be retried.
        cancelled = True
        d.dismiss_popup(popup.element)

        def parent_ready():
            d._require_focus()
            if not d.cf.CFEqual(d._read(d.root, "AXFocusedWindow", required=True),
                                capture.window_element):
                return None
            if source_guard() is not True:
                raise DesktopError("Source document changed after cancelling the VAT popup.")
            if any(d.cf.CFEqual(window.element, popup.element) for window in d.windows()):
                return None
            return True

        wait_for(parent_ready, "VAT popup closed to the exact unchanged source document", timeout=2)
        ui.actions.append({"action": "cancel_native_item_vat_popup", **evidence})

    context = ItemVatPopup(popup, source_guard, cancel, evidence)
    ui.actions.append({"action": "open_native_item_vat_popup", **evidence})
    return context


def _header_boxes(tokens, requested):
    """Recognize a static clipped Description label only with both neighbors."""
    labels = list(dict.fromkeys(["Qty.", "Item No.", requested]))
    boxes = {}
    for label in labels:
        matches = find_phrases(tokens, label)
        if label == "Price":
            units = list(dict.fromkeys(find_phrases(tokens, "U.Price")
                                       + find_phrases(tokens, "U. Price")))
            if len(units) != 1:
                raise DesktopError("Standalone Price needs one complete U.Price header.")
            unit = units[0]
            matches = [box for box in matches if not (
                unit[0] <= box[0] and unit[1] <= box[1]
                and box[2] <= unit[2] and box[3] <= unit[3])]
            boxes["U.Price"] = unit
            if any(box[0] <= unit[2] for box in matches):
                raise DesktopError("Standalone Price is not after the complete U.Price header.")
        if len(matches) == 1:
            boxes[label] = matches[0]
        elif label != "Description" or matches:
            raise DesktopError(f"Native item discovery needs one complete {label!r} header.")
    interpretation = None
    if requested == "Description" and requested not in boxes:
        neighbors = {}
        for label in ("Name", "VAT"):
            matches = find_phrases(tokens, label)
            if len(matches) != 1:
                raise DesktopError("Clipped Description requires unique full Name and VAT neighbors.")
            neighbors[label] = matches[0]
        candidates = [token for token in tokens
                      if re.fullmatch(r"Descri(?:\.{2,}|…)", token.text)
                      and neighbors["Name"][2] < token.left < token.right < neighbors["VAT"][0]]
        if len(candidates) != 1:
            raise DesktopError("The clipped Description label is not uniquely established.")
        token = candidates[0]
        if any(other is not token and re.search(r"[A-Za-z0-9]", other.text)
               and neighbors["Name"][2] < other.cx < neighbors["VAT"][0]
               for other in tokens):
            raise DesktopError("Another header intervenes between Name, clipped Description and VAT.")
        boxes[requested] = (token.left, token.top, token.right, token.bottom)
        boxes.update(neighbors)
        interpretation = {"method": "static Description prefix with full Name/VAT neighbors",
                          "visible_text": token.text, "source_data_inferred": False}
    heights = sorted(box[3] - box[1] for box in boxes.values())
    glyph_height = heights[0]
    centers = [(box[1] + box[3]) / 2 for box in boxes.values()]
    if max(centers) - min(centers) > glyph_height * .75:
        raise DesktopError("Native item headers do not share one visible baseline.")
    if boxes["Qty."][2] >= boxes["Item No."][0]:
        raise DesktopError("Qty. and Item No. headers are not in their observed order.")
    return boxes, interpretation


def _same_window(desktop, previous, current):
    if (current.process_identity != previous.process_identity
            or current.window_title != previous.window_title
            or not desktop.cf.CFEqual(current.window_element, previous.window_element)
            or current.window_bounds != previous.window_bounds
            or current.screen_rect != previous.screen_rect
            or current.image.size != previous.image.size):
        raise DesktopError("Native item discovery changed window or capture geometry.")


def _description_dialog(ui, capture, point, table, expected_sku):
    """Description's observed one-click modal is separate from inline cells."""
    d = ui.desktop
    d.click(capture, *point)

    def modal_ready():
        matches = [window for window in d.windows() if window.title == "position description"]
        if len(matches) > 1:
            raise DesktopError("Multiple position description dialogs are ambiguous.")
        return bool(matches and d.cf.CFEqual(
            matches[0].element, d._read(d.root, "AXFocusedWindow", required=True)))

    wait_for(modal_ready, "focused position description dialog", timeout=2)
    dialog = Ui(d, "position description", ui.evidence_dir / f"description-{ui.sequence:04d}",
                text_input=ui.text_input)
    editing = dialog.snapshot("native-description-editor", ocr=False)
    if editing.process_identity != capture.process_identity:
        raise DesktopError("The description dialog belongs to another process.")
    # Require one TextArea regardless of title, within this exact owned dialog.
    window = d.guard(editing)
    stack, seen, matches = [(window.element, 0)], [], []
    deadline = time.monotonic() + 2
    while stack:
        element, depth = stack.pop()
        if any(d.cf.CFEqual(element, previous) for previous in seen):
            continue
        if len(seen) >= 1000 or depth > 12 or time.monotonic() >= deadline:
            raise DesktopError("Description dialog traversal is incomplete; uniqueness is unknown.")
        seen.append(element)
        if d._read(element, "AXRole") == "AXTextArea":
            matches.append(d._control(element))
        children = list(d._read(element, "AXChildren") or [])
        children += list(d._read(element, "AXContents") or [])
        stack.extend((child, depth + 1) for child in children)
    if len(matches) != 1 or not window.bounds.contains_rect(matches[0].bounds):
        raise DesktopError("Description needs one native TextArea inside its owned dialog.")
    control = matches[0]
    if not isinstance(control.value, str):
        raise DesktopError("The description TextArea has no readable text value.")
    d.click(editing, control.bounds.x + control.bounds.width / 2,
            control.bounds.y + control.bounds.height / 2)

    def focused():
        d.guard(editing)
        current = d._control(control.element)
        writable = current.enabled is True
        if current.enabled is None:
            code, settable = d.ax.AXUIElementIsAttributeSettable(control.element, "AXValue", None)
            writable = code == d.ax.kAXErrorSuccess and bool(settable)
        current_table = d._control(table.element)
        return (current.role == "AXTextArea" and current.bounds == control.bounds and writable
                and d._read(control.element, "AXFocused") is True
                and current_table.role == "AXScrollArea" and current_table.bounds == table.bounds)

    wait_for(focused, "exact description TextArea focus", timeout=2)
    ui.actions.append({"action": "open_native_description_dialog", "sku": expected_sku,
                       "dialog": "position description", "evidence_dir": str(dialog.evidence_dir)})
    return NativeCell(editing, control, focused, "Description", expected_sku, table, dialog)


def visible_row_bands(image, boxes, bottom):
    """Derive complete visible rows from shared grey rules in Qty and SKU cells."""
    start = max(boxes[label][3] for label in ("Qty.", "Item No."))
    strips = [np.asarray(image.convert("RGB"))[start:int(bottom), box[0]:box[2], :]
              for label in ("Qty.", "Item No.") for box in [boxes[label]]]
    if any(strip.size == 0 for strip in strips):
        raise DesktopError("The visible item grid has no measurable row area.")
    masks = [((strip.max(axis=2).astype(int) - strip.min(axis=2) <= 4)
              & (strip.mean(axis=2) < 225)).mean(axis=1) >= .85 for strip in strips]
    ys = np.flatnonzero(masks[0] & masks[1]) + start
    groups = []
    for y in ys.tolist():
        if groups and y == groups[-1][-1] + 1:
            groups[-1].append(y)
        else:
            groups.append([y])
    font = min(box[3] - box[1] for box in boxes.values())
    bands = [(a[-1] + 1, b[0]) for a, b in zip(groups, groups[1:])]
    if (not bands or groups[0][0] - start > font / 2
            or any(not font <= high-low <= font*3 for low, high in bands)):
        raise DesktopError("Complete visible item row boundaries are not established.")
    return bands


def _item_table_at(ui, capture, point):
    """Resolve the grid when Tab has already opened its next inline editor."""
    d = ui.desktop
    hit = d.hit_test(capture, *point)
    if hit.role not in EDITOR_ROLES:
        return _table_at(ui, capture, point)
    parent = d._control(d._read(hit.element, "AXParent", required=True))
    hops = 1
    if parent.role == "SWTComposite":
        wrapper = parent
        parent = d._control(d._read(wrapper.element, "AXParent", required=True))
        hops = 2
        if parent.bounds != wrapper.bounds:
            raise DesktopError("The inline cell's table wrapper bounds differ.")
    if parent.role != "AXScrollArea" or not parent.bounds.contains_rect(hit.bounds):
        raise DesktopError("The inline editor has no directly owned item grid parent.")
    _no_scrollbars(d, parent)
    return parent, {"hit_role": hit.role, "parent_hops": hops,
                    "inline_editor": True}


def _observe_item(ui, header, title, row_index):
    d = ui.desktop
    capture = ui.snapshot("before-native-item")
    scope = editor_scope(ui, capture, title, after="Items", before="Remarks")
    heading = ui.label(capture, "Items", scope=ui.scope(capture, title))
    height = heading[3] - heading[1]
    band = (round(scope[0]), round(heading[1] - height / 2),
            round(scope[2]), round(heading[3] + height / 2))
    original = capture.image.crop(band)
    prepared = ImageOps.grayscale(original).point(lambda value: 0 if value > 225 else 255)
    tokens = ocr_tokens(prepared, psm=11)
    # The short caption crop can turn Qty. into a different word. Give OCR the
    # surrounding Items pixels, then retain only the actual header band. Body
    # text cannot supply a caption; exact SKU and common-baseline guards remain.
    quantity_fallback = None
    if not find_phrases(tokens, "Qty."):
        context = capture.image.crop((band[0], band[1], band[2], round(scope[3])))
        context_prepared = ImageOps.grayscale(context).point(lambda value: 0 if value > 225 else 255)
        context_tokens = [token for token in ocr_tokens(context_prepared, psm=11)
                          if 0 <= token.cy < original.height]
        quantity = find_phrases(context_tokens, "Qty.")
        if len(quantity) == 1:
            box = quantity[0]
            additions = [token for token in context_tokens
                         if box[0] <= token.cx < box[2] and box[1] <= token.cy < box[3]]
            if additions and all(token.confidence >= 85 for token in additions):
                tokens += additions
                context_path = ui.evidence_dir / f"native-item-header-{ui.sequence:04d}-context.png"
                context_prepared.save(context_path)
                quantity_fallback = {"method": "same header pixels with Items OCR context",
                                     "context_crop": str(context_path),
                                     "header_band_height": original.height,
                                     "tokens": token_evidence(additions)}
    prefix = ui.evidence_dir / f"native-item-header-{ui.sequence:04d}"
    original.save(prefix.with_suffix(".png"))
    prepared.save(prefix.with_name(prefix.name + "-contrast.png"))
    local_boxes, interpretation = _header_boxes(tokens, header)
    boxes = {label: (band[0] + box[0], band[1] + box[1],
                     band[0] + box[2], band[1] + box[3])
             for label, box in local_boxes.items()}
    rows = visible_row_bands(capture.image, boxes, scope[3])
    if row_index >= len(rows):
        raise DesktopError("The requested item occurrence is not completely visible.")
    low, high = rows[row_index]
    row_y = (low + high) / 2
    box = boxes[header]
    point = capture.screen_point((box[0] + box[2]) / 2, row_y)
    table, resolution = _item_table_at(ui, capture, point)
    table_box = _pixel_box(capture, table.bounds)
    _no_scrollbars(d, table)
    # Keep the Qty column's vertical edge out of the ordinal crop. Its thin
    # rule was read as another character in the first recorded ordinal probe.
    ordinal_box = (round(table_box[0]), low, round(boxes["Qty."][0] - height), high)
    ordinal_crop = capture.image.crop(ordinal_box)
    ordinal_tokens = ocr_tokens(ordinal_crop, psm=7)
    ordinal_crop.save(prefix.with_name(prefix.name + "-ordinal.png"))
    if (len(ordinal_tokens) != 1 or ordinal_tokens[0].text != str(row_index + 1)
            or ordinal_tokens[0].confidence < 85):
        raise DesktopError("The visible row ordinal differs from the source occurrence.")
    # NatTable's reported scroll area extends below the visible Items boundary
    # in the recorded M4 layout. Guard the candidate and editing field themselves
    # inside Items; exact native SKU establishes the source row identity.
    if not (scope[0] <= (box[0] + box[2]) / 2 < scope[2]
            and scope[1] <= row_y < scope[3]):
        raise DesktopError("The native item candidate is outside the current Items scope.")
    ui.discoveries.append({"method": "visible grid rules, source ordinal and native cell",
                           "header": header, "headers": boxes,
                           "header_tokens": token_evidence(tokens),
                           "candidate_row_pixel_y": row_y,
                           "visible_row_bands": rows, "row_index": row_index,
                           "ordinal_tokens": token_evidence(ordinal_tokens),
                           "native_table": table.bounds.as_dict(),
                           "native_resolution": resolution, "interpretation": interpretation,
                           "quantity_header_fallback": quantity_fallback})
    fresh = ui.snapshot("before-native-item-open", ocr=False)
    _same_window(d, capture, fresh)
    current_table = d._control(table.element)
    if current_table.role != "AXScrollArea" or current_table.bounds != table.bounds:
        raise DesktopError("The native item table changed after header discovery.")
    before = capture.image.crop(table_box).convert("RGB")
    after = fresh.image.crop(table_box).convert("RGB")
    if ImageChops.difference(before, after).getbbox() is not None:
        raise DesktopError("The item table changed after discovery; cell open refused.")
    hit, _ = _item_table_at(ui, fresh, point)
    if not d.cf.CFEqual(hit.element, table.element) or hit.bounds != table.bounds:
        raise DesktopError("The candidate no longer belongs to the observed item table.")
    global_tokens = tuple({**token, "box": [token["box"][0] + band[0], token["box"][1] + band[1],
                                           token["box"][2] + band[0], token["box"][3] + band[1]]}
                          for token in token_evidence(tokens))
    return ItemObservation(fresh, table, table_box, scope, boxes, (low, high), point,
                           str(ui.evidence_dir / f"{ui.sequence:04d}-before-native-item-open.png"),
                           global_tokens)


def _same_occurrence(ui, original, current):
    """Require byte-identical selected-row pixels, not merely table geometry."""
    if original is None:
        raise DesktopError("The verified source item occurrence has no retained observation.")
    d = ui.desktop
    _same_window(d, original.capture, current.capture)
    if (not d.cf.CFEqual(original.table.element, current.table.element)
            or original.table.bounds != current.table.bounds
            or original.row_band != current.row_band
            or original.table_box != current.table_box
            or original.scope != current.scope):
        raise DesktopError("The verified source item occurrence changed before target opening.")
    low, high = original.row_band
    box = (round(original.table_box[0]), low, round(original.table_box[2]), high)
    before = original.capture.image.crop(box).convert("RGB")
    after = current.capture.image.crop(box).convert("RGB")
    if ImageChops.difference(before, after).getbbox() is not None:
        raise DesktopError("The verified source item row changed before target opening; input refused.")


def _open(ui, header, expected_sku, title, expected_role, row_index, *, vat_source=None,
          occurrence=None, document_guard=None):
    observed = _observe_item(ui, header, title, row_index)
    d = ui.desktop
    if occurrence is not None:
        _same_occurrence(ui, occurrence, observed)
    if document_guard is not None and document_guard() is not True:
        raise DesktopError("The exact source document changed before item opening.")
    fresh, table, scope, point = (observed.capture, observed.table,
                                  observed.scope, observed.point)
    if header == "Description":
        cell = _description_dialog(ui, fresh, point, table, expected_sku)
        original_focus = cell.focused
        cell.focused = lambda: (original_focus() and (
            document_guard is None or document_guard() is True))
        cell.observation, cell.document_guard = observed, document_guard
        return cell
    if vat_source is not None:
        if header != "VAT":
            raise ValueError("The single-click popup branch supports only VAT.")
        return _vat_popup(ui, fresh, point, table, expected_sku, row_index, vat_source)
    d.double_click(fresh, *point)
    editing = ui.snapshot("native-item-editor", ocr=False)
    _same_window(d, fresh, editing)
    control = d.hit_test(editing, *point, expected_role=expected_role)
    if control.role not in EDITOR_ROLES or not table.bounds.contains_rect(control.bounds):
        raise DesktopError("The native cell editor has an unsupported role or lies outside its table.")
    ui.require_inside_scope(editing, control, scope)

    def focused():
        d.guard(editing)
        current = d._control(control.element)
        owned_table = d._control(table.element)
        return (current.role == control.role and current.bounds == control.bounds
                and current.enabled is True and d._read(control.element, "AXFocused") is True
                and owned_table.role == "AXScrollArea" and owned_table.bounds == table.bounds
                and (document_guard is None or document_guard() is True))

    wait_for(focused, "exact native item editor focus", timeout=2)
    return NativeCell(editing, control, focused, header, expected_sku, table,
                      observation=observed, document_guard=document_guard)


def _painted_cell_box(image, header_box, row_band, table_box, scope, *, header_tokens=()):
    """Bound one column by continuous grey rules across this complete row."""
    low, high = row_band
    left, right = max(round(table_box[0]), round(scope[0])), min(round(table_box[2]), round(scope[2]))
    if not (0 <= left < right <= image.width and 0 <= low < high <= image.height):
        raise DesktopError("Painted item row/table geometry is outside its captured scope.")
    strip = np.asarray(image.convert("RGB"))[low:high, left:right, :].astype(int)
    grey = (strip.max(axis=2) - strip.min(axis=2) <= 4) & (strip.mean(axis=2) < 225)
    columns = np.flatnonzero(grey.mean(axis=0) >= .85) + left
    groups = []
    for column in columns.tolist():
        if groups and column == groups[-1][-1] + 1:
            groups[-1].append(column)
        else:
            groups.append([column])
    candidates = [(a, b) for a, b in zip(groups, groups[1:])
                  if a[-1] < header_box[0] < header_box[2] < b[0]]
    if len(candidates) != 1:
        raise DesktopError("Painted item needs two unique shared column rules enclosing its complete header.")
    a, b = candidates[0]
    # Match ui_table's row inset: the selected cell's dashed focus outline can
    # remain inside the shared grey row rules and otherwise obscure OCR.
    box = (a[-1] + 1, low + 2, b[0], high - 2)
    for token in header_tokens:
        text_box = token["box"]
        inside_caption = (header_box[0] <= text_box[0] and header_box[1] <= text_box[1]
                          and text_box[2] <= header_box[2] and text_box[3] <= header_box[3])
        if (not inside_caption and re.search(r"[A-Za-z0-9]", token["text"])
                and box[0] < (text_box[0] + text_box[2]) / 2 < box[2]):
            raise DesktopError("A missing column rule would merge another header into the painted item cell.")
    if box[2] - box[0] < 5 or box[3] - box[1] < 5:
        raise DesktopError("The complete painted item cell is too small to read.")
    return box, {"left_rule_span": [a[0], a[-1] + 1],
                 "right_rule_span": [b[0], b[-1] + 1],
                 "method": "neutral grey vertical rules shared across at least 85% of the current row"}


def _vat_arrow_resource(desktop):
    """Read ComboBoxPainter's down_2 resource from this connected application."""
    import hashlib
    from io import BytesIO
    from pathlib import Path
    from zipfile import BadZipFile, ZipFile
    from PIL import Image

    bundle = Path(desktop.identity[2])
    jars = list((bundle / "Contents/Eclipse/plugins").glob("org.eclipse.nebula.widgets.nattable.core_*.jar"))
    if len(jars) != 1:
        raise DesktopError("VAT arrow needs one NatTable core jar in the exact connected application.")
    resource = "org/eclipse/nebula/widgets/nattable/images/down_2.png"
    try:
        with ZipFile(jars[0]) as archive:
            data = archive.read(resource)
        with Image.open(BytesIO(data)) as icon:
            if icon.format != "PNG" or not (1 <= icon.width <= 32 and 1 <= icon.height <= 32):
                raise DesktopError("The connected VAT arrow resource has unsupported image dimensions.")
            dimensions = [icon.width, icon.height]
    except (OSError, KeyError, BadZipFile) as error:
        raise DesktopError("The connected VAT arrow resource is unreadable.") from error
    return {"jar": str(jars[0]), "resource": resource, "logical_dimensions": dimensions,
            "resource_sha256": hashlib.sha256(data).hexdigest(),
            "source": "NatTable ComboBoxPainter: RIGHT ImagePainter(GUIHelper.getImage('down_2'))"}


def _vat_text_box(desktop, capture, cell_box):
    """Exclude only the source-sized right slot with its observed dark triangle."""
    resource = _vat_arrow_resource(desktop)
    width = round(resource["logical_dimensions"][0] * capture.scale_x)
    height_limit = round(resource["logical_dimensions"][1] * capture.scale_y)
    left, top, right, bottom = cell_box
    if width < 5 or height_limit < 3 or right-left-width < 5:
        raise DesktopError("VAT arrow slot leaves no complete readable text area.")
    slot_box = (right-width, top, right, bottom)
    slot = np.asarray(capture.image.crop(slot_box).convert("RGB"), dtype=int)
    background = np.median(slot.reshape(-1, 3), axis=0)
    if (background[2] < 225 or background[2] - max(background[0], background[1]) <= 25):
        raise DesktopError("VAT arrow separation requires the observed blue selected-row background.")
    mask = slot.max(axis=2) < 225
    ys, xs = np.where(mask)
    if not len(xs):
        raise DesktopError("The VAT right slot has no observed dark dropdown arrow.")
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()+1), int(ys.min()), int(ys.max()+1)
    if (not (0 < x0 < x1 < width and 0 < y0 < y1 < bottom-top)
            or not (3 <= y1-y0 <= height_limit and y1-y0 <= x1-x0 <= width-2)):
        raise DesktopError("VAT dropdown glyph is clipped or outside its source-sized arrow footprint.")
    row_widths, centers = [], []
    for row in mask[y0:y1]:
        pixels = np.flatnonzero(row)
        if not len(pixels) or pixels[-1]-pixels[0]+1 != len(pixels):
            raise DesktopError("VAT dropdown glyph is not one contiguous triangle.")
        row_widths.append(len(pixels))
        centers.append(float((pixels[0]+pixels[-1])/2))
    maximum = max(row_widths)
    first, last = row_widths.index(maximum), len(row_widths)-1-row_widths[::-1].index(maximum)
    if (max(centers)-min(centers) > 1 or last >= len(row_widths)-2
            or any(a > b for a, b in zip(row_widths[:first], row_widths[1:first+1]))
            or any(value != maximum for value in row_widths[first:last+1])
            or any(a <= b for a, b in zip(row_widths[last:], row_widths[last+1:]))
            or row_widths[-1] > maximum/2):
        raise DesktopError("The VAT right-slot glyph does not form the observed downward triangle.")
    return (left, top, right-width, bottom), {
        "resource": resource, "slot_box": list(slot_box), "slot_width_pixels": width,
        "selected_background_rgb": background.tolist(),
        "arrow_glyph_box": [slot_box[0]+x0, top+y0, slot_box[0]+x1, top+y1],
        "arrow_row_widths": row_widths, "arrow_row_centers_in_slot": centers,
        "method": "source icon width and current capture scale, verified observed downward triangle"}


def read_painted_item_cell(ui, *, row_index=0, expected_sku, header, title="New Order"):
    """Read VAT or calculated Price pixels; target cell receives no input.

    Existing native SKU open/Escape proves the source occurrence. This helper
    never opens, selects or edits the painted VAT/Price target, resizes columns,
    retries a clipped read, or derives a value from arithmetic.
    """
    from .ui_table import _cell

    if header not in {"VAT", "Price"}:
        raise ValueError("Painted item reads support VAT or calculated Price only.")
    if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
        raise ValueError("A nonnegative source row index is required.")
    if not isinstance(expected_sku, str) or not expected_sku or "\x00" in expected_sku:
        raise ValueError("A nonempty exact source SKU is required.")
    capture = ui.snapshot("before-painted-item-identity")
    document_guard, document = _vat_source(ui, capture, title)
    sku = open_item_cell(ui, row_index=row_index, expected_sku=expected_sku,
                         header="Item No.", title=title)
    d = ui.desktop

    def exact_sku():
        return sku.focused() and d._control(sku.control.element).value == expected_sku

    if not exact_sku():
        raise DesktopError("The native SKU changed before painted-cell observation.")
    d.key(sku.capture, "escape", focus_guard=exact_sku)
    observed = _observe_item(ui, header, title, row_index)
    if (not d.cf.CFEqual(observed.table.element, sku.table.element)
            or observed.table.bounds != sku.table.bounds or document_guard() is not True):
        raise DesktopError("Painted item is not in the unchanged document/table of the verified source SKU.")
    _same_occurrence(ui, sku.observation, observed)
    evidence = {"header": header, "sku": expected_sku, "native_sku": sku.control.value,
                "row_index": row_index, "document": document,
                "capture": observed.capture.metadata(), "capture_path": observed.capture_path,
                "header_box": list(observed.boxes[header]), "row_band": list(observed.row_band),
                "table_bounds": observed.table.bounds.as_dict(), "target_input_requests": 0}
    record = {"action": "read_painted_item_cell", "verified": False, "evidence": evidence}
    ui.actions.append(record)
    try:
        box, geometry = _painted_cell_box(observed.capture.image, observed.boxes[header],
                                         observed.row_band, observed.table_box, observed.scope,
                                         header_tokens=observed.header_tokens)
        evidence["column_geometry"] = geometry
        evidence["cell_box"] = list(box)
        text_box = box
        if header == "VAT":
            full_path = ui.evidence_dir / f"painted-item-{ui.sequence:04d}-VAT-full.png"
            observed.capture.image.crop(box).save(full_path)
            evidence["full_cell_crop"] = str(full_path)
            text_box, arrow = _vat_text_box(d, observed.capture, box)
            arrow_path = ui.evidence_dir / f"painted-item-{ui.sequence:04d}-VAT-arrow.png"
            observed.capture.image.crop(arrow["slot_box"]).save(arrow_path)
            evidence["arrow"] = {**arrow, "slot_crop": str(arrow_path)}
        evidence["text_crop_box"] = list(text_box)
        text, reading = _cell(observed.capture.image, text_box,
                              ui.evidence_dir / f"painted-item-{ui.sequence:04d}-{header}.png")
        evidence["cell_readback"] = reading
        fresh = ui.snapshot("after-painted-item-read", ocr=False)
        _same_window(d, observed.capture, fresh)
        current = d._control(observed.table.element)
        if (current.role != "AXScrollArea" or current.bounds != observed.table.bounds
                or document_guard() is not True):
            raise DesktopError("The source document/item table changed during painted-cell readback.")
        before = observed.capture.image.crop(observed.table_box).convert("RGB")
        after = fresh.image.crop(observed.table_box).convert("RGB")
        if ImageChops.difference(before, after).getbbox() is not None:
            raise DesktopError("The item pixels changed during painted-cell readback.")
        d.guard(fresh)
        record.update(verified=True, text=text)
        return {"text": text, "evidence": evidence}
    except DesktopError as error:
        if hasattr(error, "table_cell_evidence"):
            evidence["cell_readback"] = error.table_cell_evidence
        error.painted_item_evidence = evidence
        record["error"] = str(error)
        raise


def open_item_vat_popup(ui, *, row_index=0, expected_sku, title="New Order"):
    """Observe one VAT popup after proving its source document and native SKU.

    The returned cancellation dispatches one Escape only to that exact popup.
    Selected VAT readback remains item_vat's responsibility; choices prove none.
    """
    sku = open_item_cell(ui, row_index=row_index, expected_sku=expected_sku,
                         header="Item No.", title=title)
    d = ui.desktop
    d.key(sku.capture, "escape", focus_guard=sku.focused)
    capture = ui.snapshot("vat-source-document")
    guard, evidence = _vat_source(ui, capture, title)
    return _open(ui, "VAT", expected_sku, title, "AXPopUpButton", row_index,
                 vat_source=(guard, evidence, sku.table),
                 occurrence=sku.observation, document_guard=guard)


def open_item_cell(ui, *, row_index=0, expected_sku, header, title="New Order",
                   expected_role="AXTextField"):
    """Open a cell after exact native SKU readback; caller must close it.

    ``row_index`` is zero-based and must be fully visible. Description opens its
    explicit modal TextArea; other cells use the requested inline editor role.
    """
    if isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0:
        raise ValueError("A nonnegative source row index is required.")
    if not isinstance(expected_sku, str) or not expected_sku or "\x00" in expected_sku:
        raise ValueError("A nonempty exact source SKU is required.")
    if header not in HEADERS or expected_role not in EDITOR_ROLES:
        raise ValueError("Unsupported item header or native editor role.")
    document_capture = ui.snapshot("native-item-source-document")
    document_guard, _ = _vat_source(ui, document_capture, title)
    sku_cell = _open(ui, "Item No.", expected_sku, title, "AXTextField", row_index,
                     document_guard=document_guard)
    if sku_cell.control.value != expected_sku:
        ui.desktop.key(sku_cell.capture, "escape", focus_guard=sku_cell.focused)
        raise DesktopError("The row's full native SKU differs from the requested source item.")
    ui.desktop.key(sku_cell.capture, "escape", focus_guard=sku_cell.focused)
    # The initial opening can change which row is selected. Reopen the exact
    # SKU once from that selected painted-row baseline, then carry its complete
    # row pixels into target discovery. No colors, text, or unknown pixels are
    # masked to accommodate selection changes.
    sku_cell = _open(ui, "Item No.", expected_sku, title, "AXTextField", row_index,
                     document_guard=document_guard)
    if sku_cell.control.value != expected_sku:
        ui.desktop.key(sku_cell.capture, "escape", focus_guard=sku_cell.focused)
        raise DesktopError("The row's full native SKU changed while establishing its selected occurrence.")
    if header == "Item No.":
        return sku_cell
    ui.desktop.key(sku_cell.capture, "escape", focus_guard=sku_cell.focused)
    cell = _open(ui, header, expected_sku, title, expected_role, row_index,
                 occurrence=sku_cell.observation, document_guard=document_guard)
    if (not ui.desktop.cf.CFEqual(cell.table.element, sku_cell.table.element)
            or cell.table.bounds != sku_cell.table.bounds):
        raise DesktopError("The target cell is not in the same table as the verified native SKU.")
    return cell


def read_item_cell(ui, **kwargs):
    """Read exact text; Description closes with Cancel, inline cells Escape."""
    cell = open_item_cell(ui, **kwargs)
    if not cell.focused():
        raise DesktopError("The item cell lost exact focus before readback.")
    observed = ui.desktop._control(cell.control.element).value
    if ((cell.modal_ui is None and cell.control.role != "AXTextField")
            or not isinstance(observed, str)):
        raise DesktopError("Native item readback currently requires an AXTextField string.")
    if cell.modal_ui is not None:
        try:
            cell.modal_ui.press("Cancel", action_guard=cell.focused)
            ui.wait_tab(kwargs.get("title", "New Order"))
        finally:
            cell.modal_ui.write_actions()
    else:
        ui.desktop.key(cell.capture, "escape", focus_guard=cell.focused)
    ui.actions.append({"action": "read_native_item_cell", "header": cell.header,
                       "sku": cell.expected_sku, "row_index": kwargs.get("row_index", 0), "observed": observed})
    return observed


def set_item_cell(ui, *, value, expected_reopened, **kwargs):
    """Commit once (Description OK, inline Tab), then verify by reopening."""
    header = kwargs.get("header")
    if header not in EDITABLE_TEXT:
        raise ValueError("This helper writes only supported source text cells, never SKU or computed Price.")
    if any(not isinstance(text, str) or "\x00" in text for text in (value, expected_reopened)):
        raise ValueError("Input and expected reopened text must be strings without null characters.")
    cell = open_item_cell(ui, **kwargs)
    if cell.modal_ui is None and cell.control.role != "AXTextField":
        raise DesktopError("Native text editing currently requires AXTextField.")
    d = ui.desktop
    record = {"action": "set_native_item_cell", "header": header,
              "sku": cell.expected_sku, "row_index": kwargs.get("row_index", 0), "before": cell.control.value,
              "requested": value, "expected_reopened": expected_reopened,
              "commit_requested": False, "committed_value_verified": False,
              "commit_action": "OK" if cell.modal_ui is not None else "Tab"}
    ui.actions.append(record)
    if cell.modal_ui is not None:
        d.guard(cell.capture)
        code, settable = d.ax.AXUIElementIsAttributeSettable(cell.control.element, "AXValue", None)
        if code != d.ax.kAXErrorSuccess or not settable or not cell.focused():
            raise DesktopError("Description's exact UI value is not writable.")
        code = d.ax.AXUIElementSetAttributeValue(cell.control.element, "AXValue", value)
        if code != d.ax.kAXErrorSuccess:
            raise DesktopError(f"Description UI value input failed: {code}.")
        record["input_method"] = "exposed Description UI value"
    else:
        d.key(cell.capture, "cmd+a", focus_guard=cell.focused)
        ui.enter_text(cell.capture, value, focus_guard=cell.focused)
        record["input_method"] = ui.text_input

    def exact():
        return cell.focused() and d._control(cell.control.element).value == value

    wait_for(exact, "exact source item value before commit", timeout=2)
    record["commit_requested"] = True
    if cell.modal_ui is not None:
        try:
            cell.modal_ui.press("OK", action_guard=exact)
            ui.wait_tab(kwargs.get("title", "New Order"))
        finally:
            cell.modal_ui.write_actions()
    else:
        d.key(cell.capture, "tab", focus_guard=exact)
    record["observed_reopened"] = read_item_cell(ui, **kwargs)
    if record["observed_reopened"] != expected_reopened:
        raise DesktopError("The committed item cell differs after reopening; do not repeat the input blindly.")
    record["committed_value_verified"] = True
    return record
