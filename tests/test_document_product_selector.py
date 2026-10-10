"""Observe readiness after one scoped icon input; no native desktop calls."""

from types import SimpleNamespace as NS

import pytest

from tjm import document_workflow as module
from tjm.desktop import DesktopError, Rect


class Scene:
    def __init__(self, monkeypatch):
        self.title = 'Fakturama - /prepared-data'
        self.identity = (123, 'Fakturama.ID', '/tested/Fakturama2.app', 1.0)
        self.main = NS(title=self.title, bounds=Rect(0, 0, 800, 600), element={'pid': 123})
        self.modal = NS(title='Select a product', bounds=Rect(100, 100, 400, 300), element={'pid': 123})
        self.delay = self.observations = self.icon_clicks = 0
        self.elapsed = 0.0
        self.never_appears = self.modal_wrong_focus = self.ambiguous = self.wrong_app_focus = False
        self.unexpected_window = False
        self.capture = NS(window_bounds=self.main.bounds, screen_rect=self.main.bounds, scale_x=1, scale_y=1)
        self.desktop = NS(identity=self.identity, pid=123,
                          ax=NS(kAXErrorSuccess=0, AXUIElementGetPid=lambda element, unused: (0, element['pid'])),
                          _require_focus=self.require_focus, _focused_window=self.focused_window,
                          windows=self.windows)
        self.ui = NS(desktop=self.desktop, title=self.title, actions=[], snapshot=lambda *a: self.capture,
                     scope=self.scope, label=self.label, icon=self.icon)
        def wait(predicate, description, *, timeout):
            deadline = self.elapsed + timeout
            while True:
                value = predicate()
                if value:
                    return value
                if self.elapsed >= deadline:
                    raise DesktopError('Timed out waiting for ' + description + '.')
                self.elapsed += .1
        monkeypatch.setattr(module, 'wait_for', wait)

    @staticmethod
    def scope(capture, title):
        assert title == 'New Order'
        return (180, 20, 780, 570)

    @staticmethod
    def label(capture, label, *, scope):
        return {'Items': (200, 350, 250, 370), 'Remarks': (200, 530, 280, 550)}[label]

    def icon(self, filename, *, scope, expected_help):
        assert filename == 'product_list.png'
        assert scope == (180, 340.0, 780, 530)
        assert expected_help == 'Pick an item from the list of all products'
        self.icon_clicks += 1

    def require_focus(self):
        if self.wrong_app_focus:
            raise DesktopError('Target process is not freshly focused; input/capture refused.')

    def focused_window(self, window):
        if self.modal_wrong_focus:
            raise DesktopError('The selected window is not the application\'s focused window.')

    def windows(self):
        self.observations += 1
        if self.unexpected_window:
            return [self.main, NS(title='Unexpected modal')]
        if self.never_appears or self.observations <= self.delay:
            return [self.main]
        return [self.main, self.modal, self.modal] if self.ambiguous else [self.main, self.modal]

    def run(self):
        return module.open_product_selector(self.ui, title='New Order')


def test_delayed_owned_focused_modal_after_one_scoped_icon_input(monkeypatch):
    scene = Scene(monkeypatch)
    scene.delay = 3
    proof = scene.run()
    assert scene.icon_clicks == 1 and scene.observations == 4
    assert proof['modal_verified'] is True and proof['modal_title'] == 'Select a product'
    assert proof['process_identity'] == list(scene.identity)
    assert scene.ui.actions == [proof]


def test_missing_modal_waits_boundedly_without_repeated_icon_input(monkeypatch):
    scene = Scene(monkeypatch)
    scene.never_appears = True
    with pytest.raises(DesktopError, match='after one icon click'):
        scene.run()
    assert scene.icon_clicks == 1
    assert 8 <= scene.elapsed < 8.11
    assert scene.ui.actions == []


@pytest.mark.parametrize('problem', ['wrong_modal_focus', 'ambiguous_windows', 'wrong_app_focus', 'unexpected_window'])
def test_modal_readiness_requires_unambiguous_expected_focused_window(monkeypatch, problem):
    scene = Scene(monkeypatch)
    if problem == 'wrong_modal_focus':
        scene.modal_wrong_focus = True
    elif problem == 'ambiguous_windows':
        scene.ambiguous = True
    elif problem == 'wrong_app_focus':
        scene.wrong_app_focus = True
    else:
        scene.unexpected_window = True
    with pytest.raises(DesktopError):
        scene.run()
    assert scene.icon_clicks == 1 and scene.ui.actions == []


def test_matching_modal_title_in_another_process_is_rejected(monkeypatch):
    scene = Scene(monkeypatch)
    scene.modal.element['pid'] = 999
    with pytest.raises(DesktopError, match='not owned by the retained'):
        scene.run()
    assert scene.icon_clicks == 1 and scene.ui.actions == []


def test_failed_scoped_icon_dispatch_does_not_retry_or_poll_modal(monkeypatch):
    scene = Scene(monkeypatch)
    def fail(*args, **kwargs):
        scene.icon_clicks += 1
        raise DesktopError('Scoped icon dispatch failed.')
    scene.ui.icon = fail
    with pytest.raises(DesktopError, match='Scoped icon dispatch failed'):
        scene.run()
    assert scene.icon_clicks == 1 and scene.observations == 0
