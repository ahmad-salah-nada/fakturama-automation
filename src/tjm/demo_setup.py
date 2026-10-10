"""Explicit disposable demo setup; never read or write business database records."""

import json
import os
from pathlib import Path
import subprocess
import time

from .desktop import Desktop, DesktopError, _load_native
from .readiness import SUPPORTED_BUNDLE_ID, _select_workspace_desktop


DEMO_SETTINGS = {
    'CONTACT_FORMAT_ADDRESS': '{company}<br>{street}<br>{zip} {city}<br>{country}',
    'CURRENCY_USE_SYMBOL': 'CODE', 'PREFERENCE_CURRENCY_LOCALE': 'ga/IE',
    'GENERAL_HAS_THOUSANDS_SEPARATOR': 'false', 'GENERAL_CLOSE_OTHER_EDITORS': 'false',
    'DOCUMENT_USE_DISCOUNT_EACH_ITEM': 'true', 'DOCUMENT_USE_DISCOUNT_ALL_ITEMS': 'true',
    'DOCUMENT_IMMEDIATELY_OVERTAKE_ITEMNUMBER_FROM_PRODUCTS_DIALOG': 'false',
    'PRODUCT_USE_DESCRIPTION': 'true', 'PRODUCT_USE_QUNIT': 'false',
    'eclipse.preferences.version': '1',
}

RESTART_TEXT = 'To switch the workspace,\nFakturama will be restarted!'


def _write_setup(path, result):
    with path.open('w', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _demo_apps(bundle):
    _, cocoa, _, _ = _load_native()
    # Apple documents that this cache updates only on a common-mode main-loop
    # turn: https://developer.apple.com/documentation/appkit/nsworkspace/runningapplications
    # The same-process process-discovery-probe.json records [] before event
    # delivery and the actual launched PID afterward. Sleep alone never delivers it.
    if not cocoa.NSThread.isMainThread():
        raise DesktopError('Fresh demo process discovery requires the main Cocoa thread.')
    cocoa.NSRunLoop.mainRunLoop().runUntilDate_(cocoa.NSDate.dateWithTimeIntervalSinceNow_(.01))
    apps = []
    for app in cocoa.NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == SUPPORTED_BUNDLE_ID and not app.isTerminated():
            url = app.bundleURL()
            if url is None:
                raise DesktopError('A running Fakturama bundle URL is unreadable.')
            if Path(str(url.path())).resolve() == bundle.resolve():
                apps.append(app)
    return apps


def _running_demo_identities(bundle):
    return [Desktop._identity(app) for app in _demo_apps(bundle)]


def _starting_windows(error):
    return (getattr(error, 'attribute', None) == 'AXWindows'
            and getattr(error, 'code', None) in (-25204, -25212))


def _setup_dialog_shape(desktop, window, workspace, kind):
    """Read only the two demonstrated direct native dialog shapes."""
    desktop._process()
    children = list(desktop._read(window.element, 'AXChildren', required=True))
    if not children or len(children) > 32:
        raise DesktopError('First-use setup has an unsupported direct native layout.')
    rows, ok = [], []
    for element in children:
        code, pid = desktop.ax.AXUIElementGetPid(element, None)
        if code != 0 or int(pid) != desktop.pid:
            raise DesktopError('First-use setup contains an unowned control.')
        row = {'role': desktop._read(element, 'AXRole', required=True),
               'title': desktop._read(element, 'AXTitle'), 'value': desktop._read(element, 'AXValue')}
        rows.append(row)
        if row['role'] == 'AXButton' and row['title'] == 'OK':
            ok.append(element)
    if len(ok) != 1:
        raise DesktopError('First-use setup requires one exact native OK button.')
    if kind == 'initialization':
        fields = [row['value'] for row in rows if row['role'] == 'AXTextField']
        checks = [row for row in rows if row['role'] == 'AXCheckBox']
        allowed_roles = {'AXStaticText', 'AXScrollArea', 'AXImage', 'AXButton', 'AXCheckBox', 'AXTextField'}
        allowed_buttons = {None, '', 'OK', 'Cancel', '...'}
        if fields != ['', workspace] or len(checks) != 1 or checks[0]['title'] != 'use default database settings' or checks[0]['value'] != 1:
            raise DesktopError('First-use initialization does not show the exact new workspace and default database setting.')
    else:
        texts = [row['value'] for row in rows if row['role'] == 'AXStaticText' and row['value']]
        allowed_roles = {'AXStaticText', 'AXImage', 'AXButton'}
        allowed_buttons = {None, '', 'OK'}
        if sorted(texts) != sorted([RESTART_TEXT, 'Information']):
            raise DesktopError('Information is not the demonstrated workspace restart confirmation.')
    if any(row['role'] not in allowed_roles or
           (row['role'] == 'AXButton' and row['title'] not in allowed_buttons) for row in rows):
        raise DesktopError('First-use setup contains an unknown control or action.')
    control = desktop._control(ok[0])
    if control.enabled is not True or not window.bounds.contains_rect(control.bounds):
        raise DesktopError('First-use native OK is disabled or outside its owned window.')
    return control, rows


def _relaunch_after_restart(app, apps, bundle, baseline, confirmed, result, path):
    """Once-only setup repair for the observed internal restart's absent launch date.

    No business input has occurred. Normal termination and explicit launch restore
    the existing strict process identity; no launch timestamp is invented.
    """
    baseline_pids = {identity[0] for identity in baseline}
    new_apps = [value for value in apps if int(value.processIdentifier()) not in baseline_pids]
    pid = int(app.processIdentifier())
    old_pid = confirmed.get('restart', (None,))[0]
    if (result.get('restart_relaunch') is not None or old_pid is None
            or len(new_apps) != 1 or new_apps[0] is not app
            or pid == old_pid or any(int(value.processIdentifier()) == old_pid for value in apps)
            or app.launchDate() is not None or app.bundleIdentifier() != SUPPORTED_BUNDLE_ID
            or app.bundleURL() is None or Path(str(app.bundleURL().path())).resolve() != bundle.resolve()):
        raise DesktopError('The internal restart cannot establish the single setup-only relaunch guard.')
    entry = {'restart_process_exited': old_pid, 'unusable_restart_pid': pid,
             'bundle_id': app.bundleIdentifier(), 'bundle_path': str(app.bundleURL().path()),
             'launch_date': None, 'status': 'normal termination intent', 'business_inputs': 0}
    result['restart_relaunch'] = entry
    _write_setup(path, result)
    if not app.terminate():
        raise DesktopError('The setup-only restarted process refused normal termination; no relaunch requested.')
    entry['status'] = 'normal termination requested once'
    _write_setup(path, result)
    deadline = time.monotonic() + 10
    while any(int(value.processIdentifier()) == pid for value in _demo_apps(bundle)):
        if time.monotonic() >= deadline:
            raise DesktopError('Setup-only normal termination was not observed within 10 seconds.')
        time.sleep(.1)
    entry['status'] = 'terminated; explicit relaunch intent'
    _write_setup(path, result)
    subprocess.run(result['launch_command'], check=True, timeout=10)
    entry['status'] = 'explicit relaunch requested once'
    _write_setup(path, result)


def _await_fresh_window(result, path, bundle, baseline):
    """Bound startup observations; each recognized setup confirmation runs once."""
    deadline = time.monotonic() + 45
    confirmed = {}
    while time.monotonic() < deadline:
        dialog_visible = False
        apps = _demo_apps(bundle)
        for app in apps:
            if int(app.processIdentifier()) in {identity[0] for identity in baseline}:
                continue
            if app.launchDate() is None:
                _relaunch_after_restart(app, apps, bundle, baseline, confirmed, result, path)
                deadline = time.monotonic() + 45
                dialog_visible = True
                break
            identity = Desktop._identity(app)
            if list(identity) not in result['startup_processes']:
                result['startup_processes'].append(list(identity))
                _write_setup(path, result)
            desktop = Desktop(pid=identity[0], bundle_id=SUPPORTED_BUNDLE_ID)
            if desktop.identity != identity:
                raise DesktopError('First-use process identity changed during observation.')
            try:
                windows = desktop.windows()
            except DesktopError as error:
                if _starting_windows(error):
                    continue
                raise
            dialogs = [window for window in windows if window.title in {'Fakturama Initialization', 'Information'}]
            if any(window.title not in {'', result['window_title'], 'Fakturama Initialization', 'Information'} for window in windows):
                raise DesktopError('An unknown or ambiguous first-use application prompt blocks startup.')
            if len(dialogs) > 1:
                # Fakturama leaves Initialization behind its restart Information
                # dialog. Permit only that observed pair after our own first OK.
                if (len(dialogs) != 2 or {window.title for window in dialogs} != {'Fakturama Initialization', 'Information'}
                        or confirmed.get('initialization') != identity):
                    raise DesktopError('An unknown or ambiguous first-use application prompt blocks startup.')
                initialization = next(window for window in dialogs if window.title == 'Fakturama Initialization')
                information = next(window for window in dialogs if window.title == 'Information')
                _setup_dialog_shape(desktop, initialization, result['workspace'], 'initialization')
                _setup_dialog_shape(desktop, information, result['workspace'], 'restart')
                desktop._focused_window(information)
                dialogs = [information]
            if not dialogs:
                continue
            dialog_visible = True
            window = dialogs[0]
            kind = 'initialization' if window.title == 'Fakturama Initialization' else 'restart'
            control, rows = _setup_dialog_shape(desktop, window, result['workspace'], kind)
            if kind == 'restart' and 'initialization' not in confirmed:
                raise DesktopError('Workspace restart appeared without this invocation confirming initialization.')
            if kind in confirmed:
                if confirmed[kind] != identity:
                    raise DesktopError('First-use confirmation reappeared in another process; input is not repeated.')
                continue
            desktop.activate()
            capture = desktop.capture(window.title)
            control, rows = _setup_dialog_shape(desktop, desktop.window(window.title), result['workspace'], kind)
            capture_path = path.with_name('first-use-' + kind + '.png')
            capture.image.save(capture_path)
            entry = {'kind': kind, 'process_identity': list(identity), 'native_children': rows,
                     'capture': capture.metadata(), 'image': str(capture_path), 'status': 'intent'}
            result['setup_confirmations'].append(entry)
            confirmed[kind] = identity
            _write_setup(path, result)

            def unchanged():
                fresh, fresh_rows = _setup_dialog_shape(desktop, desktop.window(window.title), result['workspace'], kind)
                return fresh == control and fresh_rows == rows

            desktop.press(capture, control, action_guard=unchanged)
            entry['status'] = 'dispatched once'
            _write_setup(path, result)
        if not dialog_visible:
            try:
                desktop, selection = _select_workspace_desktop(result['window_title'])
                if desktop.identity in baseline or Path(desktop.identity[2]).resolve() != bundle.resolve():
                    raise DesktopError('The fresh workspace is not owned by the newly launched tested bundle.')
                return desktop, selection
            except DesktopError as error:
                if not _starting_windows(error) and 'found 0 across' not in str(error):
                    raise
        time.sleep(.3)
    raise DesktopError('Fresh demo startup did not finish within its bounded 45-second launch stage.')


def launch_fresh_demo(workspace, evidence_dir):
    """Create previously absent data/settings, launch once, and retain ownership.

    Preferences prepare the already approved demo display. Generated counters,
    masters and documents are created only by Fakturama. Existing paths stop.
    """
    workspace = Path(workspace).expanduser().resolve()
    settings = workspace.with_name(workspace.name + '-settings')
    root = Path(__file__).resolve().parents[2]
    app = root / '.tools/apps/Fakturama/Fakturama2.app'
    java = Path('/Library/Java/JavaVirtualMachines/temurin-22.jdk/Contents/Home/bin/java')
    if workspace.exists() or settings.exists():
        raise DesktopError('Fresh demo requires previously absent data and settings paths; no path is overwritten.')
    if not app.is_dir() or not java.is_file():
        raise DesktopError('The tested Fakturama bundle and Java runtime must be installed before demo setup.')
    workspace.mkdir(parents=True, exist_ok=False)
    preference_dir = settings / '.metadata/.plugins/org.eclipse.core.runtime/.settings'
    preference_dir.mkdir(parents=True, exist_ok=False)
    preference = preference_dir / 'com.sebulli.fakturama.rcp.prefs'
    preference.write_text(''.join(key + '=' + value + '\n' for key, value in DEMO_SETTINGS.items()))
    result = {'previously_absent': True, 'workspace': str(workspace), 'settings': str(settings),
              'settings_preset': DEMO_SETTINGS, 'business_database_writes': 0,
              'window_title': 'Fakturama - ' + str(workspace), 'phase': 'launch requested',
              'startup_processes': [], 'setup_confirmations': []}
    result['launch_command'] = ['open', '-n', str(app), '--args', '-vm', str(java),
                                '--workspace', str(workspace), '-data', str(settings), '-nl', 'en_US']
    path = Path(evidence_dir) / 'fresh-demo-setup.json'
    _write_setup(path, result)
    try:
        baseline = _running_demo_identities(app)
        result['pre_launch_processes'] = [list(identity) for identity in baseline]
        _write_setup(path, result)
        subprocess.run(result['launch_command'], check=True, timeout=10)
        desktop, selection = _await_fresh_window(result, path, app, baseline)
        desktop.activate()
        window = desktop.window(result['window_title'])
        display = desktop.quartz.CGDisplayBounds(desktop.quartz.CGMainDisplayID())
        width = min(1500, int(display.size.width) - 24)
        height = min(900, int(display.size.height) - 80)
        for attribute, value in (
            ('AXPosition', desktop.ax.AXValueCreate(desktop.ax.kAXValueCGPointType,
                                                   desktop.quartz.CGPointMake(display.origin.x + 12, display.origin.y + 38))),
            ('AXSize', desktop.ax.AXValueCreate(desktop.ax.kAXValueCGSizeType,
                                               desktop.quartz.CGSizeMake(width, height))),
        ):
            code, settable = desktop.ax.AXUIElementIsAttributeSettable(window.element, attribute, None)
            if code != 0 or settable is not True:
                raise DesktopError('Demo window layout is not natively adjustable.')
            if desktop.ax.AXUIElementSetAttributeValue(window.element, attribute, value) != 0:
                raise DesktopError('Demo window layout request failed; inspect before continuing.')
        result.update(phase='launched owned fresh workspace', process_identity=list(desktop.identity),
                      selection=selection, observed_bounds=desktop.window(result['window_title']).bounds.as_dict())
        _write_setup(path, result)
        return result
    except Exception as error:
        result.update(phase='startup stopped', error={'type': type(error).__name__, 'message': str(error)})
        _write_setup(path, result)
        raise
