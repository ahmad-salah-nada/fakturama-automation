"""Prove row deselection precedes one VAT-only resize without retries or Saves."""

import json
from copy import copy
from types import SimpleNamespace

from PIL import Image
import pytest

from tjm import document_presentation as module
from tjm.desktop import DesktopError


@pytest.fixture
def scene(monkeypatch, tmp_path):
    calls = []
    scene = SimpleNamespace(
        calls=calls, widths=[150, 150, 300],
        colors=[(185, 215, 253), (255, 255, 255), (255, 255, 255)],
        observed=[], mutate=lambda index, observed: None, document_ok=True,
        native_values=['SKU', 'SKU'])
    table = SimpleNamespace(element='table', bounds=('native-table-bounds',), role='AXScrollArea')
    scene.live_table = copy(table)
    scene.order = SimpleNamespace(items=[SimpleNamespace(sku='SKU'), SimpleNamespace(sku='OTHER')])
    boxes = {'VAT': (620, 2, 645, 15), 'Qty.': (20, 2, 40, 15), 'Item No.': (170, 2, 220, 15)}
    painted = {}

    def observe(*args):
        calls.append('observe')
        index = len(scene.observed)
        width = scene.widths.pop(0)
        image = Image.new('RGB', (1000, 100), scene.colors.pop(0))
        capture = SimpleNamespace(
            image=image, metadata=lambda: {'phase': index}, screen_point=lambda x, y: (x, y),
            process_identity=('process',), window_bounds=('window-bounds',), window_element='window')
        cells = {
            'Qty.': {'cell': (10, 22, 158, 42), 'width': 150},
            'Item No.': {'cell': (160, 22, 308, 42), 'width': 150},
            'VAT': {'cell': (610, 22, 610 + width - 2, 42), 'width': width}}
        observed = SimpleNamespace(
            capture=capture, boxes=boxes.copy(), row_band=(20, 44), table_box=(0, 15, 1000, 100),
            scope=(0, 15, 1000, 100), header_tokens=(), table=copy(table), cells=cells)
        scene.mutate(index, observed)
        scene.observed.append(observed)
        painted[id(image)] = observed
        return observed

    def geometry(image, box, *args, **kwargs):
        observed = painted[id(image)]
        label = next(label for label, candidate in observed.boxes.items() if candidate == box)
        cell = observed.cells[label]['cell']
        return cell, {'left_rule_span': [cell[0] - 2, cell[0]],
                      'right_rule_span': [cell[2], cell[2] + 2]}

    def native(*args, **kwargs):
        calls.append('native-sku')
        return scene.native_values.pop(0)

    def click(capture, x, y, *, command=False):
        assert command is True
        assert capture is scene.observed[0].capture
        assert (x, y) == (234, 32)
        calls.append('command-click')

    def drag(capture, x0, y0, x1, y1):
        assert capture is scene.observed[1].capture
        assert (x0, y0, x1, y1) == (759, 8.5, 909, 8.5)
        calls.append('drag')

    scene.ui = SimpleNamespace(evidence_dir=tmp_path, desktop=SimpleNamespace(
        drag=drag, click=click, cf=SimpleNamespace(CFEqual=lambda a, b: a == b),
        guard=lambda capture: None, _control=lambda element: scene.live_table))
    monkeypatch.setattr(module, '_row_coverage', lambda *a: calls.append('coverage') or {'observed_count': 2})
    monkeypatch.setattr(module, 'read_item_cell', native)
    monkeypatch.setattr(module, '_observe_item', observe)
    monkeypatch.setattr(module, '_painted_cell_box', geometry)
    monkeypatch.setattr(module, '_vat_source', lambda *a:
                        (lambda: scene.document_ok, {'document_number': 'PO1', 'reference': 'source'}))
    return scene


def test_native_sku_and_deselection_precede_exactly_one_vat_only_drag(scene):
    result = module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert result['passed'] and result['width_after'] == 300
    assert scene.calls == ['coverage', 'native-sku', 'observe', 'command-click', 'observe',
                           'drag', 'observe', 'native-sku']
    assert result['selected_background_before'] == [185, 215, 253]
    assert result['selected_background_after'] == [255, 255, 255]
    assert result['geometry_before']['Item No.']['cell'] == result['geometry_after']['Item No.']['cell']
    assert result['native_sku_after'] == 'SKU' and result['save_requests'] == 0


def test_one_source_row_never_reaches_resize(scene):
    scene.order.items.pop()
    with pytest.raises(DesktopError, match='exactly two'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls == []


def test_unknown_width_is_left_untouched(scene):
    scene.widths[:] = [200]
    with pytest.raises(DesktopError, match='no resize'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert 'command-click' not in scene.calls and 'drag' not in scene.calls


def test_uncertain_resize_is_retained_and_never_repeated(scene):
    scene.widths[:] = [150, 150, 301]
    with pytest.raises(DesktopError) as error:
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls.count('drag') == 1 and scene.calls.count('command-click') == 1
    proof = json.loads(open(error.value.document_presentation_evidence).read())
    assert proof['drag_requested'] and proof['passed'] is False
    assert proof['width_after'] == 301


def test_first_visible_row_of_two_row_source_prepares_its_exact_prefix(scene, monkeypatch):
    counts = []
    monkeypatch.setattr(module, '_row_coverage', lambda ui, title, count:
                        counts.append(count) or {'observed_count': count, 'blank_remaining_complete_rows': 3})
    result = module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=1)
    assert counts == [1] and result['expected_row_count'] == result['coverage']['observed_count'] == 1
    assert result['passed'] is True and result['width_after'] == 300
    assert scene.calls.count('drag') == 1


@pytest.mark.parametrize('count', [None, True, False, 0, 3, 1.0, '1'])
def test_invalid_prefix_count_refuses_before_any_ui_read_or_drag(scene, count):
    with pytest.raises(DesktopError, match='current source prefix'):
        module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=count)
    assert scene.calls == []


def test_single_row_source_is_still_unsupported_even_with_one_row_prefix(scene):
    scene.order.items.pop()
    with pytest.raises(DesktopError, match='exactly two source rows'):
        module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=1)
    assert scene.calls == []


def test_extra_or_hidden_rows_refuse_before_prefix_resize(scene, monkeypatch):
    from tjm.workflow_rules import WorkflowReview

    def extra_rows(ui, title, count):
        assert count == 1
        raise WorkflowReview('document_item_count_mismatch', 'Extra source occurrence remains below the visible prefix.')

    monkeypatch.setattr(module, '_row_coverage', extra_rows)
    with pytest.raises(WorkflowReview) as failure:
        module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=1)
    assert failure.value.code == 'document_item_count_mismatch'
    assert scene.calls == []


def test_second_prefix_confirms_existing_width_without_another_click_or_drag(scene, monkeypatch):
    counts = []
    monkeypatch.setattr(module, '_row_coverage', lambda ui, title, count:
                        counts.append(count) or {'observed_count': count})
    scene.widths[:] = [150, 150, 300, 300]
    scene.colors.append((185, 215, 253))
    scene.native_values.append('SKU')
    first = module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=1)
    second = module.prepare_document_presentation(scene.ui, scene.order, title='New Order', expected_row_count=2)
    assert counts == [1, 2]
    assert first['drag_requested'] is True and second['drag_requested'] is False
    assert second['deselection_requested'] is False
    assert scene.calls.count('drag') == scene.calls.count('command-click') == 1
    assert second['width_before'] == 300


@pytest.mark.parametrize('color', [(255, 255, 255), (180, 190, 210), (185, 215, 225)])
def test_unexpected_initial_selection_refuses_before_command_click_or_drag(scene, color):
    scene.colors[0] = color
    with pytest.raises(DesktopError, match='blue selected'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert 'command-click' not in scene.calls and 'drag' not in scene.calls


@pytest.mark.parametrize('color', [(185, 215, 253), (254, 255, 255), (240, 240, 240)])
def test_unproved_deselection_refuses_before_drag(scene, color):
    scene.colors[1] = color
    with pytest.raises(DesktopError, match='white unselected'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls.count('command-click') == 1 and 'drag' not in scene.calls


@pytest.mark.parametrize('change', ['cell', 'vat-width', 'row-band', 'table-box', 'scope',
                                    'process', 'window-bounds', 'window-element', 'table-element',
                                    'table-bounds', 'table-role', 'document'])
def test_changed_deselection_context_refuses_before_drag(scene, change):
    def mutate(index, observed):
        if index != 1:
            return
        if change == 'cell':
            observed.cells['Qty.']['cell'] = (10, 22, 159, 42)
        elif change == 'vat-width':
            observed.cells['VAT']['cell'] = (610, 22, 759, 42)
        elif change in ('row-band', 'table-box', 'scope'):
            setattr(observed, change.replace('-', '_'), ('changed',))
        elif change in ('process', 'window-bounds', 'window-element'):
            attribute = {'process': 'process_identity', 'window-bounds': 'window_bounds',
                         'window-element': 'window_element'}[change]
            setattr(observed.capture, attribute, ('changed',))
        elif change in ('table-element', 'table-bounds', 'table-role'):
            setattr(observed.table, change.split('-')[1], 'changed')
        elif change == 'document':
            scene.document_ok = False

    scene.mutate = mutate
    with pytest.raises(DesktopError):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls.count('command-click') == 1 and 'drag' not in scene.calls


def test_within_cell_caption_pixel_drift_does_not_change_actual_column_geometry(scene):
    def mutate(index, observed):
        if index in (1, 2):
            observed.boxes.update({'Qty.': (20, 1, 41, 15),
                                   'Item No.': (169, 2, 221, 16),
                                   'VAT': (619, 2, 645, 15)})

    scene.mutate = mutate
    result = module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert result['passed'] is True
    assert result['geometry_before'] == result['geometry_deselected']
    assert all(result['geometry_before'][label]['cell'] == result['geometry_after'][label]['cell']
               for label in ('Qty.', 'Item No.'))
    assert scene.calls.count('command-click') == scene.calls.count('drag') == 1


def test_native_table_disappearing_before_deselection_refuses_all_input(scene):
    scene.live_table.role = 'AXButton'
    with pytest.raises(DesktopError, match='source window, table'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert 'command-click' not in scene.calls and 'drag' not in scene.calls


def test_unmatched_initial_native_sku_refuses_all_input(scene):
    scene.native_values[0] = 'OTHER'
    with pytest.raises(DesktopError, match='native SKU differs'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls == ['coverage', 'native-sku']


@pytest.mark.parametrize('change', ['qty-cell', 'sku-cell', 'all-columns', 'vat-left'])
def test_identity_cell_resize_refused_even_when_header_boxes_unchanged(scene, change):
    def mutate(index, observed):
        if index != 2:
            return
        labels = {'qty-cell': ['Qty.'], 'sku-cell': ['Item No.'],
                  'all-columns': ['Qty.', 'Item No.'], 'vat-left': ['VAT']}[change]
        for label in labels:
            cell = observed.cells[label]['cell']
            if change == 'vat-left':
                observed.cells[label]['cell'] = (cell[0] + 1, cell[1], cell[2] + 1, cell[3])
            else:
                observed.cells[label]['cell'] = (cell[0], cell[1], cell[2] + 150, cell[3])

    scene.mutate = mutate
    with pytest.raises(DesktopError, match='preserve the observed identity columns') as failure:
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls.count('drag') == scene.calls.count('command-click') == 1
    assert scene.calls.count('native-sku') == 1
    evidence = json.loads(open(failure.value.document_presentation_evidence).read())
    assert evidence['geometry_after'] and evidence['passed'] is False


def test_native_sku_changed_after_resize_refuses_without_repeating(scene):
    scene.native_values[1] = 'OTHER'
    with pytest.raises(DesktopError, match='full native source SKU'):
        module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert scene.calls.count('drag') == scene.calls.count('command-click') == 1


def test_already_300_width_needs_no_selection_change_or_drag(scene):
    scene.widths[:] = [300]
    scene.colors[:] = [(255, 255, 255)]
    result = module.prepare_document_presentation(scene.ui, scene.order, title='Order')
    assert result['passed'] is True
    assert result['deselection_requested'] is False and result['drag_requested'] is False
    assert scene.calls == ['coverage', 'native-sku', 'observe']
