"""Small macOS input/capture helpers; callers own all workflow and Save policy.

Coordinates accepted by input and hit testing are screen units, not image pixels.
Use ``Capture.screen_point`` to transform OCR locations. PyObjC is imported only
when connecting to the desktop; importing this module never touches the UI.
"""

import math
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageGrab

from .ocr import Token, ocr_tokens


class DesktopError(RuntimeError):
    """A desktop observation or input guard failed; callers should stop."""


class StaticLabelRightEdge(DesktopError):
    """A read-only native label was returned at its exact half-open right edge.

    Generic hit testing still refuses this result. A field-discovery caller
    may inspect the label's literal value and skip only its own exact label.
    """

    def __init__(self, control, point):
        super().__init__("Hit-tested control bounds do not match the target point/window.")
        self.control, self.point = control, point
        self.evidence = {"native_static_label_right_edge": {
            "role": control.role, "value": control.value,
            "bounds": control.bounds.as_dict(), "point": list(point)}}


class _AXReadError(DesktopError):
    """Retain a native read code for the narrowly allowed inventory refresh."""

    def __init__(self, attribute, code):
        super().__init__(f"Could not read {attribute}: accessibility error {code}.")
        self.attribute = attribute
        self.code = code


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.x, self.y, self.width, self.height)):
            raise DesktopError("Rectangle contains a non-finite value.")
        if self.width <= 0 or self.height <= 0:
            raise DesktopError("Rectangle dimensions must be positive.")

    def contains(self, x, y):
        return (math.isfinite(x) and math.isfinite(y) and self.x <= x < self.x + self.width
                and self.y <= y < self.y + self.height)

    def contains_rect(self, other):
        return (self.x <= other.x and self.y <= other.y
                and other.x + other.width <= self.x + self.width
                and other.y + other.height <= self.y + self.height)

    @property
    def bbox(self):
        return (math.floor(self.x), math.floor(self.y), math.ceil(self.x + self.width),
                math.ceil(self.y + self.height))

    def as_dict(self):
        return dict(x=self.x, y=self.y, width=self.width, height=self.height)


@dataclass(frozen=True)
class Window:
    title: str
    bounds: Rect
    element: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class Capture:
    image: Image.Image = field(repr=False, compare=False)
    window_title: str
    window_bounds: Rect
    screen_rect: Rect
    scale_x: float
    scale_y: float
    captured_at: float
    pid: int
    process_identity: tuple
    display_id: int
    window_element: object = field(repr=False, compare=False)
    visibility_evidence: object = field(default=None, repr=False, compare=False)
    _tokens: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        if not all(math.isfinite(v) and v > 0 for v in (self.scale_x, self.scale_y)):
            raise DesktopError("Capture scaling must be finite and positive.")
        if not (math.isclose(self.image.width / self.screen_rect.width, self.scale_x)
                and math.isclose(self.image.height / self.screen_rect.height, self.scale_y)):
            raise DesktopError("Capture scaling disagrees with pixel dimensions.")
        if not self.screen_rect.contains_rect(self.window_bounds):
            raise DesktopError("Capture rectangle does not contain the selected window.")
        if not math.isfinite(self.captured_at):
            raise DesktopError("Capture time must be finite.")

    def screen_point(self, pixel_x, pixel_y):
        if not (math.isfinite(pixel_x) and math.isfinite(pixel_y)
                and 0 <= pixel_x < self.image.width and 0 <= pixel_y < self.image.height):
            raise DesktopError("Pixel point lies outside the captured image.")
        return (self.screen_rect.x + pixel_x / self.scale_x,
                self.screen_rect.y + pixel_y / self.scale_y)

    def tokens(self, *, psm=11) -> list[Token]:
        """Cache OCR of this immutable observation, never a later screen state."""
        if psm not in self._tokens:
            tokens = ocr_tokens(self.image, psm=psm)
            for token in tokens:
                if (token.left < 0 or token.top < 0 or token.width <= 0 or token.height <= 0
                        or token.right > self.image.width or token.bottom > self.image.height
                        or not math.isfinite(token.confidence) or not 0 <= token.confidence <= 100):
                    raise DesktopError("OCR returned an invalid token rectangle or confidence.")
            self._tokens[psm] = tokens
        return list(self._tokens[psm])

    def save(self, path: Path):
        """Write evidence once; preserve an earlier capture at the same path."""
        with Path(path).open("xb") as stream:
            self.image.save(stream, format="PNG")

    def metadata(self):
        result = {"window_title": self.window_title, "window_bounds": self.window_bounds.as_dict(),
                "screen_rectangle": list(self.screen_rect.bbox), "pid": self.pid,
                "pixel_width": self.image.width, "pixel_height": self.image.height,
                "pixels_per_screen_unit_x": self.scale_x,
                "pixels_per_screen_unit_y": self.scale_y, "display_id": self.display_id}
        if self.visibility_evidence is not None:
            result["window_visibility"] = self.visibility_evidence
        return result


@dataclass(frozen=True)
class AXControl:
    role: str
    title: str | None
    value: object
    bounds: Rect
    enabled: bool | None
    element: object = field(repr=False, compare=False)


def normalized_label(text):
    """Normalize labels for discovery only, never stored customer/product values."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def wait_for(predicate, description, *, timeout=8, interval=.1):
    """Accept only observations completed before the deadline.

    A synchronous predicate cannot be interrupted here; a late result is
    refused after that call returns. This helper never retries a mutation.
    """
    if not (math.isfinite(timeout) and 0 < timeout <= 60
            and math.isfinite(interval) and 0 < interval <= timeout):
        raise ValueError("Polling requires 0 < interval <= timeout <= 60 seconds.")
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            raise DesktopError(f"Timed out waiting for {description}.")
        result = predicate()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DesktopError(f"Timed out waiting for {description}.")
        if result:
            return result
        time.sleep(min(interval, remaining))


def _load_native():
    try:
        import ApplicationServices as AX
        import Cocoa
        import CoreFoundation as CF
        import Quartz
    except ImportError as exc:
        raise DesktopError("macOS UI helpers require the optional PyObjC desktop dependencies.") from exc
    return AX, Cocoa, CF, Quartz


class Desktop:
    """Connect to one exact running application. This class never launches it."""

    def __init__(self, *, pid=None, bundle_id="Fakturama.ID", max_capture_age=5):
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise ValueError("PID must be a positive integer.")
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise ValueError("An exact nonempty application bundle ID is required.")
        if not math.isfinite(max_capture_age) or not 0 < max_capture_age <= 5:
            raise ValueError("Capture age must be positive and at most five seconds.")
        self.ax, self.cocoa, self.cf, self.quartz = _load_native()
        self.bundle_id = bundle_id
        self.max_capture_age = max_capture_age
        self._require_permission()
        apps = [app for app in self.cocoa.NSWorkspace.sharedWorkspace().runningApplications()
                if app.bundleIdentifier() == bundle_id and not app.isTerminated()
                and (pid is None or int(app.processIdentifier()) == pid)]
        if len(apps) != 1:
            raise DesktopError(f"Expected one running {bundle_id} application; found {len(apps)}.")
        self.pid = int(apps[0].processIdentifier())
        self.identity = self._identity(apps[0])
        self.process_rechecks = []
        self.root = self.ax.AXUIElementCreateApplication(self.pid)
        code = self.ax.AXUIElementSetMessagingTimeout(self.root, .5)
        if code != self.ax.kAXErrorSuccess:
            raise DesktopError(f"Could not bound accessibility messaging latency: {code}.")

    def permissions(self):
        return {"accessibility": bool(self.ax.AXIsProcessTrusted()),
                "screen_recording": bool(self.quartz.CGPreflightScreenCaptureAccess())}

    def _require_permission(self, *, capture=False):
        if not self.ax.AXIsProcessTrusted():
            raise DesktopError("Accessibility permission is unavailable; no permission requested.")
        if capture and not self.quartz.CGPreflightScreenCaptureAccess():
            raise DesktopError("Screen Recording permission is unavailable; no permission requested.")

    @staticmethod
    def _identity(app):
        url, launched = app.bundleURL(), app.launchDate()
        if url is None or launched is None:
            raise DesktopError("Running application identity is incomplete.")
        timestamp = float(launched.timeIntervalSince1970())
        if not math.isfinite(timestamp):
            raise DesktopError("Application launch time is invalid.")
        return (int(app.processIdentifier()), str(app.bundleIdentifier()), str(url.path()), timestamp)

    def _process(self):
        self._require_permission()
        app = self.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
        recheck = None
        late = False
        if app is None:
            # Wait only for a missing observation; never replay input or reset
            # ownership. Native-call/scheduler latency can exceed this nominal
            # budget, so a late result is refused as well.
            started = time.monotonic()
            deadline = started + .25
            recheck = {'index': len(self.process_rechecks), 'expected_identity': list(self.identity),
                       'initial_lookup_present': False, 'second_lookup_present': False,
                       'second_observed_terminated': None, 'second_observed_identity': None,
                       'passed': False, 'refusal_reason': None,
                       'policy': 'bounded_exact_identity_wait', 'timeout_seconds': .25,
                       'interval_seconds': .05, 'lookup_count': 1,
                       'delayed_lookup_count': 0, 'elapsed_seconds': 0.0}
            def require_wait_permission():
                try:
                    self._require_permission()
                except DesktopError as error:
                    recheck.update(refusal_reason='permission_unavailable',
                                   elapsed_seconds=time.monotonic() - started)
                    self.process_rechecks.append(recheck)
                    error.evidence = {'expected_identity': list(self.identity),
                                      'lookup_present': app is not None,
                                      'observed_terminated': None, 'observed_identity': None,
                                      'refusal_reason': 'permission_unavailable',
                                      'process_recheck': dict(recheck)}
                    raise

            for _ in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(.05, remaining))
                require_wait_permission()
                if time.monotonic() > deadline:
                    break
                recheck['lookup_count'] += 1
                recheck['delayed_lookup_count'] += 1
                app = self.cocoa.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
                # These legacy fields now describe the final delayed read.
                recheck['second_lookup_present'] = app is not None
                if app is not None:
                    require_wait_permission()
                    late = time.monotonic() > deadline
                    break
        terminated, observed, reason = None, None, None
        if app is None:
            reason = 'None'
        else:
            terminated = bool(app.isTerminated())
            if terminated:
                reason = 'terminated'
            else:
                try:
                    observed = self._identity(app)
                except DesktopError as error:
                    if recheck is not None:
                        recheck.update(second_observed_terminated=False,
                                       refusal_reason='identity_unreadable',
                                       second_identity_error=str(error),
                                       elapsed_seconds=time.monotonic() - started)
                        self.process_rechecks.append(recheck)
                        error.evidence = {'expected_identity': list(self.identity),
                                          'lookup_present': True, 'observed_terminated': False,
                                          'observed_identity': None,
                                          'refusal_reason': 'identity_unreadable',
                                          'process_recheck': dict(recheck)}
                    raise
                if observed != self.identity:
                    reason = 'identity_changed'
                elif late or (recheck is not None and time.monotonic() > deadline):
                    reason = 'deadline_exceeded'
        if recheck is not None:
            recheck.update(second_observed_terminated=terminated,
                           second_observed_identity=list(observed) if observed is not None else None,
                           passed=reason is None, refusal_reason=reason,
                           elapsed_seconds=time.monotonic() - started)
            self.process_rechecks.append(recheck)
        if reason is not None:
            error = DesktopError("Target process stopped or changed; input/capture refused.")
            error.evidence = {'expected_identity': list(self.identity), 'lookup_present': app is not None,
                              'observed_terminated': terminated,
                              'observed_identity': list(observed) if observed is not None else None,
                              'refusal_reason': reason}
            if recheck is not None:
                error.evidence['process_recheck'] = dict(recheck)
            raise error
        return app

    def _read(self, element, attribute, *, required=False):
        code, value = self.ax.AXUIElementCopyAttributeValue(element, attribute, None)
        if code != self.ax.kAXErrorSuccess:
            absent = {self.ax.kAXErrorAttributeUnsupported, self.ax.kAXErrorNoValue}
            if required or code not in absent:
                raise _AXReadError(attribute, code)
            return None
        if required and value is None:
            raise DesktopError(f"Required accessibility attribute {attribute} is empty.")
        return value

    def focused_pid(self):
        system = self.ax.AXUIElementCreateSystemWide()
        app = self._read(system, "AXFocusedApplication", required=True)
        code, pid = self.ax.AXUIElementGetPid(app, None)
        if code != self.ax.kAXErrorSuccess:
            raise DesktopError(f"Focused PID read failed: accessibility error {code}.")
        return int(pid)

    def _require_focus(self):
        self._process()
        if self.focused_pid() != self.pid:
            raise DesktopError("Target process is not freshly focused; input/capture refused.")

    def activate(self):
        app = self._process()
        transient = {
            "Could not read AXFocusedApplication: accessibility error -25212.",
            "Could not read AXFocusedApplication: accessibility error -25204.",
        }
        def readiness_pid():
            try:
                return self.focused_pid()
            except DesktopError as exc:
                if str(exc) in transient:
                    return None
                raise
        if (readiness_pid() != self.pid
                and not app.activateWithOptions_(self.cocoa.NSApplicationActivateIgnoringOtherApps)):
            raise DesktopError("Application activation failed.")
        matches = 0
        def ready():
            nonlocal matches
            matches = matches + 1 if readiness_pid() == self.pid else 0
            return matches >= 2
        return wait_for(ready, "two consecutive target application focus observations", timeout=2)

    def _geometry(self, element):
        def decoded(attribute, kind):
            value = self._read(element, attribute, required=True)
            result = self.ax.AXValueGetValue(value, kind, None)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
                success, result = result
                if not success:
                    raise DesktopError(f"Could not decode {attribute}.")
            return result
        point = decoded("AXPosition", self.ax.kAXValueCGPointType)
        size = decoded("AXSize", self.ax.kAXValueCGSizeType)
        return Rect(float(point.x), float(point.y), float(size.width), float(size.height))

    def windows(self):
        # A closed SWT auxiliary window can leave one invalid AXWindows entry.
        # Refresh the whole inventory once; never skip that entry or retry input.
        for attempt in range(2):
            self._process()
            try:
                return self._window_inventory()
            except _AXReadError as exc:
                if attempt or exc.code != self.ax.kAXErrorInvalidUIElement:
                    raise

    def _window_inventory(self):
        windows = self._read(self.root, "AXWindows", required=True)
        result = []
        for element in windows:
            if self._read(element, "AXRole") == "AXHelpTag":
                code, pid = self.ax.AXUIElementGetPid(element, None)
                modal = self._read(element, "AXModal")
                focused = self._read(self.root, "AXFocusedWindow", required=True)
                if (code != self.ax.kAXErrorSuccess or int(pid) != self.pid
                        or (modal is not None and modal is not False)
                        or self.cf.CFEqual(element, focused)):
                    raise DesktopError("Tooltip ownership, modality or focus is uncertain; selection refused.")
                # Native hover help is not an application dialog and cannot be
                # selected. Only an owned, nonfocused, nonmodal help tag is skipped.
                continue
            title = self._read(element, "AXTitle")
            if title is not None and not isinstance(title, str):
                raise DesktopError("An application window has an invalid title; selection refused.")
            if title is None or not title.strip():
                # SWT/macOS can expose a small unnamed nonmodal auxiliary
                # window. Inventory it without making it selectable. Unknown
                # or modal windows still stop selection; focus checks below
                # continue to require the exact named target window.
                if self._read(element, "AXModal") is not False:
                    raise DesktopError("An unnamed modal or unknown-modal window blocks selection.")
                title = ""
            result.append(Window(title, self._geometry(element), element))
        return result

    def window(self, title):
        if not isinstance(title, str) or not title.strip():
            raise DesktopError("Window selection requires an exact nonempty title.")
        found = [window for window in self.windows() if window.title == title]
        if len(found) != 1:
            raise DesktopError(f"Expected one exact window {title!r}; found {len(found)}.")
        return found[0]

    def _focused_window(self, window):
        focused = self._read(self.root, "AXFocusedWindow", required=True)
        if not self.cf.CFEqual(window.element, focused):
            raise DesktopError("The selected window is not the application's focused window.")

    def _capture_visibility(self, window):
        """Reject foreign windows above/intersecting the exact target in Quartz order."""
        q = self.quartz
        evidence = {"expected_pid": self.pid, "expected_bounds": window.bounds.as_dict(),
                    "method": "onscreen Quartz front-to-back window inventory"}
        parsed = []

        def refuse(message):
            error = DesktopError(message)
            error.evidence = {"capture_visibility": evidence}
            raise error

        try:
            inventory = q.CGWindowListCopyWindowInfo(
                q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements,
                q.kCGNullWindowID)
            if inventory is None:
                refuse("Onscreen window inventory is unavailable; capture refused.")
            for item in inventory:
                number, pid = item[q.kCGWindowNumber], item[q.kCGWindowOwnerPID]
                bounds = item[q.kCGWindowBounds]
                values = [bounds[key] for key in ("X", "Y", "Width", "Height")]
                if (isinstance(number, bool) or not isinstance(number, int) or number <= 0
                        or isinstance(pid, bool) or not isinstance(pid, int) or pid < 0
                        or any(isinstance(value, bool) or not isinstance(value, (int, float))
                               or not math.isfinite(value) for value in values)
                        or values[2] < 0 or values[3] < 0):
                    refuse("Onscreen window metadata is invalid; capture refused.")
                entry = {"window_number": number, "owner_pid": pid,
                         "bounds": dict(zip(("x", "y", "width", "height"), values))}
                # These attributes are optional for ordinary windows. Their
                # complete native triple is required for the cursor exception.
                for label, attribute in (("owner_name", "kCGWindowOwnerName"),
                                         ("name", "kCGWindowName"), ("layer", "kCGWindowLayer")):
                    key = getattr(q, attribute, None)
                    value = item.get(key) if key is not None else None
                    if value is not None:
                        if ((label == "layer" and (isinstance(value, bool) or not isinstance(value, int)))
                                or (label != "layer" and not isinstance(value, str))):
                            refuse("Onscreen window metadata is invalid; capture refused.")
                        entry[label] = value
                parsed.append(entry)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            evidence["inventory_error_type"] = type(error).__name__
            evidence["readable_window_records"] = parsed
            refuse("Onscreen window inventory is incomplete; capture refused.")
        matches = [(index, item) for index, item in enumerate(parsed)
                   if item["owner_pid"] == self.pid and item["bounds"] == window.bounds.as_dict()]
        evidence["exact_target_matches"] = len(matches)
        if len(matches) != 1:
            evidence["readable_window_records"] = parsed
            refuse("Exact target window is not unique in onscreen inventory; capture refused.")
        index, target = matches[0]
        evidence["target_window"] = target
        evidence["above_window_count"] = index
        blockers, ignored_cursors = [], []
        cursor_level = None
        if any(item.get("owner_name") == "Window Server" and item.get("name") == "Cursor"
               for item in parsed[:index]):
            try:
                cursor_level = q.CGWindowLevelForKey(q.kCGCursorWindowLevelKey)
                if isinstance(cursor_level, bool) or not isinstance(cursor_level, int):
                    cursor_level = None
            except (AttributeError, TypeError, ValueError):
                cursor_level = None
            evidence["cursor_window_level"] = cursor_level
        for item in parsed[:index]:
            bounds = item["bounds"]
            if (item["owner_pid"] != self.pid and bounds["width"] > 0 and bounds["height"] > 0
                    and bounds["x"] < window.bounds.x + window.bounds.width
                    and window.bounds.x < bounds["x"] + bounds["width"]
                    and bounds["y"] < window.bounds.y + window.bounds.height
                    and window.bounds.y < bounds["y"] + bounds["height"]):
                if (cursor_level is not None and item.get("owner_name") == "Window Server"
                        and item.get("name") == "Cursor" and item.get("layer") == cursor_level):
                    # The cursor's native window is not included by ImageGrab.
                    # Do not generalize this to high layers, names or small marks.
                    ignored_cursors.append(item)
                else:
                    blockers.append(item)
        evidence["ignored_cursor_windows"] = ignored_cursors
        evidence["overlapping_foreign_windows"] = blockers
        if blockers:
            refuse("A foreign onscreen window obscures the exact target; capture refused.")
        return evidence

    def capture(self, window_title):
        self._require_permission(capture=True)
        self._require_focus()
        before = self.window(window_title)
        self._focused_window(before)
        x0, y0, x1, y1 = before.bounds.bbox
        rect = Rect(x0, y0, x1 - x0, y1 - y0)
        displays = []
        for screen in self.cocoa.NSScreen.screens():
            display_id = int(screen.deviceDescription()["NSScreenNumber"])
            frame = self.quartz.CGDisplayBounds(display_id)
            bounds = Rect(frame.origin.x, frame.origin.y, frame.size.width, frame.size.height)
            if bounds.contains_rect(rect):
                displays.append((display_id, float(screen.backingScaleFactor())))
        if len(displays) != 1:
            raise DesktopError("Window must be fully visible within exactly one display.")
        visibility_before = self._capture_visibility(before)
        image = ImageGrab.grab(bbox=rect.bbox)
        self._require_focus()
        after = self.window(window_title)
        self._focused_window(after)
        if before.bounds != after.bounds or not self.cf.CFEqual(before.element, after.element):
            raise DesktopError("Window changed during capture; image discarded.")
        visibility_after = self._capture_visibility(after)
        if visibility_before["target_window"] != visibility_after["target_window"]:
            error = DesktopError("Onscreen target window identity changed during capture; image discarded.")
            error.evidence = {"capture_visibility": {"before": visibility_before,
                                                      "after": visibility_after}}
            raise error
        scale_x, scale_y = image.width / rect.width, image.height / rect.height
        if not (math.isclose(scale_x, displays[0][1], abs_tol=.01)
                and math.isclose(scale_y, displays[0][1], abs_tol=.01)):
            raise DesktopError("Captured pixel dimensions disagree with the display scale.")
        return Capture(image, window_title, before.bounds, rect, scale_x, scale_y,
                       time.monotonic(), self.pid, self.identity, displays[0][0], before.element,
                       visibility_evidence={"before": visibility_before, "after": visibility_after})

    def guard(self, capture):
        """Reject stale geometry, changed process/focus, and a different focused window."""
        self._require_permission(capture=True)
        self._require_focus()
        age = time.monotonic() - capture.captured_at
        if capture.pid != self.pid or capture.process_identity != self.identity:
            raise DesktopError("Capture belongs to another process.")
        if not 0 <= age <= self.max_capture_age:
            raise DesktopError("Capture is stale; observe the window again before acting.")
        window = self.window(capture.window_title)
        self._focused_window(window)
        if (window.bounds != capture.window_bounds
                or not self.cf.CFEqual(window.element, capture.window_element)):
            raise DesktopError("Window moved or resized; observe it again before acting.")
        return window

    def _point(self, capture, x, y):
        window = self.guard(capture)
        if not window.bounds.contains(x, y):
            raise DesktopError("Point lies outside the current target window.")
        return window

    def _control(self, element):
        code, pid = self.ax.AXUIElementGetPid(element, None)
        if code != self.ax.kAXErrorSuccess or int(pid) != self.pid:
            raise DesktopError("Accessibility control is not owned by the target process.")
        return AXControl(str(self._read(element, "AXRole", required=True)),
                         self._read(element, "AXTitle"), self._read(element, "AXValue"),
                         self._geometry(element), self._read(element, "AXEnabled"), element)

    def hit_test(self, capture, x, y, *, expected_role=None):
        """Read the exact element at a freshly guarded screen point, including its value."""
        window = self._point(capture, x, y)
        code, element = self.ax.AXUIElementCopyElementAtPosition(
            self.ax.AXUIElementCreateSystemWide(), x, y, None)
        if code != self.ax.kAXErrorSuccess or element is None:
            raise DesktopError(f"Accessibility hit test failed: {code}.")
        control = self._control(element)
        if (not control.bounds.contains(x, y)
                or not window.bounds.contains_rect(control.bounds)):
            if (expected_role is None and control.role == "AXStaticText"
                    and isinstance(control.value, str) and control.value.strip()
                    and window.bounds.contains_rect(control.bounds)
                    and x == control.bounds.x + control.bounds.width
                    and control.bounds.y <= y < control.bounds.y + control.bounds.height):
                raise StaticLabelRightEdge(control, (x, y))
            raise DesktopError("Hit-tested control bounds do not match the target point/window.")
        if expected_role is not None and control.role != expected_role:
            raise DesktopError(f"Expected role {expected_role}; observed {control.role}.")
        return control

    def find_ax(self, capture, *, role, title, max_nodes=1000, max_depth=12):
        """Find one exact exposed control. Incomplete traversal cannot establish uniqueness."""
        if not (isinstance(max_nodes, int) and 0 < max_nodes <= 1000
                and isinstance(max_depth, int) and 0 <= max_depth <= 12):
            raise ValueError("Accessibility traversal bounds are invalid.")
        window = self.guard(capture)
        seen, matches = [], []
        stack = [(window.element, 0)]
        deadline = time.monotonic() + 2
        while stack:
            element, depth = stack.pop()
            if any(self.cf.CFEqual(element, previous) for previous in seen):
                continue
            if len(seen) >= max_nodes or time.monotonic() >= deadline:
                raise DesktopError("Accessibility traversal was truncated; uniqueness is unknown.")
            seen.append(element)
            if self._read(element, "AXRole") == role and self._read(element, "AXTitle") == title:
                matches.append(self._control(element))
            children = list(self._read(element, "AXChildren") or [])
            children += list(self._read(element, "AXContents") or [])
            if children and depth >= max_depth:
                raise DesktopError("Accessibility depth limit reached; uniqueness is unknown.")
            stack.extend((child, depth + 1) for child in children)
        if len(matches) != 1:
            raise DesktopError(f"Expected one exact {role}/{title!r}; found {len(matches)}.")
        if not window.bounds.contains_rect(matches[0].bounds):
            raise DesktopError("Discovered control extends outside the selected window.")
        return matches[0]

    def press(self, capture, control, *, action_guard=None):
        """Press an exposed action once; successful dispatch does not prove its result."""
        if action_guard is not None and not callable(action_guard):
            raise ValueError("Action guard must be callable when supplied.")
        window = self.guard(capture)
        fresh = self._control(control.element)
        if (fresh != control or fresh.enabled is not True
                or not window.bounds.contains_rect(fresh.bounds)):
            raise DesktopError("Accessibility control changed or is not enabled; press refused.")
        if action_guard is not None and not action_guard():
            raise DesktopError("Action context changed; accessibility press refused.")
        code = self.ax.AXUIElementPerformAction(control.element, "AXPress")
        if code != self.ax.kAXErrorSuccess:
            raise DesktopError(f"Accessibility press failed: {code}.")

    def click(self, capture, x, y, *, clicks=1, command=False):
        if clicks not in (1, 2) or isinstance(clicks, bool):
            raise ValueError("Only a single click or double click is supported.")
        if type(command) is not bool or (command and clicks != 1):
            raise ValueError("Command selection supports one explicit single click only.")
        self._point(capture, x, y)
        q = self.quartz
        move = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, (x, y), q.kCGMouseButtonLeft)
        if move is None:
            raise DesktopError("Could not allocate mouse movement event.")
        q.CGEventSetFlags(move, 0)
        q.CGEventPost(q.kCGHIDEventTap, move)
        for count in range(1, clicks + 1):
            down = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDown,
                                           (x, y), q.kCGMouseButtonLeft)
            up = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseUp,
                                         (x, y), q.kCGMouseButtonLeft)
            if down is None or up is None:
                raise DesktopError("Could not allocate mouse button events.")
            for event in (down, up):
                q.CGEventSetFlags(event, 0)
                q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, count)
            if command:
                q.CGEventSetFlags(down, q.kCGEventFlagMaskCommand)
            self._point(capture, x, y)
            try:
                q.CGEventPost(q.kCGHIDEventTap, down)
                time.sleep(.05)
            finally:
                # A released button is required even if focus changes after mouse-down.
                q.CGEventPost(q.kCGHIDEventTap, up)
            if count < clicks:
                time.sleep(.05)

    def move(self, capture, x, y):
        """Move the pointer within the freshly guarded window without clicking."""
        self._point(capture, x, y)
        q = self.quartz
        event = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved,
                                        (x, y), q.kCGMouseButtonLeft)
        if event is None:
            raise DesktopError("Could not allocate pointer movement event.")
        q.CGEventSetFlags(event, 0)
        self._point(capture, x, y)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def double_click(self, capture, x, y):
        self.click(capture, x, y, clicks=2)

    def click_owned_popup(self, window, row, x, y, *, source_guard, content_guard):
        """Click one proved native row in its exact focused unnamed popup.

        This narrow operation does not permit unnamed-window capture or
        general coordinate input. The caller proves the complete choices,
        then observes parent focus/popup closure and the resulting value.
        An uncertain dispatch is never retried by this method.
        """
        if not callable(source_guard) or not callable(content_guard):
            raise ValueError("Popup selection requires callable source and complete-content guards.")
        if not isinstance(window, Window) or not isinstance(row, AXControl):
            raise ValueError("Popup selection requires an observed Window and AXControl row.")

        def guarded():
            self._require_permission(capture=True)
            self._require_focus()
            current_window = self._control(window.element)
            focused = self._read(self.root, "AXFocusedWindow", required=True)
            matches = [item for item in self.windows()
                       if self.cf.CFEqual(item.element, window.element)]
            if (window.title != "" or len(matches) != 1 or matches[0].title != ""
                    or matches[0].bounds != window.bounds
                    or current_window.role != "AXWindow" or current_window.title not in (None, "")
                    or current_window.bounds != window.bounds or current_window.enabled is False
                    or self._read(window.element, "AXModal", required=True) is not False
                    or not self.cf.CFEqual(focused, window.element)):
                raise DesktopError("The exact owned unnamed popup changed; row click refused.")
            current_row = self._control(row.element)
            if (row.role != "AXRow" or current_row != row or current_row.enabled is False
                    or not window.bounds.contains_rect(current_row.bounds)
                    or not current_row.bounds.contains(x, y) or not window.bounds.contains(x, y)):
                raise DesktopError("The exact popup row or its contained point changed; click refused.")
            if source_guard() is not True or content_guard() is not True:
                raise DesktopError("The exact popup source or complete choices changed; click refused.")
            return self._capture_visibility(window)

        visibility = {"before_allocation": guarded()}
        q = self.quartz
        point = (x, y)
        move = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, point, q.kCGMouseButtonLeft)
        down = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDown, point, q.kCGMouseButtonLeft)
        up = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseUp, point, q.kCGMouseButtonLeft)
        if move is None or down is None or up is None:
            raise DesktopError("Could not allocate the one popup click; no input dispatched.")
        for event in (move, down, up):
            q.CGEventSetFlags(event, 0)
        for event in (down, up):
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
        guarded()
        q.CGEventPost(q.kCGHIDEventTap, move)
        visibility["before_mouse_down"] = guarded()
        try:
            q.CGEventPost(q.kCGHIDEventTap, down)
            time.sleep(.05)
        finally:
            # Selection can close the popup on mouse-down. Release must not
            # depend on its continued existence, focus or permission state.
            q.CGEventPost(q.kCGHIDEventTap, up)
        return {"method": "one guarded native popup row click", "point": list(point),
                "row_bounds": row.bounds.as_dict(), "popup_bounds": window.bounds.as_dict(),
                "window_visibility": visibility, "click_dispatches": 1}

    def drag(self, capture, start_x, start_y, end_x, end_y, *, duration=.3):
        """Drag between freshly measured screen points, releasing the button on failure."""
        if not math.isfinite(duration) or not 0 < duration <= 1:
            raise ValueError("Drag duration must be positive and at most one second.")
        self._point(capture, start_x, start_y)
        self._point(capture, end_x, end_y)
        distance = math.hypot(end_x - start_x, end_y - start_y)
        if distance == 0:
            raise ValueError("A drag requires different start and end points.")
        q = self.quartz
        start = (start_x, start_y)
        move = q.CGEventCreateMouseEvent(None, q.kCGEventMouseMoved, start, q.kCGMouseButtonLeft)
        down = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDown, start, q.kCGMouseButtonLeft)
        up = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseUp, start, q.kCGMouseButtonLeft)
        if move is None or down is None or up is None:
            raise DesktopError("Could not allocate drag boundary events.")
        for event in (move, down, up):
            q.CGEventSetFlags(event, 0)
        for event in (down, up):
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
        q.CGEventPost(q.kCGHIDEventTap, move)
        self._point(capture, *start)
        last_point = start
        steps = min(20, max(2, math.ceil(distance / 10)))
        deadline = time.monotonic() + 2
        try:
            q.CGEventPost(q.kCGHIDEventTap, down)
            for index in range(1, steps + 1):
                if time.monotonic() >= deadline:
                    raise DesktopError("Drag observation deadline exceeded; motion stopped.")
                point = (start_x + (end_x - start_x) * index / steps,
                         start_y + (end_y - start_y) * index / steps)
                event = q.CGEventCreateMouseEvent(None, q.kCGEventLeftMouseDragged,
                                                point, q.kCGMouseButtonLeft)
                if event is None:
                    raise DesktopError("Could not allocate drag motion event.")
                q.CGEventSetFlags(event, 0)
                self._point(capture, *point)
                if time.monotonic() >= deadline:
                    raise DesktopError("Drag observation deadline exceeded; motion stopped.")
                last_point = point
                q.CGEventPost(q.kCGHIDEventTap, event)
                time.sleep(duration / steps)
        finally:
            # Keep the preallocated release event even if a later allocation,
            # observation, or motion dispatch fails. Release does not require
            # continued focus; leaving the button held would be unsafe.
            try:
                q.CGEventSetLocation(up, last_point)
            finally:
                q.CGEventPost(q.kCGHIDEventTap, up)

    def dismiss_popup(self, window_element):
        """Dispatch only Escape to the exact focused, unnamed nonmodal window.

        This narrow exception does not make unnamed windows selectable for
        capture or coordinate input. The caller must first identify the expected
        popup contents and then wait for the named parent window to regain focus.
        Successful dispatch alone does not establish that the popup closed.
        """
        q = self.quartz
        down = q.CGEventCreateKeyboardEvent(None, 0x35, True)
        up = q.CGEventCreateKeyboardEvent(None, 0x35, False)
        if down is None or up is None:
            raise DesktopError("Could not allocate popup Escape events.")
        q.CGEventSetFlags(down, 0)
        q.CGEventSetFlags(up, 0)
        self._require_focus()
        code, pid = self.ax.AXUIElementGetPid(window_element, None)
        title = self._read(window_element, "AXTitle")
        if (code != self.ax.kAXErrorSuccess or int(pid) != self.pid
                or self._read(window_element, "AXRole", required=True) != "AXWindow"
                or self._read(window_element, "AXModal") is not False
                or (title is not None and (not isinstance(title, str) or title.strip()))):
            raise DesktopError("Popup identity or nonmodal state is invalid; Escape refused.")
        focused = self._read(self.root, "AXFocusedWindow", required=True)
        if not self.cf.CFEqual(window_element, focused):
            raise DesktopError("The exact popup is not focused; Escape refused.")
        self._require_focus()
        try:
            q.CGEventPost(q.kCGHIDEventTap, down)
        finally:
            q.CGEventPost(q.kCGHIDEventTap, up)

    def key(self, capture, name, *, focus_guard=None):
        if focus_guard is not None and not callable(focus_guard):
            raise ValueError("Keyboard focus guard must be callable when supplied.")
        q = self.quartz
        keys = {"return": (0x24, 0), "tab": (0x30, 0), "escape": (0x35, 0),
                "space": (0x31, 0), "delete": (0x33, 0), "f2": (0x78, 0),
                "home": (0x73, 0), "end": (0x77, 0),
                "cmd+home": (0x73, q.kCGEventFlagMaskCommand),
                "cmd+end": (0x77, q.kCGEventFlagMaskCommand),
                "cmd+a": (0x00, q.kCGEventFlagMaskCommand),
                "cmd+s": (0x01, q.kCGEventFlagMaskCommand),
                "cmd+v": (0x09, q.kCGEventFlagMaskCommand),
                "cmd+c": (0x08, q.kCGEventFlagMaskCommand),
                "cmd+w": (0x0D, q.kCGEventFlagMaskCommand),
                "cmd+q": (0x0C, q.kCGEventFlagMaskCommand),
                "shift+tab": (0x30, q.kCGEventFlagMaskShift)}
        if name not in keys:
            raise ValueError(f"Unsupported keyboard action: {name!r}.")
        code, flags = keys[name]
        down = q.CGEventCreateKeyboardEvent(None, code, True)
        up = q.CGEventCreateKeyboardEvent(None, code, False)
        if down is None or up is None:
            raise DesktopError("Could not allocate keyboard events.")
        q.CGEventSetFlags(down, flags)
        q.CGEventSetFlags(up, 0)
        self.guard(capture)
        if focus_guard is not None and not focus_guard():
            raise DesktopError("Exact field focus changed; keyboard dispatch refused.")
        try:
            q.CGEventPost(q.kCGHIDEventTap, down)
        finally:
            q.CGEventPost(q.kCGHIDEventTap, up)

    def type_text(self, capture, text, *, focus_guard):
        """Send one short Unicode text event without reading or changing the clipboard.

        This inserts text only: the caller selects/replaces the value, commits
        the edit and verifies the actual UI value. A framework can ignore the
        Unicode payload, so dispatch is not proof of successful entry. The
        1024 UTF-16-unit limit is this helper's input bound, not an Apple limit.
        """
        if not isinstance(text, str) or not text or len(text) > 1024 or "\x00" in text:
            raise ValueError("Unicode input requires nonempty text of at most 1024 UTF-16 units without null characters.")
        if not callable(focus_guard):
            raise ValueError("Unicode text input requires a callable exact field focus guard.")
        try:
            # UniCharCount counts UTF-16 units, including both halves of a
            # supplementary character. PyObjC accepts the original str for ^T.
            units = len(text.encode("utf-16-le")) // 2
        except UnicodeEncodeError as exc:
            raise ValueError("Unicode input contains an unpaired surrogate.") from exc
        if units > 1024:
            raise ValueError("Unicode input exceeds 1024 UTF-16 units.")
        self.guard(capture)
        q = self.quartz
        down = q.CGEventCreateKeyboardEvent(None, 0, True)
        up = q.CGEventCreateKeyboardEvent(None, 0, False)
        if down is None or up is None:
            raise DesktopError("Could not allocate Unicode keyboard events.")
        for event in (down, up):
            q.CGEventSetFlags(event, 0)
            q.CGEventKeyboardSetUnicodeString(event, units, text)
        self.guard(capture)
        if not focus_guard():
            raise DesktopError("Exact field focus changed; Unicode text dispatch refused.")
        try:
            q.CGEventPost(q.kCGHIDEventTap, down)
        finally:
            q.CGEventPost(q.kCGHIDEventTap, up)

    def paste(self, capture, text, *, focus_guard=None):
        """Preserve clipboard formats and check optional exact field focus before paste."""
        if not isinstance(text, str) or "\x00" in text:
            raise ValueError("Clipboard input must be text without null characters.")
        if focus_guard is not None and not callable(focus_guard):
            raise ValueError("Paste focus guard must be callable when supplied.")
        self.guard(capture)
        board = self.cocoa.NSPasteboard.generalPasteboard()
        original_change = int(board.changeCount())
        previous = []
        for item in board.pasteboardItems() or []:
            saved = self.cocoa.NSPasteboardItem.alloc().init()
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is None or not saved.setData_forType_(data, kind):
                    raise DesktopError("Could not preserve every clipboard format; paste refused.")
            previous.append(saved)
        replacement = self.cocoa.NSPasteboardItem.alloc().init()
        if not replacement.setString_forType_(text, self.cocoa.NSPasteboardTypeString):
            raise DesktopError("Could not prepare clipboard text.")
        self.guard(capture)
        if int(board.changeCount()) != original_change:
            raise DesktopError("Clipboard changed while preserving it; paste refused.")
        own_change = None
        try:
            board.clearContents()
            own_change = int(board.changeCount())
            written = board.writeObjects_([replacement])
            own_change = int(board.changeCount())
            if not written:
                raise DesktopError("Could not place text on the clipboard.")
            self.key(capture, "cmd+v", focus_guard=focus_guard)
            time.sleep(.3)
        finally:
            if own_change is not None:
                if int(board.changeCount()) != own_change:
                    raise DesktopError("Clipboard changed during paste; newer contents preserved.")
                board.clearContents()
                if previous and not board.writeObjects_(previous):
                    raise DesktopError("Clipboard restoration failed; inspect the clipboard.")
