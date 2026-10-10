"""Desktop boundary tests use simulated native bindings; they never send UI input."""

from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from tjm import desktop as module
from tjm.desktop import Desktop, DesktopError, Rect, StaticLabelRightEdge, Window, wait_for


class ClipboardItem:
    def __init__(self):
        self.values = {}

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def types(self):
        return list(self.values)

    def dataForType_(self, kind):
        return self.values[kind]

    def setData_forType_(self, data, kind):
        self.values[kind] = data
        return True

    def setString_forType_(self, text, kind):
        self.values[kind] = text.encode("utf-8")
        return True


class Clipboard:
    def __init__(self):
        self.items, self.count = [], 0
        self.fail_next_write = False

    def pasteboardItems(self):
        return self.items

    def changeCount(self):
        return self.count

    def clearContents(self):
        self.items = []
        self.count += 1

    def writeObjects_(self, items):
        if self.fail_next_write:
            self.fail_next_write = False
            return False
        self.items = list(items)
        self.count += 1
        return True


class Native:
    def __init__(self):
        self.pid, self.focus, self.launch = 123, 123, 100.0
        self.accessibility = self.screen_recording = True
        self.alive = True
        self.activation_calls = 0
        self.events, self.actions = [], []
        self.on_post = None
        self.board = Clipboard()
        self.main = self.element("AXWindow", "Fakturama", Rect(10, 20, 100, 80))
        self.field = self.element("AXTextField", None, Rect(30, 40, 50, 20), "Müller 東京")
        self.button = self.element("AXButton", "Save", Rect(30, 80, 30, 10))
        self.main["AXChildren"] = [self.button]
        self.main["AXContents"] = [self.field]
        self.windows = [self.main]
        self.focused_window = self.main
        self.window_info_override = None
        self.hit = self.field
        self.root, self.system = {}, {}
        app = NS(processIdentifier=lambda: self.pid,
                 bundleIdentifier=lambda: "Fakturama.ID",
                 bundleURL=lambda: NS(path=lambda: "/isolated/Fakturama2.app"),
                 launchDate=lambda: NS(timeIntervalSince1970=lambda: self.launch),
                 isTerminated=lambda: not self.alive,
                 activateWithOptions_=self.activate)
        self.app = app
        workspace = NS(runningApplications=lambda: [app] if self.alive else [])
        screen = NS(deviceDescription=lambda: {"NSScreenNumber": 1},
                    backingScaleFactor=lambda: 2.0)
        self.cocoa = NS(NSWorkspace=NS(sharedWorkspace=lambda: workspace),
                        NSRunningApplication=NS(
                            runningApplicationWithProcessIdentifier_=lambda pid: app),
                        NSApplicationActivateIgnoringOtherApps=1,
                        NSScreen=NS(screens=lambda: [screen]),
                        NSPasteboard=NS(generalPasteboard=lambda: self.board),
                        NSPasteboardItem=ClipboardItem, NSPasteboardTypeString="public.text")
        self.ax = NS(kAXErrorSuccess=0, kAXErrorAttributeUnsupported=-25205,
                     kAXErrorNoValue=-25212, kAXErrorInvalidUIElement=-25202,
                     kAXValueCGPointType=1, kAXValueCGSizeType=2,
                     AXIsProcessTrusted=lambda: self.accessibility,
                     AXUIElementCreateApplication=lambda pid: self.root,
                     AXUIElementCreateSystemWide=lambda: self.system,
                     AXUIElementSetMessagingTimeout=lambda element, timeout: 0,
                     AXUIElementCopyAttributeValue=self.read,
                     AXUIElementGetPid=lambda element, ignored: (0, element.get("pid", self.pid)),
                     AXValueGetValue=lambda value, kind, ignored: value,
                     AXUIElementCopyElementAtPosition=lambda *args: (0, self.hit),
                     AXUIElementPerformAction=self.press)
        self.quartz = NS(CGPreflightScreenCaptureAccess=lambda: self.screen_recording,
                         CGDisplayBounds=lambda display: NS(origin=NS(x=0, y=0),
                                                            size=NS(width=2000, height=1000)),
                         kCGWindowListOptionOnScreenOnly=1, kCGWindowListExcludeDesktopElements=16,
                         kCGNullWindowID=0, kCGWindowNumber="number", kCGWindowOwnerPID="owner_pid",
                         kCGWindowBounds="bounds", CGWindowListCopyWindowInfo=self.window_info,
                         kCGWindowOwnerName="owner_name", kCGWindowName="name", kCGWindowLayer="layer",
                         kCGCursorWindowLevelKey=19, CGWindowLevelForKey=lambda key: 2147483630,
                         kCGEventMouseMoved="move", kCGEventLeftMouseDown="down",
                         kCGEventLeftMouseDragged="dragged",
                         kCGEventLeftMouseUp="up", kCGMouseButtonLeft=0,
                         kCGMouseEventClickState="clicks", kCGHIDEventTap="hid",
                         kCGEventFlagMaskCommand=256, kCGEventFlagMaskShift=512,
                         CGEventCreateMouseEvent=lambda source, kind, point, button:
                         dict(kind=kind, point=point, flags=999),
                         CGEventCreateKeyboardEvent=lambda source, code, down:
                         dict(kind="key-down" if down else "key-up", code=code, flags=999),
                         CGEventSetFlags=lambda event, flags: event.update(flags=flags),
                         CGEventKeyboardSetUnicodeString=lambda event, count, text:
                         event.update(unicode=text, utf16_units=count),
                         CGEventSetLocation=lambda event, point: event.update(point=point),
                         CGEventSetIntegerValueField=lambda event, field, value:
                         event.update({field: value}), CGEventPost=self.post)
        self.cf = NS(CFEqual=lambda a, b: a is b)

    def window_info(self, options, relative):
        if self.window_info_override is not None:
            return self.window_info_override
        position, size = self.main["AXPosition"], self.main["AXSize"]
        return [{"number": 7, "owner_pid": self.pid,
                 "bounds": {"X": position.x, "Y": position.y,
                            "Width": size.width, "Height": size.height}}]

    def element(self, role, title, rect, value=None):
        return {"pid": self.pid, "AXRole": role, "AXTitle": title, "AXValue": value,
                "AXPosition": NS(x=rect.x, y=rect.y),
                "AXSize": NS(width=rect.width, height=rect.height), "AXEnabled": True}

    def read(self, element, attribute, ignored):
        if element is self.system and attribute == "AXFocusedApplication":
            return 0, {"pid": self.focus}
        if element is self.root:
            return 0, {"AXWindows": self.windows,
                       "AXFocusedWindow": self.focused_window}.get(attribute)
        return (0, element[attribute]) if attribute in element else (-25205, None)

    def post(self, tap, event):
        self.events.append(dict(event))
        if self.on_post:
            self.on_post(event)

    def press(self, element, action):
        self.actions.append((element, action))
        return 0

    def activate(self, options):
        self.activation_calls += 1
        self.focus = self.pid
        return True


@pytest.fixture
def native(monkeypatch):
    native = Native()
    monkeypatch.setattr(module, "_load_native",
                        lambda: (native.ax, native.cocoa, native.cf, native.quartz))
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(module.time, "sleep", lambda delay: None)
    monkeypatch.setattr(module.ImageGrab, "grab", lambda bbox: Image.new("RGB", (200, 160)))
    return native


@pytest.fixture
def connected(native):
    driver = Desktop()
    return driver, driver.capture("Fakturama")


@pytest.mark.parametrize("values", [(0, 0, 0, 1), (0, 0, 1, -1),
                                     (float("nan"), 0, 1, 1), (0, float("inf"), 1, 1)])
def test_rectangle_rejects_invalid_geometry(values):
    with pytest.raises(DesktopError):
        Rect(*values)


@pytest.mark.parametrize('reason', ['terminated', 'identity_changed'])
def test_process_guard_retains_exact_refusal_observation_without_retry(native, reason):
    driver = Desktop()
    expected = list(driver.identity)
    calls = []
    if reason == 'terminated':
        native.alive = False
    elif reason == 'identity_changed':
        native.launch = 200.0
    def lookup(pid):
        calls.append(pid)
        return native.app
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    with pytest.raises(DesktopError, match='^Target process stopped or changed; input/capture refused.$') as stopped:
        driver._process()
    assert calls == [driver.pid]
    assert stopped.value.evidence == {
        'expected_identity': expected,
        'lookup_present': True,
        'observed_terminated': reason == 'terminated',
        'observed_identity': expected[:-1] + [200.0] if reason == 'identity_changed' else None,
        'refusal_reason': reason,
    }
    assert driver.process_rechecks == []
    assert json.loads(json.dumps(stopped.value.evidence)) == stopped.value.evidence
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize(('identity_problem', 'message'), [
    ('missing_date', 'Running application identity is incomplete.'),
    ('missing_bundle', 'Running application identity is incomplete.'),
    ('invalid_date', 'Application launch time is invalid.'),
    ('infinite_date', 'Application launch time is invalid.'),
])
def test_process_guard_preserves_distinct_incomplete_and_invalid_identity_errors(native, identity_problem, message):
    driver = Desktop()
    calls = []
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = (
        lambda pid: calls.append(pid) or native.app)
    if identity_problem == 'missing_bundle':
        native.app.bundleURL = lambda: None
    else:
        value = float('inf') if identity_problem == 'infinite_date' else float('nan')
        native.app.launchDate = (lambda: None) if identity_problem == 'missing_date' else (
            lambda: NS(timeIntervalSince1970=lambda: value))
    with pytest.raises(DesktopError) as stopped:
        driver._process()
    assert str(stopped.value) == message
    assert not hasattr(stopped.value, 'evidence')
    assert calls == [driver.pid]
    assert driver.process_rechecks == []
    assert native.events == [] and native.actions == []


class ProcessClock:
    """Advance only when the guard sleeps; native observations can consume time."""

    def __init__(self, monkeypatch, *, start=10.0, advance=True, on_sleep=None):
        self.now = start
        self.sleeps = []
        self.advance = advance
        self.on_sleep = on_sleep
        monkeypatch.setattr(module.time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(module.time, 'sleep', self.sleep)

    def sleep(self, delay):
        self.sleeps.append(delay)
        if self.advance:
            self.now = round(self.now + delay, 9)
        if self.on_sleep is not None:
            self.on_sleep()


def test_present_same_process_avoids_wait_or_alternative_discovery(native, monkeypatch):
    driver = Desktop()
    calls = []
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = (
        lambda pid: calls.append(pid) or native.app)
    monkeypatch.setattr(module.time, 'sleep', lambda delay: pytest.fail('No wait is needed.'))
    native.cocoa.NSWorkspace.sharedWorkspace = lambda: pytest.fail('Ownership must stay fixed.')
    native.cocoa.NSRunLoop = NS(mainRunLoop=lambda: pytest.fail('No run-loop delivery is permitted.'))
    assert driver._process() is native.app
    assert calls == [driver.pid] and driver.process_rechecks == []
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize('available_after', [.04, .14, .24])
def test_missing_process_lookup_waits_only_for_same_identity_without_callbacks_or_input(
        native, monkeypatch, available_after):
    driver = Desktop()
    identity = driver.identity
    clock = ProcessClock(monkeypatch)
    calls = []
    def lookup(pid):
        calls.append((pid, clock.now))
        assert len(calls) <= 6
        return native.app if clock.now - 10.0 >= available_after else None
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    native.cocoa.NSRunLoop = NS(mainRunLoop=lambda: pytest.fail('No run-loop delivery is permitted.'))
    assert driver._process() is native.app
    assert all(pid == driver.pid for pid, _ in calls)
    assert calls[0] == (driver.pid, 10.0)
    assert len(clock.sleeps) == len(calls) - 1
    assert all(0 < delay <= .05 for delay in clock.sleeps)
    assert 0 < clock.now - 10.0 <= .25
    assert driver.identity == identity
    assert len(driver.process_rechecks) == 1
    observed = driver.process_rechecks[0]
    assert observed['index'] == 0 and observed['expected_identity'] == list(identity)
    assert observed['initial_lookup_present'] is False
    assert observed['second_lookup_present'] is True
    assert observed['second_observed_terminated'] is False
    assert observed['second_observed_identity'] == list(identity)
    assert observed['passed'] is True and observed['refusal_reason'] is None
    assert observed['policy'] == 'bounded_exact_identity_wait'
    assert observed['timeout_seconds'] == .25 and observed['interval_seconds'] == .05
    assert observed['lookup_count'] == len(calls)
    assert observed['delayed_lookup_count'] == len(clock.sleeps)
    assert observed['elapsed_seconds'] == pytest.approx(clock.now - 10.0)
    assert json.loads(json.dumps(driver.process_rechecks)) == driver.process_rechecks
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize('advance', [True, False])
def test_permanently_missing_process_is_bounded_by_deadline_and_observation_cap(
        native, monkeypatch, advance):
    driver = Desktop()
    clock = ProcessClock(monkeypatch, advance=advance)
    calls = []
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = (
        lambda pid: calls.append((pid, clock.now)) or None)
    native.cocoa.NSRunLoop = NS(mainRunLoop=lambda: pytest.fail('No run-loop delivery is permitted.'))
    with pytest.raises(DesktopError, match='Target process stopped or changed') as stopped:
        driver._process()
    assert [pid for pid, _ in calls] == [driver.pid] * 6
    assert len(clock.sleeps) == 5 and all(0 < delay <= .05 for delay in clock.sleeps)
    assert clock.now == pytest.approx(10.25 if advance else 10.0)
    observed = driver.process_rechecks[0]
    assert observed['initial_lookup_present'] is False
    assert observed['second_lookup_present'] is False
    assert observed['second_observed_identity'] is None
    assert observed['passed'] is False and observed['refusal_reason'] == 'None'
    assert observed['lookup_count'] == 6 and observed['delayed_lookup_count'] == 5
    assert observed['elapsed_seconds'] == pytest.approx(.25 if advance else 0)
    assert stopped.value.evidence['process_recheck'] == observed
    assert json.loads(json.dumps(stopped.value.evidence, allow_nan=False)) == stopped.value.evidence
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize('revoked_at', ['sleep', 'reappearance'])
def test_process_wait_lost_permission_stops_before_returning_a_recovered_application(
        native, monkeypatch, revoked_at):
    driver = Desktop()
    clock = ProcessClock(monkeypatch, on_sleep=(
        lambda: setattr(native, 'accessibility', False)) if revoked_at == 'sleep' else None)
    calls = []
    def lookup(pid):
        calls.append(pid)
        if len(calls) == 1:
            return None
        native.accessibility = False
        return native.app
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    with pytest.raises(DesktopError, match='Accessibility permission is unavailable') as stopped:
        driver._process()
    expected_count = 1 if revoked_at == 'sleep' else 2
    assert calls == [driver.pid] * expected_count and clock.sleeps == [.05]
    assert driver.identity == (123, 'Fakturama.ID', '/isolated/Fakturama2.app', 100.0)
    observed = driver.process_rechecks[0]
    assert observed['passed'] is False and observed['refusal_reason'] == 'permission_unavailable'
    assert observed['lookup_count'] == expected_count
    assert observed['delayed_lookup_count'] == expected_count - 1
    assert observed['second_lookup_present'] is (revoked_at == 'reappearance')
    assert stopped.value.evidence['process_recheck'] == observed
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_process_reappearance_exactly_at_deadline_can_pass_without_additional_wait(
        native, monkeypatch):
    driver = Desktop()
    clock = ProcessClock(monkeypatch)
    calls = []
    def lookup(pid):
        calls.append((pid, clock.now))
        return native.app if clock.now == 10.25 else None
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    assert driver._process() is native.app
    assert len(calls) == 6 and calls[-1] == (driver.pid, 10.25)
    assert len(clock.sleeps) == 5
    assert driver.process_rechecks[0]['passed'] is True
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize('late_at', ['sleep', 'native_lookup'])
def test_same_identity_cannot_be_accepted_after_the_process_wait_deadline(
        native, monkeypatch, late_at):
    driver = Desktop()
    clock = ProcessClock(monkeypatch, on_sleep=(
        lambda: setattr(clock, 'now', 10.251)) if late_at == 'sleep' else None)
    calls = []
    def lookup(pid):
        calls.append(pid)
        if len(calls) == 1:
            return None
        clock.now = 10.251
        return native.app
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    with pytest.raises(DesktopError, match='Target process stopped or changed') as stopped:
        driver._process()
    assert calls == [driver.pid] * (1 if late_at == 'sleep' else 2)
    assert clock.sleeps == [.05]
    observed = driver.process_rechecks[0]
    assert observed['passed'] is False
    assert observed['elapsed_seconds'] == pytest.approx(.251)
    if late_at == 'native_lookup':
        assert observed['refusal_reason'] == 'deadline_exceeded'
        assert observed['second_observed_identity'] == list(driver.identity)
        assert observed['second_observed_terminated'] is False
    assert stopped.value.evidence['process_recheck'] == observed
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_delayed_native_lookup_error_is_preserved_without_false_permission_evidence(
        native, monkeypatch):
    driver = Desktop()
    clock = ProcessClock(monkeypatch)
    original = DesktopError('Exact process lookup failed for a non-permission reason.')
    calls = []
    def lookup(pid):
        calls.append(pid)
        if len(calls) == 1:
            return None
        raise original
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    with pytest.raises(DesktopError) as stopped:
        driver._process()
    assert stopped.value is original and not hasattr(original, 'evidence')
    assert calls == [driver.pid, driver.pid] and clock.sleeps == [.05]
    assert driver.process_rechecks == []
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_identity_metadata_read_crossing_deadline_refuses_the_unchanged_application(
        native, monkeypatch):
    driver = Desktop()
    clock = ProcessClock(monkeypatch)
    expected_identity = driver.identity
    calls, metadata_reads = [], []
    bundle_url = native.app.bundleURL
    def delayed_url():
        metadata_reads.append(clock.now)
        clock.now = 10.251
        return bundle_url()
    native.app.bundleURL = delayed_url
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = (
        lambda pid: calls.append(pid) or (None if len(calls) == 1 else native.app))
    with pytest.raises(DesktopError, match='Target process stopped or changed') as stopped:
        driver._process()
    assert calls == [driver.pid, driver.pid] and clock.sleeps == [.05]
    assert metadata_reads == [10.05]
    observed = driver.process_rechecks[0]
    assert observed['second_lookup_present'] is True
    assert observed['second_observed_terminated'] is False
    assert observed['second_observed_identity'] == list(expected_identity)
    assert observed['passed'] is False and observed['refusal_reason'] == 'deadline_exceeded'
    assert observed['elapsed_seconds'] == pytest.approx(.251)
    assert stopped.value.evidence['process_recheck'] == observed
    assert driver.identity == expected_identity
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize(('outcome', 'message', 'reason'), [
    ('terminated', 'Target process stopped or changed', 'terminated'),
    ('pid', 'Target process stopped or changed', 'identity_changed'),
    ('bundle', 'Target process stopped or changed', 'identity_changed'),
    ('path', 'Target process stopped or changed', 'identity_changed'),
    ('launch', 'Target process stopped or changed', 'identity_changed'),
    ('missing_date', 'Running application identity is incomplete.', 'identity_unreadable'),
    ('missing_bundle', 'Running application identity is incomplete.', 'identity_unreadable'),
    ('invalid_date', 'Application launch time is invalid.', 'identity_unreadable'),
    ('infinite_date', 'Application launch time is invalid.', 'identity_unreadable'),
])
def test_process_wait_refuses_first_untrusted_reappearance_without_later_healthy_lookup(
        native, monkeypatch, outcome, message, reason):
    driver = Desktop()
    expected_identity = driver.identity
    clock = ProcessClock(monkeypatch)
    second = NS(**vars(native.app))
    if outcome == 'terminated':
        second.isTerminated = lambda: True
    elif outcome == 'pid':
        second.processIdentifier = lambda: 999
    elif outcome == 'bundle':
        second.bundleIdentifier = lambda: 'Other.app'
    elif outcome == 'path':
        second.bundleURL = lambda: NS(path=lambda: '/another/Fakturama2.app')
    elif outcome == 'launch':
        second.launchDate = lambda: NS(timeIntervalSince1970=lambda: 200.0)
    elif outcome == 'missing_date':
        second.launchDate = lambda: None
    elif outcome == 'missing_bundle':
        second.bundleURL = lambda: None
    elif outcome in {'invalid_date', 'infinite_date'}:
        value = float('nan') if outcome == 'invalid_date' else float('inf')
        second.launchDate = lambda: NS(timeIntervalSince1970=lambda: value)
    calls = []
    def lookup(pid):
        calls.append(pid)
        assert len(calls) <= 2, 'A third lookup must never accept a later healthy result.'
        return None if len(calls) == 1 else second
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    native.cocoa.NSRunLoop = NS(mainRunLoop=lambda: pytest.fail('No run-loop delivery is permitted.'))
    with pytest.raises(DesktopError, match=message) as stopped:
        driver._process()
    assert calls == [driver.pid, driver.pid]
    assert clock.sleeps == [.05]
    assert driver.identity == expected_identity
    assert len(driver.process_rechecks) == 1
    observed = driver.process_rechecks[0]
    assert observed['index'] == 0
    assert observed['initial_lookup_present'] is False
    assert observed['second_lookup_present'] is True
    assert observed['expected_identity'] == list(expected_identity)
    assert observed['passed'] is False and observed['refusal_reason'] == reason
    assert observed['lookup_count'] == 2 and observed['delayed_lookup_count'] == 1
    assert observed['elapsed_seconds'] == pytest.approx(.05)
    assert stopped.value.evidence['process_recheck'] == observed
    assert json.loads(json.dumps(stopped.value.evidence, allow_nan=False)) == stopped.value.evidence
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_process_recheck_history_preserves_each_separate_read_observation(native, monkeypatch):
    driver = Desktop()
    clock = ProcessClock(monkeypatch)
    calls = []
    def lookup(pid):
        calls.append(pid)
        return None if len(calls) % 2 else native.app
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    assert driver._process() is native.app
    first = dict(driver.process_rechecks[0])
    assert driver._process() is native.app
    assert calls == [driver.pid] * 4
    assert [row['index'] for row in driver.process_rechecks] == [0, 1]
    assert driver.process_rechecks[0] == first
    assert all(row['passed'] is True for row in driver.process_rechecks)
    assert clock.sleeps == [.05, .05]
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


@pytest.mark.parametrize(('change', 'message'), [
    (lambda n: setattr(n, 'focus', 999), 'not freshly focused'),
    (lambda n: n.main.update(AXPosition=NS(x=11, y=20)), 'moved or resized'),
    (lambda n: setattr(n, 'focused_window', n.field), 'focused window'),
])
def test_process_wait_does_not_bypass_fresh_focus_geometry_or_window_guards(
        connected, native, monkeypatch, change, message):
    driver, capture = connected
    ProcessClock(monkeypatch)
    calls = []
    def lookup(pid):
        calls.append(pid)
        if len(calls) == 1:
            return None
        if len(calls) == 2:
            change(native)
        return native.app
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = lookup
    with pytest.raises(DesktopError, match=message):
        driver.click(capture, 40, 50)
    assert len(driver.process_rechecks) == 1 and driver.process_rechecks[0]['passed'] is True
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_successful_process_wait_does_not_extend_the_capture_freshness_limit(
        connected, native, monkeypatch):
    driver, capture = connected
    clock = ProcessClock(monkeypatch, start=14.98)
    calls = []
    native.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_ = (
        lambda pid: calls.append(pid) or (None if len(calls) == 1 else native.app))
    with pytest.raises(DesktopError, match='Capture is stale'):
        driver.click(capture, 40, 50)
    assert calls == [driver.pid, driver.pid] and clock.now == pytest.approx(15.03)
    assert driver.process_rechecks[0]['passed'] is True
    assert capture.captured_at == 10.0
    assert native.events == [] and native.actions == [] and native.activation_calls == 0


def test_coordinate_transform_preserves_offsets_and_rejects_far_edge(connected):
    _, capture = connected
    assert capture.screen_point(40, 60) == (30, 50)
    assert capture.screen_point(0, 0) == (10, 20)
    with pytest.raises(DesktopError, match="outside"):
        capture.screen_point(200, 0)
    with pytest.raises(DesktopError, match="outside"):
        capture.screen_point(float("nan"), 0)
    with pytest.raises(DesktopError, match="scaling"):
        replace(capture, scale_x=1)


def test_capture_ocr_cached_by_psm_and_copy_of_list_is_returned(connected, monkeypatch):
    _, capture = connected
    calls = []
    token = module.Token("Save", 1, 1, 10, 8, 99)
    monkeypatch.setattr(module, "ocr_tokens", lambda image, psm: calls.append(psm) or [token])
    first = capture.tokens()
    first.clear()
    assert capture.tokens() == [token]
    capture.tokens(psm=6)
    assert calls == [11, 6]


def test_invalid_ocr_geometry_never_cached(connected, monkeypatch):
    _, capture = connected
    monkeypatch.setattr(module, "ocr_tokens",
                        lambda image, psm: [module.Token("wrong", 199, 0, 5, 8, 99)])
    with pytest.raises(DesktopError, match="invalid token"):
        capture.tokens()
    assert not capture._tokens


def test_evidence_does_not_overwrite_an_earlier_capture(connected, tmp_path):
    _, capture = connected
    path = tmp_path / "capture.png"
    capture.save(path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        capture.save(path)
    assert path.read_bytes() == before


def activation_clock(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    return clock


def sequence_focus(native, samples):
    read = native.ax.AXUIElementCopyAttributeValue
    samples = iter(samples)
    observed = []
    def sequenced(element, attribute, ignored):
        if element is native.system and attribute == "AXFocusedApplication":
            code, pid = next(samples)
            observed.append((code, pid))
            return code, {"pid": pid} if pid is not None else None
        return read(element, attribute, ignored)
    native.ax.AXUIElementCopyAttributeValue = sequenced
    return observed


def test_already_focused_activation_avoids_reactivation_but_observes_two_matches(native, monkeypatch):
    clock = activation_clock(monkeypatch)
    observed = sequence_focus(native, [(0, 123), (0, 123), (0, 123)])
    assert Desktop().activate() is True
    assert native.activation_calls == 0
    assert observed == [(0, 123)] * 3  # Precheck, then two separate polling observations.
    assert clock[0] == pytest.approx(10.1)


def test_activation_transient_focus_reads_reset_match_streak_until_two_ready(native, monkeypatch):
    clock = activation_clock(monkeypatch)
    observed = sequence_focus(native, [(0, 999), (-25212, None), (0, 123),
                                       (-25204, None), (0, 123), (0, 123)])
    assert Desktop().activate() is True
    assert native.activation_calls == 1
    assert len(observed) == 6
    assert clock[0] == pytest.approx(10.4)
    assert not native.events


def test_activation_other_pid_between_matches_resets_readiness(native, monkeypatch):
    clock = activation_clock(monkeypatch)
    observed = sequence_focus(native, [(0, 999), (0, 123), (0, 999), (0, 123), (0, 123)])
    assert Desktop().activate() is True
    assert native.activation_calls == 1
    assert len(observed) == 5
    assert clock[0] == pytest.approx(10.3)


@pytest.mark.parametrize("code", [-25212, -25204])
def test_activation_transient_precheck_can_activate_then_wait(native, monkeypatch, code):
    activation_clock(monkeypatch)
    sequence_focus(native, [(code, None), (0, 123), (0, 123)])
    assert Desktop().activate() is True
    assert native.activation_calls == 1


def test_activation_other_attribute_errors_stop_instead_of_being_retried(native, monkeypatch):
    activation_clock(monkeypatch)
    sequence_focus(native, [(0, 999), (-25205, None)])
    with pytest.raises(DesktopError, match="error -25205"):
        Desktop().activate()
    assert native.activation_calls == 1


def test_activation_pid_query_failure_is_not_treated_as_transient_attribute_error(native):
    native.ax.AXUIElementGetPid = lambda element, ignored: (-25204, None)
    with pytest.raises(DesktopError, match="Focused PID read failed"):
        Desktop().activate()
    assert native.activation_calls == 0


def test_activation_readiness_timeout_is_bounded(native, monkeypatch):
    clock = activation_clock(monkeypatch)
    native.focus = 999
    native.app.activateWithOptions_ = lambda options: True  # Activation never establishes AX focus.
    with pytest.raises(DesktopError, match="Timed out"):
        Desktop().activate()
    assert clock[0] == pytest.approx(12)
    assert not native.events


@pytest.mark.parametrize("change,error", [
    (lambda n: setattr(n, "focus", 999), "not freshly focused"),
    (lambda n: setattr(n, "alive", False), "process stopped or changed"),
    (lambda n: setattr(n, "launch", 200), "process stopped or changed"),
    (lambda n: setattr(n, "screen_recording", False), "Screen Recording"),
    (lambda n: n.main.update(AXPosition=NS(x=11, y=20)), "moved or resized"),
    (lambda n: setattr(n, "focused_window", n.field), "focused window"),
])
def test_changed_desktop_state_refuses_input(connected, native, change, error):
    driver, capture = connected
    change(native)
    with pytest.raises(DesktopError, match=error):
        driver.click(capture, 40, 50)
    assert native.events == []


def test_same_title_and_geometry_cannot_reuse_a_replaced_window(connected, native):
    driver, capture = connected
    replacement = native.element("AXWindow", "Fakturama", capture.window_bounds)
    native.windows = [replacement]
    native.focused_window = replacement
    with pytest.raises(DesktopError, match="moved or resized"):
        driver.click(capture, 40, 50)
    assert not native.events


def test_invalid_window_entry_refreshes_complete_inventory_once(native):
    stale = native.element("AXWindow", "closed", Rect(60, 60, 20, 15))
    read = native.ax.AXUIElementCopyAttributeValue
    inventories = []
    def inventory(element, attribute, ignored):
        if element is native.root and attribute == "AXWindows":
            inventories.append(True)
            return 0, [stale, native.main] if len(inventories) == 1 else [native.main]
        if element is stale:
            return -25202, None
        return read(element, attribute, ignored)
    native.ax.AXUIElementCopyAttributeValue = inventory
    windows = Desktop().windows()
    assert [window.element for window in windows] == [native.main]
    assert len(inventories) == 2
    assert not native.events


@pytest.mark.parametrize("code,expected_inventories", [(-25202, 2), (-25204, 1)])
def test_repeated_invalid_window_or_other_read_error_stops_inventory(
        native, code, expected_inventories):
    broken = native.element("AXWindow", "broken", Rect(60, 60, 20, 15))
    read = native.ax.AXUIElementCopyAttributeValue
    inventories = []
    def inventory(element, attribute, ignored):
        if element is native.root and attribute == "AXWindows":
            inventories.append(True)
            return 0, [native.main, broken]
        if element is broken:
            return code, None
        return read(element, attribute, ignored)
    native.ax.AXUIElementCopyAttributeValue = inventory
    with pytest.raises(DesktopError, match=f"AXRole: accessibility error {code}"):
        Desktop().windows()
    assert len(inventories) == expected_inventories
    assert not native.events


def test_invalid_window_refresh_requires_unchanged_process(native):
    broken = native.element("AXWindow", "broken", Rect(60, 60, 20, 15))
    read = native.ax.AXUIElementCopyAttributeValue
    inventories = []
    def inventory(element, attribute, ignored):
        if element is native.root and attribute == "AXWindows":
            inventories.append(True)
            return 0, [broken]
        if element is broken:
            native.launch = 200
            return -25202, None
        return read(element, attribute, ignored)
    native.ax.AXUIElementCopyAttributeValue = inventory
    with pytest.raises(DesktopError, match="process stopped or changed"):
        Desktop().windows()
    assert len(inventories) == 1
    assert not native.events


def test_unnamed_explicitly_nonmodal_auxiliary_is_inventory_only(native):
    auxiliary = native.element("AXWindow", "", Rect(60, 60, 20, 15))
    auxiliary["AXModal"] = False
    native.windows.insert(0, auxiliary)
    driver = Desktop()
    assert [window.title for window in driver.windows()] == ["", "Fakturama"]
    assert driver.window("Fakturama").element is native.main
    capture = driver.capture("Fakturama")
    assert driver.guard(capture).element is native.main
    for title in ("", " ", None):
        with pytest.raises(DesktopError, match="exact nonempty title"):
            driver.window(title)
    assert not native.events


@pytest.mark.parametrize("modal", [True, None])
def test_unnamed_modal_or_unknown_modal_window_blocks_inventory(native, modal):
    auxiliary = native.element("AXWindow", "", Rect(60, 60, 20, 15))
    if modal is not None:
        auxiliary["AXModal"] = modal
    native.windows.insert(0, auxiliary)
    driver = Desktop()
    with pytest.raises(DesktopError, match="unnamed modal or unknown-modal"):
        driver.windows()
    with pytest.raises(DesktopError, match="unnamed modal or unknown-modal"):
        driver.capture("Fakturama")
    assert not native.events


def test_unnamed_nonmodal_auxiliary_focus_blocks_main_window_actions(connected, native):
    driver, capture = connected
    auxiliary = native.element("AXWindow", "", Rect(60, 60, 20, 15))
    auxiliary["AXModal"] = False
    native.windows.insert(0, auxiliary)
    native.focused_window = auxiliary
    with pytest.raises(DesktopError, match="focused window"):
        driver.click(capture, 40, 50)
    assert not native.events


@pytest.mark.parametrize("modal", [None, False])
def test_owned_nonfocused_native_help_tag_is_skipped_without_weakening_main_guards(native, modal):
    help_tag = native.element("AXHelpTag", None, Rect(60, 60, 25, 10))
    if modal is not None:
        help_tag["AXModal"] = modal
    native.windows.insert(0, help_tag)
    driver = Desktop()
    assert [window.title for window in driver.windows()] == ["Fakturama"]
    assert driver.capture("Fakturama").window_element is native.main
    assert not native.events


@pytest.mark.parametrize("change", [
    lambda n, tag: tag.update(pid=999),
    lambda n, tag: tag.update(AXModal=True),
    lambda n, tag: tag.update(AXModal="unknown"),
    lambda n, tag: setattr(n, "focused_window", tag),
    lambda n, tag: setattr(n, "focused_window", None),
])
def test_help_tag_exception_refuses_foreign_modal_or_unknown_focus(native, change):
    help_tag = native.element("AXHelpTag", None, Rect(60, 60, 25, 10))
    native.windows.insert(0, help_tag)
    change(native, help_tag)
    with pytest.raises(DesktopError):
        Desktop().windows()
    assert not native.events


def test_help_tag_pid_read_error_prevents_exception(native):
    help_tag = native.element("AXHelpTag", None, Rect(60, 60, 25, 10))
    native.windows.insert(0, help_tag)
    get_pid = native.ax.AXUIElementGetPid
    native.ax.AXUIElementGetPid = lambda element, ignored: (
        (-25204, None) if element is help_tag else get_pid(element, ignored))
    with pytest.raises(DesktopError, match="Tooltip ownership"):
        Desktop().windows()
    assert not native.events


@pytest.mark.parametrize("time", [4, 15.001])
def test_future_or_stale_capture_is_refused(connected, native, monkeypatch, time):
    driver, capture = connected
    monkeypatch.setattr(module.time, "monotonic", lambda: time)
    with pytest.raises(DesktopError, match="stale"):
        driver.key(capture, "return")
    assert not native.events


def test_capture_from_different_process_is_refused(connected, native):
    driver, capture = connected
    with pytest.raises(DesktopError, match="another process"):
        driver.click(replace(capture, pid=777), 40, 50)
    assert not native.events


def test_capture_discards_focus_change_and_wrong_backing_scale(native, monkeypatch):
    driver = Desktop()
    def changed(bbox):
        native.focus = 999
        return Image.new("RGB", (200, 160))
    monkeypatch.setattr(module.ImageGrab, "grab", changed)
    with pytest.raises(DesktopError, match="focused"):
        driver.capture("Fakturama")
    native.focus = native.pid
    monkeypatch.setattr(module.ImageGrab, "grab", lambda bbox: Image.new("RGB", (100, 80)))
    with pytest.raises(DesktopError, match="display scale"):
        driver.capture("Fakturama")


def foreign_window(*, x=30, y=40, width=50, height=20, number=8):
    return {"number": number, "owner_pid": 987,
            "bounds": {"X": x, "Y": y, "Width": width, "Height": height}}


def test_foreign_overlapping_window_above_target_refuses_before_screen_read(native, monkeypatch):
    target = native.window_info(17, 0)[0]
    native.window_info_override = [foreign_window(), target]
    grabs = []
    monkeypatch.setattr(module.ImageGrab, "grab", lambda bbox: grabs.append(bbox))
    with pytest.raises(DesktopError, match="foreign onscreen window") as stopped:
        Desktop().capture("Fakturama")
    assert not grabs and not native.events
    assert stopped.value.evidence["capture_visibility"]["overlapping_foreign_windows"] == [
        {"window_number": 8, "owner_pid": 987,
         "bounds": {"x": 30, "y": 40, "width": 50, "height": 20}}]


@pytest.mark.parametrize("inventory", ["behind", "disjoint", "touching"])
def test_foreign_nonoccluding_window_preserves_exact_capture(native, inventory):
    target = native.window_info(17, 0)[0]
    if inventory == "behind":
        native.window_info_override = [target, foreign_window()]
    else:
        # x=110 touches the target's right edge; there is no positive-area overlap.
        native.window_info_override = [foreign_window(x=110 if inventory == "touching" else 500), target]
    capture = Desktop().capture("Fakturama")
    assert capture.image.size == (200, 160)
    assert capture.metadata()["window_visibility"]["after"]["target_window"]["window_number"] == 7
    assert not native.events


def test_foreign_window_appearing_during_capture_discards_image(native, monkeypatch):
    target = native.window_info(17, 0)[0]

    def obstructed(bbox):
        native.window_info_override = [foreign_window(), target]
        return Image.new("RGB", (200, 160))

    monkeypatch.setattr(module.ImageGrab, "grab", obstructed)
    with pytest.raises(DesktopError, match="foreign onscreen window"):
        Desktop().capture("Fakturama")


@pytest.mark.parametrize("inventory", [[], "duplicate", "invalid", "missing_field"])
def test_unknown_or_nonunique_window_inventory_refuses_capture(native, monkeypatch, inventory):
    target = native.window_info(17, 0)[0]
    if inventory == "duplicate":
        value = [target, {**target, "number": 9}]
    elif inventory == "invalid":
        value = [foreign_window(width=float("nan")), target]
    elif inventory == "missing_field":
        value = [{"owner_pid": 987}, target]
    else:
        value = inventory
    native.window_info_override = value
    grabs = []
    monkeypatch.setattr(module.ImageGrab, "grab", lambda bbox: grabs.append(bbox))
    with pytest.raises(DesktopError):
        Desktop().capture("Fakturama")
    assert not grabs


def test_native_focused_target_and_equal_geometry_cannot_hide_replaced_quartz_window(native, monkeypatch):
    target = native.window_info(17, 0)[0]

    def replaced(bbox):
        native.window_info_override = [{**target, "number": 100}]
        return Image.new("RGB", (200, 160))

    monkeypatch.setattr(module.ImageGrab, "grab", replaced)
    with pytest.raises(DesktopError, match="Onscreen target window identity changed") as stopped:
        Desktop().capture("Fakturama")
    assert stopped.value.evidence["capture_visibility"]["before"]["target_window"]["window_number"] == 7
    assert stopped.value.evidence["capture_visibility"]["after"]["target_window"]["window_number"] == 100


def test_unavailable_quartz_inventory_refuses_before_capture(native, monkeypatch):
    native.quartz.CGWindowListCopyWindowInfo = lambda *args: None
    grabs = []
    monkeypatch.setattr(module.ImageGrab, "grab", lambda bbox: grabs.append(bbox))
    with pytest.raises(DesktopError, match="inventory is unavailable"):
        Desktop().capture("Fakturama")
    assert not grabs


def test_complete_native_window_server_cursor_triple_is_recorded_without_occlusion(native):
    target = native.window_info(17, 0)[0]
    cursor = {**foreign_window(), "owner_name": "Window Server", "name": "Cursor", "layer": 2147483630}
    native.window_info_override = [cursor, target]
    capture = Desktop().capture("Fakturama")
    for stage in ("before", "after"):
        observed = capture.metadata()["window_visibility"][stage]
        assert observed["overlapping_foreign_windows"] == []
        assert observed["ignored_cursor_windows"][0]["name"] == "Cursor"
        assert observed["ignored_cursor_windows"][0]["owner_name"] == "Window Server"
        assert observed["ignored_cursor_windows"][0]["layer"] == observed["cursor_window_level"]


@pytest.mark.parametrize("changed", [
    lambda cursor: cursor.update(layer=0),
    lambda cursor: cursor.update(name="Cursor Highlight"),
    lambda cursor: cursor.update(owner_name="Another app"),
    lambda cursor: cursor.pop("layer"),
    lambda cursor: cursor.pop("name"),
    lambda cursor: cursor.pop("owner_name"),
])
def test_cursor_name_owner_or_high_layer_alone_cannot_hide_an_overlay(native, changed):
    target = native.window_info(17, 0)[0]
    cursor = {**foreign_window(), "owner_name": "Window Server", "name": "Cursor", "layer": 2147483630}
    changed(cursor)
    native.window_info_override = [cursor, target]
    with pytest.raises(DesktopError, match="foreign onscreen window") as stopped:
        Desktop().capture("Fakturama")
    assert stopped.value.evidence["capture_visibility"]["ignored_cursor_windows"] == []


@pytest.mark.parametrize("unavailable", ["constant", "readback"])
def test_unreadable_documented_cursor_level_cannot_exempt_window(native, unavailable):
    target = native.window_info(17, 0)[0]
    cursor = {**foreign_window(), "owner_name": "Window Server", "name": "Cursor", "layer": 2147483630}
    native.window_info_override = [cursor, target]
    if unavailable == "constant":
        del native.quartz.kCGCursorWindowLevelKey
    else:
        native.quartz.CGWindowLevelForKey = lambda key: None
    with pytest.raises(DesktopError, match="foreign onscreen window"):
        Desktop().capture("Fakturama")


@pytest.fixture
def owned_popup(native):
    native.main["AXTitle"] = ""
    native.main["AXModal"] = False
    native.main["AXEnabled"] = None
    element = native.element("AXRow", None, Rect(30, 40, 50, 20))
    element["AXEnabled"] = None
    driver = Desktop()
    window = Window("", Rect(10, 20, 100, 80), native.main)
    row = driver._control(element)
    return driver, window, row, element


def popup_click(driver, window, row, **guards):
    return driver.click_owned_popup(window, row, 55, 50,
                                    source_guard=guards.get("source_guard", lambda: True),
                                    content_guard=guards.get("content_guard", lambda: True))


def test_owned_popup_click_dispatches_one_clean_paired_click_without_generic_capture(owned_popup, native,
                                                                                    monkeypatch):
    driver, window, row, _ = owned_popup
    monkeypatch.setattr(module.ImageGrab, "grab", lambda **kwargs: pytest.fail("No unnamed capture allowed"))
    result = popup_click(driver, window, row)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]
    assert all(event["flags"] == 0 for event in native.events)
    assert [event["clicks"] for event in native.events[1:]] == [1, 1]
    assert result["click_dispatches"] == 1
    assert result["row_bounds"] == row.bounds.as_dict()


@pytest.mark.parametrize("changed", [
    lambda n, element: element.update(AXValue="changed"),
    lambda n, element: element.update(AXEnabled=False),
    lambda n, element: element.update(AXPosition=NS(x=31, y=40)),
    lambda n, element: element.update(pid=999),
    lambda n, element: n.main.update(AXModal=True),
    lambda n, element: n.main.update(AXTitle="Another popup"),
    lambda n, element: n.main.update(AXPosition=NS(x=11, y=20)),
    lambda n, element: setattr(n, "focus", 999),
    lambda n, element: setattr(n, "focused_window", n.field),
])
def test_changed_popup_row_window_or_focus_refuses_before_any_mouse_down(owned_popup, native, changed):
    driver, window, row, element = owned_popup
    changed(native, element)
    with pytest.raises(DesktopError):
        popup_click(driver, window, row)
    assert not native.events


@pytest.mark.parametrize("guards", [
    {"source_guard": lambda: False}, {"content_guard": lambda: False},
    {"source_guard": lambda: None}, {"content_guard": lambda: "unproved"},
])
def test_unknown_source_or_choices_refuse_popup_click(owned_popup, native, guards):
    driver, window, row, _ = owned_popup
    with pytest.raises(DesktopError, match="source or complete choices changed"):
        popup_click(driver, window, row, **guards)
    assert not native.events


@pytest.mark.parametrize("point", [(80, 50), (55, 60), (float("nan"), 50)])
def test_popup_point_must_remain_inside_exact_row(owned_popup, native, point):
    driver, window, row, _ = owned_popup
    with pytest.raises(DesktopError, match="contained point"):
        driver.click_owned_popup(window, row, *point, source_guard=lambda: True, content_guard=lambda: True)
    assert not native.events


def test_foreign_popup_occlusion_refuses_before_mouse_down(owned_popup, native):
    driver, window, row, _ = owned_popup
    target = native.window_info(17, 0)[0]
    native.window_info_override = [foreign_window(), target]
    with pytest.raises(DesktopError, match="foreign onscreen window"):
        popup_click(driver, window, row)
    assert not native.events


def test_choices_changing_after_pointer_move_refuse_before_mouse_down(owned_popup, native):
    driver, window, row, _ = owned_popup
    choices = [True]
    native.on_post = lambda event: choices.__setitem__(0, False) if event["kind"] == "move" else None
    with pytest.raises(DesktopError, match="source or complete choices changed"):
        popup_click(driver, window, row, content_guard=lambda: choices[0])
    assert [event["kind"] for event in native.events] == ["move"]


def test_foreign_window_appearing_after_popup_pointer_move_refuses_mouse_down(owned_popup, native):
    driver, window, row, _ = owned_popup
    target = native.window_info(17, 0)[0]

    def obscures(event):
        if event["kind"] == "move":
            native.window_info_override = [foreign_window(), target]

    native.on_post = obscures
    with pytest.raises(DesktopError, match="foreign onscreen window"):
        popup_click(driver, window, row)
    assert [event["kind"] for event in native.events] == ["move"]


def test_popup_row_changed_during_allocation_refuses_before_pointer_or_down(owned_popup, native):
    driver, window, row, element = owned_popup
    allocate = native.quartz.CGEventCreateMouseEvent

    def changed(source, kind, point, button):
        event = allocate(source, kind, point, button)
        if kind == "move":
            element["AXValue"] = "changed"
        return event

    native.quartz.CGEventCreateMouseEvent = changed
    with pytest.raises(DesktopError, match="exact popup row"):
        popup_click(driver, window, row)
    assert not native.events


def test_popup_row_must_fit_completely_inside_window_even_if_point_is_inside_both(owned_popup, native):
    driver, window, _, element = owned_popup
    element["AXPosition"] = NS(x=90, y=40)
    row = driver._control(element)
    with pytest.raises(DesktopError, match="exact popup row"):
        driver.click_owned_popup(window, row, 95, 50, source_guard=lambda: True, content_guard=lambda: True)
    assert not native.events


def test_closed_popup_after_mouse_down_still_gets_its_one_preallocated_release(owned_popup, native):
    driver, window, row, _ = owned_popup

    def closes(event):
        if event["kind"] == "down":
            native.focused_window = native.field
            native.windows = []

    native.on_post = closes
    popup_click(driver, window, row)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]


def test_uncertain_popup_mouse_down_is_not_retried_and_button_is_released(owned_popup, native):
    driver, window, row, _ = owned_popup

    def uncertain(event):
        if event["kind"] == "down":
            raise DesktopError("Uncertain native dispatch")

    native.on_post = uncertain
    with pytest.raises(DesktopError, match="Uncertain native dispatch"):
        popup_click(driver, window, row)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]


@pytest.mark.parametrize("failed_kind", ["move", "down", "up"])
def test_popup_allocates_release_before_sending_any_input(owned_popup, native, failed_kind):
    driver, window, row, _ = owned_popup
    allocate = native.quartz.CGEventCreateMouseEvent
    native.quartz.CGEventCreateMouseEvent = lambda source, kind, point, button: (
        None if kind == failed_kind else allocate(source, kind, point, button))
    with pytest.raises(DesktopError, match="Could not allocate"):
        popup_click(driver, window, row)
    assert not native.events


def test_generic_hit_still_refuses_exact_native_static_label_right_edge(connected, native):
    driver, capture = connected
    native.hit = native.element("AXStaticText", None, Rect(30, 40, 50, 20), "Cust.Ref.")
    with pytest.raises(StaticLabelRightEdge) as stopped:
        driver.hit_test(capture, 80, 50)
    assert stopped.value.control.value == "Cust.Ref."
    assert stopped.value.control.role == "AXStaticText"
    assert stopped.value.point == (80, 50)
    assert not native.events


@pytest.mark.parametrize("role", ["AXTextField", "AXButton", "AXComboBox"])
def test_actionable_control_edges_never_get_the_static_label_discovery_exception(connected, native, role):
    driver, capture = connected
    native.hit = native.element(role, "Cust.Ref.", Rect(30, 40, 50, 20), "Cust.Ref.")
    with pytest.raises(DesktopError) as stopped:
        driver.hit_test(capture, 80, 50)
    assert not isinstance(stopped.value, StaticLabelRightEdge)
    assert not native.events


@pytest.mark.parametrize("point", [(80.01, 50), (80, 60), (80, 39.99)])
def test_static_label_discovery_exception_does_not_extend_other_edges_or_nearby_points(connected, native,
                                                                                       point):
    driver, capture = connected
    native.hit = native.element("AXStaticText", None, Rect(30, 40, 50, 20), "Cust.Ref.")
    with pytest.raises(DesktopError) as stopped:
        driver.hit_test(capture, *point)
    assert not isinstance(stopped.value, StaticLabelRightEdge)


def test_static_label_edge_does_not_bypass_an_explicit_requested_role(connected, native):
    driver, capture = connected
    native.hit = native.element("AXStaticText", None, Rect(30, 40, 50, 20), "Cust.Ref.")
    with pytest.raises(DesktopError) as stopped:
        driver.hit_test(capture, 80, 50, expected_role="AXTextField")
    assert not isinstance(stopped.value, StaticLabelRightEdge)


def test_window_partly_outside_display_is_not_captured(native):
    native.main["AXPosition"] = NS(x=-1, y=20)
    with pytest.raises(DesktopError, match="fully visible"):
        Desktop().capture("Fakturama")


def test_double_click_has_clean_flags_and_click_counts(connected, native):
    driver, capture = connected
    driver.double_click(capture, 40, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "up", "down", "up"]
    assert [event["clicks"] for event in native.events[1:]] == [1, 1, 2, 2]
    assert all(event["flags"] == 0 for event in native.events)


def test_command_selection_click_has_one_modified_down_and_clean_release(connected, native):
    driver, capture = connected
    driver.click(capture, 40, 50, command=True)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]
    assert [event["flags"] for event in native.events] == [0, native.quartz.kCGEventFlagMaskCommand, 0]
    assert [event["clicks"] for event in native.events[1:]] == [1, 1]


@pytest.mark.parametrize("options", [dict(command=1), dict(command=None), dict(command="cmd"),
                                    dict(command=True, clicks=2)])
def test_unknown_or_repeated_command_click_refuses_before_input(connected, native, options):
    driver, capture = connected
    with pytest.raises(ValueError, match="Command selection"):
        driver.click(capture, 40, 50, **options)
    assert native.events == []


def test_command_selection_click_keeps_existing_window_ownership_guard(connected, native):
    driver, capture = connected
    native.focus = 777
    with pytest.raises(DesktopError):
        driver.click(capture, 40, 50, command=True)
    assert native.events == []


def test_pointer_move_has_only_one_unmodified_movement_event(connected, native):
    driver, capture = connected
    driver.move(capture, 40, 50)
    assert native.events == [{"kind": "move", "point": (40, 50), "flags": 0}]


@pytest.mark.parametrize("point", [(110, 50), (40, 100), (float("nan"), 50), (40, float("inf"))])
def test_invalid_pointer_move_refuses_before_allocation_or_input(connected, native, point):
    driver, capture = connected
    calls = []
    native.quartz.CGEventCreateMouseEvent = lambda *args: calls.append(args)
    with pytest.raises(DesktopError):
        driver.move(capture, *point)
    assert calls == []
    assert not native.events


def test_stale_pointer_move_refuses_before_allocation(connected, native, monkeypatch):
    driver, capture = connected
    calls = []
    native.quartz.CGEventCreateMouseEvent = lambda *args: calls.append(args)
    monkeypatch.setattr(module.time, "monotonic", lambda: 16)
    with pytest.raises(DesktopError, match="stale"):
        driver.move(capture, 40, 50)
    assert calls == []
    assert not native.events


def test_pointer_move_allocation_failure_has_no_input_effect(connected, native):
    driver, capture = connected
    native.quartz.CGEventCreateMouseEvent = lambda *args: None
    with pytest.raises(DesktopError, match="allocate pointer movement"):
        driver.move(capture, 40, 50)
    assert not native.events


def test_pointer_move_rechecks_geometry_immediately_before_posting(connected, native):
    driver, capture = connected
    create = native.quartz.CGEventCreateMouseEvent
    def moved(*args):
        event = create(*args)
        native.main["AXPosition"] = NS(x=11, y=20)
        return event
    native.quartz.CGEventCreateMouseEvent = moved
    with pytest.raises(DesktopError, match="moved or resized"):
        driver.move(capture, 40, 50)
    assert not native.events


def test_mouse_up_still_dispatched_after_focus_change_stops_second_click(connected, native):
    driver, capture = connected
    native.on_post = lambda event: setattr(native, "focus", 999) if event["kind"] == "down" else None
    with pytest.raises(DesktopError, match="focused"):
        driver.double_click(capture, 40, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]


def test_drag_follows_measured_endpoints_with_clean_flags_and_release(connected, native):
    driver, capture = connected
    driver.drag(capture, 40, 50, 80, 50)
    assert [event["kind"] for event in native.events] == [
        "move", "down", "dragged", "dragged", "dragged", "dragged", "up"]
    assert [event["point"] for event in native.events[2:-1]] == [
        (50, 50), (60, 50), (70, 50), (80, 50)]
    assert native.events[-1]["point"] == (80, 50)
    assert all(event["flags"] == 0 for event in native.events)


@pytest.mark.parametrize("points", [(9, 50, 80, 50), (40, 50, 110, 50),
                                      (40, 50, float("nan"), 50)])
def test_drag_checks_both_endpoints_before_any_input(connected, native, points):
    driver, capture = connected
    with pytest.raises(DesktopError):
        driver.drag(capture, *points)
    assert not native.events


@pytest.mark.parametrize("duration", [0, 1.01, float("nan")])
def test_drag_duration_is_bounded_before_input(connected, native, duration):
    driver, capture = connected
    with pytest.raises(ValueError):
        driver.drag(capture, 40, 50, 80, 50, duration=duration)
    assert not native.events


def test_drag_refuses_stale_capture_and_zero_distance(connected, native, monkeypatch):
    driver, capture = connected
    with pytest.raises(ValueError, match="different start and end"):
        driver.drag(capture, 40, 50, 40, 50)
    monkeypatch.setattr(module.time, "monotonic", lambda: 16)
    with pytest.raises(DesktopError, match="stale"):
        driver.drag(capture, 40, 50, 80, 50)
    assert not native.events


@pytest.mark.parametrize("change", [
    lambda n: n.main.update(AXPosition=NS(x=11, y=20)),
    lambda n: setattr(n, "focus", 999),
])
def test_drag_rechecks_geometry_and_focus_after_mouse_down_and_releases(connected, native, change):
    driver, capture = connected
    native.on_post = lambda event: change(native) if event["kind"] == "down" else None
    with pytest.raises(DesktopError):
        driver.drag(capture, 40, 50, 80, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]
    assert native.events[-1]["point"] == (40, 50)


def test_drag_motion_post_failure_still_releases_at_attempted_point(connected, native):
    driver, capture = connected
    def failed(event):
        if event["kind"] == "dragged":
            raise RuntimeError("simulated dispatch failure")
    native.on_post = failed
    with pytest.raises(RuntimeError, match="dispatch failure"):
        driver.drag(capture, 40, 50, 80, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "dragged", "up"]
    assert native.events[-1]["point"] == (50, 50)


def test_drag_motion_allocation_failure_uses_preallocated_release(connected, native):
    driver, capture = connected
    allocate = native.quartz.CGEventCreateMouseEvent
    native.quartz.CGEventCreateMouseEvent = lambda source, kind, point, button: (
        None if kind == "dragged" else allocate(source, kind, point, button))
    with pytest.raises(DesktopError, match="drag motion event"):
        driver.drag(capture, 40, 50, 80, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]


def test_drag_deadline_stops_motion_but_still_releases(connected, native, monkeypatch):
    driver, capture = connected
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    native.on_post = lambda event: clock.__setitem__(0, 12.1) if event["kind"] == "down" else None
    with pytest.raises(DesktopError, match="deadline exceeded"):
        driver.drag(capture, 40, 50, 80, 50)
    assert [event["kind"] for event in native.events] == ["move", "down", "up"]


def test_drag_release_location_failure_does_not_suppress_mouse_up(connected, native):
    driver, capture = connected
    def failed(event, point):
        raise RuntimeError("simulated location failure")
    native.quartz.CGEventSetLocation = failed
    with pytest.raises(RuntimeError, match="location failure"):
        driver.drag(capture, 40, 50, 80, 50)
    assert native.events[-1]["kind"] == "up"


def test_keyboard_up_releases_command_flags_even_if_focus_changes(connected, native):
    driver, capture = connected
    native.on_post = lambda event: setattr(native, "focus", 999)
    driver.key(capture, "cmd+a")
    assert [(e["kind"], e["flags"]) for e in native.events] == [("key-down", 256), ("key-up", 0)]


@pytest.mark.parametrize("text,units", [("Müller 東京", 9), ("A😀B", 4)])
def test_unicode_text_preserves_codepoints_and_clipboard(connected, native, text, units):
    driver, capture = connected
    native.cocoa.NSPasteboard.generalPasteboard = lambda: (_ for _ in ()).throw(
        AssertionError("Unicode entry must not access the clipboard"))
    checks = []
    driver.type_text(capture, text, focus_guard=lambda: checks.append(list(native.events)) or True)
    assert checks == [[]]
    assert native.events == [
        {"kind": "key-down", "code": 0, "flags": 0, "unicode": text, "utf16_units": units},
        {"kind": "key-up", "code": 0, "flags": 0, "unicode": text, "utf16_units": units},
    ]


def test_unicode_text_refuses_lost_exact_field_focus_before_dispatch(connected, native):
    driver, capture = connected
    native.field["AXFocused"] = False
    with pytest.raises(DesktopError, match="Exact field focus changed"):
        driver.type_text(capture, "source", focus_guard=lambda: native.field["AXFocused"])
    assert not native.events


def test_unicode_text_rechecks_target_after_event_preparation(connected, native):
    driver, capture = connected
    prepared = native.quartz.CGEventKeyboardSetUnicodeString
    def changed(event, count, text):
        prepared(event, count, text)
        native.focus = 999
    native.quartz.CGEventKeyboardSetUnicodeString = changed
    callbacks = []
    with pytest.raises(DesktopError, match="not freshly focused"):
        driver.type_text(capture, "source", focus_guard=lambda: callbacks.append(True) or True)
    assert not native.events
    assert not callbacks


def test_unicode_text_releases_key_after_partial_post_failure(connected, native):
    driver, capture = connected
    def failed(event):
        if event["kind"] == "key-down":
            native.focus = 999
            raise RuntimeError("simulated dispatch failure")
    native.on_post = failed
    with pytest.raises(RuntimeError, match="dispatch failure"):
        driver.type_text(capture, "source", focus_guard=lambda: True)
    assert [(event["kind"], event["flags"]) for event in native.events] == [
        ("key-down", 0), ("key-up", 0)]


@pytest.mark.parametrize("text", [None, "", "\x00", "bad\ud800", "x" * 1025, "😀" * 513])
def test_unicode_text_invalid_or_overlong_input_has_no_input_effect(connected, native, text):
    driver, capture = connected
    with pytest.raises(ValueError):
        driver.type_text(capture, text, focus_guard=lambda: True)
    assert not native.events


def test_unicode_text_requires_exact_focus_callback(connected, native):
    driver, capture = connected
    with pytest.raises(ValueError, match="callable exact field focus guard"):
        driver.type_text(capture, "source", focus_guard=None)
    assert not native.events


@pytest.mark.parametrize("name,code,flags", [("home", 0x73, 0), ("end", 0x77, 0),
                                            ("cmd+home", 0x73, 256), ("cmd+end", 0x77, 256)])
def test_home_and_end_codes_and_modifier_release(connected, native, name, code, flags):
    driver, capture = connected
    driver.key(capture, name)
    assert [(e["kind"], e["code"], e["flags"]) for e in native.events] == [
        ("key-down", code, flags), ("key-up", code, 0)]


def focused_unnamed_popup(native):
    popup = native.element("AXWindow", "", Rect(60, 60, 20, 15))
    popup["AXModal"] = False
    native.windows.insert(0, popup)
    native.focused_window = popup
    return popup


def test_popup_exception_dispatches_only_unmodified_escape_and_releases_key(native):
    driver = Desktop()
    popup = focused_unnamed_popup(native)
    native.on_post = lambda event: setattr(native, "focus", 999)
    driver.dismiss_popup(popup)
    assert [(e["kind"], e["code"], e["flags"]) for e in native.events] == [
        ("key-down", 0x35, 0), ("key-up", 0x35, 0)]


@pytest.mark.parametrize("change", [
    lambda n, p: p.update(pid=999),
    lambda n, p: p.update(AXRole="AXButton"),
    lambda n, p: p.update(AXModal=True),
    lambda n, p: p.pop("AXModal"),
    lambda n, p: p.update(AXTitle="Named dialog"),
    lambda n, p: setattr(n, "focused_window", n.main),
    lambda n, p: setattr(n, "focus", 999),
    lambda n, p: setattr(n, "accessibility", False),
    lambda n, p: setattr(n, "launch", 200),
])
def test_popup_exception_requires_exact_owner_window_modality_and_focus(native, change):
    driver = Desktop()
    popup = focused_unnamed_popup(native)
    change(native, popup)
    with pytest.raises(DesktopError):
        driver.dismiss_popup(popup)
    assert not native.events


def test_popup_exception_does_not_accept_same_shaped_different_focused_window(native):
    driver = Desktop()
    popup = focused_unnamed_popup(native)
    lookalike = dict(popup)
    native.focused_window = lookalike
    with pytest.raises(DesktopError, match="exact popup"):
        driver.dismiss_popup(popup)
    assert not native.events


def test_popup_focus_change_during_native_identity_reads_stops_escape(native):
    driver = Desktop()
    popup = focused_unnamed_popup(native)
    read = native.ax.AXUIElementCopyAttributeValue
    def changed(element, attr, ignored):
        if element is popup and attr == "AXModal":
            native.focus = 999
        return read(element, attr, ignored)
    native.ax.AXUIElementCopyAttributeValue = changed
    with pytest.raises(DesktopError, match="not freshly focused"):
        driver.dismiss_popup(popup)
    assert not native.events


def test_outside_point_and_unknown_key_have_no_input_effect(connected, native):
    driver, capture = connected
    with pytest.raises(DesktopError, match="outside"):
        driver.click(capture, 110, 50)
    with pytest.raises(ValueError, match="Unsupported"):
        driver.key(capture, "invented")
    assert not native.events


def test_hit_test_reads_exact_unicode_without_ocr_transformation(connected):
    driver, capture = connected
    control = driver.hit_test(capture, 40, 50, expected_role="AXTextField")
    assert control.value == "Müller 東京"
    assert control.bounds == Rect(30, 40, 50, 20)


def test_hit_test_wrong_owner_role_and_bounds_are_rejected(connected, native):
    driver, capture = connected
    with pytest.raises(DesktopError, match="Expected role"):
        driver.hit_test(capture, 40, 50, expected_role="AXButton")
    native.field["pid"] = 999
    with pytest.raises(DesktopError, match="owned"):
        driver.hit_test(capture, 40, 50)
    native.field["pid"] = native.pid
    native.field["AXPosition"] = NS(x=60, y=40)
    with pytest.raises(DesktopError, match="bounds"):
        driver.hit_test(capture, 40, 50)


def test_ax_traversal_follows_contents_and_cycles_without_repeating_nodes(connected, native):
    driver, capture = connected
    native.field["AXChildren"] = [native.main]
    control = driver.find_ax(capture, role="AXTextField", title=None)
    assert control.value == "Müller 東京"
    save = driver.find_ax(capture, role="AXButton", title="Save")
    driver.press(capture, save)
    assert native.actions == [(native.button, "AXPress")]


def test_ax_duplicate_and_incomplete_tree_do_not_prove_uniqueness(connected, native):
    driver, capture = connected
    with pytest.raises(DesktopError, match="truncated"):
        driver.find_ax(capture, role="AXButton", title="Save", max_nodes=1)
    with pytest.raises(DesktopError, match="depth limit"):
        driver.find_ax(capture, role="AXButton", title="Save", max_depth=0)
    native.main["AXChildren"].append(native.element("AXButton", "Save", Rect(65, 80, 30, 10)))
    with pytest.raises(DesktopError, match="found 2"):
        driver.find_ax(capture, role="AXButton", title="Save")


def test_ax_communication_failure_cannot_look_like_an_empty_tree(connected, native):
    driver, capture = connected
    read = native.ax.AXUIElementCopyAttributeValue
    native.ax.AXUIElementCopyAttributeValue = lambda element, attr, ignored: (
        (-25204, None) if element is native.main and attr == "AXChildren"
        else read(element, attr, ignored))
    with pytest.raises(DesktopError, match="Could not read AXChildren"):
        driver.find_ax(capture, role="AXButton", title="Save")


def test_ax_changed_or_disabled_control_is_not_pressed(connected, native):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    native.button["AXEnabled"] = False
    with pytest.raises(DesktopError, match="not enabled"):
        driver.press(capture, control)
    assert not native.actions


@pytest.mark.parametrize("result", [False, None])
def test_action_context_guard_refuses_before_accessibility_dispatch(connected, native, result):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    calls = []
    def context():
        calls.append("context")
        assert native.actions == []
        return result
    with pytest.raises(DesktopError, match="Action context changed"):
        driver.press(capture, control, action_guard=context)
    assert calls == ["context"]
    assert not native.actions


def test_action_context_guard_exception_prevents_accessibility_dispatch(connected, native):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    def context():
        raise DesktopError("Wrong editor selected")
    with pytest.raises(DesktopError, match="Wrong editor selected"):
        driver.press(capture, control, action_guard=context)
    assert not native.actions


@pytest.mark.parametrize("change", [
    lambda n: n.button.update(AXEnabled=False),
    lambda n: n.button.update(AXTitle="Another action"),
    lambda n: setattr(n, "focus", 999),
])
def test_failed_fresh_guards_do_not_call_action_context_predicate(connected, native, change):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    calls = []
    change(native)
    with pytest.raises(DesktopError):
        driver.press(capture, control, action_guard=lambda: calls.append("context") or True)
    assert calls == []
    assert not native.actions


def test_action_context_guard_accepts_one_fresh_accessibility_press(connected, native):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    calls = []
    def context():
        assert native.actions == []
        calls.append("context")
        return True
    driver.press(capture, control, action_guard=context)
    assert calls == ["context"]
    assert native.actions == [(native.button, "AXPress")]


def test_invalid_action_guard_is_rejected_before_any_desktop_observation(
        connected, native, monkeypatch):
    driver, capture = connected
    control = driver.find_ax(capture, role="AXButton", title="Save")
    def unexpected(snapshot):
        raise AssertionError("An invalid guard must fail before observing the desktop")
    monkeypatch.setattr(driver, "guard", unexpected)
    with pytest.raises(ValueError, match="Action guard must be callable"):
        driver.press(capture, control, action_guard=False)
    assert not native.actions


def test_paste_preserves_all_materialized_formats_and_exact_unicode(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old", "public.image": b"binary image"}
    native.board.items = [original]
    pasted = []
    native.on_post = lambda event: pasted.append(native.board.items[0].values["public.text"])
    driver.paste(capture, "Müller 東京")
    assert pasted == ["Müller 東京".encode()] * 2
    assert native.board.items[0].values == original.values


def test_clipboard_is_restored_if_keyboard_guard_fails(connected, native, monkeypatch):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"original"}
    native.board.items = [original]
    monkeypatch.setattr(driver, "key", lambda *args, **kwargs: (_ for _ in ()).throw(DesktopError("focus changed")))
    with pytest.raises(DesktopError, match="focus changed"):
        driver.paste(capture, "new")
    assert native.board.items[0].values == original.values


def test_paste_exact_focus_guard_runs_after_clipboard_setup_and_before_dispatch(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old", "public.image": b"image"}
    native.board.items = [original]
    checks = []
    def focused():
        checks.append((native.board.items[0].values["public.text"], list(native.events)))
        return True
    driver.paste(capture, "replacement", focus_guard=focused)
    assert checks == [(b"replacement", [])]
    assert [event["kind"] for event in native.events] == ["key-down", "key-up"]
    assert native.board.items[0].values == original.values


def test_paste_exact_field_focus_change_during_key_guard_restores_clipboard_without_input(
        connected, native, monkeypatch):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old", "public.image": b"image"}
    native.board.items = [original]
    native.field["AXFocused"] = True
    guard = driver.guard
    calls = [0]
    def changed(snapshot):
        result = guard(snapshot)
        calls[0] += 1
        if calls[0] == 3:  # The keyboard guard, after the two clipboard guards.
            native.field["AXFocused"] = False
        return result
    monkeypatch.setattr(driver, "guard", changed)
    with pytest.raises(DesktopError, match="Exact field focus changed"):
        driver.paste(capture, "replacement", focus_guard=lambda: native.field["AXFocused"])
    assert calls[0] == 3
    assert not native.events
    assert native.board.items[0].values == original.values


def test_paste_focus_callback_exception_restores_clipboard(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old"}
    native.board.items = [original]
    def failed():
        raise DesktopError("Exact control disappeared")
    with pytest.raises(DesktopError, match="control disappeared"):
        driver.paste(capture, "replacement", focus_guard=failed)
    assert not native.events
    assert native.board.items[0].values == original.values


def test_new_external_clipboard_is_preserved_without_overwriting_it(connected, native):
    driver, capture = connected
    newer = ClipboardItem()
    newer.values = {"public.text": b"new user clipboard"}
    def changed(event):
        if event["kind"] == "key-up":
            native.board.clearContents()
            native.board.writeObjects_([newer])
    native.on_post = changed
    with pytest.raises(DesktopError, match="newer contents preserved"):
        driver.paste(capture, "order text")
    assert native.board.items[0].values == newer.values


def test_unmaterialized_clipboard_format_refuses_paste_without_input(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"lazy.format": None}
    native.board.items = [original]
    with pytest.raises(DesktopError, match="preserve every"):
        driver.paste(capture, "order text")
    assert native.board.items == [original]
    assert not native.events


def test_clipboard_changed_while_materializing_is_preserved(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old"}
    newer = ClipboardItem()
    newer.values = {"public.text": b"new user clipboard"}
    def replaced(kind):
        native.board.clearContents()
        native.board.writeObjects_([newer])
        return original.values[kind]
    original.dataForType_ = replaced
    native.board.items = [original]
    with pytest.raises(DesktopError, match="while preserving"):
        driver.paste(capture, "order text")
    assert native.board.items[0].values == newer.values
    assert not native.events


def test_clipboard_write_failure_restores_previous_contents(connected, native):
    driver, capture = connected
    original = ClipboardItem()
    original.values = {"public.text": b"old"}
    native.board.items = [original]
    native.board.fail_next_write = True
    with pytest.raises(DesktopError, match="Could not place"):
        driver.paste(capture, "order text")
    assert native.board.items[0].values == original.values
    assert not native.events


def test_bounded_polling_returns_observation_and_stops_at_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    result = wait_for(lambda: {"ready": True} if clock[0] >= .2 else None, "ready", timeout=.5)
    assert result == {"ready": True}
    with pytest.raises(DesktopError, match="Timed out"):
        wait_for(lambda: False, "missing", timeout=.3)
    assert clock[0] == pytest.approx(.5)
    with pytest.raises(ValueError):
        wait_for(lambda: True, "bad wait", timeout=61)


@pytest.mark.parametrize("finished_at", [.1, .2])
def test_polling_refuses_truthy_observation_completed_at_or_after_deadline(monkeypatch, finished_at):
    clock, calls = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def slow_observation():
        calls.append(True)
        clock[0] = finished_at
        return {"ready": True}

    with pytest.raises(DesktopError, match="Timed out waiting for slow observation"):
        wait_for(slow_observation, "slow observation", timeout=.1)
    assert calls == [True]


def test_polling_does_not_call_predicate_again_when_sleep_reaches_deadline(monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def absent():
        calls.append(clock[0])
        return False

    with pytest.raises(DesktopError, match="Timed out"):
        wait_for(absent, "absent", timeout=.1, interval=.1)
    assert calls == [0.0]


def test_importing_desktop_does_not_import_pyobjc_in_fresh_process():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c",
                             "import sys; import tjm.desktop; "
                             "assert 'ApplicationServices' not in sys.modules; "
                             "assert 'Quartz' not in sys.modules"],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
