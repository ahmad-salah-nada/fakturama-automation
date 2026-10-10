"""Form-discovery safety checks with a simulated desktop; no native UI calls."""

from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace as NS
from zipfile import ZipFile

import pytest
from PIL import Image

from tjm import ui as module
from tjm.desktop import AXControl, Capture, DesktopError, Rect, StaticLabelRightEdge
from tjm.ocr import Token
from tjm.ui import Ui


class Screenshot:
    def __init__(self, tokens=()):
        self.image = Image.new("RGB", (400, 200), "white")
        self.screen_rect = self.window_bounds = Rect(0, 0, 400, 200)
        self.scale_x = self.scale_y = 1
        self.words = list(tokens)
        self.window_title = "Fakturama"
        self.window_element = {"pid": 123, "AXRole": "AXWindow", "AXTitle": "Fakturama",
                               "bounds": self.window_bounds, "AXModal": False}

    def tokens(self):
        return self.words

    def screen_point(self, x, y):
        if not self.screen_rect.contains(x, y):
            raise DesktopError("Point outside screenshot.")
        return x, y


class SimulatedDesktop:
    def __init__(self):
        self.pid = 123
        self.controls, self.inputs, self.native_actions = [], [], []
        self.focused = None
        self.drop_click = False
        self.on_click = None
        self.settable_calls = []
        self.settable_result = (0, True)
        self.root = {"AXMenuBar": {"AXChildren": []}}
        self.cf = NS(CFEqual=lambda a, b: a is b)
        self.ax = NS(kAXErrorSuccess=0, AXUIElementPerformAction=self.perform,
                     AXUIElementGetPid=lambda e, ignored: (0, e.get("pid", self.pid)),
                     AXUIElementIsAttributeSettable=self.is_settable)

    def is_settable(self, element, attribute, ignored):
        self.settable_calls.append((element, attribute))
        return self.settable_result

    def add(self, role, title, bounds, value=None, enabled=True):
        element = {"pid": self.pid, "AXRole": role, "AXTitle": title,
                   "bounds": bounds, "AXValue": value, "AXEnabled": enabled,
                   "AXFocused": False}
        self.controls.append(element)
        return self._control(element)

    def _control(self, element):
        if element.get("pid", self.pid) != self.pid:
            raise DesktopError("Control is not owned by target process.")
        return AXControl(element["AXRole"], element.get("AXTitle"), element.get("AXValue"),
                         element["bounds"], element.get("AXEnabled"), element)

    def _read(self, element, attribute, **kwargs):
        return element.get(attribute)

    def _geometry(self, element):
        return element["bounds"]

    def hit_test(self, capture, x, y, **kwargs):
        result = AXControl("AXGroup", None, None, capture.window_bounds, True, {})
        for element in self.controls:
            if element["bounds"].contains(x, y):
                result = self._control(element)
                break
        if kwargs.get("expected_role") and result.role != kwargs["expected_role"]:
            raise DesktopError("Hit-tested control has unexpected role.")
        return result

    def find_ax(self, capture, *, role, title, **kwargs):
        matches = [element for element in self.controls
                   if element["AXRole"] == role and element.get("AXTitle") == title]
        if len(matches) != 1:
            raise DesktopError(f"Expected one exact {role}/{title!r}; found {len(matches)}.")
        return self._control(matches[0])

    def click(self, capture, x, y, **kwargs):
        self.inputs.append(("click", x, y))
        if not self.drop_click:
            for element in self.controls:
                element["AXFocused"] = element["bounds"].contains(x, y)
                if element["AXFocused"]:
                    self.focused = element
        if self.on_click:
            self.on_click()

    def key(self, capture, key, *, focus_guard=None):
        if focus_guard is not None and not focus_guard():
            raise DesktopError("Exact field focus changed before keyboard dispatch.")
        self.inputs.append(("key", key))
        if key == "tab" and self.focused and self.focused["AXRole"] == "AXTextArea":
            self.focused["AXValue"] += "\t"

    def paste(self, capture, text, *, focus_guard=None):
        if focus_guard is not None and not focus_guard():
            raise DesktopError("Exact field focus changed before paste dispatch.")
        self.inputs.append(("paste", text))
        if self.focused:
            self.focused["AXValue"] = text

    def press(self, capture, control, *, action_guard=None):
        if action_guard is not None and not callable(action_guard):
            raise ValueError("Action guard must be callable when supplied.")
        if action_guard is not None and not action_guard():
            raise DesktopError("Action context changed; accessibility press refused.")
        self.inputs.append(("press", control.title))

    def perform(self, element, action):
        self.native_actions.append((element, action))
        if "popup" in element:
            element["popup"]["AXValue"] = element["AXTitle"]
        if element.get("AXRole") == "AXCheckBox":
            element["AXValue"] = 1 - int(element["AXValue"])
        return 0

    def activate(self):
        return True

    def _require_focus(self):
        return None

    def guard(self, capture):
        return NS(bounds=capture.window_bounds)


@pytest.fixture
def form(tmp_path, monkeypatch):
    desktop = SimulatedDesktop()
    screenshot = Screenshot()
    desktop.root["AXFocusedWindow"] = screenshot.window_element
    ui = Ui(desktop, "Fakturama", tmp_path)
    monkeypatch.setattr(ui, "snapshot", lambda *args, **kwargs: screenshot)
    clock = [0.0]
    desktop.clock = clock
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def simulated_poll(predicate, description, *, timeout=8, interval=.1):
        deadline = clock[0] + timeout
        while True:
            result = predicate()
            if result:
                return result
            if clock[0] >= deadline:
                raise DesktopError(f"Timed out waiting for {description}.")
            clock[0] = min(deadline, clock[0] + interval)
    monkeypatch.setattr(module, "wait_for", simulated_poll)
    return ui, desktop, screenshot


def word(text, x, *, y=30, width=30):
    return Token(text, x, y, width, 12, 99)


def test_duplicate_visible_label_is_rejected_before_any_input(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Date", 10), word("Date", 200)]
    with pytest.raises(DesktopError, match="found 2"):
        ui.set_text("Date", "2026-07-14")
    assert desktop.inputs == []


def test_scoped_label_does_not_match_a_label_in_another_editor(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Date", 10), word("Date", 200)]
    assert ui.label(screenshot, "Date", scope=(0, 0, 150, 100)) == (10, 30, 40, 42)
    assert not desktop.inputs


def test_missing_adjacent_field_cannot_scan_past_another_label(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Date", 10), word("Reference", 200, width=60)]
    desktop.add("AXTextField", None, Rect(270, 25, 100, 25), "unrelated")
    with pytest.raises(DesktopError):
        ui.field(screenshot, "Date", scope=(0, 0, 400, 100))
    assert not desktop.inputs


def test_incompatible_adjacent_editable_control_does_not_select_later_field(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Date", 10)]
    desktop.add("AXPopUpButton", None, Rect(50, 25, 80, 25), "Gross")
    desktop.add("AXTextField", None, Rect(230, 25, 100, 25), "unrelated")
    with pytest.raises(DesktopError):
        ui.field(screenshot, "Date", roles=("AXTextField",), scope=(0, 0, 400, 100))
    assert not desktop.inputs


def test_adjacent_field_must_be_wholly_inside_requested_scope(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Date", 10)]
    desktop.add("AXTextField", None, Rect(50, 25, 200, 25), "outside pane")
    with pytest.raises(DesktopError):
        ui.field(screenshot, "Date", scope=(0, 0, 150, 100))
    assert not desktop.inputs


def test_dropped_click_must_stop_before_select_all_or_paste(form):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "target original")
    unrelated = desktop.add("AXTextField", None, Rect(230, 25, 100, 25), "unrelated original")
    desktop.focused = unrelated.element
    unrelated.element["AXFocused"] = True
    desktop.drop_click = True
    with pytest.raises(DesktopError):
        ui.set_control(screenshot, target, "replacement", label="Date")
    assert not any(action[0] in {"key", "paste"} for action in desktop.inputs)
    assert unrelated.element["AXValue"] == "unrelated original"
    assert target.element["AXValue"] == "target original"


def test_exact_field_focus_and_unicode_readback_succeed(form):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "before")
    observed = ui.set_control(screenshot, target, "Müller 東京", label="Name")
    assert observed.value == "Müller 東京"
    assert ui.actions[-1]["observed"] == "Müller 東京"


def test_focus_drift_after_select_all_stops_paste_even_when_target_already_matches(form, monkeypatch):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "already requested")
    unrelated = desktop.add("AXTextField", None, Rect(230, 25, 100, 25), "unrelated original")
    original_key = desktop.key

    def change_focus(capture, key, **kwargs):
        original_key(capture, key, **kwargs)
        if key == "cmd+a":
            target.element["AXFocused"] = False
            unrelated.element["AXFocused"] = True
            desktop.focused = unrelated.element

    monkeypatch.setattr(desktop, "key", change_focus)
    with pytest.raises(DesktopError, match="focus changed before paste"):
        ui.set_control(screenshot, target, "already requested", label="Name")
    assert ("key", "cmd+a") in desktop.inputs
    assert not any(action[0] == "paste" for action in desktop.inputs)
    assert target.element["AXValue"] == "already requested"
    assert unrelated.element["AXValue"] == "unrelated original"
    assert not ui.actions


def test_focus_drift_after_paste_stops_tab_and_does_not_report_success(form, monkeypatch):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "before")
    unrelated = desktop.add("AXTextField", None, Rect(230, 25, 100, 25), "unrelated original")
    original_paste = desktop.paste

    def change_focus(capture, text, **kwargs):
        original_paste(capture, text, **kwargs)
        target.element["AXFocused"] = False
        unrelated.element["AXFocused"] = True
        desktop.focused = unrelated.element

    monkeypatch.setattr(desktop, "paste", change_focus)
    with pytest.raises(DesktopError, match="focus changed before committing input"):
        ui.set_control(screenshot, target, "requested", label="Name")
    assert target.element["AXValue"] == "requested"
    assert unrelated.element["AXValue"] == "unrelated original"
    assert ("key", "tab") not in desktop.inputs
    assert not ui.actions


def test_textarea_never_receives_literal_tab_commit(form):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextArea", None, Rect(50, 25, 100, 25), "before")
    observed = ui.set_control(screenshot, target, "M4 Probe Office", label="Company")
    assert observed.value == "M4 Probe Office"
    assert target.element["AXValue"] == "M4 Probe Office"
    assert ("key", "tab") not in desktop.inputs
    assert desktop.clock[0] >= .3
    # Demonstrate why the simulated widget requires the special input policy.
    desktop.key(screenshot, "tab")
    assert target.element["AXValue"] == "M4 Probe Office\t"


def test_initial_readback_match_does_not_accept_queued_post_input_change(form, monkeypatch):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "before")
    original = desktop._control
    readings = []
    def queued(element):
        if element is target.element and ("paste", "requested") in desktop.inputs:
            element["AXValue"] = "requested" if desktop.clock[0] < .1 else "queued different value"
            readings.append((desktop.clock[0], element["AXValue"]))
        return original(element)
    monkeypatch.setattr(desktop, "_control", queued)
    with pytest.raises(DesktopError, match="exact Name readback"):
        ui.set_control(screenshot, target, "requested", label="Name")
    assert readings[0] == (0, "requested")
    assert any(value == "queued different value" for _, value in readings)
    assert not ui.actions


def test_temporary_readback_mismatch_restarts_stability_interval(form, monkeypatch):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextField", None, Rect(50, 25, 100, 25), "before")
    original = desktop._control
    readings = []
    def delayed(element):
        if element is target.element and ("paste", "requested") in desktop.inputs:
            element["AXValue"] = "temporary" if .1 <= desktop.clock[0] < .2 else "requested"
            readings.append((desktop.clock[0], element["AXValue"]))
        return original(element)
    monkeypatch.setattr(desktop, "_control", delayed)
    observed = ui.set_control(screenshot, target, "requested", label="Name")
    assert observed.value == "requested"
    assert any(value == "temporary" for _, value in readings)
    assert desktop.clock[0] - .2 >= .3
    assert ui.actions[-1]["passed"] is True


def test_textarea_missing_enabled_requires_positive_native_writability_proof(form):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextArea", None, Rect(50, 25, 100, 25), "before", enabled=None)
    observed = ui.set_control(screenshot, target, "Company", label="Company")
    assert observed.value == "Company"
    assert desktop.settable_calls
    assert all(call == (target.element, "AXValue") for call in desktop.settable_calls)
    assert ("key", "tab") not in desktop.inputs


@pytest.mark.parametrize("result", [(0, False), (-25204, True), (-25204, False)])
def test_textarea_missing_enabled_without_native_writability_refuses_before_keys(form, result):
    ui, desktop, screenshot = form
    target = desktop.add("AXTextArea", None, Rect(50, 25, 100, 25), "before", enabled=None)
    desktop.settable_result = result
    with pytest.raises(DesktopError, match="exact Company field focus"):
        ui.set_control(screenshot, target, "Company", label="Company")
    assert desktop.settable_calls
    assert not any(action[0] in {"key", "paste"} for action in desktop.inputs)
    assert target.element["AXValue"] == "before"


@pytest.mark.parametrize("role,enabled", [("AXTextArea", False), ("AXTextField", None)])
def test_writability_exception_never_overrides_explicit_disable_or_other_role(form, role, enabled):
    ui, desktop, screenshot = form
    target = desktop.add(role, None, Rect(50, 25, 100, 25), "before", enabled=enabled)
    with pytest.raises(DesktopError, match="exact Name field focus"):
        ui.set_control(screenshot, target, "Name", label="Name")
    assert not desktop.settable_calls
    assert not any(action[0] in {"key", "paste"} for action in desktop.inputs)
    assert target.element["AXValue"] == "before"


def test_same_native_search_label_padding_does_not_block_its_adjacent_field(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 10)]
    desktop.add("AXStaticText", None, Rect(0, 25, 85, 25), "Search")
    target = desktop.add("AXTextField", None, Rect(90, 25, 100, 25), "")
    observed = ui.field(screenshot, "Search")
    assert observed.element is target.element
    assert not desktop.inputs


@pytest.mark.parametrize("label,value", [("Standard", "Cash"), ("Total", "EUR 678.30")])
def test_readonly_field_discovery_skips_its_own_static_label_padding(form, label, value):
    ui, desktop, screenshot = form
    screenshot.words = [word(label, 10)]
    own_label = desktop.add("AXStaticText", None, Rect(0, 25, 105, 25), label)
    target = desktop.add("AXStaticText", None, Rect(110, 25, 180, 25), value)
    observed = ui.field(screenshot, label, roles=("AXStaticText",), scope=(0, 0, 400, 100))
    assert observed.element is target.element
    assert observed.element is not own_label.element
    assert observed.value == value
    assert not desktop.inputs


def test_standard_readonly_label_is_distinct_from_set_as_standard_button(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Standard", 10, width=60), word("Cash", 125, width=35),
                        word("Set", 205, width=25), word("as", 238, width=15),
                        word("standard", 260, width=65)]
    desktop.add("AXStaticText", None, Rect(0, 25, 105, 25), "Standard")
    cash = desktop.add("AXStaticText", None, Rect(110, 25, 80, 25), "Cash")
    desktop.add("AXButton", "Set as standard", Rect(195, 25, 160, 25))
    observed = ui.field(screenshot, "Standard", roles=("AXStaticText",))
    assert observed.element is cash.element
    assert observed.value == "Cash"
    with pytest.raises(DesktopError, match="found 2"):
        ui.label(screenshot, "Standard")
    assert not desktop.inputs


def test_checkbox_label_is_not_silently_excluded_from_label_ambiguity(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Standard", 10, width=60), word("Standard", 240, width=60)]
    desktop.add("AXStaticText", None, Rect(0, 25, 105, 25), "Standard")
    desktop.add("AXCheckBox", "Standard", Rect(230, 25, 110, 25), 0)
    with pytest.raises(DesktopError, match="found 2"):
        ui.label(screenshot, "Standard", exclude_inputs=True)
    assert not desktop.inputs


def test_search_placeholder_is_not_mistaken_for_a_second_visible_label(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 10, width=40), word("Search", 110, width=40)]
    desktop.add("AXStaticText", None, Rect(0, 25, 85, 25), "Search")
    target = desktop.add("AXTextField", None, Rect(90, 25, 130, 25), "Search")
    assert ui.label(screenshot, "Search", exclude_inputs=True) == (10, 30, 50, 42)
    assert ui.field(screenshot, "Search").element is target.element
    # General label discovery retains strict ambiguity; filtering is deliberate.
    with pytest.raises(DesktopError, match="found 2"):
        ui.label(screenshot, "Search")
    assert not desktop.inputs


@pytest.mark.parametrize("role", ["AXTextField", "AXTextArea", "AXPopUpButton", "AXComboBox"])
def test_visible_text_inside_an_input_alone_cannot_supply_its_label(form, role):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 110, width=40)]
    desktop.add(role, None, Rect(90, 25, 130, 25), "Search")
    with pytest.raises(DesktopError, match="found 0"):
        ui.label(screenshot, "Search", exclude_inputs=True)
    assert not desktop.inputs


def test_two_actual_search_labels_stay_ambiguous_when_inputs_are_excluded(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 10, width=40), word("Search", 240, width=40)]
    desktop.add("AXStaticText", None, Rect(0, 25, 85, 25), "Search")
    desktop.add("AXStaticText", None, Rect(230, 25, 85, 25), "Search")
    with pytest.raises(DesktopError, match="found 2"):
        ui.label(screenshot, "Search", exclude_inputs=True)
    assert not desktop.inputs


def test_single_character_native_button_magnifier_does_not_block_search_field(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 10), word("Q", 65, width=12)]
    desktop.add("AXButton", "", Rect(60, 25, 25, 25))
    target = desktop.add("AXTextField", None, Rect(90, 25, 100, 25), "")
    observed = ui.field(screenshot, "Search")
    assert observed.element is target.element
    assert not desktop.inputs


@pytest.mark.parametrize("role,text", [("AXStaticText", "Q"), ("AXButton", "Find")])
def test_intervening_non_magnifier_text_still_blocks_later_search_field(form, role, text):
    ui, desktop, screenshot = form
    screenshot.words = [word("Search", 10), word(text, 65, width=25)]
    desktop.add(role, None, Rect(60, 25, 30, 25), text)
    desktop.add("AXTextField", None, Rect(95, 25, 100, 25), "unrelated")
    with pytest.raises(DesktopError):
        ui.field(screenshot, "Search")
    assert not desktop.inputs


def test_paired_second_field_clipped_by_scope_refuses_before_either_write(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("ZIP", 10)]
    first = desktop.add("AXTextField", None, Rect(50, 25, 70, 25), "old zip")
    second = desktop.add("AXTextField", None, Rect(150, 25, 150, 25), "old city")
    with pytest.raises(DesktopError):
        ui.set_pair("ZIP", "00421", "Berlin", scope=(0, 0, 200, 100))
    assert not desktop.inputs
    assert first.element["AXValue"] == "old zip"
    assert second.element["AXValue"] == "old city"


def test_paired_second_field_vertical_clipping_refuses_before_either_write(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("ZIP", 10)]
    first = desktop.add("AXTextField", None, Rect(50, 25, 70, 25), "old zip")
    second = desktop.add("AXTextField", None, Rect(150, 25, 100, 100), "old city")
    with pytest.raises(DesktopError):
        ui.set_pair("ZIP", "00421", "Berlin", scope=(0, 0, 300, 100))
    assert not desktop.inputs
    assert first.element["AXValue"] == "old zip"
    assert second.element["AXValue"] == "old city"


@pytest.fixture
def matched_icon(form, tmp_path, monkeypatch):
    """Reach native dispatch checks without depending on actual desktop pixels."""
    import cv2
    import numpy as np

    ui, desktop, screenshot = form
    app = tmp_path / "Fakturama2.app"
    plugins = app / "Contents/Eclipse/plugins"
    plugins.mkdir(parents=True)
    data = BytesIO()
    Image.new("RGBA", (20, 20), (20, 70, 120, 255)).save(data, format="PNG")
    with ZipFile(plugins / "com.sebulli.fakturama.resources.icons_2.2.0.probe.jar", "w") as jar:
        jar.writestr("icons/20x20/probe.png", data.getvalue())
    desktop.identity = (desktop.pid, "Fakturama.ID", str(app), 1.0)
    # Image matching is intentionally isolated: these tests prove dispatch guards.
    monkeypatch.setattr(cv2, "matchTemplate", lambda source, template, method: np.array([[.99]], dtype="float32"))
    return ui, desktop, screenshot


@pytest.mark.parametrize("role,enabled", [("AXTextField", True), ("AXButton", False), ("AXButton", None)])
def test_matched_icon_wrong_role_or_not_explicitly_enabled_refuses_click(matched_icon, role, enabled):
    ui, desktop, _ = matched_icon
    desktop.add(role, "test icon", Rect(0, 0, 20, 20), enabled=enabled)
    with pytest.raises(DesktopError, match="not an enabled native button"):
        ui.icon("probe.png")
    assert not desktop.inputs
    assert not ui.actions


def test_matched_enabled_icon_still_requires_exact_requested_help(matched_icon):
    ui, desktop, _ = matched_icon
    button = desktop.add("AXButton", "test icon", Rect(0, 0, 20, 20))
    button.element["AXHelp"] = "different action"
    with pytest.raises(DesktopError, match="native help"):
        ui.icon("probe.png", expected_help="required action")
    assert not desktop.inputs
    assert not ui.actions


def test_matched_enabled_icon_with_expected_help_dispatches_once(matched_icon):
    ui, desktop, _ = matched_icon
    button = desktop.add("AXButton", "test icon", Rect(0, 0, 20, 20))
    button.element["AXHelp"] = "required action"
    ui.icon("probe.png", expected_help="required action")
    assert desktop.inputs == [("click", 10.0, 10.0)]
    assert ui.actions[-1]["native_role"] == "AXButton"
    assert ui.actions[-1]["native_help"] == "required action"


def test_matched_image_selector_with_exact_help_dispatches_once(matched_icon):
    ui, desktop, _ = matched_icon
    image = desktop.add("AXImage", "selector", Rect(0, 0, 20, 20))
    image.element["AXHelp"] = "required selector"
    ui.icon("probe.png", expected_help="required selector")
    assert desktop.inputs == [("click", 10.0, 10.0)]
    assert ui.actions[-1]["native_role"] == "AXImage"
    assert ui.actions[-1]["native_help"] == "required selector"


def test_matched_image_selector_without_explicit_help_refuses_dispatch(matched_icon):
    ui, desktop, _ = matched_icon
    image = desktop.add("AXImage", "selector", Rect(0, 0, 20, 20))
    image.element["AXHelp"] = "required selector"
    with pytest.raises(DesktopError, match="not an enabled native button"):
        ui.icon("probe.png")
    assert not desktop.inputs
    assert not ui.actions


def test_search_label_cannot_be_satisfied_by_research_substring(form, monkeypatch):
    ui, desktop, _ = form
    desktop.add("AXTextField", None, Rect(120, 25, 100, 25), "old")
    monkeypatch.setattr(module, "ocr_tokens", lambda image, psm: [word("Research", 10)])
    with pytest.raises(DesktopError, match="Search"):
        ui.search("needle")
    assert not desktop.inputs


def test_exact_search_label_and_readback_succeed(form, monkeypatch):
    ui, desktop, _ = form
    field = desktop.add("AXTextField", None, Rect(120, 25, 100, 25), "old")
    monkeypatch.setattr(module, "ocr_tokens", lambda image, psm: [word("Search", 10)])
    observed = ui.search("exact SKU")
    assert observed.value == "exact SKU"
    assert field.element["AXValue"] == "exact SKU"


def menu_item(desktop, title, *, enabled=True, pid=None, popup=None):
    element = {"pid": desktop.pid if pid is None else pid, "AXRole": "AXMenuItem",
               "AXTitle": title, "AXEnabled": enabled, "AXChildren": []}
    if popup:
        element["popup"] = popup
    return element


def test_ui_press_forwards_context_guard_after_exact_control_discovery(form, monkeypatch):
    ui, desktop, _ = form
    desktop.add("AXButton", "Save", Rect(50, 25, 100, 25))
    events = []
    original_find = desktop.find_ax

    def discover(*args, **kwargs):
        control = original_find(*args, **kwargs)
        events.append("discovered Save")
        return control

    def context():
        events.append("checked action context")
        return True

    monkeypatch.setattr(desktop, "find_ax", discover)
    ui.press("Save", action_guard=context)
    assert events == ["discovered Save", "checked action context"]
    assert desktop.inputs == [("press", "Save")]
    assert ui.actions == [{"action": "AXPress", "title": "Save", "role": "AXButton"}]


def test_ui_press_false_action_guard_never_dispatches_or_reports_action(form):
    ui, desktop, _ = form
    desktop.add("AXButton", "Save", Rect(50, 25, 100, 25))
    with pytest.raises(DesktopError, match="Action context changed"):
        ui.press("Save", action_guard=lambda: False)
    assert not desktop.inputs
    assert not ui.actions


def test_ui_press_action_guard_exception_never_dispatches_or_reports_action(form):
    ui, desktop, _ = form
    desktop.add("AXButton", "Save", Rect(50, 25, 100, 25))

    def unknown_context():
        raise DesktopError("Active editor identity could not be established.")

    with pytest.raises(DesktopError, match="editor identity"):
        ui.press("Save", action_guard=unknown_context)
    assert not desktop.inputs
    assert not ui.actions


def test_ui_press_default_guard_and_requested_role_preserve_exact_discovery(form):
    ui, desktop, _ = form
    desktop.add("AXCheckBox", "paid", Rect(50, 25, 100, 25), 0)
    ui.press("paid", role="AXCheckBox")
    assert desktop.inputs == [("press", "paid")]
    assert ui.actions == [{"action": "AXPress", "title": "paid", "role": "AXCheckBox"}]


def test_duplicate_application_menu_names_stop_without_dispatch(form):
    ui, desktop, _ = form
    desktop.root["AXMenuBar"]["AXChildren"] = [menu_item(desktop, "New Order"),
                                                menu_item(desktop, "New Order")]
    with pytest.raises(DesktopError):
        ui.menu("New Order")
    assert not desktop.native_actions


@pytest.mark.parametrize("enabled,pid", [(False, 123), (True, 999)])
def test_application_menu_disabled_or_foreign_item_is_not_dispatched(form, enabled, pid):
    ui, desktop, _ = form
    desktop.root["AXMenuBar"]["AXChildren"] = [menu_item(desktop, "New Order", enabled=enabled, pid=pid)]
    with pytest.raises(DesktopError):
        ui.menu("New Order")
    assert not desktop.native_actions


def test_truncated_dropdown_cannot_claim_a_unique_choice(form):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "Gross")
    desired = menu_item(desktop, "Net", popup=popup.element)
    duplicate = menu_item(desktop, "Net", popup=popup.element)
    chain = duplicate
    for _ in range(1001):
        chain = {"AXRole": "AXGroup", "AXChildren": [chain]}
    popup.element["AXChildren"] = [chain, desired]
    with pytest.raises(DesktopError):
        ui.choose_control(screenshot, popup, "Net", label="Price mode")
    assert not desktop.native_actions
    assert popup.element["AXValue"] == "Gross"


@pytest.mark.parametrize("enabled,pid", [(False, 123), (True, 999)])
def test_dropdown_disabled_or_foreign_item_is_not_dispatched(form, enabled, pid):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "Gross")
    popup.element["AXChildren"] = [menu_item(desktop, "Net", enabled=enabled, pid=pid,
                                            popup=popup.element)]
    with pytest.raises(DesktopError):
        ui.choose_control(screenshot, popup, "Net", label="Price mode")
    assert not desktop.native_actions
    assert popup.element["AXValue"] == "Gross"


def test_application_menu_elapsed_time_limit_stops_before_dispatch(form, monkeypatch):
    ui, desktop, _ = form
    items = [menu_item(desktop, f"Other {index}") for index in range(30)]
    desktop.root["AXMenuBar"]["AXChildren"] = [menu_item(desktop, "New Order"), *items]
    clock = [0.0]
    def ticking():
        clock[0] += .5
        return clock[0]
    monkeypatch.setattr(module.time, "monotonic", ticking)
    with pytest.raises(DesktopError, match="incomplete"):
        ui.menu("New Order")
    assert not desktop.native_actions


def test_popup_children_cycle_is_visited_once_and_unique_choice_succeeds(form):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "Gross")
    choice = menu_item(desktop, "Net", popup=popup.element)
    group = {"AXRole": "AXGroup", "AXChildren": [choice]}
    group["AXChildren"].append(popup.element)
    popup.element["AXChildren"] = [group]
    ui.choose_control(screenshot, popup, "Net", label="Price mode")
    assert desktop.native_actions == [(choice, "AXPress")]
    assert popup.element["AXValue"] == "Net"


def test_enum_display_whitespace_optin_selects_raw_native_title_and_preserves_evidence(form):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "Mutually defined ")
    raw_title = " Credit transfer \t"
    choice = menu_item(desktop, raw_title, popup=popup.element)
    popup.element["AXChildren"] = [choice]
    ui.choose_control(screenshot, popup, "Credit transfer", label="Payment code", display_whitespace=True)
    assert desktop.native_actions == [(choice, "AXPress")]
    assert popup.element["AXValue"] == raw_title
    assert ui.actions[-1] == {"action": "choose", "label": "Payment code", "selected": "Credit transfer",
                              "native_menu_title": raw_title, "native_value": raw_title,
                              "display_whitespace": True, "passed": True}


@pytest.mark.parametrize("name", ["Credit transfer", "Bank Transfer"])
def test_choice_defaults_preserve_exact_enum_or_master_name_matching(form, name):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "original")
    popup.element["AXChildren"] = [menu_item(desktop, name + " ", popup=popup.element)]
    with pytest.raises(DesktopError, match="choice"):
        ui.choose_control(screenshot, popup, name, label="Method or code")
    assert not desktop.native_actions
    assert popup.element["AXValue"] == "original"
    assert not ui.actions


def test_enum_display_whitespace_never_merges_two_alias_choices(form):
    ui, desktop, screenshot = form
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 100, 25), "Mutually defined ")
    popup.element["AXChildren"] = [menu_item(desktop, "Credit transfer", popup=popup.element),
                                    menu_item(desktop, "Credit transfer ", popup=popup.element)]
    with pytest.raises(DesktopError, match="Multiple visible choices"):
        ui.choose_control(screenshot, popup, "Credit transfer", label="Payment code", display_whitespace=True)
    assert not desktop.native_actions
    assert popup.element["AXValue"] == "Mutually defined "
    assert not ui.actions


def test_choose_forwards_explicit_enum_display_whitespace_option(form):
    ui, desktop, screenshot = form
    screenshot.words = [word("Code", 10)]
    popup = desktop.add("AXPopUpButton", None, Rect(50, 25, 150, 25), "Mutually defined ")
    choice = menu_item(desktop, "Credit transfer ", popup=popup.element)
    popup.element["AXChildren"] = [choice]
    ui.choose("Code", "Credit transfer", scope=(0, 0, 400, 100), display_whitespace=True)
    assert desktop.native_actions == [(choice, "AXPress")]
    assert ui.actions[-1]["display_whitespace"] is True
    assert ui.actions[-1]["native_menu_title"] == "Credit transfer "
    assert ui.actions[-1]["native_value"] == "Credit transfer "


def prepare_address_popup(form, *, invoice=0, delivery=1, returned_parent=None):
    ui, desktop, screenshot = form
    screenshot.words = [word("address", 10, width=50), word("type", 65, width=30)]
    desktop.add("AXTextField", None, Rect(110, 25, 100, 25), "roles")
    desktop.add("AXButton", "", Rect(210, 25, 25, 25))
    invoice_control = desktop.add("AXCheckBox", "Invoice address", Rect(130, 70, 220, 20), invoice)
    delivery_control = desktop.add("AXCheckBox", "Delivery address", Rect(130, 105, 220, 20), delivery)
    popup = {"pid": desktop.pid, "AXRole": "AXWindow", "AXTitle": "", "AXModal": False,
             "bounds": Rect(120, 60, 260, 110),
             "AXChildren": [invoice_control.element, delivery_control.element]}
    desktop.on_click = lambda: desktop.root.update(AXFocusedWindow=popup)
    def dismiss(element):
        assert element is popup
        desktop.inputs.append(("dismiss-popup",))
        desktop.root["AXFocusedWindow"] = screenshot.window_element if returned_parent is None else returned_parent
    desktop.dismiss_popup = dismiss
    return popup, invoice_control, delivery_control


def test_address_roles_set_both_explicit_states_and_return_to_exact_parent(form):
    ui, desktop, screenshot = form
    popup, invoice, delivery = prepare_address_popup(form)
    observed = ui.address_roles({"Invoice address"}, scope=(0, 0, 400, 200))
    assert observed == {"Invoice address": 1, "Delivery address": 0}
    assert invoice.element["AXValue"] == 1
    assert delivery.element["AXValue"] == 0
    assert [element for element, _ in desktop.native_actions] == [delivery.element, invoice.element]
    assert desktop.root["AXFocusedWindow"] is screenshot.window_element
    assert desktop.inputs[-1] == ("dismiss-popup",)
    assert popup["AXModal"] is False


def test_address_roles_already_matching_are_not_toggled_again(form):
    ui, desktop, _ = form
    prepare_address_popup(form, invoice=1, delivery=0)
    assert ui.address_roles({"Invoice address"}, scope=(0, 0, 400, 200)) == {
        "Invoice address": 1, "Delivery address": 0}
    assert not desktop.native_actions
    assert desktop.inputs[-1] == ("dismiss-popup",)


@pytest.mark.parametrize("change", [
    lambda popup, i, d: popup.update(pid=999),
    lambda popup, i, d: popup.update(AXRole="AXGroup"),
    lambda popup, i, d: popup.update(AXModal=True),
    lambda popup, i, d: popup.update(bounds=Rect(350, 60, 260, 110)),
    lambda popup, i, d: i.element.update(AXTitle="Delivery address"),
    lambda popup, i, d: d.element.update(AXEnabled=False),
    lambda popup, i, d: d.element.update(AXValue=2),
    lambda popup, i, d: i.element.update(AXValue=2),
])
def test_address_popup_identity_and_checkbox_pattern_checked_before_changes(form, change):
    ui, desktop, _ = form
    popup, invoice, delivery = prepare_address_popup(form)
    change(popup, invoice, delivery)
    with pytest.raises(DesktopError):
        ui.address_roles({"Invoice address"}, scope=(0, 0, 400, 200))
    assert not desktop.native_actions
    assert not any(action[0] == "dismiss-popup" for action in desktop.inputs)


def test_address_popup_return_to_same_named_lookalike_is_not_proof(form):
    ui, desktop, screenshot = form
    returned_parent = dict(screenshot.window_element)
    prepare_address_popup(form, invoice=1, delivery=0, returned_parent=returned_parent)
    with pytest.raises(DesktopError):
        ui.address_roles({"Invoice address"}, scope=(0, 0, 400, 200))
    assert not desktop.native_actions
    assert not ui.actions


def test_first_ui_session_keeps_the_requested_empty_directory(tmp_path):
    requested = tmp_path / 'selector'
    ui = Ui(None, 'Select the address', requested)
    assert ui.evidence_dir == requested
    assert (requested / '.ui-session').is_file()
    assert ui.sequence == 0


def test_second_ui_session_before_first_capture_has_separate_directory(tmp_path):
    requested = tmp_path / 'selector'
    first = Ui(None, 'Select the address', requested)
    second = Ui(None, 'Select the address', requested)
    assert first.evidence_dir == requested
    assert second.evidence_dir.parent == requested
    assert second.evidence_dir != first.evidence_dir
    assert (second.evidence_dir / '.ui-session').is_file()


def test_existing_nonempty_evidence_is_preserved_in_reused_requested_directory(tmp_path):
    previous = tmp_path / '0001-before-field.png'
    previous.write_bytes(b'previous evidence must remain unchanged')
    ui = Ui(None, 'Select the address', tmp_path)
    assert ui.evidence_dir.parent == tmp_path
    assert previous.read_bytes() == b'previous evidence must remain unchanged'


def test_repeated_ui_snapshots_keep_exclusive_capture_and_action_evidence(tmp_path):
    rectangle = Rect(0, 0, 20, 20)
    identity = (123, 'Fakturama.ID', '/tested/Fakturama2.app', 1.0)
    color = ['red']
    def capture(title):
        return Capture(Image.new('RGB', (20, 20), color[0]), title, rectangle, rectangle,
                       1, 1, 0.0, 123, identity, 1, {})
    desktop = NS(activate=lambda: None, capture=capture)
    first = Ui(desktop, 'Select the address', tmp_path)
    first.snapshot('before-field', ocr=False)
    first.actions.append({'session': 'first'})
    first.write_actions()
    original_image = (first.evidence_dir / '0001-before-field.png').read_bytes()
    original_actions = (first.evidence_dir / 'actions.json').read_bytes()
    color[0] = 'blue'
    second = Ui(desktop, 'Select the address', tmp_path)
    second.snapshot('before-field', ocr=False)
    second.actions.append({'session': 'second'})
    second.write_actions()
    assert (first.evidence_dir / '0001-before-field.png').read_bytes() == original_image
    assert (first.evidence_dir / 'actions.json').read_bytes() == original_actions
    assert (second.evidence_dir / '0001-before-field.png').read_bytes() != original_image
    assert (second.evidence_dir / 'actions.json').read_bytes() != original_actions
    with pytest.raises(FileExistsError):
        capture('Select the address').save(first.evidence_dir / '0001-before-field.png')


def test_simultaneous_ui_constructors_never_share_the_same_evidence_directory(tmp_path):
    barrier = Barrier(3)
    def construct():
        barrier.wait()
        return Ui(None, 'Select the address', tmp_path).evidence_dir
    with ThreadPoolExecutor(max_workers=3) as executor:
        directories = list(executor.map(lambda unused: construct(), range(3)))
    assert len(set(directories)) == 3
    assert all((directory / '.ui-session').is_file() for directory in directories)


def test_field_discovery_advances_after_its_exact_native_label_right_edge(tmp_path):
    desktop = SimulatedDesktop()
    ui = Ui(desktop, "Fakturama", tmp_path)
    capture = Screenshot()
    label = desktop.add("AXStaticText", None, Rect(20, 20, 38, 16), "Cust.Ref.")
    field = desktop.add("AXTextField", None, Rect(60, 20, 90, 22), "")
    ui.label = lambda *args, **kwargs: (20, 20, 50, 36)
    points = []

    def hit(capture, x, y):
        points.append((x, y))
        if (x, y) == (58, 28):
            raise StaticLabelRightEdge(label, (x, y))
        assert field.bounds.contains(x, y)
        return field

    desktop.hit_test = hit
    assert ui.field(capture, "Cust.Ref.") == field
    assert points == [(58, 28), (74, 28)]
    assert ui.discoveries[0]["method"] == "skip exact native label right edge"
    assert desktop.inputs == desktop.native_actions == []


@pytest.mark.parametrize("role,value", [("AXStaticText", "Other label"),
                                         ("AXTextField", "Cust.Ref.")])
def test_field_discovery_does_not_skip_other_native_edge_results(tmp_path, role, value):
    desktop = SimulatedDesktop()
    ui = Ui(desktop, "Fakturama", tmp_path)
    capture = Screenshot()
    wrong = desktop.add(role, None, Rect(20, 20, 38, 16), value)
    ui.label = lambda *args, **kwargs: (20, 20, 50, 36)

    def hit(capture, x, y):
        raise StaticLabelRightEdge(wrong, (x, y))

    desktop.hit_test = hit
    with pytest.raises(StaticLabelRightEdge):
        ui.field(capture, "Cust.Ref.")
    assert ui.discoveries == desktop.inputs == desktop.native_actions == []
