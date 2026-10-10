from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm.desktop import AXControl, DesktopError, Rect, Window
from tjm.item_vat import ensure_item_vat, parse_item_vat_display, read_vat_popup
from tjm.models import Item
from tjm.workflow_rules import SaveLedger, WorkflowReview


@pytest.mark.parametrize("display,name,percent", [
    ("VAT 19% (19.0%)", "VAT 19%", "19.0"),
    ("Tax-free (0.0%)", "Tax-free", "0.0"),
    ("A name containing 19% (12.5%)", "A name containing 19%", "12.5"),
    ("VAT (special) (7.5%)", "VAT (special)", "7.5"),
])
def test_complete_final_percentage_is_read_independently_of_name(display, name, percent):
    assert parse_item_vat_display(display) == (name, Decimal(percent))


@pytest.mark.parametrize("display", [
    "", None, "VAT 19%", "VAT 19% (19.0", "VAT 19% (19...%)", "VAT (19,0%)",
    "VAT (-1.0%)", "VAT (100.1%)", "VAT (19.0%) extra", " (19.0%)",
])
def test_blank_truncated_or_unsupported_display_does_not_establish_vat(display):
    with pytest.raises(DesktopError):
        parse_item_vat_display(display)


class Element:
    def __init__(self, role, bounds, *, value=None, title=None, enabled=None, owner=99, **attributes):
        self.control = AXControl(role, title, value, bounds, enabled, self)
        self.owner = owner
        self.attributes = attributes


class NativePopup:
    def __init__(self, selection):
        self.pid = 99
        self.cf = SimpleNamespace(CFEqual=lambda first, second: first is second)
        self.bounds = Rect(100, 200, 200, 100)
        self.rows = []
        for index, value in enumerate(("Tax-free (0.0%)", "VAT 19% (19.0%)")):
            bounds = Rect(110, 210 + index * 25, 180, 20)
            text = Element("AXStaticText", bounds, value=value, enabled=True)
            self.rows.append(Element("AXRow", bounds, AXChildren=[text]))
        self.table = Element("AXTable", Rect(105, 205, 190, 90), enabled=True,
                             AXRows=self.rows, AXSelectedRows=[self.rows[index] for index in selection])
        scroll = Element("AXScrollArea", self.table.control.bounds, AXChildren=[self.table])
        self.popup = Element("AXWindow", self.bounds, title="", AXModal=False, AXChildren=[scroll])
        self.root = Element("AXApplication", self.bounds, AXFocusedWindow=self.popup)
        self.window = Window("", self.bounds, self.popup)

    def _require_focus(self):
        pass

    def windows(self):
        return [self.window]

    def _control(self, element):
        if element.owner != self.pid:
            raise DesktopError("Native control is not owned by Fakturama.")
        return element.control

    @staticmethod
    def _read(element, attribute, *, required=False):
        value = element.attributes.get(attribute)
        if value is None and required:
            raise DesktopError("Required native attribute is unobservable.")
        return value


def popup_context(native, guard=lambda: True):
    return SimpleNamespace(popup=native.window, source_guard=guard,
                           evidence={"sku": "SOURCE-SKU", "row_index": 0})


def test_only_native_selected_row_establishes_vat_even_when_another_option_is_available():
    native = NativePopup([0])
    actual = read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))
    assert actual["percent"] == "0.0"
    assert actual["display"] == "Tax-free (0.0%)"
    assert actual["evidence"]["selected_row_index"] == 0
    assert actual["evidence"]["available_row_count"] == 2


@pytest.mark.parametrize("selection", [[], [0, 1]])
def test_available_choices_with_zero_or_multiple_selections_are_not_current_vat(selection):
    native = NativePopup(selection)
    with pytest.raises(DesktopError, match="one selected native row"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


def test_selection_attribute_must_be_exposed():
    native = NativePopup([1])
    native.table.attributes.pop("AXSelectedRows")
    with pytest.raises(DesktopError, match="unobservable"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


def test_selected_row_must_belong_to_this_popup_table():
    native = NativePopup([1])
    native.table.attributes["AXSelectedRows"] = [Element("AXRow", native.rows[0].control.bounds)]
    with pytest.raises(DesktopError, match="exact member"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


def test_popup_focus_identity_is_checked_without_targeting_another_unnamed_window():
    native = NativePopup([1])
    native.root.attributes["AXFocusedWindow"] = Element("AXWindow", native.bounds, title="")
    with pytest.raises(DesktopError, match="no longer focused"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


@pytest.mark.parametrize("guard_value", [False, 1, "yes"])
def test_source_guard_requires_exact_true_before_reading_popup_selection(guard_value):
    native = NativePopup([1])
    with pytest.raises(DesktopError, match="source item/document context changed"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native, lambda: guard_value))


def test_source_context_change_during_readback_is_refused():
    native = NativePopup([1])
    values = iter((True, False))
    with pytest.raises(DesktopError, match="source item/document context changed"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native, lambda: next(values)))


def test_selected_static_text_owned_by_another_process_is_refused():
    native = NativePopup([1])
    native.rows[1].attributes["AXChildren"][0].owner = 100
    with pytest.raises(DesktopError, match="not owned"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


def test_changed_selection_after_value_readback_is_refused():
    native = NativePopup([1])
    original_read, reads = native._read, 0

    def changing_read(element, attribute, *, required=False):
        nonlocal reads
        if element is native.table and attribute == "AXSelectedRows":
            reads += 1
            if reads == 2:
                return [native.rows[0]]
        return original_read(element, attribute, required=required)

    native._read = changing_read
    with pytest.raises(DesktopError, match="selection/value changed"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


def real_shape_native():
    """Only the recorded Tax-free row is supplied; no source-VAT choice is invented."""
    tree = json.loads((Path(__file__).parent / "fixtures/item_vat_popup_real_shape.json").read_text())

    def build(node):
        element = Element(node["role"], Rect(**node["bounds"]), value=node["value"],
                          title=node["title"], enabled=node["enabled"])
        element.actions = node["actions"]
        element.attributes["AXChildren"] = [build(child) for child in node.get("children", [])]
        return element

    native = NativePopup([0])
    native.popup = build(tree)
    native.popup.attributes["AXModal"] = False
    scroll = native.popup.attributes["AXChildren"][0]
    native.table = scroll.attributes["AXChildren"][0]
    native.rows = [child for child in native.table.attributes["AXChildren"] if child.control.role == "AXRow"]
    native.table.attributes.update(AXRows=native.rows, AXSelectedRows=native.rows[:1])
    native.bounds = native.popup.control.bounds
    native.window = Window("", native.bounds, native.popup)
    native.root.attributes["AXFocusedWindow"] = native.popup
    return native


def test_recorded_one_unit_table_border_keeps_actual_tax_free_choice_readable():
    native = real_shape_native()
    actual = read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))
    assert actual["name"] == "Tax-free" and actual["percent"] == "0.0"
    assert actual["evidence"]["available_row_count"] == 1
    assert native.rows[0].actions == native.rows[0].attributes["AXChildren"][0].actions == []
    assert native.rows[0].control.enabled is None


@pytest.mark.parametrize("bounds", [Rect(950, 353, 89, 17), Rect(951, 352, 88, 18),
                                    Rect(951, 353, 89, 17), Rect(951, 353, 88, 18)])
def test_arbitrary_table_overflow_does_not_use_the_exact_border_exception(bounds):
    native = real_shape_native()
    native.table.control = AXControl("AXTable", None, None, bounds, True, native.table)
    with pytest.raises(DesktopError, match="outside its parent"):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


@pytest.mark.parametrize("role", ["AXRow", "AXStaticText"])
def test_row_or_text_inside_table_border_but_outside_scroll_is_refused(role):
    native = real_shape_native()
    row = native.rows[0]
    if role == "AXRow":
        row.control = AXControl("AXRow", None, None, native.table.control.bounds, None, row)
    else:
        text = row.attributes["AXChildren"][0]
        text.control = AXControl("AXStaticText", None, "Tax-free (0.0%)", native.table.control.bounds, True, text)
    with pytest.raises(DesktopError):
        read_vat_popup(SimpleNamespace(desktop=native), popup_context(native))


class SelectionNative(NativePopup):
    """Fake native reads/popup click exercise the real VAT selector."""
    def __init__(self, *, selected=0, wanted=True):
        super().__init__([selected])
        self.rows[0].attributes["AXChildren"][0].control = self.rows[0].attributes["AXChildren"][0].control.__class__(
            "AXStaticText", None, "VAT 7% (7.0%)", self.rows[0].control.bounds, True,
            self.rows[0].attributes["AXChildren"][0])
        if not wanted:
            self.table.attributes["AXRows"] = self.rows[:1]
        self.main = Element("AXWindow", Rect(50, 50, 400, 400), title="Main")
        self.main_window = Window("Main", self.main.control.bounds, self.main)
        self.popup_window = self.window
        del self.window
        self.opened = False
        self.actual_vat_display = self.rows[selected].attributes["AXChildren"][0].control.value
        self.context_valid = True
        self.action_names = []
        self.action_error = 0
        self.wrong_after = False
        self.dispatches, self.cancels, self.open_calls, self.resolve_calls = [], 0, 0, 0
        self.action_names_hook = None
        for row in self.rows:
            row.control = row.control.__class__("AXRow", None, None, row.control.bounds, None, row)
        self.ax = SimpleNamespace(kAXErrorSuccess=0,
            AXUIElementCopyActionNames=self.copy_action_names,
            AXUIElementGetPid=lambda element, ignored: (0, element.owner))

    def copy_action_names(self, element, ignored):
        if self.action_names_hook is not None:
            hook, self.action_names_hook = self.action_names_hook, None
            hook()
        return 0, self.action_names

    def perform_action(self, element, action):
        self.dispatches.append((element, action))
        if self.action_error:
            raise DesktopError("Owned-popup click result uncertain; do not repeat it blindly.")
        row = next(row for row in self.table.attributes["AXRows"]
                   if element is row or element in row.attributes["AXChildren"])
        self.table.attributes["AXSelectedRows"] = [self.rows[0] if self.wrong_after else row]
        self.actual_vat_display = self.table.attributes["AXSelectedRows"][0].attributes["AXChildren"][0].control.value
        self.opened = False
        self.root.attributes["AXFocusedWindow"] = self.main
        return 0

    def click_owned_popup(self, window, row, x, y, *, source_guard, content_guard):
        assert window == self.popup_window and row.role == "AXRow"
        assert row.bounds.contains(x, y)
        for _ in range(2):
            assert source_guard() is True and content_guard() is True
        return self.perform_action(row.element, "click")

    def windows(self):
        return [self.main_window, self.popup_window] if self.opened else [self.main_window]

    def window(self, title):
        assert title == "Main"
        return self.main_window

    def open(self, ui, **kwargs):
        self.open_calls += 1
        self.opened = True
        self.root.attributes["AXFocusedWindow"] = self.popup
        context = SimpleNamespace(popup=self.popup_window, source_guard=lambda: self.context_valid,
                                  evidence={"row_index": kwargs["row_index"]})
        context.evidence.update(document_number="O-A", reference="SOURCE", sku=kwargs["expected_sku"])

        def cancel():
            from tjm.item_vat import _popup_guard
            _popup_guard(self, context)
            self.cancels += 1
            self.opened = False
            self.root.attributes["AXFocusedWindow"] = self.main
        context.cancel = cancel
        return context

    def resolve(self, *args, **kwargs):
        assert not self.opened
        self.resolve_calls += 1
        self.table.attributes["AXRows"] = self.rows
        return {"passed": True, "created": True, "scope": "VAT definition resolution double"}


def source_item():
    return Item(sku="SOURCE-SKU", description="Source description", quantity=Decimal("2"),
                unit_net=Decimal("250"), vat_percent=Decimal("19"), discount_percent=Decimal("10"),
                line_net=Decimal("450"), product_gross=Decimal("297.50"))


def writer_ui(monkeypatch, native):
    monkeypatch.setattr("tjm.native_items.open_item_vat_popup", native.open)
    monkeypatch.setattr("tjm.product_sequence.resolve_vat", native.resolve)
    monkeypatch.setattr("tjm.native_items.read_painted_item_cell", lambda *a, **k:
                        {"text": native.actual_vat_display, "scope": "painted cell read double"})
    return SimpleNamespace(desktop=native, title="Main", actions=[])


def test_real_writer_corrects_existing_product_vat_once_and_reopens_selection(monkeypatch, tmp_path):
    native = SelectionNative()
    ui = writer_ui(monkeypatch, native)
    ledger = SaveLedger()
    actual = ensure_item_vat(ui, source_item(), ledger, tmp_path, row_index=1)
    assert actual["verified"] is True and actual["selection_requested"] is True
    assert actual["before"]["percent"] == "7" and actual["after"]["percent"] == "19"
    assert len(native.dispatches) == 1 and native.dispatches[0][1] == "click"
    assert native.open_calls == 1 and native.cancels == 0 and native.resolve_calls == 0
    assert ledger.as_json() == []


def test_equal_selected_vat_is_confirmed_without_input_or_definition_resolution(monkeypatch, tmp_path):
    native = SelectionNative(selected=1)
    actual = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert actual["verified"] is True and actual["selection_requested"] is False
    assert not native.dispatches and native.open_calls == native.cancels == 0
    assert native.resolve_calls == 0


def test_correct_painted_vat_ignores_stale_empty_selection_and_tax_free_only_cache(monkeypatch, tmp_path):
    native = SelectionNative(wanted=False)
    native.actual_vat_display = "VAT 19% (19.0%)"
    native.table.attributes["AXSelectedRows"] = []
    actual = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert actual["verified"] is True and actual["selection_requested"] is False
    assert actual["before"]["evidence"]["method"] == "complete painted VAT cell"
    assert native.open_calls == native.resolve_calls == native.cancels == 0
    assert not native.dispatches


def test_different_painted_vat_can_select_unique_source_choice_with_no_popup_highlight(monkeypatch, tmp_path):
    native = SelectionNative()
    native.table.attributes["AXSelectedRows"] = []
    actual = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert actual["popup_highlight_indexes"] == []
    assert actual["before"]["percent"] == "7" and actual["after"]["percent"] == "19"
    assert actual["verified"] is True and len(native.dispatches) == 1


def test_popup_highlight_of_source_does_not_replace_different_painted_actual_vat(monkeypatch, tmp_path):
    native = SelectionNative(selected=1)
    native.actual_vat_display = "VAT 7% (7.0%)"
    actual = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert actual["popup_highlight_indexes"] == [1]
    assert actual["selection_requested"] is True and actual["verified"] is True
    assert actual["before"]["percent"] == "7" and actual["after"]["percent"] == "19"
    assert len(native.dispatches) == 1


def test_equal_percentage_under_custom_name_is_confirmed_without_popup_or_master_rewrite(monkeypatch, tmp_path):
    native = SelectionNative()
    text = native.rows[0].attributes["AXChildren"][0]
    text.control = AXControl("AXStaticText", None, "Another rate (19.0%)", text.control.bounds, True, text)
    native.actual_vat_display = text.control.value
    result = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert result["before"]["percent"] == result["after"]["percent"] == "19"
    assert result["before"]["name"] == result["after"]["name"] == "Another rate"
    assert result["verified"] is True and result["selection_requested"] is False
    assert not native.dispatches and native.open_calls == native.resolve_calls == native.cancels == 0


def test_disabled_native_action_targets_are_refused_without_dispatch(monkeypatch, tmp_path):
    native = SelectionNative()
    row = native.rows[1]
    row.control = AXControl("AXRow", None, None, row.control.bounds, False, row)
    text = row.attributes["AXChildren"][0]
    text.control = AXControl("AXStaticText", None, text.control.value, text.control.bounds, False, text)
    with pytest.raises(DesktopError, match="disabled"):
        ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert not native.dispatches


def test_missing_vat_is_resolved_once_before_fresh_source_selection(monkeypatch, tmp_path):
    native = SelectionNative(wanted=False)
    actual = ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert actual["verified"] is True and native.resolve_calls == 1
    assert native.open_calls == 2 and native.cancels == 1 and len(native.dispatches) == 1


def test_definition_still_missing_after_one_resolution_refuses_without_loop(monkeypatch, tmp_path):
    native = SelectionNative(wanted=False)
    ui = writer_ui(monkeypatch, native)
    def unresolved(*args):
        native.resolve_calls += 1
        return {"passed": True, "scope": "resolution double; popup remains stale"}
    monkeypatch.setattr("tjm.product_sequence.resolve_vat", unresolved)
    with pytest.raises(WorkflowReview) as failure:
        ensure_item_vat(ui, source_item(), SaveLedger(), tmp_path)
    assert failure.value.code == "vat_not_available"
    assert native.resolve_calls == 1 and native.open_calls == native.cancels == 2
    assert not native.dispatches


def test_recorded_tax_free_only_popup_never_invents_a_missing_source_vat(monkeypatch, tmp_path):
    recorded = real_shape_native()
    native = SelectionNative(wanted=False)
    native.popup, native.popup_window = recorded.popup, recorded.window
    native.table, native.rows = recorded.table, recorded.rows
    native.actual_vat_display = "VAT 7% (7.0%)"
    native.table.attributes["AXSelectedRows"] = []
    ui = writer_ui(monkeypatch, native)
    with pytest.raises(WorkflowReview) as failure:
        ensure_item_vat(ui, source_item(), SaveLedger(), tmp_path)
    assert failure.value.code == "vat_not_available"
    assert ui.actions[0]["before"]["display"] == "VAT 7% (7.0%)"
    assert native.resolve_calls == 1 and native.open_calls == native.cancels == 2
    assert not native.dispatches


@pytest.mark.parametrize("change,match", [
    (lambda n: setattr(n, "action_names", ["AXPress"]), "actionless native shape"),
    (lambda n: setattr(n, "context_valid", False), "source item/document context changed"),
    (lambda n: n.rows[1].attributes["AXChildren"][0].__setattr__("control", AXControl(
        "AXStaticText", None, "VAT 19% (7.0%)", n.rows[1].control.bounds, True,
        n.rows[1].attributes["AXChildren"][0])), "another displayed rate"),
    (lambda n: n.rows[0].attributes["AXChildren"][0].__setattr__("control", AXControl(
        "AXStaticText", None, "Unreadable", n.rows[0].control.bounds, True,
        n.rows[0].attributes["AXChildren"][0])), "complete native name"),
    (lambda n: n.table.attributes.update(AXRows=n.rows + [n.rows[1]]), "duplicate native row"),
])
def test_unsafe_or_unsupported_choice_never_dispatches_or_resolves(monkeypatch, tmp_path, change, match):
    native = SelectionNative()
    change(native)
    with pytest.raises((DesktopError, WorkflowReview), match=match):
        ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert not native.dispatches and native.resolve_calls == 0


@pytest.mark.parametrize("change", [
    lambda n: setattr(n, "context_valid", False),
    lambda n: n.table.attributes.update(AXSelectedRows=[n.rows[1]]),
    lambda n: n.rows[0].attributes["AXChildren"][0].__setattr__("control", AXControl(
        "AXStaticText", None, "VAT 7% (8.0%)", n.rows[0].control.bounds, True,
        n.rows[0].attributes["AXChildren"][0])),
    lambda n: n.rows[1].attributes["AXChildren"][0].__setattr__("control", AXControl(
        "AXStaticText", None, "VAT 19% (20.0%)", n.rows[1].control.bounds, True,
        n.rows[1].attributes["AXChildren"][0])),
])
def test_change_after_action_discovery_is_refused_before_dispatch(monkeypatch, tmp_path, change):
    native = SelectionNative()
    native.action_names_hook = lambda: change(native)
    with pytest.raises(DesktopError):
        ensure_item_vat(writer_ui(monkeypatch, native), source_item(), SaveLedger(), tmp_path)
    assert not native.dispatches


def test_uncertain_action_result_is_recorded_and_never_retried(monkeypatch, tmp_path):
    native = SelectionNative()
    native.action_error = -25204
    ui = writer_ui(monkeypatch, native)
    with pytest.raises(DesktopError, match="do not repeat"):
        ensure_item_vat(ui, source_item(), SaveLedger(), tmp_path)
    assert len(native.dispatches) == 1 and native.open_calls == 1
    assert ui.actions[0]["selection_requested"] is True and ui.actions[0]["verified"] is False


def test_wrong_selected_vat_after_reopen_stops_without_repeating_input(monkeypatch, tmp_path):
    native = SelectionNative()
    native.wrong_after = True
    ui = writer_ui(monkeypatch, native)
    with pytest.raises(WorkflowReview) as failure:
        ensure_item_vat(ui, source_item(), SaveLedger(), tmp_path)
    assert failure.value.code == "item_vat_mismatch"
    assert len(native.dispatches) == 1 and native.open_calls == 1 and native.cancels == 0
    assert ui.actions[0]["verified"] is False
