"""Guarded popup boundaries tested without native input or a running app."""

from types import SimpleNamespace

import pytest

import tjm.native_items as items
from tjm.desktop import AXControl, DesktopError, Rect, Window


class Element:
    def __init__(self, role, bounds, *, title=None, value=None, enabled=True, **attributes):
        self.role, self.bounds, self.title, self.value = role, bounds, title, value
        self.enabled, self.attributes = enabled, attributes


class Native:
    def __init__(self):
        self.identity = (99, "test", "app", 1)
        self.cf = SimpleNamespace(CFEqual=lambda a, b: a is b)
        self.main = Element("AXWindow", Rect(10, 20, 500, 500), title="Main")
        self.tab = Element("AXRadioButton", Rect(200, 40, 100, 20), title="New Order")
        self.group = Element("AXTabGroup", Rect(180, 35, 320, 400), title="New Order",
                             AXValue=self.tab)
        self.number = Element("AXTextField", Rect(200, 70, 100, 20), value="PO000007")
        self.reference = Element("AXTextField", Rect(200, 95, 100, 20), value="SOURCE-REF")
        self.items = Element("AXScrollArea", Rect(190, 200, 310, 180))
        self.popup_table = Element("AXTable", Rect(310, 260, 100, 60))
        scroll = Element("AXScrollArea", self.popup_table.bounds, AXChildren=[self.popup_table])
        self.popup = Element("AXWindow", Rect(305, 255, 110, 70), title="",
                             AXModal=False, AXChildren=[scroll])
        self.root = Element("AXApplication", self.main.bounds, AXFocusedWindow=self.main)
        self.inventory, self.clicks, self.escapes = [self.main], [], []
        self.focused = True
        self.capture = SimpleNamespace(
            process_identity=self.identity, window_element=self.main, window_bounds=self.main.bounds,
            metadata=lambda: {"window_title": "Main"})

    def _require_focus(self):
        if not self.focused:
            raise DesktopError("Target process is not freshly focused.")

    def windows(self):
        return [Window(element.title, element.bounds, element) for element in self.inventory]

    def window(self, title):
        matches = [window for window in self.windows() if window.title == title]
        if len(matches) != 1:
            raise DesktopError("Expected one exact window.")
        return matches[0]

    def _control(self, element):
        return AXControl(element.role, element.title, element.value, element.bounds,
                         element.enabled, element)

    @staticmethod
    def _read(element, attribute, *, required=False):
        value = element.attributes.get(attribute)
        if value is None and required:
            raise DesktopError("Required native attribute is unobservable.")
        return value

    def find_ax(self, capture, *, role, title):
        self.guard(capture)
        if self.group.role != role or self.group.title != title:
            raise DesktopError("Expected one exact control; found 0.")
        return self._control(self.group)

    def guard(self, capture):
        self._require_focus()
        if self.root.attributes["AXFocusedWindow"] is not capture.window_element:
            raise DesktopError("Parent window is not focused.")

    def click(self, capture, *point):
        self.guard(capture)
        self.clicks.append(point)
        self.inventory.append(self.popup)
        self.root.attributes["AXFocusedWindow"] = self.popup

    def dismiss_popup(self, element):
        self._require_focus()
        assert self.root.attributes["AXFocusedWindow"] is element
        self.escapes.append(element)
        self.inventory.remove(element)
        self.root.attributes["AXFocusedWindow"] = self.main


@pytest.fixture
def source(monkeypatch):
    native = Native()
    ui = SimpleNamespace(desktop=native, title="Main", actions=[],
                         field=lambda capture, label, scope: native._control(
                             native.number if label == "No." else native.reference))
    monkeypatch.setattr(items, "editor_scope", lambda *args, **kwargs: (180, 35, 500, 150))

    def wait(predicate, description, **kwargs):
        result = predicate()
        if not result:
            raise DesktopError("Timed out waiting for " + description)
        return result

    monkeypatch.setattr(items, "wait_for", wait)
    guard, evidence = items._vat_source(ui, native.capture, "New Order")
    return native, ui, (guard, evidence, native._control(native.items))


def open_popup(source):
    native, ui, identity = source
    return items._vat_popup(ui, native.capture, (350, 240), native._control(native.items),
                            "SOURCE-SKU", 1, identity)


def saved_order_source(source, *, dirty=False):
    """Use the saved Order tab geometry recorded in diagnostic-01's final focus wait."""
    native, ui, _ = source
    native.main.bounds = Rect(12, 38, 1500, 900)
    native.capture.window_bounds = native.main.bounds
    native.group.bounds = Rect(315, 130, 1197, 459)
    native.group.title = native.tab.title = "*PO000001" if dirty else "PO000001"
    native.tab.bounds = Rect(715, 130, 136 if dirty else 131, 23)
    native.number.value = "PO000001"
    native.number.bounds = Rect(357, 168, 202, 21)
    native.reference.bounds = Rect(387, 228, 405, 21)
    guard, evidence = items._vat_source(ui, native.capture, "PO000001")
    return native, guard, evidence


def mark_saved_order(native, *, dirty):
    native.group.title = native.tab.title = "*PO000001" if dirty else "PO000001"
    native.tab.bounds = Rect(715, 130, 136 if dirty else 131, 23)


def test_captured_clean_to_dirty_tab_transition_keeps_all_sixteen_guard_checks_ready(source):
    # diagnostic-01 recorded one unchanged selected tab widening 131 -> 136 as
    # its title gained '*'; the old bounds check rejected all 16 focus predicates.
    native, guard, evidence = saved_order_source(source)
    assert evidence["selected_tab_title"] == "PO000001"
    mark_saved_order(native, dirty=True)
    assert [guard() for _ in range(16)] == [True] * 16
    mark_saved_order(native, dirty=False)
    assert guard() is True
    assert native.clicks == native.escapes == []


def test_reverse_dirty_to_clean_width_transition_preserves_exact_selected_tab(source):
    native, guard, evidence = saved_order_source(source, dirty=True)
    assert evidence["selected_tab_title"] == "*PO000001"
    mark_saved_order(native, dirty=False)
    assert guard() is True
    mark_saved_order(native, dirty=True)
    assert guard() is True
    assert native.clicks == native.escapes == []


@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("width_change", [-5, 5])
def test_same_title_tab_width_change_is_not_a_dirty_marker_transition(source, dirty, width_change):
    native, guard, _ = saved_order_source(source, dirty=dirty)
    bounds = native.tab.bounds
    native.tab.bounds = Rect(bounds.x, bounds.y, bounds.width + width_change, bounds.height)
    assert guard() is False


@pytest.mark.parametrize("changed", [
    lambda n: setattr(n.tab, "bounds", Rect(716, 130, 136, 23)),
    lambda n: setattr(n.tab, "bounds", Rect(715, 131, 136, 23)),
    lambda n: setattr(n.tab, "bounds", Rect(715, 130, 136, 24)),
    lambda n: setattr(n.tab, "bounds", Rect(715, 130, 126, 23)),
    lambda n: setattr(n.tab, "bounds", Rect(715, 130, 798, 23)),
    lambda n: setattr(n.group, "bounds", Rect(316, 130, 1197, 459)),
    lambda n: setattr(n.tab, "role", "AXButton"),
    lambda n: setattr(n.tab, "enabled", False),
    lambda n: setattr(n.tab, "title", None),
    lambda n: setattr(n.tab, "title", "*PO000002"),
    lambda n: n.group.attributes.update(AXValue=Element("AXRadioButton", n.tab.bounds,
                                                       title=n.tab.title)),
    lambda n: setattr(n.number, "value", "PO000002"),
    lambda n: setattr(n.reference, "value", "OTHER"),
    lambda n: setattr(n.reference, "bounds", Rect(388, 228, 405, 21)),
    lambda n: setattr(n, "identity", (99, "test", "app", 2)),
    lambda n: setattr(n.main, "bounds", Rect(13, 38, 1500, 900)),
    lambda n: setattr(n, "focused", False),
])
def test_dirty_width_transition_does_not_relax_other_source_identity_guards(source, changed):
    native, guard, _ = saved_order_source(source)
    mark_saved_order(native, dirty=True)
    changed(native)
    if not native.focused:
        with pytest.raises(DesktopError, match="freshly focused"):
            guard()
    else:
        assert guard() is False
    assert native.clicks == native.escapes == []


def test_exact_source_context_and_single_escape_preserve_the_document(source):
    native, ui, _ = source
    context = open_popup(source)
    assert context.source_guard() is True
    assert context.evidence["document_number"] == "PO000007"
    assert context.evidence["reference"] == "SOURCE-REF"
    assert context.evidence["row_index"] == 1
    assert native.clicks == [(350, 240)]
    context.cancel()
    assert native.escapes == [native.popup]
    assert native.root.attributes["AXFocusedWindow"] is native.main
    with pytest.raises(DesktopError, match="already received"):
        context.cancel()
    assert len(native.escapes) == 1
    assert [action["action"] for action in ui.actions] == [
        "open_native_item_vat_popup", "cancel_native_item_vat_popup"]


@pytest.mark.parametrize("changed", [
    lambda n: setattr(n.number, "value", "PO000008"),
    lambda n: setattr(n.reference, "value", "OTHER"),
    lambda n: setattr(n.reference, "bounds", Rect(201, 95, 100, 20)),
    lambda n: n.group.attributes.update(AXValue=Element("AXRadioButton", n.tab.bounds,
                                                       title="New Order")),
    lambda n: setattr(n.tab, "enabled", False),
    lambda n: setattr(n, "identity", (99, "test", "app", 2)),
    lambda n: setattr(n.main, "bounds", Rect(11, 20, 500, 500)),
    lambda n: setattr(n, "focused", False),
])
def test_source_identity_change_prevents_click_or_escape(source, changed):
    native, _, _ = source
    changed(native)
    with pytest.raises(DesktopError):
        open_popup(source)
    assert native.clicks == native.escapes == []


def test_same_shaped_different_item_table_prevents_click(source):
    native, ui, identity = source
    other = Element("AXScrollArea", native.items.bounds)
    with pytest.raises(DesktopError, match="table of the verified source SKU"):
        items._vat_popup(ui, native.capture, (350, 240), native._control(other),
                         "SOURCE-SKU", 1, identity)
    assert not native.clicks


@pytest.mark.parametrize("change", [
    lambda n: setattr(n.reference, "value", "CHANGED"),
    lambda n: setattr(n.items, "bounds", Rect(191, 200, 310, 180)),
    lambda n: n.root.attributes.update(AXFocusedWindow=Element("AXWindow", n.popup.bounds, title="")),
    lambda n: n.popup.attributes.update(AXModal=True),
    lambda n: setattr(n.popup, "bounds", Rect(305, 255, 110, 71)),
    lambda n: setattr(n, "focused", False),
])
def test_changed_source_or_popup_prevents_cancellation(source, change):
    native, _, _ = source
    context = open_popup(source)
    change(native)
    with pytest.raises(DesktopError):
        context.cancel()
    assert not native.escapes


def test_unrelated_popup_shape_is_not_adopted_or_dismissed(source):
    native, _, _ = source
    native.popup.attributes["AXChildren"] = [Element("AXCheckBox", native.popup.bounds)]
    with pytest.raises(DesktopError, match="direct AXScrollArea"):
        open_popup(source)
    assert len(native.clicks) == 1
    assert not native.escapes


def test_old_unnamed_window_cannot_be_adopted_as_the_new_vat_popup(source):
    native, _, _ = source
    native.inventory.append(native.popup)
    with pytest.raises(DesktopError, match="one new owned unnamed popup"):
        open_popup(source)
    assert not native.escapes


def test_uncertain_escape_result_is_not_retried(source):
    native, _, _ = source
    context = open_popup(source)

    def uncertain(element):
        native.escapes.append(element)
        raise DesktopError("Dispatch result uncertain")

    native.dismiss_popup = uncertain
    with pytest.raises(DesktopError, match="uncertain"):
        context.cancel()
    with pytest.raises(DesktopError, match="already received"):
        context.cancel()
    assert len(native.escapes) == 1


def test_popup_close_that_changes_reference_is_not_verified(source):
    native, _, _ = source
    context = open_popup(source)
    dismiss = native.dismiss_popup

    def changed_after_escape(element):
        dismiss(element)
        native.reference.value = "OTHER"

    native.dismiss_popup = changed_after_escape
    with pytest.raises(DesktopError, match="after cancelling"):
        context.cancel()
    assert len(native.escapes) == 1
