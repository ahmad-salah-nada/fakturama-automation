"""Bounded setup tests simulate native dialogs and never operate a desktop."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from tjm import demo_setup as module
from tjm.desktop import AXControl, DesktopError, Rect, _AXReadError


def test_demo_preset_disables_single_result_product_autoaccept():
    assert module.DEMO_SETTINGS['DOCUMENT_IMMEDIATELY_OVERTAKE_ITEMNUMBER_FROM_PRODUCTS_DIALOG'] == 'false'


class Scene:
    def __init__(self, monkeypatch, tmp_path, state='ready'):
        self.bundle = tmp_path / 'Fakturama2.app'
        self.workspace = str(tmp_path / 'fresh-data')
        self.title = 'Fakturama - ' + self.workspace
        self.path = tmp_path / 'fresh-demo-setup.json'
        self.result = {'workspace': self.workspace, 'window_title': self.title,
                       'startup_processes': [], 'setup_confirmations': [],
                       'launch_command': ['open', '-n', str(self.bundle), '--args', '--workspace', self.workspace]}
        self.state, self.elapsed = state, 0.0
        self.presses, self.relaunches, self.terminations = [], [], []
        self.hold_initialization = 0
        self.fields = ['', self.workspace]
        self.default = 1
        self.information = module.RESTART_TEXT
        self.windows_error = None
        self.discovery_errors = []
        self.restart_without_date = False
        self.second_without_date = False
        self.dispatch_error = False
        self.paired_information = False
        self.focused_title = None
        self.baseline = (77, 'Fakturama.ID', str(self.bundle), 0.5)
        self.app1 = self.app(100, 1.0)
        self.app2 = self.app(101, 2.0)
        self.app3 = self.app(102, 3.0)
        self.native = NS(AXUIElementGetPid=lambda element, unused: (0, element['pid']))
        self.desktops = {100: self.desktop(self.app1), 101: self.desktop(self.app2), 102: self.desktop(self.app3)}
        monkeypatch.setattr(module, '_demo_apps', lambda bundle: self.apps())
        # A callable object supplies both the constructor and strict identity read.
        class Factory:
            _identity = staticmethod(lambda app: app.identity)
            def __new__(cls, **kw):
                return self.desktops[kw['pid']]
        monkeypatch.setattr(module, 'Desktop', Factory)
        monkeypatch.setattr(module, '_select_workspace_desktop', self.discover)
        monkeypatch.setattr(module.time, 'monotonic', lambda: self.elapsed)
        monkeypatch.setattr(module.time, 'sleep', self.sleep)
        monkeypatch.setattr(module.subprocess, 'run', self.relaunch)

    def app(self, pid, date):
        app = NS(identity=(pid, 'Fakturama.ID', str(self.bundle), date),
                 processIdentifier=lambda: pid, bundleIdentifier=lambda: 'Fakturama.ID',
                 bundleURL=lambda: NS(path=lambda: str(self.bundle)),
                 launchDate=lambda: date, isTerminated=lambda: False)
        def terminate():
            self.terminations.append(pid)
            self.state = 'terminated'
            return True
        app.terminate = terminate
        return app

    def apps(self):
        if self.state == 'terminated':
            return []
        if self.state == 'ready-relaunch':
            return [self.app3]
        if self.state == 'ready-restart':
            return [self.app2]
        return [self.app1]

    def sleep(self, seconds):
        self.elapsed += seconds
        if self.hold_initialization and self.state == 'initialization':
            self.hold_initialization -= 1
            if not self.hold_initialization:
                self.state = 'information'

    def relaunch(self, command, **kwargs):
        self.relaunches.append(command)
        self.state = 'ready-relaunch'
        if self.second_without_date:
            self.app3.launchDate = lambda: None
        return NS(returncode=0)

    def row(self, role, title=None, value=None, pid=100):
        return {'AXRole': role, 'AXTitle': title, 'AXValue': value, 'pid': pid}

    def window(self, pid):
        if self.state in {'initialization', 'repeated-initialization'}:
            title = 'Fakturama Initialization'
            children = [self.row('AXTextField', value=value, pid=pid) for value in self.fields]
            children += [self.row('AXCheckBox', 'use default database settings', self.default, pid),
                         self.row('AXButton', 'OK', pid=pid)]
        elif self.state == 'information':
            title = 'Information'
            children = [self.row('AXStaticText', value=self.information, pid=pid),
                        self.row('AXStaticText', value='Information', pid=pid),
                        self.row('AXButton', 'OK', pid=pid)]
        elif self.state == 'unknown':
            title, children = 'Unexpected prompt', []
        else:
            title, children = self.title, []
        return NS(title=title, bounds=Rect(0, 0, 300, 200),
                  element={'AXChildren': children})

    def desktop(self, app):
        pid = app.identity[0]
        def windows():
            if self.windows_error:
                error, self.windows_error = self.windows_error, None
                raise error
            result = [self.window(pid)]
            if self.paired_information and self.state == 'information':
                self.state = 'initialization'
                result.append(self.window(pid))
                self.state = 'information'
            return result
        def focused_window(window):
            if self.focused_title is not None and window.title != self.focused_title:
                raise DesktopError('The selected window is not the application\'s focused window.')
        def press(capture, control, action_guard):
            assert action_guard()
            self.presses.append(control.title)
            if self.dispatch_error:
                raise DesktopError('Uncertain native dispatch')
            if self.state == 'initialization':
                if not self.hold_initialization:
                    self.state = 'information'
            elif self.state == 'information':
                self.state = 'ready-restart'
                if self.restart_without_date:
                    self.app2.launchDate = lambda: None
        return NS(identity=app.identity, pid=pid, ax=self.native,
                  windows=windows, window=lambda title: self.window(pid),
                  _process=lambda: app, _read=lambda element, key, **kw: element.get(key),
                  _focused_window=focused_window,
                  _control=lambda element: AXControl(element['AXRole'], element['AXTitle'], element['AXValue'],
                                                     Rect(10, 10, 20, 20), True, element),
                  activate=lambda: None, capture=lambda title: NS(image=Image.new('RGB', (20, 20)),
                                                                   metadata=lambda: {'pid': pid, 'window_title': title}),
                  press=press)

    def discover(self, title):
        if self.discovery_errors:
            raise self.discovery_errors.pop(0)
        if self.state not in {'ready', 'ready-restart', 'ready-relaunch'}:
            raise DesktopError('Requested workspace needs one exact running Fakturama main window; found 0 across 1 applications.')
        app = self.apps()[0]
        return self.desktops[app.identity[0]], {'exact_window': title}

    def run(self):
        return module._await_fresh_window(self.result, self.path, self.bundle, [self.baseline])


@pytest.mark.parametrize('code', [-25204, -25212])
def test_transient_real_axwindows_error_then_ready(monkeypatch, tmp_path, code):
    scene = Scene(monkeypatch, tmp_path)
    scene.discovery_errors = [_AXReadError('AXWindows', code)]
    desktop, _ = scene.run()
    assert desktop.pid == 100
    assert scene.elapsed == .3
    assert scene.presses == []


def test_error_retains_attribute_and_unrelated_attribute_is_not_retried(monkeypatch, tmp_path):
    error = _AXReadError('AXRole', -25204)
    assert error.attribute == 'AXRole' and error.code == -25204
    scene = Scene(monkeypatch, tmp_path)
    scene.discovery_errors = [error]
    with pytest.raises(DesktopError, match='AXRole'):
        scene.run()
    assert scene.elapsed == 0 and scene.presses == []


def test_exact_initialization_and_restart_confirm_once_then_new_process_ready(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    desktop, _ = scene.run()
    assert desktop.pid == 101
    assert scene.presses == ['OK', 'OK']
    retained = json.loads(scene.path.read_text())
    assert [entry['kind'] for entry in retained['setup_confirmations']] == ['initialization', 'restart']
    assert all(entry['status'] == 'dispatched once' for entry in retained['setup_confirmations'])
    assert [identity[0] for identity in retained['startup_processes']] == [100, 101]


def test_observed_restart_information_with_initialization_behind_is_supported(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.paired_information = True
    scene.focused_title = 'Information'
    desktop, _ = scene.run()
    assert desktop.pid == 101 and scene.presses == ['OK', 'OK']


def test_paired_restart_dialog_requires_information_focused(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.paired_information = True
    scene.focused_title = 'Fakturama Initialization'
    with pytest.raises(DesktopError, match='focused window'):
        scene.run()
    assert scene.presses == ['OK']


def test_paired_restart_dialog_before_owned_initialization_refuses_input(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'information')
    scene.paired_information = True
    with pytest.raises(DesktopError, match='unknown or ambiguous'):
        scene.run()
    assert scene.presses == []


@pytest.mark.parametrize('problem', ['wrong_path', 'old_path', 'unchecked_default', 'extra_field'])
def test_wrong_initialization_fields_stop_without_confirming(monkeypatch, tmp_path, problem):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    if problem == 'wrong_path':
        scene.fields[1] = '/wrong'
    elif problem == 'old_path':
        scene.fields[0] = '/old'
    elif problem == 'unchecked_default':
        scene.default = 0
    else:
        scene.fields.append('unexpected')
    with pytest.raises(DesktopError, match='exact new workspace'):
        scene.run()
    assert scene.presses == []


def test_unknown_information_after_setup_is_not_confirmed(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.information = 'Something else happened.'
    with pytest.raises(DesktopError, match='not the demonstrated'):
        scene.run()
    assert scene.presses == ['OK']


def test_known_dialog_remaining_visible_does_not_repeat_confirmation(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.hold_initialization = 3
    scene.run()
    assert scene.presses == ['OK', 'OK']
    assert len(scene.result['setup_confirmations']) == 2


def test_uncertain_setup_dispatch_keeps_intent_and_is_never_retried(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.dispatch_error = True
    with pytest.raises(DesktopError, match='Uncertain native dispatch'):
        scene.run()
    assert scene.presses == ['OK']
    assert json.loads(scene.path.read_text())['setup_confirmations'][0]['status'] == 'intent'


def test_internal_restart_missing_launchdate_terminates_and_relaunches_once(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.restart_without_date = True
    desktop, _ = scene.run()
    assert desktop.pid == 102
    assert scene.terminations == [101]
    assert scene.relaunches == [scene.result['launch_command']]
    assert scene.presses == ['OK', 'OK']
    assert scene.result['restart_relaunch']['status'] == 'explicit relaunch requested once'


def test_missing_launchdate_without_known_restart_refuses_termination(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path)
    scene.app1.launchDate = lambda: None
    with pytest.raises(DesktopError, match='single setup-only relaunch guard'):
        scene.run()
    assert scene.terminations == [] and scene.relaunches == []


def test_second_missing_launchdate_stops_without_second_relaunch(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.restart_without_date = scene.second_without_date = True
    with pytest.raises(DesktopError, match='single setup-only relaunch guard'):
        scene.run()
    assert scene.terminations == [101] and len(scene.relaunches) == 1


def test_unknown_owned_prompt_stops_without_input(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'unknown')
    with pytest.raises(DesktopError, match='unknown or ambiguous'):
        scene.run()
    assert scene.presses == []


@pytest.mark.parametrize('problem', ['old_alive', 'multiple_new', 'wrong_bundle', 'wrong_path', 'baseline_pid'])
def test_restart_relaunch_guard_refuses_ambiguous_or_unowned_process(monkeypatch, tmp_path, problem):
    scene = Scene(monkeypatch, tmp_path)
    scene.app2.launchDate = lambda: None
    apps = [scene.app2]
    baseline = [scene.baseline]
    if problem == 'old_alive':
        apps.append(scene.app1)
    elif problem == 'multiple_new':
        apps.append(scene.app3)
    elif problem == 'wrong_bundle':
        scene.app2.bundleIdentifier = lambda: 'Wrong.ID'
    elif problem == 'wrong_path':
        scene.app2.bundleURL = lambda: NS(path=lambda: '/wrong/Fakturama.app')
    else:
        baseline.append(scene.app2.identity)
    with pytest.raises(DesktopError, match='single setup-only relaunch guard'):
        module._relaunch_after_restart(scene.app2, apps, scene.bundle, baseline,
                                       {'restart': scene.app1.identity}, scene.result, scene.path)
    assert scene.terminations == [] and scene.relaunches == []


def test_setup_termination_refusal_has_no_explicit_relaunch(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.restart_without_date = True
    scene.app2.terminate = lambda: False
    with pytest.raises(DesktopError, match='refused normal termination'):
        scene.run()
    assert scene.relaunches == []
    assert scene.result['restart_relaunch']['status'] == 'normal termination intent'


def test_still_visible_dialog_times_out_without_repeated_input(monkeypatch, tmp_path):
    scene = Scene(monkeypatch, tmp_path, 'initialization')
    scene.hold_initialization = 10000
    with pytest.raises(DesktopError, match='bounded 45-second'):
        scene.run()
    assert scene.presses == ['OK']
    assert 45 <= scene.elapsed < 45.31


def test_launch_retains_startup_failure_without_business_database_writes(monkeypatch, tmp_path):
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'src/tjm/demo_setup.py'))
    (tmp_path / '.tools/apps/Fakturama/Fakturama2.app').mkdir(parents=True)
    java = Path('/Library/Java/JavaVirtualMachines/temurin-22.jdk/Contents/Home/bin/java')
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda value: value == java or original_is_file(value))
    monkeypatch.setattr(module, '_running_demo_identities', lambda bundle: [])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: NS(returncode=0))
    def fail(*args):
        raise DesktopError('Unknown startup prompt; stop.')
    monkeypatch.setattr(module, '_await_fresh_window', fail)
    workspace = tmp_path / 'fresh-data'
    with pytest.raises(DesktopError, match='Unknown startup prompt'):
        module.launch_fresh_demo(workspace, tmp_path)
    retained = json.loads((tmp_path / 'fresh-demo-setup.json').read_text())
    assert retained['phase'] == 'startup stopped'
    assert retained['error']['message'] == 'Unknown startup prompt; stop.'
    assert retained['business_database_writes'] == 0
    assert list(workspace.iterdir()) == []


def test_process_inventory_delivers_bounded_main_loop_events_before_enumeration(monkeypatch, tmp_path):
    calls = []
    app = NS(bundleIdentifier=lambda: 'Fakturama.ID', isTerminated=lambda: False,
             bundleURL=lambda: NS(path=lambda: str(tmp_path)))
    def date(interval):
        calls.append(('deadline', interval))
        return 'bounded-date'
    def enumerate_apps():
        calls.append('enumerate')
        return [app]
    cocoa = NS(NSThread=NS(isMainThread=lambda: True),
               NSDate=NS(dateWithTimeIntervalSinceNow_=date),
               NSRunLoop=NS(mainRunLoop=lambda: NS(runUntilDate_=lambda value: calls.append(('main-loop', value)))),
               NSWorkspace=NS(sharedWorkspace=lambda: NS(runningApplications=enumerate_apps)))
    monkeypatch.setattr(module, '_load_native', lambda: (None, cocoa, None, None))
    assert module._demo_apps(tmp_path) == [app]
    assert calls == [('deadline', .01), ('main-loop', 'bounded-date'), 'enumerate']


def test_main_loop_delivery_failure_stops_before_enumeration(monkeypatch, tmp_path):
    calls = []
    def fail(value):
        raise RuntimeError('Native loop failed')
    cocoa = NS(NSThread=NS(isMainThread=lambda: True),
               NSDate=NS(dateWithTimeIntervalSinceNow_=lambda interval: 'bounded-date'),
               NSRunLoop=NS(mainRunLoop=lambda: NS(runUntilDate_=fail)),
               NSWorkspace=NS(sharedWorkspace=lambda: NS(runningApplications=lambda: calls.append('enumerate'))))
    monkeypatch.setattr(module, '_load_native', lambda: (None, cocoa, None, None))
    with pytest.raises(RuntimeError, match='Native loop failed'):
        module._demo_apps(tmp_path)
    assert calls == []


def test_background_thread_refuses_main_loop_process_inventory(monkeypatch, tmp_path):
    cocoa = NS(NSThread=NS(isMainThread=lambda: False))
    monkeypatch.setattr(module, '_load_native', lambda: (None, cocoa, None, None))
    with pytest.raises(DesktopError, match='main Cocoa thread'):
        module._demo_apps(tmp_path)
