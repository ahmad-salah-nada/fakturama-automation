"""Visible OCR labels connected to exact native controls on the current screen.

This is the Fakturama form layer. No sample values or stored screen coordinates
are used. Missing labels, ambiguous scopes and failed readback stop the caller.
"""

import json
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from PIL import Image, ImageOps

from .desktop import Desktop, DesktopError, StaticLabelRightEdge, wait_for
from .fields import find_phrases, normalized
from .ocr import token_evidence, ocr_tokens


class Ui:
    def __init__(self, desktop: Desktop, window_title: str, evidence_dir: Path, *, text_input="paste"):
        if text_input not in {"paste", "unicode"}:
            raise ValueError("Choose explicit paste or Unicode keyboard text input.")
        self.desktop, self.title = desktop, window_title
        self.text_input = text_input
        requested = Path(evidence_dir)
        requested.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = requested
        if any(requested.iterdir()):
            self.evidence_dir = requested / ('session-' + uuid4().hex)
            self.evidence_dir.mkdir(exist_ok=False)
        # Claim even an empty folder before the first capture. Reopening the
        # same dialog creates another Ui with sequence zero and must not reuse
        # that session's filenames. Exclusive creation also covers concurrent
        # constructors observing the same empty requested directory.
        try:
            (self.evidence_dir / '.ui-session').touch(exist_ok=False)
        except FileExistsError:
            if self.evidence_dir != requested:
                raise
            self.evidence_dir = requested / ('session-' + uuid4().hex)
            self.evidence_dir.mkdir(exist_ok=False)
            (self.evidence_dir / '.ui-session').touch(exist_ok=False)
        self.sequence = 0
        self.actions = []
        self.discoveries = []

    def snapshot(self, label="observation", *, ocr=True):
        self.desktop.activate()
        capture = self.desktop.capture(self.title)
        self.sequence += 1
        prefix = self.evidence_dir / f"{self.sequence:04d}-{label}"
        capture.save(prefix.with_suffix(".png"))
        metadata = capture.metadata()
        if ocr:
            metadata["tokens"] = token_evidence(capture.tokens())
        prefix.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        return capture

    def wait_tab(self, title, *, timeout=8):
        self.desktop.activate()
        def ready():
            capture = self.desktop.capture(self.title)
            try:
                self.scope(capture, title)
            except DesktopError as exc:
                if str(exc).endswith("found 0."):
                    return None
                raise
            return capture
        return wait_for(ready, f"visible {title} panel", timeout=timeout)

    def label(self, capture, text, *, scope=None, exclude_inputs=False):
        tokens = capture.tokens()
        if scope is not None:
            tokens = [t for t in tokens if scope[0] <= t.cx < scope[2] and scope[1] <= t.cy < scope[3]]
        boxes = find_phrases(tokens, text)
        method = "raw OCR PSM11"
        if not boxes:
            # Colored focus backgrounds can make whole-window OCR omit labels.
            # This preparation is for discovery; exact native values remain the
            # readback source for entered customer/product text.
            prepared = ImageOps.grayscale(capture.image).point(lambda v: 0 if v < 160 else 255)
            tokens = ocr_tokens(prepared, psm=11)
            if scope is not None:
                tokens = [t for t in tokens if scope[0] <= t.cx < scope[2] and scope[1] <= t.cy < scope[3]]
            boxes = find_phrases(tokens, text)
            method = "grayscale threshold160 OCR PSM11"
            self.discoveries.append({"sequence": self.sequence, "label": text,
                                     "method": method, "tokens": token_evidence(tokens)})
        if exclude_inputs:
            inputs = {"AXTextField", "AXTextArea", "AXPopUpButton", "AXComboBox", "AXButton"}
            observed = [(box, self.desktop.hit_test(
                capture, *capture.screen_point((box[0] + box[2]) / 2,
                                              (box[1] + box[3]) / 2)).role) for box in boxes]
            # Exposed field labels are AXStaticText. SWT can report an enclosing
            # composite for custom-button captions, such as Set as standard.
            static = [box for box, role in observed if role == "AXStaticText"]
            boxes = (static + [box for box, role in observed if role == "AXCheckBox"]
                     if static else [box for box, role in observed if role not in inputs])
        if len(boxes) != 1:
            raise DesktopError(f"Expected one visible {text!r} label in its current scope; found {len(boxes)}.")
        self.discoveries.append({"sequence": self.sequence, "label": text,
                                 "method": method, "pixel_box": list(boxes[0]), "scope": scope})
        return boxes[0]

    def scope(self, capture, scope):
        if not isinstance(scope, str):
            return scope
        try:
            control = self.desktop.find_ax(capture, role="AXTabGroup", title=scope)
        except DesktopError as exc:
            if not str(exc).endswith("found 0."):
                raise
            # SWT exposes the visible dirty marker as part of the tab title.
            control = self.desktop.find_ax(capture, role="AXTabGroup", title="*" + scope)
        rect = control.bounds
        return ((rect.x - capture.screen_rect.x) * capture.scale_x,
                (rect.y - capture.screen_rect.y) * capture.scale_y,
                (rect.x + rect.width - capture.screen_rect.x) * capture.scale_x,
                (rect.y + rect.height - capture.screen_rect.y) * capture.scale_y)

    def field(self, capture, label, *, roles=("AXTextField",), scope=None):
        scope = self.scope(capture, scope)
        box = self.label(capture, label, scope=scope, exclude_inputs=True)
        height = box[3] - box[1]
        right_limit = scope[2] if scope else capture.image.width
        editable = {"AXTextField", "AXTextArea", "AXPopUpButton", "AXComboBox", "AXCheckBox"}
        adjacent = []
        for token in capture.tokens():
            if (token.left > box[2] and token.left < right_limit
                    and abs(token.cy - (box[1] + box[3]) / 2) <= height / 2):
                neighbor = self.desktop.hit_test(capture, *capture.screen_point(token.cx, token.cy))
                if neighbor.role == "AXButton" and len(token.text) != 1:
                    parent = self.desktop._read(neighbor.element, "AXParent")
                    if parent is not None and self.desktop._read(parent, "AXRole") == "AXTextField":
                        buttons = [self.desktop._read(parent, name)
                                   for name in ("AXSearchButton", "AXClearButton")]
                        if any(button is not None and self.desktop.cf.CFEqual(button, neighbor.element)
                               for button in buttons):
                            continue
                if (neighbor.role == "AXButton" and len(token.text) == 1) or (
                        neighbor.role == "AXStaticText"
                        and normalized(str(neighbor.value)) == normalized(label)):
                    continue
                if neighbor.role not in editable.union(roles):
                    right_limit = min(right_limit, token.left)
                elif neighbor.role in editable:
                    left = (neighbor.bounds.x - capture.screen_rect.x) * capture.scale_x
                    if box[2] <= left <= box[2] + height*2:
                        adjacent.append((left, neighbor))
        # A populated field can expose a precise native control from its own
        # visible text. Prefer that adjacent control to hit-testing blank padding,
        # where SWT can return an oversized parent rather than a valid leaf.
        if adjacent:
            left, control = min(adjacent, key=lambda candidate: candidate[0])
            if left < right_limit:
                if control.role not in roles:
                    raise DesktopError(f"The first adjacent input for {label!r} has incompatible role {control.role}.")
                self.require_inside_scope(capture, control, scope)
                return control
        # Inspect adjacent current controls along the observed label baseline.
        # The stride comes from the glyph height, not remembered screen pixels.
        for x in range(round(box[2] + height / 2), round(right_limit), max(2, round(height))):
            point = capture.screen_point(x, (box[1] + box[3]) / 2)
            try:
                control = self.desktop.hit_test(capture, *point)
            except StaticLabelRightEdge as error:
                if error.control.role != "AXStaticText" or error.control.value != label:
                    raise
                self.discoveries.append({"method": "skip exact native label right edge",
                                         "label": label, **error.evidence})
                continue
            if (control.role == "AXStaticText"
                    and normalized(str(control.value)) == normalized(label)):
                continue
            if control.role in roles:
                self.require_inside_scope(capture, control, scope)
                return control
            if control.role in editable:
                raise DesktopError(f"The first adjacent input for {label!r} has incompatible role {control.role}.")
            if control.role == "AXStaticText" and str(control.value).strip():
                if normalized(str(control.value)) == normalized(label):
                    continue
                raise DesktopError(f"An intervening text label blocks discovery for {label!r}.")
        raise DesktopError(f"No adjacent {roles} control found for {label!r}.")

    @staticmethod
    def require_inside_scope(capture, control, scope):
        if scope is None:
            return
        r = control.bounds
        box = ((r.x - capture.screen_rect.x) * capture.scale_x,
               (r.y - capture.screen_rect.y) * capture.scale_y,
               (r.x + r.width - capture.screen_rect.x) * capture.scale_x,
               (r.y + r.height - capture.screen_rect.y) * capture.scale_y)
        if not (scope[0] <= box[0] and scope[1] <= box[1]
                and box[2] <= scope[2] and box[3] <= scope[3]):
            raise DesktopError("Discovered input extends outside its current semantic scope.")

    def section(self, capture, editor, start, end):
        scope = self.scope(capture, editor)
        first = self.label(capture, start, scope=scope)
        last = self.label(capture, end, scope=scope)
        if first[3] >= last[1]:
            raise DesktopError("Section headings are not in the expected order.")
        return (scope[0], first[1], scope[2], last[1])

    def icon(self, filename, *, size=20, scope=None, expected_help=None, action_guard=None):
        """Match an installed Fakturama icon inside its current semantic area."""
        import cv2
        import numpy as np

        capture = self.snapshot("before-icon")
        scope = self.scope(capture, scope)
        app = Path(self.desktop.identity[2])
        jars = list((app / "Contents/Eclipse/plugins").glob("com.sebulli.fakturama.resources.icons_2.2.0.*.jar"))
        if len(jars) != 1:
            raise DesktopError("One installed Fakturama 2.2.0 icon resource bundle is required.")
        with ZipFile(jars[0]) as jar:
            template = Image.open(BytesIO(jar.read(f"icons/{size}x{size}/{filename}"))).convert("RGBA")
        background = Image.new("RGBA", template.size, (240, 240, 240, 255))
        background.alpha_composite(template)
        template = background.convert("RGB").resize((round(size * capture.scale_x),
                                                       round(size * capture.scale_y)))
        x0, y0, x1, y1 = tuple(map(round, scope or (0, 0, capture.image.width, capture.image.height)))
        source = np.asarray(capture.image.convert("RGB"))[y0:y1, x0:x1]
        scores = cv2.matchTemplate(source, np.asarray(template), cv2.TM_CCOEFF_NORMED)
        candidates = []
        while scores.size:
            _, score, _, point = cv2.minMaxLoc(scores)
            if score < .94:
                break
            x, y = point
            candidates.append((x + x0, y + y0, score))
            scores[max(0, y-template.height):y+template.height,
                   max(0, x-template.width):x+template.width] = -1
        if len(candidates) != 1:
            raise DesktopError(f"Expected one {filename} icon in its current area; found {len(candidates)}.")
        x, y, score = candidates[0]
        point = capture.screen_point(x + template.width / 2, y + template.height / 2)
        control = self.desktop.hit_test(capture, *point)
        help_text = self.desktop._read(control.element, "AXHelp")
        # SWT renders its document selectors as clickable AXImage elements.
        # Permit that observed role only with an exact caller-supplied tooltip.
        image_action = control.role == "AXImage" and bool(expected_help)
        if (control.role != "AXButton" and not image_action) or control.enabled is not True:
            raise DesktopError(f"Matched icon is not an enabled native button: role={control.role!r}, "
                               f"title={control.title!r}, enabled={control.enabled!r}; dispatch refused.")
        if expected_help and help_text != expected_help:
            raise DesktopError(f"Icon native help does not identify the intended selector: {help_text!r}.")
        if action_guard is not None and (not callable(action_guard) or not action_guard()):
            raise DesktopError("The icon action's exact source state changed; dispatch refused.")
        self.desktop.click(capture, *point)
        self.actions.append({"action": "icon", "resource": filename, "score": score,
                             "pixel_box": [x, y, x + template.width, y + template.height],
                             "native_role": control.role, "native_help": help_text})

    def set_text(self, label, value, *, scope=None, expected=None):
        capture = self.snapshot("before-field")
        control = self.field(capture, label, scope=scope, roles=("AXTextField", "AXTextArea"))
        return self.set_control(capture, control, value, label=label, expected=expected)

    def enter_text(self, capture, text, *, focus_guard):
        """Use the selected transport explicitly, without an automatic fallback."""
        if not text:
            self.desktop.key(capture, "delete", focus_guard=focus_guard)
        elif self.text_input == "unicode":
            self.desktop.type_text(capture, text, focus_guard=focus_guard)
        else:
            self.desktop.paste(capture, text, focus_guard=focus_guard)

    def set_control(self, capture, control, value, *, label, expected=None):
        targets = ((str(value),) if expected is None else
                   (expected,) if isinstance(expected, str) else expected)
        if (not isinstance(targets, tuple) or not 1 <= len(targets) <= 2
                or any(not isinstance(item, str) for item in targets)):
            raise ValueError("Readback requires one exact string or two explicit display alternatives.")
        self.desktop.click(capture, control.bounds.x + control.bounds.width / 2,
                           control.bounds.y + control.bounds.height / 2)
        def exact_focus():
            self.desktop.guard(capture)
            fresh = self.desktop._control(control.element)
            writable = fresh.enabled is True
            if fresh.enabled is None and fresh.role == "AXTextArea":
                code, settable = self.desktop.ax.AXUIElementIsAttributeSettable(
                    fresh.element, "AXValue", None)
                writable = code == self.desktop.ax.kAXErrorSuccess and bool(settable)
            return (fresh.role == control.role and fresh.bounds == control.bounds
                    and writable
                    and self.desktop._read(control.element, "AXFocused") is True)
        wait_for(exact_focus, f"exact {label} field focus", timeout=2)
        self.desktop.key(capture, "cmd+a", focus_guard=exact_focus)
        if not exact_focus():
            raise DesktopError(f"Exact {label} field focus changed before paste.")
        self.enter_text(capture, str(value), focus_guard=exact_focus)
        # SWT's multiline Company widget accepts Tab as a literal character.
        # Its Modify binding updates without a focus change; the next explicit
        # field click provides any needed focus transition.
        if control.role != "AXTextArea":
            if not exact_focus():
                raise DesktopError(f"Exact {label} field focus changed before committing input.")
            self.desktop.key(capture, "tab", focus_guard=exact_focus)
        matched_since = None
        def readback():
            nonlocal matched_since
            fresh = self.desktop._control(control.element)
            if str(fresh.value) not in targets:
                matched_since = None
                return None
            if matched_since is None:
                matched_since = time.monotonic()
            return fresh if time.monotonic() - matched_since >= .3 else None
        observed = wait_for(readback, f"exact {label} readback")
        self.actions.append({"action": "set_text", "label": label, "requested": str(value),
                             "observed": observed.value, "passed": True})
        return observed

    def set_pair(self, label, first, second, *, scope=None):
        capture = self.snapshot("before-paired-fields")
        scope = self.scope(capture, scope)
        a = self.field(capture, label, scope=scope)
        midpoint = a.bounds.y + a.bounds.height / 2
        step = max(1, a.bounds.height / 2)
        x = a.bounds.x + a.bounds.width + step
        limit = capture.window_bounds.x + capture.window_bounds.width
        if scope:
            limit = capture.screen_rect.x + scope[2] / capture.scale_x
        b = None
        while x < limit:
            observed = self.desktop.hit_test(capture, x, midpoint)
            if observed.role == "AXTextField" and observed.bounds.x > a.bounds.x + a.bounds.width:
                self.require_inside_scope(capture, observed, scope)
                if observed.bounds.x + observed.bounds.width > limit:
                    raise DesktopError("The second paired field extends outside the current scope.")
                b = observed
                break
            x += step
        if b is None:
            raise DesktopError(f"No second adjacent text field for {label!r}.")
        self.set_control(capture, a, first, label=label + " first")
        fresh = self.snapshot("before-paired-second", ocr=False)
        current_b = self.desktop._control(b.element)
        if current_b.bounds != b.bounds:
            raise DesktopError("Paired field geometry changed; rediscover before acting.")
        self.set_control(fresh, current_b, second, label=label + " second")

    def click_heading_button(self, heading, button_text, *, scope=None):
        capture = self.snapshot("before-heading-button")
        scope = self.scope(capture, scope)
        box = self.label(capture, heading, scope=scope)
        height = box[3] - box[1]
        area = (box[0], max(0, box[1] - height),
                scope[2] if scope else capture.image.width, box[3] + height)
        crop = capture.image.crop(area)
        enlarged = crop.resize((crop.width * 2, crop.height * 2))
        tokens = ocr_tokens(enlarged, psm=7)
        matches = [t for t in tokens if t.text == button_text]
        if len(matches) != 1:
            raise DesktopError(f"Expected one visible {button_text!r} button beside {heading!r}.")
        token = matches[0]
        point = capture.screen_point(area[0] + token.cx / 2, area[1] + token.cy / 2)
        control = self.desktop.hit_test(capture, *point, expected_role="AXButton")
        self.desktop.click(capture, *point)
        self.actions.append({"action": "heading_button", "heading": heading,
                             "button_text": button_text, "native_title": control.title})

    def address_roles(self, desired, *, scope):
        permitted = {"Invoice address", "Delivery address"}
        desired = set(desired)
        if not desired or not desired.issubset(permitted):
            raise ValueError("Choose Invoice address and/or Delivery address explicitly.")
        capture = self.snapshot("before-address-roles")
        field = self.field(capture, "address type", scope=scope)
        point = (field.bounds.x + field.bounds.width + field.bounds.height / 2,
                 field.bounds.y + field.bounds.height / 2)
        self.desktop.hit_test(capture, *point, expected_role="AXButton")
        self.desktop.click(capture, *point)

        def read_popup():
            d = self.desktop
            d._require_focus()
            popup = d._read(d.root, "AXFocusedWindow", required=True)
            if d._read(popup, "AXTitle") not in {None, ""}:
                return None
            if d._read(popup, "AXModal") is not False:
                raise DesktopError("Address roles require the identified non-modal popup.")
            code, pid = d.ax.AXUIElementGetPid(popup, None)
            if (code != d.ax.kAXErrorSuccess or int(pid) != d.pid
                    or d._read(popup, "AXRole") != "AXWindow"):
                raise DesktopError("Address popup is not an owned native window.")
            bounds = d._geometry(popup)
            if not capture.window_bounds.contains_rect(bounds):
                raise DesktopError("Address popup extends outside its observed parent.")
            stack, seen, checks = [popup], [], []
            deadline = time.monotonic() + 2
            while stack:
                element = stack.pop()
                if any(d.cf.CFEqual(element, old) for old in seen):
                    continue
                seen.append(element)
                if len(seen) > 50 or time.monotonic() > deadline:
                    raise DesktopError("Address role popup inspection is incomplete.")
                if d._read(element, "AXRole") == "AXCheckBox":
                    checks.append(d._control(element))
                stack.extend(d._read(element, "AXChildren") or [])
            if sorted(c.title for c in checks) != sorted(permitted):
                raise DesktopError("The popup does not expose exactly the two expected address roles.")
            if not all(bounds.contains_rect(c.bounds) and c.enabled is True
                       and c.value in {0, 1} for c in checks):
                raise DesktopError("Address-role control bounds/state are not valid.")
            return popup, checks

        popup, controls = wait_for(read_popup, "address-role popup")
        observed = {}
        for original in controls:
            d = self.desktop
            d._require_focus()
            if not d.cf.CFEqual(popup, d._read(d.root, "AXFocusedWindow", required=True)):
                raise DesktopError("Address popup focus changed before selection.")
            current = d._control(original.element)
            expected = int(current.title in desired)
            if (current.role != "AXCheckBox" or current.title != original.title
                    or current.bounds != original.bounds or current.enabled is not True
                    or current.value not in {0, 1}):
                raise DesktopError("Address-role control changed before selection.")
            if int(current.value) != expected:
                code = d.ax.AXUIElementPerformAction(current.element, "AXPress")
                if code != d.ax.kAXErrorSuccess:
                    raise DesktopError(f"Address-role action failed: {code}.")
            wait_for(lambda: d._read(current.element, "AXValue") == expected,
                     f"{current.title} checkbox readback")
            observed[current.title] = expected
        self.desktop.dismiss_popup(popup)
        def parent_focused():
            window = self.desktop._read(self.desktop.root, "AXFocusedWindow")
            return (window is not None and self.desktop.cf.CFEqual(window, capture.window_element)
                    and self.desktop._read(window, "AXTitle") == self.title)
        wait_for(parent_focused, "address popup closed to its exact named parent")
        self.actions.append({"action": "address_roles", "desired": sorted(desired),
                             "observed_checkboxes": observed, "passed": True})
        return observed

    def search(self, value, *, scope=None):
        capture = self.snapshot("before-search", ocr=False)
        control = self.desktop.find_ax(capture, role="AXTextField", title=None)
        scope = self.scope(capture, scope)
        self.require_inside_scope(capture, control, scope)
        r = control.bounds
        x = (r.x - capture.screen_rect.x) * capture.scale_x
        y = (r.y - capture.screen_rect.y) * capture.scale_y
        crop = capture.image.crop((max(0, x - r.width * capture.scale_x), y,
                                   x + r.width * capture.scale_x,
                                   y + r.height * capture.scale_y))
        crop = ImageOps.grayscale(crop).point(lambda v: 0 if v < 160 else 255)
        crop = ImageOps.expand(crop.resize((crop.width * 2, crop.height * 2)), border=20, fill="white")
        words = ocr_tokens(crop, psm=7)
        if not find_phrases(words, "Search"):
            raise DesktopError("The unique dialog text field has no visible Search label; search refused.")
        return self.set_control(capture, control, value, label="Search")

    def relayout_product_dialog(self):
        """Trigger one visible resize for the observed SWT selector layout defect.

        M1 showed an invisible search field inside the title bar on initial open;
        a one-unit size change restored layout. This action alone proves no cure:
        search() still requires its visible label before entering a query.
        """
        if self.title != "Select a product":
            raise DesktopError("The layout probe is restricted to the Product selector.")
        capture = self.snapshot("before-product-dialog-relayout", ocr=False)
        d = self.desktop
        window = d.guard(capture)
        code, settable = d.ax.AXUIElementIsAttributeSettable(window.element, "AXSize", None)
        if code != d.ax.kAXErrorSuccess or not settable:
            raise DesktopError("The Product selector size is not settable.")
        width, height = window.bounds.width + 1, window.bounds.height + 1
        display = d.quartz.CGDisplayBounds(capture.display_id)
        if (window.bounds.x + width > display.origin.x + display.size.width
                or window.bounds.y + height > display.origin.y + display.size.height):
            raise DesktopError("The Product selector layout resize would leave its display.")
        value = d.ax.AXValueCreate(d.ax.kAXValueCGSizeType, d.quartz.CGSizeMake(width, height))
        d.guard(capture)
        code = d.ax.AXUIElementSetAttributeValue(window.element, "AXSize", value)
        if code != d.ax.kAXErrorSuccess:
            raise DesktopError(f"Product selector layout resize failed: {code}.")
        actual = d.window(self.title).bounds
        if (actual.width, actual.height) != (width, height):
            raise DesktopError("The Product selector did not take its requested layout size.")
        self.actions.append({"action": "product_dialog_relayout", "before": window.bounds.as_dict(),
                             "after": actual.as_dict(), "search_visibility_verified": False})

    def click_label(self, label, *, scope=None, clicks=1):
        capture = self.snapshot("before-label-click")
        scope = self.scope(capture, scope)
        box = self.label(capture, label, scope=scope)
        self.desktop.click(capture, *capture.screen_point((box[0] + box[2]) / 2,
                                                        (box[1] + box[3]) / 2), clicks=clicks)
        self.actions.append({"action": "click_label", "label": label})

    def press(self, title, *, role="AXButton", action_guard=None):
        capture = self.snapshot("before-press", ocr=False)
        control = self.desktop.find_ax(capture, role=role, title=title)
        self.desktop.press(capture, control, action_guard=action_guard)
        self.actions.append({"action": "AXPress", "title": title, "role": role})

    def menu(self, title):
        """Select an exposed application menu item once, after fresh focus checks."""
        self.desktop.activate()
        self.desktop._require_focus()
        root = self.desktop._read(self.desktop.root, "AXMenuBar", required=True)
        stack, matches, seen = [(root, 0)], [], []
        deadline = time.monotonic() + 2
        while stack:
            element, depth = stack.pop()
            if any(self.desktop.cf.CFEqual(element, e) for e in seen):
                continue
            seen.append(element)
            if len(seen) > 1000 or depth > 12 or time.monotonic() > deadline:
                raise DesktopError("Menu traversal incomplete.")
            if (self.desktop._read(element, "AXRole") == "AXMenuItem"
                    and self.desktop._read(element, "AXTitle") == title):
                matches.append(element)
            stack.extend((child, depth + 1) for child in self.desktop._read(element, "AXChildren") or [])
        if len(matches) != 1 or self.desktop._read(matches[0], "AXEnabled") is not True:
            raise DesktopError(f"One enabled menu item {title!r} is required.")
        self.press_menu_item(matches[0], title)
        self.actions.append({"action": "menu", "title": title})

    def choose(self, label, value, *, scope=None, display_whitespace=False):
        capture = self.snapshot("before-choice")
        control = self.field(capture, label, roles=("AXPopUpButton",), scope=scope)
        self.choose_control(capture, control, value, label=label,
                            display_whitespace=display_whitespace)

    def choose_current(self, current, value, *, scope=None):
        capture = self.snapshot("before-current-choice")
        scope = self.scope(capture, scope)
        tokens = capture.tokens()
        if scope:
            tokens = [t for t in tokens if scope[0] <= t.cx < scope[2] and scope[1] <= t.cy < scope[3]]
        controls = []
        for box in find_phrases(tokens, current):
            point = capture.screen_point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            control = self.desktop.hit_test(capture, *point)
            if control.role == "AXPopUpButton" and control.value == current:
                controls.append(control)
        if len(controls) != 1:
            raise DesktopError(f"Expected one visible popup currently set to {current!r}; found {len(controls)}.")
        self.choose_control(capture, controls[0], value, label=current)

    def choose_control(self, capture, control, value, *, label, display_whitespace=False):
        def display(value):
            return value.strip() if display_whitespace and isinstance(value, str) else value
        self.desktop.press(capture, control)
        # Menu children become available through this native UI element. Reading
        # them is UI automation, not an internal application API.
        def options():
            found, seen = [], []
            stack = [(control.element, 0)]
            deadline = time.monotonic() + 2
            while stack:
                element, depth = stack.pop()
                if any(self.desktop.cf.CFEqual(element, old) for old in seen):
                    continue
                seen.append(element)
                if len(seen) > 1000 or depth > 12 or time.monotonic() > deadline:
                    raise DesktopError("Choice traversal incomplete; uniqueness is unknown.")
                if (self.desktop._read(element, "AXRole") == "AXMenuItem"
                        and display(self.desktop._read(element, "AXTitle")) == value):
                    found.append(element)
                stack.extend((child, depth + 1) for child in self.desktop._read(element, "AXChildren") or [])
            if len(found) > 1:
                raise DesktopError(f"Multiple visible choices {value!r}.")
            return found[0] if found else None
        item = wait_for(options, f"choice {value}")
        actual_title = self.desktop._read(item, "AXTitle", required=True)
        self.press_menu_item(item, actual_title)
        observed = wait_for(lambda: display(self.desktop._control(control.element).value) == value,
                            f"selected {label}={value}")
        self.actions.append({"action": "choose", "label": label, "selected": value,
                             "native_menu_title": actual_title,
                             "native_value": self.desktop._control(control.element).value,
                             "display_whitespace": display_whitespace, "passed": observed})

    def press_menu_item(self, item, title):
        self.desktop._require_focus()
        code, pid = self.desktop.ax.AXUIElementGetPid(item, None)
        if (code != self.desktop.ax.kAXErrorSuccess or int(pid) != self.desktop.pid
                or self.desktop._read(item, "AXRole") != "AXMenuItem"
                or self.desktop._read(item, "AXTitle") != title
                or self.desktop._read(item, "AXEnabled") is not True):
            raise DesktopError("Menu item identity/state changed; dispatch refused.")
        code = self.desktop.ax.AXUIElementPerformAction(item, "AXPress")
        if code != self.desktop.ax.kAXErrorSuccess:
            raise DesktopError(f"Menu item action failed: {code}.")

    def write_actions(self):
        (self.evidence_dir / "actions.json").write_text(json.dumps(self.actions, ensure_ascii=False, indent=2) + "\n")
        (self.evidence_dir / "discovery.json").write_text(json.dumps(self.discoveries, ensure_ascii=False, indent=2) + "\n")
