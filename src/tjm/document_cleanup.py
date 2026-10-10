"""Close a verified document inspection without issuing another Save.

Some native item readers leave the inspected editor marked dirty. Only a
passing, durable content report permits discarding that inspection's marker.
The observed Save Parts table must name exactly that one document.
"""

from collections.abc import Mapping
import json
import os
from pathlib import Path
from uuid import uuid4

from .desktop import DesktopError, wait_for
from .ui import Ui
from .workflow import editor_scope


def _verified_content(content_evidence, number, reference):
    if not isinstance(content_evidence, Mapping):
        raise ValueError("Supply the passing document-content report mapping.")
    path = content_evidence.get("report")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("The content evidence needs its durable report path.")
    try:
        recorded = json.loads(Path(path).read_text(encoding="utf-8"))
        supplied = json.loads(json.dumps(dict(content_evidence), allow_nan=False))
    except (OSError, ValueError, TypeError) as error:
        raise DesktopError("The durable content report cannot be verified.") from error
    if recorded != supplied or recorded.get("passed") is not True:
        raise DesktopError("The supplied content evidence is not the exact passing durable report.")
    observed, expected = recorded.get("observed"), recorded.get("expected")
    if (not isinstance(observed, dict) or observed != expected
            or observed.get("number") != number
            or observed.get("external_reference") != reference):
        raise DesktopError("The verified content report identifies different document content.")
    raw = recorded.get("raw", {})
    header, final_header = raw.get("header", {}), raw.get("final_header", {})
    if (header != final_header or header.get("number") != number
            or header.get("reference") != reference):
        raise DesktopError("The verified native document headers do not identify this inspection.")
    source_capture = recorded.get("evidence", {}).get("initial_row_coverage", {}).get("capture")
    if not isinstance(source_capture, dict):
        raise DesktopError("The content report lacks its observed application/window identity.")
    return path, source_capture


def _parts(ui, capture, number):
    """Read the exact observed AXTable/AXChildren/AXRow shape, not OCR labels."""
    d = ui.desktop
    d.guard(capture)
    table = d.find_ax(capture, role="AXTable", title=None)
    children = list(d._read(table.element, "AXChildren", required=True))
    if len(children) > 8:
        raise DesktopError("Save Parts has an unsupported table-child count.")
    rows = [d._control(element) for element in children
            if d._read(element, "AXRole", required=True) == "AXRow"]
    if len(rows) != 1:
        raise DesktopError("Save Parts must contain exactly one native document row.")
    row = rows[0]
    cells = list(d._read(row.element, "AXChildren", required=True))
    if len(cells) != 2 or not table.bounds.contains_rect(row.bounds):
        raise DesktopError("Save Parts row has an unsupported native shape.")
    controls = [d._control(element) for element in cells]
    text = [control for control in controls if control.role == "AXStaticText"]
    check = [control for control in controls if control.role == "AXCheckBox"]
    if (len(text) != 1 or len(check) != 1 or text[0].value != number
            or any(not row.bounds.contains_rect(control.bounds) for control in controls)):
        raise DesktopError("Save Parts does not identify only the verified document.")
    checkbox = check[0]
    if checkbox.enabled is not True or checkbox.value not in (0, 1):
        raise DesktopError("Save Parts needs one enabled binary checkbox.")
    return table, row, text[0], checkbox


def close_verified_inspection(ui, *, title, number, reference, content_evidence):
    """Close one verified inspection; a dirty marker is discarded, never saved.

    ``content_evidence`` is the exact mapping returned by
    ``collect_document_content`` and already written at its ``report`` path.
    Failure preserves its phase and evidence; callers must inspect, not retry.
    """
    if (any(not isinstance(value, str) or not value.strip()
            for value in (title, number, reference))
            or title.startswith("*") or title in {"New Order", "New Invoice"}):
        raise ValueError("Supply the saved document's clean title, number and reference.")
    report_path, source_capture = _verified_content(content_evidence, number, reference)
    result = {"passed": False, "closed": False, "number": number, "reference": reference,
              "content_report": report_path, "save_requests": 0, "close_requests": 0,
              "phase": "verify inspected document"}
    path = ui.evidence_dir / ("close-inspection-" + uuid4().hex[:12] + ".json")
    result["report"] = str(path)
    dialog = None
    try:
        capture = ui.snapshot("before-verified-inspection-close")
        d = ui.desktop
        if (source_capture.get("pid") != d.pid
                or source_capture.get("window_title") != ui.title):
            raise DesktopError("The content report belongs to another application/window.")
        groups = []
        for candidate in (title, "*" + title):
            try:
                groups.append(d.find_ax(capture, role="AXTabGroup", title=candidate))
            except DesktopError as error:
                if not str(error).endswith("found 0."):
                    raise
        if len(groups) != 1:
            raise DesktopError("Exactly one clean or dirty inspected document group is required.")
        group = groups[0]
        selected = d._read(group.element, "AXValue", required=True)
        tab = d._control(selected)
        if (tab.role != "AXRadioButton" or tab.title != group.title
                or not group.bounds.contains_rect(tab.bounds)):
            raise DesktopError("The verified document is not the exact selected editor.")
        scope = editor_scope(ui, capture, title, before="Addresses")
        identity = ui.field(capture, "No.", scope=scope)
        source_reference = ui.field(capture, "Cust.Ref.", scope=scope)
        if identity.value != number or source_reference.value != reference:
            raise DesktopError("The current document number/reference differs from the content proof.")
        dirty = group.title == "*" + title
        result["dirty_inspection"] = dirty

        def same_document():
            current_tab = d._control(selected)
            return (d._read(group.element, "AXTitle") == group.title
                    and d.cf.CFEqual(d._read(group.element, "AXValue", required=True), selected)
                    and current_tab.role == "AXRadioButton" and current_tab.title == group.title
                    and d._control(identity.element).value == number
                    and d._control(source_reference.element).value == reference
                    and d._read(identity.element, "AXFocused") is True)

        d.click(capture, identity.bounds.x + identity.bounds.width / 2,
                identity.bounds.y + identity.bounds.height / 2)
        wait_for(same_document, "verified document identity focused before Close", timeout=2)
        result.update(phase="close requested", close_requests=1)
        d.key(capture, "cmd+w", focus_guard=same_document)
        if dirty:
            result["phase"] = "inspect Save Parts"
            wait_for(lambda: any(window.title == "Save Parts" for window in d.windows()),
                     "owned Save Parts after closing dirty inspection", timeout=8)
            dialog = Ui(d, "Save Parts", ui.evidence_dir / (path.stem + "-save-parts"),
                        text_input=ui.text_input)
            current = dialog.snapshot("before-inspection-discard", ocr=False)
            original = _parts(dialog, current, number)
            result["save_parts"] = {"row_count": 1, "text": original[2].value,
                                    "initial_checkbox_state": original[3].value,
                                    "capture": current.metadata(), "checkbox_click_requests": 0}

            def same_parts(captured, state):
                fresh = _parts(dialog, captured, number)
                if any(not d.cf.CFEqual(new.element, old.element) or new.bounds != old.bounds
                       for new, old in zip(fresh, original)):
                    raise DesktopError("The inspected Save Parts row changed before its action.")
                return fresh[3].value == state

            if original[3].value == 1:
                current = dialog.snapshot("before-clearing-inspection-checkbox", ocr=False)
                if not same_parts(current, 1):
                    raise DesktopError("Save Parts checkbox changed before its one physical click.")
                checkbox = original[3]
                x, y = (checkbox.bounds.x + checkbox.bounds.width / 2,
                        checkbox.bounds.y + checkbox.bounds.height / 2)
                result["save_parts"]["checkbox_click_requests"] = 1
                d.click(current, x, y)
                wait_for(lambda: same_parts(dialog.snapshot("inspection-checkbox-readback", ocr=False), 0),
                         "only inspected document unchecked", timeout=2)
            current = dialog.snapshot("before-discard-inspection-ok", ocr=False)
            if not same_parts(current, 0):
                raise DesktopError("Save Parts must be unchecked before OK.")
            ok = d.find_ax(current, role="AXButton", title="OK")
            result["phase"] = "unchecked Save Parts OK requested"
            d.press(current, ok, action_guard=lambda: same_parts(current, 0))
            result["save_parts"]["final_checkbox_state"] = 0

        def closed():
            if any(window.title == "Save Parts" for window in d.windows()):
                if not dirty:
                    raise DesktopError("A clean inspection unexpectedly offered Save Parts; untouched.")
                return None
            fresh = ui.snapshot("after-verified-inspection-close", ocr=False)
            current = d._control(group.element)
            remaining = list(d._read(current.element, "AXChildren", required=True))
            if current.title in {title, "*" + title} or any(
                    d._read(element, "AXTitle") in {title, "*" + title} for element in remaining):
                return None
            if not isinstance(current.title, str) or not current.title.strip():
                raise DesktopError("The remaining editor is not readable after inspection Close.")
            return {"remaining_editor": current.title, "capture": fresh.metadata()}

        result.update(wait_for(closed, "verified inspection closed without another Save", timeout=8))
        result.update(passed=True, closed=True, phase="closed without Save")
        ui.actions.append({"action": "close_verified_inspection", **result})
        return result
    except Exception as error:
        result["error"] = str(error)
        error.document_cleanup_evidence = str(path)
        raise
    finally:
        if dialog is not None:
            dialog.write_actions()
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
