"""Prepare the current one/two-row prefix of a two-row source document."""

import json
from uuid import uuid4

import numpy as np

from .desktop import DesktopError
from .document_content import _row_coverage
from .native_items import _observe_item, _painted_cell_box, _vat_source, read_item_cell


def _cells(observed):
    result = {}
    for label in ('Qty.', 'Item No.', 'VAT'):
        cell, rules = _painted_cell_box(
            observed.capture.image, observed.boxes[label], observed.row_band,
            observed.table_box, observed.scope, header_tokens=observed.header_tokens)
        result[label] = {'cell': cell, 'width': cell[2] - cell[0] + 2, 'rules': rules}
    return result


def _background(observed, cell):
    pixels = np.asarray(observed.capture.image.crop(cell).convert('RGB'))
    return np.median(pixels.reshape(-1, 3), axis=0).tolist()


def _guard_source(ui, before, current, document_guard):
    d = ui.desktop
    a, b = before.capture, current.capture
    table = d._control(before.table.element)
    if (a.process_identity != b.process_identity or a.window_bounds != b.window_bounds
            or not d.cf.CFEqual(a.window_element, b.window_element)
            or not d.cf.CFEqual(before.table.element, current.table.element)
            or table.role != 'AXScrollArea' or current.table.role != 'AXScrollArea'
            or table.bounds != before.table.bounds or current.table.bounds != before.table.bounds
            or before.row_band != current.row_band or before.scope != current.scope
            or before.table_box != current.table_box):
        raise DesktopError('VAT presentation source window, table or row geometry changed.')
    d.guard(current.capture)
    if document_guard() is not True:
        raise DesktopError('VAT presentation source document changed.')


def prepare_document_presentation(ui, order, *, title, expected_row_count=2):
    result = {"passed": False, "drag_requested": False, "deselection_requested": False, "save_requests": 0,
              "expected_row_count": expected_row_count}
    path = ui.evidence_dir / ("document-presentation-" + uuid4().hex[:12] + ".json")
    result["report"] = str(path)
    try:
        if len(order.items) != 2:
            raise DesktopError("The demonstrated VAT presentation supports exactly two source rows.")
        if type(expected_row_count) is not int or expected_row_count not in (1, 2):
            raise DesktopError("VAT presentation requires a one/two-row current source prefix.")
        result['coverage'] = _row_coverage(ui, title, expected_row_count)
        result['native_sku'] = read_item_cell(
            ui, row_index=0, expected_sku=order.items[0].sku, header='Item No.', title=title)
        if result['native_sku'] != order.items[0].sku:
            raise DesktopError('VAT presentation native SKU differs from the source occurrence.')
        before = _observe_item(ui, 'VAT', title, 0)
        original = _cells(before)
        width = original['VAT']['width']
        result.update(width_before=width, before_capture=before.capture.metadata(),
                      geometry_before=original)
        if width == 150:
            document_guard, document = _vat_source(ui, before.capture, title)
            result['source_document'] = document
            _guard_source(ui, before, before, document_guard)
            cell = original['Item No.']['cell']
            selected = _background(before, cell)
            result['selected_background_before'] = selected
            if selected[2] < 225 or selected[2] - max(selected[:2]) <= 25:
                raise DesktopError('VAT resize requires the observed blue selected SKU row before deselection.')
            # NatTable applies a header resize to all fully selected columns.
            # One Command-click removes the proved row selection before dragging.
            x, y = (cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2
            _guard_source(ui, before, before, document_guard)
            result['deselection_requested'] = True
            result['deselection_method'] = 'one guarded Command-click on the selected Item No. body'
            ui.desktop.click(before.capture, *before.capture.screen_point(x, y), command=True)
            deselected = _observe_item(ui, 'VAT', title, 0)
            _guard_source(ui, before, deselected, document_guard)
            ready = _cells(deselected)
            background = _background(deselected, ready['Item No.']['cell'])
            result.update(selected_background_after=background,
                          geometry_deselected=ready,
                          deselected_capture=deselected.capture.metadata())
            if background != [255.0, 255.0, 255.0]:
                raise DesktopError('VAT resize needs the proved white unselected SKU body; no drag requested.')
            # OCR caption bounds can vary by a pixel; the shared painted rules
            # establish the actual cell geometry before and after deselection.
            if (any(original[key]['cell'] != ready[key]['cell'] for key in original)
                    or ready['VAT']['width'] != 150):
                raise DesktopError('VAT deselection changed the observed columns; no drag requested.')
            geometry = ready['VAT']['rules']
            x = sum(geometry['right_rule_span']) / 2
            y = sum((deselected.boxes['VAT'][1], deselected.boxes['VAT'][3])) / 2
            _guard_source(ui, before, deselected, document_guard)
            result['drag_requested'] = True
            ui.desktop.drag(deselected.capture, *deselected.capture.screen_point(x, y),
                            *deselected.capture.screen_point(x + 150, y))
            after = _observe_item(ui, 'VAT', title, 0)
            _guard_source(ui, before, after, document_guard)
            current = _cells(after)
            final = current['VAT']['width']
            result.update(width_after=final, after_capture=after.capture.metadata(), geometry_after=current)
            vat = ready['VAT']['cell']
            if (final != 300 or current['VAT']['cell'] != (vat[0], vat[1], vat[2] + 150, vat[3])
                    or any(original[key]['cell'] != current[key]['cell']
                           for key in ('Qty.', 'Item No.'))):
                raise DesktopError('VAT-only resize did not preserve the observed identity columns.')
            result['native_sku_after'] = read_item_cell(
                ui, row_index=0, expected_sku=order.items[0].sku, header='Item No.', title=title)
            if result['native_sku_after'] != order.items[0].sku:
                raise DesktopError('VAT resize changed the full native source SKU; do not repeat the drag.')
        elif width != 300:
            raise DesktopError('Unsupported VAT presentation width; no resize requested.')
        result['passed'] = True
        return result
    except Exception as error:
        result['error'] = str(error)
        error.document_presentation_evidence = str(path)
        raise
    finally:
        path.write_text(json.dumps(result, indent=2) + '\n')
