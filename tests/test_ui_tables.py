"""Table adapter guards with simulated captures/native controls; no GUI actions."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image, ImageDraw
import pytest

from tjm.desktop import AXControl, Capture, DesktopError, Rect
from tjm.fields import find_phrases
from tjm.ocr import Token
from tjm import ui_tables as module
from tjm.ui_tables import observe_table, read_complete_table, rowselect


HEADERS = ["SKU", "Name"]
CUSTOMER_HEADERS = ["No.", "First Name", "Name", "Company", "ZIP", "City"]
LABELS = {"SKU": (80, 100, 140, 124), "Name": (350, 100, 420, 124)}
TABLE_BOX = (60, 90, 720, 490)
SCOPE = (40, 60, 740, 540)


class Native:
    def __init__(self):
        self.element = {"pid": 123, "role": "AXScrollArea", "bounds": Rect(130, 245, 330, 200),
                        "enabled": None}
        self.names = ["AXRole", "AXPosition", "AXSize", "AXVerticalScrollBar", "AXHorizontalScrollBar"]
        self.names_code = 0
        self.attributes = {"AXVerticalScrollBar": (0, None), "AXHorizontalScrollBar": (-25212, None)}
        self.clicked, self.guarded, self.hits = [], [], []
        self.hit_element = None
        self.cf = NS(CFEqual=lambda a, b: a is b)
        self.ax = NS(kAXErrorSuccess=0, kAXErrorNoValue=-25212,
                     AXUIElementCopyAttributeNames=lambda e, ignored: (self.names_code, self.names),
                     AXUIElementCopyAttributeValue=lambda e, name, ignored: self.attributes[name])

    def _control(self, element):
        if element["pid"] != 123:
            raise DesktopError("Native table is not owned by the target process.")
        return AXControl(element["role"], None, None, element["bounds"], element["enabled"], element)

    def _read(self, element, attribute, *, required=False):
        value = element.get(attribute)
        if required and value is None:
            raise DesktopError("Required native parent attribute is missing.")
        return value

    def hit_test(self, capture, x, y, *, expected_role=None):
        self.hits.append((x, y))
        control = self._control(self.hit_element or self.element)
        if expected_role is not None and expected_role != control.role:
            raise DesktopError("Unexpected role at current table point.")
        if not control.bounds.contains(x, y):
            raise DesktopError("Native table does not contain current hit point.")
        return control

    def guard(self, capture):
        self.guarded.append(capture)

    def click(self, capture, x, y, *, clicks=1):
        self.clicked.append((capture, x, y, clicks))


class Form:
    def __init__(self, tmp_path):
        self.desktop = Native()
        self.evidence_dir = tmp_path
        self.actions = []
        self.labels = dict(LABELS)
        self.window_element = object()
        self.image = Image.new("RGB", (800, 600), "white")
        ImageDraw.Draw(self.image).text((90, 160), "SKU-1 Cabinet", fill="black")
        self.captures = []
        self.snapshot_calls = []
        self.query_value = "SKU-1"
        self.query_reads = []
        self.read_calls = []
        self.on_read = None

    def capture(self):
        return Capture(self.image.copy(), "Fakturama", Rect(100, 200, 400, 300),
                       Rect(100, 200, 400, 300), 2, 2, 0, 123,
                       (123, "Fakturama.ID", "/test/Fakturama2.app", 1.0), 1, self.window_element)

    def snapshot(self, label, *, ocr=True):
        self.snapshot_calls.append((label, ocr))
        return self.captures.pop(0) if self.captures else self.capture()

    def scope(self, capture, scope):
        return SCOPE if scope == "Data" else scope

    def label(self, capture, name, *, scope):
        if name not in self.labels:
            raise DesktopError("Missing current header.")
        return self.labels[name]

    def field(self, capture, name, *, scope):
        self.query_reads.append((capture, name, scope, self.query_value))
        return AXControl("AXTextField", None, self.query_value, Rect(140, 230, 100, 15), True, {})

    def read(self, image, scope, headers, evidence_dir, *, observed_table_box):
        self.read_calls.append({"scope": scope, "native_box": observed_table_box, "headers": headers})
        if self.on_read:
            self.on_read(len(self.read_calls))
        return {"headers": [{"text": "SKU", "box": [60, 90, 220, 140]},
                            {"text": "Name", "box": [220, 90, 720, 140]}],
                "rows": [{"values": {"SKU": "SKU-1", "Name": "Cabinet"},
                          "row_box": [60, 140, 720, 190],
                          "cell_boxes": {"SKU": [62, 142, 218, 188], "Name": [222, 142, 718, 188]}}],
                "table_box": list(TABLE_BOX), "evidence": {"report": "mock-cell-readback.json"}}


@pytest.fixture
def form(tmp_path, monkeypatch):
    ui = Form(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda amount: clock.__setitem__(0, clock[0] + amount))
    monkeypatch.setattr(module, "read_table", ui.read)
    ui.clock = clock
    return ui


def complete(form, **kwargs):
    return read_complete_table(form, HEADERS, "Data", search_field="Search:", search_value="SKU-1", **kwargs)


def customer_header_tokens(offset=(0, 0)):
    x, y = offset
    values = [("No.", 70, 20), ("First", 140, 50), ("Name", 198, 52),
              ("Name", 350, 70), ("Company", 500, 100), ("ZIP", 620, 30), ("City", 670, 40)]
    return [Token(text, left + x, 100 + y, width, 24, 99) for text, left, width in values]


def install_phrase_labels(form, tokens):
    original_capture = form.capture
    form.capture = lambda: replace(original_capture(), _tokens={11: tokens})
    form.discoveries = []

    def label(capture, name, *, scope):
        scoped = [token for token in capture.tokens()
                  if scope[0] <= token.cx < scope[2] and scope[1] <= token.cy < scope[3]]
        boxes = find_phrases(scoped, name)
        if len(boxes) != 1:
            raise DesktopError(f"Expected one visible {name!r} label in its current scope; found {len(boxes)}.")
        return boxes[0]

    form.label = label


@pytest.mark.parametrize("offset", [(0, 0), (25, 45)])
def test_name_header_excludes_only_the_name_inside_proven_first_name_header(form, offset):
    install_phrase_labels(form, customer_header_tokens(offset))
    x, y = offset
    form.desktop.element["bounds"] = Rect(130 + x / 2, 245 + y / 2, 330, 200)
    scope = tuple(value + (x if index % 2 == 0 else y) for index, value in enumerate(SCOPE))
    result = observe_table(form, CUSTOMER_HEADERS, scope)
    assert result.header_boxes["First Name"] == (140 + x, 100 + y, 250 + x, 124 + y)
    assert result.header_boxes["Name"] == (350 + x, 100 + y, 420 + x, 124 + y)
    assert form.discoveries[-1]["removed_candidates"] == [[198 + x, 100 + y, 250 + x, 124 + y]]
    assert not form.desktop.clicked


def test_duplicate_name_on_header_baseline_still_requires_review(form):
    install_phrase_labels(form, [*customer_header_tokens(), Token("Name", 270, 100, 55, 24, 99)])
    with pytest.raises(DesktopError, match="remains ambiguous"):
        observe_table(form, CUSTOMER_HEADERS, "Data")
    assert not form.desktop.hits
    assert not form.desktop.clicked


def test_duplicate_longer_header_cannot_justify_removing_short_name_candidates(form):
    extra = [Token("First", 270, 100, 50, 24, 99), Token("Name", 328, 100, 52, 24, 99)]
    install_phrase_labels(form, [*customer_header_tokens(), *extra])
    with pytest.raises(DesktopError, match="First Name.*found 2"):
        observe_table(form, CUSTOMER_HEADERS, "Data")
    assert not form.desktop.hits
    assert not form.desktop.clicked


@pytest.mark.parametrize("extra", [[Token("Name", 350, 170, 55, 24, 99)],
                                  [Token("First", 270, 170, 50, 24, 99),
                                   Token("Name", 328, 170, 52, 24, 99)]])
def test_body_name_phrases_are_excluded_by_other_proven_header_anchors(form, extra):
    install_phrase_labels(form, [*customer_header_tokens(), *extra])
    result = observe_table(form, CUSTOMER_HEADERS, "Data")
    assert result.header_boxes["Name"] == (350, 100, 420, 124)
    assert result.header_boxes["First Name"] == (140, 100, 250, 124)
    assert any(d["method"] == "coherent requested header baseline" for d in form.discoveries)
    assert not form.desktop.clicked


def test_column_fitting_uses_requested_header_containment_before_any_drag(form, monkeypatch):
    from tjm import ocr, ui_table

    tokens = customer_header_tokens()
    install_phrase_labels(form, tokens)
    monkeypatch.setattr(ocr, "ocr_tokens", lambda image, psm: tokens)
    # Name is already at the width calculated for its observed font. No input is
    # necessary; discovery must still distinguish it from First Name.
    monkeypatch.setattr(ui_table, "_header_geometry", lambda image, observed, headers: {
        "font": 20, "top": 90, "bottom": 130, "columns": [(0, 305), (305, 465), (465, 700)]})
    module.fit_columns(form, {"Name": "Klein"}, scope=(0, 0, 800, 600), headers=CUSTOMER_HEADERS)
    assert not form.desktop.clicked
    assert not form.actions
    assert form.discoveries[-1]["pixel_box"] == [350, 100, 420, 124]


def test_populated_vat_row_is_not_a_second_header_during_observation(form):
    tokens = [Token("SKU", 80, 100, 60, 24, 99), Token("Name", 350, 100, 70, 24, 99),
              Token("VAT", 600, 100, 40, 24, 99), Token("VAT", 600, 170, 40, 24, 99),
              Token("19%", 650, 170, 40, 24, 99)]
    install_phrase_labels(form, tokens)
    result = observe_table(form, ["SKU", "Name", "VAT"], "Data")
    assert result.header_boxes["VAT"] == (600, 100, 640, 124)
    assert not form.desktop.clicked


def test_duplicate_vat_on_proven_header_baseline_still_rejects(form):
    tokens = [Token("SKU", 80, 100, 60, 24, 99), Token("Name", 350, 100, 70, 24, 99),
              Token("VAT", 600, 100, 40, 24, 99), Token("VAT", 650, 100, 40, 24, 99)]
    install_phrase_labels(form, tokens)
    with pytest.raises(DesktopError, match="VAT.*ambiguous"):
        observe_table(form, ["SKU", "Name", "VAT"], "Data")
    assert not form.desktop.hits
    assert not form.desktop.clicked


def test_column_fitting_limits_vat_label_to_current_geometric_header_band(form, monkeypatch):
    from tjm import ocr, ui_table
    tokens = [Token("SKU", 80, 100, 60, 24, 99), Token("Name", 350, 100, 70, 24, 99),
              Token("VAT", 600, 100, 40, 24, 99), Token("VAT", 600, 170, 40, 24, 99),
              Token("19%", 650, 170, 40, 24, 99)]
    install_phrase_labels(form, tokens)
    monkeypatch.setattr(ocr, "ocr_tokens", lambda image, psm: tokens)
    monkeypatch.setattr(ui_table, "_header_geometry", lambda image, observed, headers: {
        "font": 20, "top": 90, "bottom": 130, "columns": [(0, 250), (250, 500), (500, 692)]})
    module.fit_columns(form, {"VAT": "VAT 19%"}, scope=(0, 0, 800, 600), headers=["SKU", "Name", "VAT"])
    assert not form.desktop.clicked
    assert not form.actions


def test_actual_populated_product_vat_text_is_disambiguated_by_its_header_baseline(form):
    from tjm.ocr import ocr_tokens
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/gui/product-vat-header.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    install_phrase_labels(form, ocr_tokens(image, psm=11))
    headers = ["Item No.", "Name", "Description", "Stock", "Price", "VAT"]
    boxes = module._header_boxes(form, form.capture(), headers, (550, 1090, 2704, 1686))
    assert boxes["VAT"] == (1365, 1174, 1404, 1190)
    assert any(d["method"] == "coherent requested header baseline" for d in form.discoveries)
    assert not form.desktop.hits
    assert not form.desktop.clicked


def test_actual_order_grid_dark_header_labels_use_only_proven_contrast_band(tmp_path):
    from tjm.ocr import ocr_tokens
    from tjm.ui import Ui
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/gui/dark-order-grid.png"
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    image = Image.open(path)
    tokens = ocr_tokens(image, psm=11)
    capture = NS(image=image, tokens=lambda: tokens)
    ui = Ui(None, "pure saved observation", tmp_path)
    headers = ["Qty.", "Item No.", "Name", "Description", "VAT", "U.Price", "Discount", "Price"]
    boxes = module._header_boxes(ui, capture, headers, (550, 596, 2704, 773))
    assert set(boxes) == set(headers)
    assert boxes["Item No."] == (856, 606, 940, 622)
    assert boxes["VAT"] == (1455, 606, 1494, 622)
    assert boxes["U.Price"] == (1606, 601, 1679, 630)
    proof = [entry for entry in ui.discoveries if entry["method"] == "observed neutral dark header contrast"]
    assert len(proof) == 1
    assert proof[0]["scope"] == [550, 601, 2704, 630]
    assert Path(proof[0]["source_crop"]).is_file()
    assert Path(proof[0]["prepared_crop"]).is_file()
    assert proof[0]["tokens"]


def test_native_table_bounds_follow_current_header_and_capture_scale(form):
    result = observe_table(form, HEADERS, "Data")
    assert result.table_box == TABLE_BOX
    assert result.scope == SCOPE
    assert result.area.element is form.desktop.element
    assert form.desktop.hits == [(225.0, 274.0)]
    assert result.native_evidence["scrollbars"]["AXHorizontalScrollBar"] == {"code": -25212, "value": None}
    assert not form.desktop.clicked


def test_raw_header_native_containment_failure_rereads_cells_without_expanding_table(form, monkeypatch):
    raw = {name: (box[0], 80, box[2], box[3]) for name, box in LABELS.items()}
    form.labels = raw
    calls = []

    def reread(ui, capture, headers, table_box, raw_boxes):
        calls.append((headers, table_box, raw_boxes))
        return dict(LABELS)

    monkeypatch.setattr(module, "_native_header_cell_boxes", reread)
    result = observe_table(form, HEADERS, "Data")
    assert calls == [(HEADERS, TABLE_BOX, raw)]
    assert result.table_box == TABLE_BOX
    assert result.header_boxes == LABELS
    assert result.native_evidence["no_vertical_scrollbar"]
    assert result.native_evidence["no_horizontal_scrollbar"]
    assert not form.desktop.clicked


def test_contained_raw_headers_do_not_trigger_cell_reread(form, monkeypatch):
    def unexpected(*args):
        pytest.fail("Contained raw headers must not trigger another reading method.")

    monkeypatch.setattr(module, "_native_header_cell_boxes", unexpected)
    assert observe_table(form, HEADERS, "Data").header_boxes == LABELS


@pytest.mark.parametrize("bad_boxes", [
    {"SKU": LABELS["SKU"], "Name": (350, 170, 420, 194)},
    {"SKU": (80, 100, 400, 124), "Name": LABELS["Name"]},
])
def test_native_header_reread_still_requires_one_baseline_and_nonoverlapping_labels(form, monkeypatch, bad_boxes):
    form.labels = {name: (box[0], 80, box[2], box[3]) for name, box in LABELS.items()}
    monkeypatch.setattr(module, "_native_header_cell_boxes", lambda *args: bad_boxes)
    with pytest.raises(DesktopError, match="Reread table header"):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_native_header_reread_does_not_waive_absent_scrollbar_requirement(form, monkeypatch):
    form.labels = {name: (box[0], 80, box[2], box[3]) for name, box in LABELS.items()}
    monkeypatch.setattr(module, "_native_header_cell_boxes", lambda *args: dict(LABELS))
    form.desktop.attributes["AXVerticalScrollBar"] = (0, object())
    with pytest.raises(DesktopError, match="AXVerticalScrollBar"):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_actual_vat_header_rule_absorption_is_reread_from_native_column_cells(tmp_path):
    path = (Path(__file__).resolve().parents[1]
            / "tests/fixtures/gui/vat-header-rules.png")
    if not path.is_file():
        pytest.skip("Local observed desktop trace is not in this checkout.")
    ui = NS(evidence_dir=tmp_path, discoveries=[])
    raw = {"Name": (821, 1228, 894, 1274), "Description": (993, 1228, 1117, 1274),
           "Value": (1154, 1228, 1302, 1274)}
    # The recorded preceding VAT read establishes this native boundary. This
    # offline regression is a pixel reader check, not a fresh desktop proof.
    table_box = (664, 1232, 2992, 1792)
    boxes = module._native_header_cell_boxes(ui, NS(image=Image.open(path)),
                                             ["Name", "Description", "Value"], table_box, raw)
    assert boxes == {"Name": (834, 1238, 891, 1254),
                     "Description": (994, 1237, 1111, 1258), "Value": (1153, 1237, 1208, 1254)}
    proof = ui.discoveries[-1]
    assert proof["status"] == "read"
    assert proof["table_box"] == list(table_box)
    assert proof["raw_boxes"] == raw
    assert Path(proof["report"]).is_file()
    required = [record for record in proof["headers"]
                if record.get("raw_text") in {"Name", "Description", "Value"}]
    assert len(required) == 3
    assert all(token["confidence"] >= 70 for record in required for token in record["tokens"])


@pytest.mark.parametrize("labels", [["SKU", "SKU", "Name"], ["SKU", None, "Other"]])
def test_native_header_cell_reread_rejects_duplicates_or_unreadable_required_labels(tmp_path, monkeypatch, labels):
    from tjm import ui_table

    ui = NS(evidence_dir=tmp_path, discoveries=[])
    monkeypatch.setattr(ui_table, "_read_tokens", lambda *args, **kwargs: [])
    monkeypatch.setattr(ui_table, "_header_geometry", lambda *args: {
        "top": 2, "bottom": 30, "font": 16, "columns": [(0, 100), (100, 200), (200, 300)]})
    reads = iter(labels)

    def cell(crop, box, path, *, header):
        text = next(reads)
        if text is None:
            raise DesktopError("Low-confidence table header.")
        return text, {"box": box, "raw_text": text,
                      "tokens": [{"box": [18, 18, 75, 57], "confidence": 99}]}

    monkeypatch.setattr(ui_table, "_cell", cell)
    with pytest.raises(DesktopError, match="more than once|missing, clipped or unreadable"):
        module._native_header_cell_boxes(ui, NS(image=Image.new("RGB", (400, 200))),
                                         HEADERS, (20, 20, 320, 180), {})
    assert ui.discoveries[-1]["status"] == "review_required"
    assert Path(ui.discoveries[-1]["report"]).is_file()


def composite_wrapper(native, parent=None):
    wrapper = {"pid": 123, "role": "SWTComposite", "bounds": native.element["bounds"],
               "enabled": None, "AXParent": native.element if parent is None else parent}
    native.hit_element = wrapper
    return wrapper


def test_owned_same_bounds_swt_wrapper_resolves_only_its_immediate_table_parent(form):
    outer = {"pid": 123, "role": "AXScrollArea", "bounds": Rect(120, 225, 350, 230), "enabled": None}
    form.desktop.element["AXParent"] = outer
    composite_wrapper(form.desktop)
    result = observe_table(form, HEADERS, "Data")
    assert result.area.element is form.desktop.element
    assert result.area.element is not outer
    assert result.table_box == TABLE_BOX
    assert result.native_evidence["discovery"] == {"hit_role": "SWTComposite", "parent_role": "AXScrollArea",
                                                   "parent_hops": 1, "identical_bounds": True}
    assert not form.desktop.clicked


@pytest.mark.parametrize("parent_change", [
    {"bounds": Rect(120, 225, 350, 230)},
    {"role": "AXGroup"},
    {"role": "SWTComposite"},
    {"pid": 999},
    {"enabled": False},
])
def test_swt_wrapper_larger_wrong_role_foreign_or_disabled_parent_is_refused(form, parent_change):
    parent = dict(form.desktop.element, **parent_change)
    composite_wrapper(form.desktop, parent)
    with pytest.raises(DesktopError):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_swt_wrapper_without_immediate_parent_is_not_resolved_by_climbing(form):
    wrapper = composite_wrapper(form.desktop)
    del wrapper["AXParent"]
    with pytest.raises(DesktopError, match="parent attribute"):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_swt_wrapper_parent_missing_scroll_attributes_is_refused(form):
    composite_wrapper(form.desktop)
    form.desktop.names = ["AXVerticalScrollBar"]
    with pytest.raises(DesktopError, match="both scrollbar attributes"):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_row_selection_resolves_same_bounds_swt_wrapper_to_exact_observed_parent(form):
    composite_wrapper(form.desktop)
    result = complete(form)
    assert all(entry["initial_native"]["discovery"]["parent_hops"] == 1
               for entry in result.evidence["observations"])
    rowselect(form, result, result.rows[0])
    assert len(form.desktop.clicked) == 1
    assert form.actions[-1]["hit_resolution"]["parent_hops"] == 1
    assert form.actions[-1]["selection_verified"] is False


def test_row_selection_same_bounds_wrapper_with_different_parent_identity_is_refused(form):
    result = complete(form)
    composite_wrapper(form.desktop, dict(form.desktop.element))
    with pytest.raises(DesktopError, match="exact observed native table"):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


@pytest.mark.parametrize("attribute", ["AXVerticalScrollBar", "AXHorizontalScrollBar"])
def test_present_scrollbar_never_establishes_complete_view(form, attribute):
    form.desktop.attributes[attribute] = (0, object())
    with pytest.raises(DesktopError, match=attribute):
        complete(form)
    assert not form.read_calls
    assert not form.desktop.clicked


@pytest.mark.parametrize("change", [
    lambda native: setattr(native, "names_code", -25204),
    lambda native: setattr(native, "names", ["AXVerticalScrollBar"]),
    lambda native: native.attributes.update(AXVerticalScrollBar=(-25205, None)),
])
def test_unknown_native_scroll_attributes_stop_before_ocr(form, change):
    change(form.desktop)
    with pytest.raises(DesktopError):
        complete(form)
    assert not form.read_calls
    assert not form.desktop.clicked


@pytest.mark.parametrize("change", [
    lambda ui: ui.desktop.element.update(pid=999),
    lambda ui: ui.desktop.element.update(role="AXGroup"),
    lambda ui: ui.desktop.element.update(bounds=Rect(110, 240, 370, 250)),
    lambda ui: ui.desktop.element.update(bounds=Rect(130, 215, 330, 230)),
    lambda ui: ui.labels.update(Name=(350, 170, 420, 194)),
])
def test_owned_inner_table_and_one_header_baseline_are_required(form, change):
    change(form)
    with pytest.raises(DesktopError):
        observe_table(form, HEADERS, "Data")
    assert not form.desktop.clicked


def test_two_equal_reads_record_native_query_and_cell_evidence(form):
    result = complete(form)
    assert len(form.read_calls) == 2
    assert all(call["native_box"] == TABLE_BOX for call in form.read_calls)
    assert len(form.query_reads) == 4  # both before and after each OCR read
    assert result.rows[0]["values"] == {"SKU": "SKU-1", "Name": "Cabinet"}
    evidence = json.loads((form.evidence_dir / result.evidence["report"]).read_text())
    assert evidence["two_equal_observations"] is True
    assert all(item["native"]["no_vertical_scrollbar"] and item["query"]["passed"]
               for item in evidence["observations"])
    assert result.readback["evidence"]["native_completeness_report"] == result.evidence["report"]
    assert not form.desktop.clicked


def test_one_requested_identity_header_still_requires_complete_native_query_and_stable_reads(form):
    del form.labels["Name"]  # Optional Name need not be readable to match an exact SKU.
    result = read_complete_table(form, ["SKU"], "Data", search_field="Search:", search_value="SKU-1")
    assert result.observation.header_boxes == {"SKU": LABELS["SKU"]}
    assert len(form.read_calls) == 2
    assert all(call["headers"] == ["SKU"] and call["native_box"] == TABLE_BOX for call in form.read_calls)
    assert len(form.query_reads) == 4
    assert result.evidence["two_equal_observations"] is True
    assert result.evidence["complete_native_view"] is True
    assert not form.desktop.clicked


def test_single_identity_header_ambiguity_cannot_be_resolved_from_one_anchor(form):
    install_phrase_labels(form, [Token("SKU", 80, 100, 60, 24, 99),
                                Token("SKU", 350, 100, 60, 24, 99)])
    with pytest.raises(DesktopError, match="found 2"):
        observe_table(form, ["SKU"], "Data")
    assert not form.desktop.hits
    assert not form.desktop.clicked


def test_single_identity_header_does_not_waive_hidden_scrollbar_guard(form):
    form.desktop.attributes["AXVerticalScrollBar"] = (0, object())
    with pytest.raises(DesktopError, match="AXVerticalScrollBar"):
        read_complete_table(form, ["SKU"], "Data", search_field="Search:", search_value="SKU-1")
    assert not form.read_calls
    assert not form.desktop.clicked


def test_changed_row_values_need_another_matching_observation(form, monkeypatch):
    original_read = form.read
    def changes(*args, **kwargs):
        result = original_read(*args, **kwargs)
        result["rows"][0]["values"]["SKU"] = "old" if len(form.read_calls) == 1 else "SKU-1"
        return result
    monkeypatch.setattr(module, "read_table", changes)
    result = complete(form)
    assert len(form.read_calls) == 3
    assert result.rows[0]["values"]["SKU"] == "SKU-1"


def test_query_change_during_ocr_stops_without_accepting_stale_absence(form):
    def changes(index):
        if index == 2:
            form.query_value = "another query"
    form.on_read = changes
    with pytest.raises(DesktopError, match="requested query") as raised:
        complete(form)
    evidence = json.loads((form.evidence_dir / raised.value.table_adapter_evidence_path).read_text())
    assert evidence["status"] == "review_required"
    assert evidence["complete_native_view"] is False
    assert len(evidence["observations"]) == 2
    assert evidence["observations"][-1]["post_read_guard_passed"] is False
    assert evidence["observations"][-1]["readback_report"] == "mock-cell-readback.json"
    assert not form.desktop.clicked


def test_table_pixels_change_during_ocr_requires_reread(form):
    form.on_read = lambda index: ImageDraw.Draw(form.image).point((100, 150), fill="red")
    with pytest.raises(DesktopError, match="pixels changed"):
        complete(form)
    assert not form.desktop.clicked


def test_scrollbar_appearing_during_ocr_is_not_accepted(form):
    form.on_read = lambda index: form.desktop.attributes.update(AXVerticalScrollBar=(0, object()))
    with pytest.raises(DesktopError, match="AXVerticalScrollBar"):
        complete(form)
    assert not form.desktop.clicked


def test_ocr_exceeding_readiness_budget_does_not_accept_result(form):
    form.on_read = lambda index: form.clock.__setitem__(0, 9.0)
    with pytest.raises(DesktopError, match="deadline"):
        complete(form)
    assert not form.desktop.clicked


@pytest.mark.parametrize("clicks", [1, 2])
def test_exact_observed_row_dispatches_once_using_fresh_scaled_coordinates(form, clicks):
    result = complete(form)
    rowselect(form, result, result.rows[0], clicks=clicks)
    assert len(form.desktop.clicked) == 1
    capture, x, y, observed_clicks = form.desktop.clicked[0]
    assert (x, y, observed_clicks) == (170.0, 282.5, clicks)
    assert capture is not result.observation.capture
    assert form.actions[-1]["selection_verified"] is False


def test_copy_of_a_row_is_not_a_row_from_this_observation(form):
    result = complete(form)
    with pytest.raises(DesktopError, match="exact row object"):
        rowselect(form, result, deepcopy(result.rows[0]))
    assert not form.desktop.clicked


def test_modified_row_coordinates_are_rejected_before_input(form):
    result = complete(form)
    result.rows[0]["row_box"][1] += 10
    with pytest.raises(DesktopError, match="modified after observation"):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


@pytest.mark.parametrize("change", [
    lambda ui: ImageDraw.Draw(ui.image).point((100, 150), fill="red"),
    lambda ui: ui.desktop.element.update(enabled=False),
    lambda ui: ui.desktop.element.update(bounds=Rect(130, 245, 320, 200)),
    lambda ui: ui.desktop.attributes.update(AXHorizontalScrollBar=(0, object())),
    lambda ui: setattr(ui, "query_value", "another query"),
])
def test_changed_pixels_native_state_or_query_stop_before_row_click(form, change):
    result = complete(form)
    change(form)
    with pytest.raises(DesktopError):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


@pytest.mark.parametrize("change", [
    lambda capture: replace(capture, pid=999),
    lambda capture: replace(capture, process_identity=(123, "other", "/other", 1.0)),
    lambda capture: replace(capture, window_title="another window"),
    lambda capture: replace(capture, window_element=object()),
])
def test_table_readback_cannot_be_used_in_another_process_or_window(form, change):
    result = complete(form)
    form.captures.append(change(form.capture()))
    with pytest.raises(DesktopError, match="another process or window"):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


def test_same_geometry_different_hit_table_is_rejected(form, monkeypatch):
    result = complete(form)
    original_hit = form.desktop.hit_test
    def lookalike(*args, **kwargs):
        control = original_hit(*args, **kwargs)
        return replace(control, element=dict(control.element))
    monkeypatch.setattr(form.desktop, "hit_test", lookalike)
    with pytest.raises(DesktopError, match="exact observed native table"):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


def test_table_becoming_disabled_at_final_row_hit_does_not_dispatch(form, monkeypatch):
    result = complete(form)
    original_hit = form.desktop.hit_test
    def disabled_hit(*args, **kwargs):
        return replace(original_hit(*args, **kwargs), enabled=False)
    monkeypatch.setattr(form.desktop, "hit_test", disabled_hit)
    with pytest.raises(DesktopError):
        rowselect(form, result, result.rows[0])
    assert not form.desktop.clicked


@pytest.mark.parametrize("kwargs", [{"search_field": "Search:"}, {"search_value": "SKU-1"},
                                     {"timeout": 0}, {"interval": 0}, {"timeout": 61}])
def test_invalid_query_or_polling_arguments_do_not_observe_ui(form, kwargs):
    with pytest.raises(ValueError):
        read_complete_table(form, HEADERS, "Data", **kwargs)
    assert not form.snapshot_calls


@pytest.mark.parametrize("clicks", [0, 3, True])
def test_invalid_click_count_does_not_dispatch(form, clicks):
    result = complete(form)
    with pytest.raises(ValueError):
        rowselect(form, result, result.rows[0], clicks=clicks)
    assert not form.desktop.clicked
