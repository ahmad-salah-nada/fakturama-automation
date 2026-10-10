from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError
from tjm import document_workflow as module


@pytest.mark.parametrize('value,title,enabled', [(0, 'paid', True), (1, 'paid', True)])
def test_paid_control_uses_actual_caption_hit_not_global_tree_search(monkeypatch, value, title, enabled):
    capture = SimpleNamespace(screen_point=lambda *point: point)
    paid = SimpleNamespace(title=title, value=value, enabled=enabled)
    desktop = SimpleNamespace(hit_test=lambda cap, *point, expected_role: paid)
    method = SimpleNamespace(value='Bank Transfer')
    ui = SimpleNamespace(desktop=desktop, label=lambda *args, **kw: (10, 20, 40, 35),
                         require_inside_scope=lambda *args: None, field=lambda *args, **kw: method)
    monkeypatch.setattr(module, 'editor_scope', lambda *args, **kw: (0, 0, 100, 100))
    assert module._invoice_payment_controls(ui, capture, 'New Invoice') == ((0, 0, 100, 100), paid, method)


@pytest.mark.parametrize('value,title,enabled', [(2, 'paid', True), (None, 'paid', True),
                                                (0, 'other', True), (0, 'paid', False)])
def test_wrong_title_state_or_disabled_paid_control_stops(monkeypatch, value, title, enabled):
    capture = SimpleNamespace(screen_point=lambda *point: point)
    paid = SimpleNamespace(title=title, value=value, enabled=enabled)
    desktop = SimpleNamespace(hit_test=lambda cap, *point, expected_role: paid)
    ui = SimpleNamespace(desktop=desktop, label=lambda *args, **kw: (10, 20, 40, 35),
                         require_inside_scope=lambda *args: None)
    monkeypatch.setattr(module, 'editor_scope', lambda *args, **kw: (0, 0, 100, 100))
    with pytest.raises(DesktopError, match='readable enabled binary state'):
        module._invoice_payment_controls(ui, capture, 'New Invoice')
