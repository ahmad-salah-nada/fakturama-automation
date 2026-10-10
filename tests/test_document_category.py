from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError, Rect
from tjm.document_sequence import _row_caption


def node(role, value=None, children=(), bounds=Rect(0, 0, 80, 20)):
    element = SimpleNamespace(children=children)
    return SimpleNamespace(role=role, value=value, title=None, bounds=bounds, element=element)


def reader():
    desktop = SimpleNamespace(_read=lambda element, attribute, **kw: element.children,
                              _control=lambda element: element)
    return SimpleNamespace(desktop=desktop, require_inside_scope=lambda *args: None)


def test_null_row_value_reads_the_observed_group_caption():
    caption = node('AXStaticText', 'Orders', bounds=Rect(18, 0, 37, 16))
    arrow = node('AXDisclosureTriangle', bounds=Rect(2, 0, 12, 16))
    group = node('AXGroup', children=(arrow, caption), bounds=Rect(0, 0, 56, 17))
    row = node('AXRow', children=(group,), bounds=Rect(0, 0, 165, 17))
    assert _row_caption(reader(), None, row, None) is caption
    assert row.value is None


def test_root_direct_static_caption_is_supported():
    caption = node('AXStaticText', '---')
    assert _row_caption(reader(), None, node('AXRow', children=(caption,)), None) is caption


@pytest.mark.parametrize('children', [(), (node('AXStaticText', 'Orders'), node('AXStaticText', 'Invoices')),
                                    (node('AXButton', 'Orders'),), (node('AXStaticText', ''),)])
def test_missing_ambiguous_and_unsupported_captions_stop(children):
    with pytest.raises(DesktopError):
        _row_caption(reader(), None, node('AXRow', children=children), None)


def test_caption_outside_group_stops():
    caption = node('AXStaticText', 'Orders', bounds=Rect(60, 0, 37, 16))
    group = node('AXGroup', children=(caption,), bounds=Rect(0, 0, 56, 17))
    with pytest.raises(DesktopError, match='outside its group'):
        _row_caption(reader(), None, node('AXRow', children=(group,), bounds=Rect(0, 0, 165, 17)), None)


def test_selection_pixel_candidates_are_bounded_and_need_native_verification():
    from PIL import Image
    from tjm.document_sequence import _selected_row_boxes
    image = Image.new('RGB', (400, 100), 'white')
    image.paste((41, 98, 217), (10, 20, 170, 40))
    image.paste((41, 98, 217), (250, 20, 260, 30))  # Small icon is not a row.
    assert _selected_row_boxes(image, (0, 0, 400, 100)) == [(10, 20, 170, 40)]
    assert _selected_row_boxes(image, (180, 0, 400, 100)) == []
