import json
from types import SimpleNamespace

import pytest

from tjm import document_cleanup
from tjm.desktop import AXControl, DesktopError, Rect


class Node:
    def __init__(self, role, title=None, value=None, *, bounds=None, enabled=True, children=()):
        self.role, self.title, self.value = role, title, value
        self.bounds = bounds or Rect(0, 0, 800, 600)
        self.enabled, self.children, self.focused = enabled, list(children), False


class NativeDesktop:
    def __init__(self, *, dirty=True, checked=1):
        self.pid = 99
        self.cf = SimpleNamespace(CFEqual=lambda first, second: first is second)
        self.number, self.reference = "PO000001", "SOURCE-REF"
        title = ("*" if dirty else "") + self.number
        self.tab = Node("AXRadioButton", title, bounds=Rect(5, 5, 160, 20))
        self.other = Node("AXRadioButton", "Contact", bounds=Rect(170, 5, 160, 20))
        self.group = Node("AXTabGroup", title, self.tab, children=[self.tab, self.other])
        self.identity = Node("AXTextField", value=self.number, bounds=Rect(100, 40, 130, 20))
        self.ref = Node("AXTextField", value=self.reference, bounds=Rect(100, 70, 180, 20))
        self.text = Node("AXStaticText", value=self.number, bounds=Rect(25, 101, 120, 14))
        self.checkbox = Node("AXCheckBox", "", checked, bounds=Rect(11, 101, 14, 14))
        self.row = Node("AXRow", bounds=Rect(10, 100, 300, 16),
                        children=[self.text, self.checkbox])
        self.table = Node("AXTable", bounds=Rect(10, 90, 310, 270),
                          children=[Node("AXColumn"), Node("AXColumn"), self.row])
        self.ok = Node("AXButton", "OK", bounds=Rect(200, 400, 90, 20))
        self.state, self.actions = "main", []
        self.stays_open = False
        self.clean_offers_dialog = False
        self.before_ok = None
        self.duplicate_group = False
        self.duplicate_table = False

    def _control(self, node):
        return AXControl(node.role, node.title, node.value, node.bounds, node.enabled, node)

    @staticmethod
    def _read(node, key, *, required=False):
        value = {"AXRole": node.role, "AXTitle": node.title, "AXValue": node.value,
                 "AXChildren": node.children, "AXFocused": node.focused}.get(key)
        if required and value is None:
            raise DesktopError("Required attribute is missing.")
        return value

    def guard(self, capture):
        wanted = "Save Parts" if self.state == "dialog" else "main"
        if capture.window_title != wanted:
            raise DesktopError("Wrong focused window.")

    def find_ax(self, capture, *, role, title):
        self.guard(capture)
        candidates = ([self.table, self.ok] if self.state == "dialog" else [self.group])
        found = [self._control(node) for node in candidates
                 if node.role == role and node.title == title]
        if role == "AXTable" and self.duplicate_table:
            found *= 2
        if role == "AXTabGroup" and self.duplicate_group and not found:
            return self._control(Node("AXTabGroup", title, self.tab, children=[self.tab]))
        if len(found) != 1:
            raise DesktopError(f"Expected one exact {role}/{title!r}; found {len(found)}.")
        return found[0]

    def windows(self):
        return [SimpleNamespace(title="main")] + (
            [SimpleNamespace(title="Save Parts")] if self.state == "dialog" else [])

    def click(self, capture, x, y):
        self.guard(capture)
        if self.state == "dialog":
            assert self.checkbox.bounds.contains(x, y)
            self.actions.append("physical checkbox click")
            self.checkbox.value = 1 - self.checkbox.value
        else:
            assert self.identity.bounds.contains(x, y)
            self.actions.append("focus document number")
            self.identity.focused = True

    def key(self, capture, name, *, focus_guard):
        self.guard(capture)
        assert name == "cmd+w", "No Save key is permitted."
        if not focus_guard():
            raise DesktopError("Exact field focus changed.")
        self.actions.append(name)
        if self.group.title.startswith("*") or self.clean_offers_dialog:
            self.state = "dialog"
        else:
            self.close()

    def press(self, capture, control, *, action_guard):
        self.guard(capture)
        assert control.role == "AXButton" and control.title == "OK", "Never AXPress the checkbox."
        if self.before_ok:
            self.before_ok()
        if not action_guard():
            raise DesktopError("Action context changed; press refused.")
        self.actions.append("OK with only checkbox clear")
        if not self.stays_open:
            self.close()

    def close(self):
        self.state = "main"
        self.group.title, self.group.value, self.group.children = "Contact", self.other, [self.other]


class FakeUi:
    def __init__(self, desktop, title, evidence_dir, *, text_input="unicode"):
        self.desktop, self.title, self.evidence_dir = desktop, title, evidence_dir
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.text_input, self.actions = text_input, []

    def snapshot(self, label, *, ocr=True):
        result = SimpleNamespace(window_title=self.title,
                                 metadata=lambda: {"pid": self.desktop.pid, "window_title": self.title})
        self.desktop.guard(result)
        return result

    def field(self, capture, label, *, scope):
        return self.desktop._control(self.desktop.identity if label == "No." else self.desktop.ref)

    def write_actions(self):
        (self.evidence_dir / "actions.json").write_text(json.dumps(self.actions))


@pytest.fixture
def prepare(tmp_path, monkeypatch):
    def bounded_wait(predicate, description, **kwargs):
        for _ in range(3):
            result = predicate()
            if result:
                return result
        raise DesktopError("Timed out waiting for " + description)

    monkeypatch.setattr(document_cleanup, "wait_for", bounded_wait)
    monkeypatch.setattr(document_cleanup, "Ui", FakeUi)
    monkeypatch.setattr(document_cleanup, "editor_scope", lambda *args, **kwargs: (0, 0, 800, 100))

    def make(*, dirty=True, checked=1):
        desktop = NativeDesktop(dirty=dirty, checked=checked)
        ui = FakeUi(desktop, "main", tmp_path / "cleanup")
        header = {"number": desktop.number, "reference": desktop.reference}
        observed = {"number": desktop.number, "external_reference": desktop.reference}
        proof = {"passed": True, "observed": observed, "expected": dict(observed),
                 "raw": {"header": header, "final_header": dict(header)},
                 "evidence": {"initial_row_coverage": {"capture": {"pid": 99, "window_title": "main"}}},
                 "report": str(tmp_path / "content.json")}
        write_proof(proof)
        return ui, proof
    return make


def write_proof(proof):
    from pathlib import Path
    Path(proof["report"]).write_text(json.dumps(proof))


def run(ui, proof):
    return document_cleanup.close_verified_inspection(
        ui, title="PO000001", number="PO000001", reference="SOURCE-REF", content_evidence=proof)


@pytest.mark.parametrize("dirty,checked,clicks", [(False, 1, 0), (True, 1, 1), (True, 0, 0)])
def test_close_never_saves_and_clears_only_checked_inspection(prepare, dirty, checked, clicks):
    ui, proof = prepare(dirty=dirty, checked=checked)
    result = run(ui, proof)
    assert result["passed"] and result["closed"] and result["remaining_editor"] == "Contact"
    assert result["save_requests"] == 0 and result["close_requests"] == 1
    assert ui.desktop.actions.count("physical checkbox click") == clicks
    assert ui.desktop.actions.count("OK with only checkbox clear") == int(dirty)
    from pathlib import Path
    assert json.loads(Path(result["report"]).read_text()) == result


@pytest.mark.parametrize("change", ["unpassed", "wrong_number", "wrong_reference", "header_changed", "different_file"])
def test_invalid_content_evidence_cannot_dispatch_close(prepare, change):
    ui, proof = prepare()
    if change == "unpassed":
        proof["passed"] = False
    elif change == "wrong_number":
        proof["observed"]["number"] = "OTHER"
    elif change == "wrong_reference":
        proof["observed"]["external_reference"] = "OTHER"
    elif change == "header_changed":
        proof["raw"]["final_header"]["reference"] = "OTHER"
    else:
        proof["extra"] = "not in durable report"
    if change != "different_file":
        write_proof(proof)
    with pytest.raises(DesktopError):
        run(ui, proof)
    assert ui.desktop.actions == []


@pytest.mark.parametrize("change", ["reference", "number", "other_selected", "two_groups", "other_workspace"])
def test_current_record_must_match_report_before_any_close(prepare, change):
    ui, proof = prepare()
    d = ui.desktop
    if change == "reference":
        d.ref.value = "OTHER"
    elif change == "number":
        d.identity.value = "OTHER"
    elif change == "other_selected":
        d.other.title = "*OTHER-DOCUMENT"
        d.group.value = d.other
    elif change == "two_groups":
        d.duplicate_group = True
    else:
        proof["evidence"]["initial_row_coverage"]["capture"]["window_title"] = "other workspace"
        write_proof(proof)
    with pytest.raises(DesktopError):
        run(ui, proof)
    assert d.actions == []


@pytest.mark.parametrize("change", ["two_rows", "wrong_record", "extra_cell", "ambiguous_table", "disabled", "nonbinary"])
def test_ambiguous_or_other_save_parts_stays_untouched(prepare, change):
    ui, proof = prepare()
    d = ui.desktop
    if change == "two_rows":
        d.table.children.append(Node("AXRow", children=[d.text, d.checkbox]))
    elif change == "wrong_record":
        d.text.value = "OTHER-DOCUMENT"
    elif change == "extra_cell":
        d.row.children.append(Node("AXCheckBox", value=1))
    elif change == "ambiguous_table":
        d.duplicate_table = True
    elif change == "disabled":
        d.checkbox.enabled = False
    else:
        d.checkbox.value = 2
    with pytest.raises(DesktopError):
        run(ui, proof)
    assert d.actions == ["focus document number", "cmd+w"]
    assert d.state == "dialog"


def test_clean_close_does_not_accept_unexpected_save_parts(prepare):
    ui, proof = prepare(dirty=False)
    ui.desktop.clean_offers_dialog = True
    with pytest.raises(DesktopError, match="clean inspection unexpectedly"):
        run(ui, proof)
    assert ui.desktop.actions == ["focus document number", "cmd+w"]


@pytest.mark.parametrize("change", ["checked", "different_record", "second_row"])
def test_ok_rechecks_same_unchecked_single_record_at_dispatch(prepare, change):
    ui, proof = prepare(checked=0)
    d = ui.desktop
    def mutate():
        if change == "checked":
            d.checkbox.value = 1
        elif change == "different_record":
            d.text.value = "OTHER"
        else:
            d.table.children.append(Node("AXRow", children=[d.text, d.checkbox]))
    d.before_ok = mutate
    with pytest.raises(DesktopError):
        run(ui, proof)
    assert d.actions == ["focus document number", "cmd+w"]
    assert d.state == "dialog"


def test_remaining_save_parts_cannot_be_reported_closed_or_trigger_repeated_ok(prepare):
    ui, proof = prepare()
    ui.desktop.stays_open = True
    with pytest.raises(DesktopError, match="Timed out") as error:
        run(ui, proof)
    from pathlib import Path
    evidence = json.loads(Path(error.value.document_cleanup_evidence).read_text())
    assert evidence["passed"] is False and evidence["closed"] is False
    assert evidence["save_requests"] == 0
    assert ui.desktop.actions.count("OK with only checkbox clear") == 1
