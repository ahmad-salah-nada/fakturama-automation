"""Actual SKU/open/close/target/input sequence with a controlled native boundary."""

from types import SimpleNamespace as N

import pytest
from PIL import Image, ImageDraw

import tjm.native_items as items
from tjm.desktop import AXControl, DesktopError, Rect


class NativeRows:
    """Only native observation/input is simulated; all item helpers are real."""

    def __init__(self, change_after_escape=None):
        self.rows = [{"sku": "SKU-A", "Qty.": "1"}, {"sku": "SKU-B", "Qty.": "2"}]
        self.cf = N(CFEqual=lambda a, b: a is b)
        self.identity = (1, "app", "path", 1)
        self.window, self.table_element = object(), object()
        self.table = AXControl("AXScrollArea", None, None, Rect(0, 0, 200, 100), True,
                               self.table_element)
        self.current, self.opened_header = None, None
        self.inputs, self.escapes, self.document = [], 0, "PO-1"
        self.change_after_escape = change_after_escape

    def capture(self, header):
        image = Image.new("RGB", (200, 100), "white")
        draw = ImageDraw.Draw(image)
        for index, row in enumerate(self.rows):
            draw.text((50, 20 + index * 25), row["sku"], fill="black")
            draw.text((5, 20 + index * 25), row["Qty."], fill="black")
        return N(header=header, image=image, process_identity=self.identity, window_title="Main",
                 window_element=self.window, window_bounds=Rect(0, 0, 200, 100),
                 screen_rect=Rect(0, 0, 200, 100), metadata=lambda: {"pid": 1})

    def guard(self, capture):
        return True

    def double_click(self, capture, *point):
        self.opened_header = capture.header
        self.current = {"role": "AXTextField", "row": self.rows[0], "header": capture.header,
                        "bounds": Rect(20, 20, 40, 20), "focused": True, "pending": None}
        self.inputs.append(("double_click", self.current["row"]["sku"], capture.header))

    def hit_test(self, capture, *point, expected_role=None):
        return self._control(self.current)

    def _control(self, element):
        if element is self.table_element:
            return self.table
        value = element["pending"]
        if value is None:
            value = element["row"]["sku" if element["header"] == "Item No." else element["header"]]
        return AXControl(element["role"], None, value, element["bounds"], True, element)

    @staticmethod
    def _read(element, attribute, **kwargs):
        if attribute == "AXFocused":
            return element["focused"]

    def key(self, capture, key, *, focus_guard):
        if not focus_guard():
            raise DesktopError("Input context changed.")
        self.inputs.append((key, self.current["row"]["sku"], self.current["header"]))
        if key == "escape":
            self.current["focused"] = False
            self.escapes += 1
            if self.change_after_escape is not None:
                self.change_after_escape(self, self.escapes)
        elif key == "tab":
            self.current["row"][self.current["header"]] = self.current["pending"]
            self.current["focused"] = False

    def enter(self, value, guard):
        if not guard():
            raise DesktopError("Input context changed.")
        self.current["pending"] = value
        self.inputs.append(("entered", self.current["row"]["sku"], value))


@pytest.fixture
def sequence(monkeypatch):
    def make(change=None):
        native = NativeRows(change)
        document = native.document
        ui = N(desktop=native, actions=[], text_input="unicode",
               require_inside_scope=lambda *args: None,
               snapshot=lambda *args, **kwargs: native.capture(native.opened_header),
               enter_text=lambda capture, value, focus_guard: native.enter(value, focus_guard))

        def observation(ui, header, title, row_index):
            return items.ItemObservation(native.capture(header), native.table, (0, 0, 200, 100),
                                         (0, 0, 200, 100), {}, (20, 40), (30, 30), "capture.png")

        monkeypatch.setattr(items, "_observe_item", observation)
        monkeypatch.setattr(items, "_vat_source", lambda *args: (
            lambda: native.document == document, {"document_number": document, "reference": "REF"}))
        return native, ui

    return make


def edit(ui):
    return items.set_item_cell(ui, header="Qty.", expected_sku="SKU-A", value="9", expected_reopened="9")


@pytest.mark.parametrize("escape_number", [1, 2])
def test_actual_item_sequence_refuses_reordered_row_before_any_text_or_tab(sequence, escape_number):
    def reorder(native, count):
        if count == escape_number:
            native.rows.reverse()

    native, ui = sequence(reorder)
    with pytest.raises(DesktopError, match="native SKU changed|source item row changed"):
        edit(ui)
    assert native.rows == [{"sku": "SKU-B", "Qty.": "2"}, {"sku": "SKU-A", "Qty.": "1"}]
    assert not any(action[0] in {"cmd+a", "entered", "tab"} for action in native.inputs)
    assert not any(action[0] == "double_click" and action[2] == "Qty." for action in native.inputs)


def test_same_native_sku_with_changed_other_row_content_is_not_the_same_occurrence(sequence):
    def changed(native, count):
        if count == 2:
            native.rows[0]["Qty."] = "8"

    native, ui = sequence(changed)
    with pytest.raises(DesktopError, match="source item row changed"):
        edit(ui)
    assert native.rows[0] == {"sku": "SKU-A", "Qty.": "8"}
    assert not any(action[0] in {"cmd+a", "entered", "tab"} for action in native.inputs)


@pytest.mark.parametrize("escape_number", [1, 2])
def test_source_document_change_refuses_item_input(sequence, escape_number):
    def changed(native, count):
        if count == escape_number:
            native.document = "PO-2"

    native, ui = sequence(changed)
    with pytest.raises(DesktopError, match="source document changed"):
        edit(ui)
    assert not any(action[0] in {"cmd+a", "entered", "tab"} for action in native.inputs)
    assert native.rows[0]["Qty."] == "1"


def test_actual_item_sequence_updates_only_exact_unchanged_source_occurrence(sequence):
    native, ui = sequence()
    record = edit(ui)
    assert native.rows == [{"sku": "SKU-A", "Qty.": "9"}, {"sku": "SKU-B", "Qty.": "2"}]
    assert [action for action in native.inputs if action[0] == "entered"] == [("entered", "SKU-A", "9")]
    assert [action for action in native.inputs if action[0] == "tab"] == [("tab", "SKU-A", "Qty.")]
    assert record["committed_value_verified"] is True
    assert record["observed_reopened"] == "9"
    assert not any("save" in action[0].lower() for action in native.inputs)


def test_source_document_changed_after_target_open_is_refused_by_input_guard(sequence):
    native, ui = sequence()
    original_key = native.key

    def changed_before_input(capture, key, *, focus_guard):
        if key == "cmd+a":
            native.document = "PO-2"
        return original_key(capture, key, focus_guard=focus_guard)

    native.key = changed_before_input
    with pytest.raises(DesktopError, match="Input context changed"):
        edit(ui)
    assert native.rows[0]["Qty."] == "1"
    assert not any(action[0] in {"cmd+a", "entered", "tab"} for action in native.inputs)
