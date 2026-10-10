"""One-Save safety at the journal/dispatch boundary, with no native UI calls.

The fake tracks Eclipse's active editor separately from readable tab content.
That distinction exercises why a global Save needs exact editor focus, rather
than treating the mere existence of a matching identity field as sufficient.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tjm.desktop import AXControl, DesktopError, Rect
from tjm.workflow import save_once
from tjm.workflow_rules import SaveLedger, WorkflowReview


TITLE = "New Term of Payment"
IDENTITY = "Bank Transfer"


class Capture:
    window_bounds = Rect(0, 0, 800, 600)

    def metadata(self):
        return {"window_title": "Fakturama", "pid": 123, "pixels_per_screen_unit_x": 2.0}


class FakeDesktop:
    def __init__(self, *, identity=IDENTITY, title=TITLE, active_editor=TITLE):
        self.group = {"AXRole": "AXTabGroup", "AXTitle": title, "AXValue": object()}
        self.identity = {"AXRole": "AXTextField", "AXTitle": None,
                         "AXValue": identity, "AXFocused": active_editor == TITLE,
                         "AXEnabled": True, "bounds": Rect(240, 100, 200, 25)}
        self.active_editor = active_editor
        self.inputs = []
        self.cf = NS(CFEqual=lambda first, second: first is second)

    def _read(self, element, attribute, *, required=False):
        if required and attribute not in element:
            raise DesktopError(f"Missing required {attribute}.")
        return element.get(attribute)

    def _control(self, element):
        return AXControl(element["AXRole"], element.get("AXTitle"), element.get("AXValue"),
                         element.get("bounds", Rect(0, 0, 800, 600)),
                         element.get("AXEnabled", True), element)

    def find_ax(self, capture, *, role, title):
        if self.group["AXRole"] != role or self.group["AXTitle"] != title:
            raise DesktopError(f"Expected one exact {role}/{title!r}; found 0.")
        return self._control(self.group)

    def guard(self, capture):
        return capture

    def click(self, capture, x, y, **kwargs):
        if not self.identity["bounds"].contains(x, y):
            raise DesktopError("Click missed the identity field.")
        self.inputs.append(("click", x, y))
        self.identity["AXFocused"] = True
        self.active_editor = TITLE


class FakeUi:
    def __init__(self, desktop):
        self.desktop = desktop
        self.capture = Capture()
        self.before_press = None
        self.fail_after_dispatch = False
        self.save_dispatches = []

    def snapshot(self, *args, **kwargs):
        return self.capture

    def field(self, capture, label, *, scope, roles):
        assert label == "Name"
        assert scope == TITLE
        assert "AXTextField" in roles
        return self.desktop._control(self.desktop.identity)

    def press(self, title, *, action_guard=None):
        assert title == "Save"
        assert callable(action_guard)
        if self.before_press is not None:
            self.before_press()
        if not action_guard():
            raise DesktopError("Action context changed; accessibility press refused.")
        self.save_dispatches.append({"title": title, "active_editor": self.desktop.active_editor})
        if self.fail_after_dispatch:
            raise DesktopError("Dispatch outcome is unknown.")


def save(ui, ledger, journal_directory):
    return save_once(ui, ledger, kind="Payment", identity=IDENTITY,
                     title=TITLE, identity_label="Name", journal_directory=journal_directory)


def journal_path(folder):
    digest = hashlib.sha256(("Payment\0" + IDENTITY).encode("utf-8")).hexdigest()
    return folder / (digest + ".json")


@pytest.mark.parametrize("wrong", ["Other method", "Bank Transfer ", "bank transfer"])
def test_wrong_identity_stops_before_journaling_or_save(tmp_path, wrong):
    ui = FakeUi(FakeDesktop(identity=wrong))
    ledger = SaveLedger()
    with pytest.raises(DesktopError, match="identity"):
        save(ui, ledger, tmp_path)
    assert ledger.as_json() == []
    assert list(tmp_path.iterdir()) == []
    assert ui.save_dispatches == []


def test_changed_editor_before_action_callback_keeps_intent_and_dispatches_no_save(tmp_path):
    ui = FakeUi(FakeDesktop())
    ui.before_press = lambda: ui.desktop.group.update(AXValue=object())
    ledger = SaveLedger()
    with pytest.raises(DesktopError, match="context changed"):
        save(ui, ledger, tmp_path)
    recorded = json.loads(journal_path(tmp_path).read_text())
    assert recorded["intent"] == ledger.as_json()[0]
    assert recorded["intent"]["save_intent"] is True
    assert recorded["intent"]["persistence_evidence"] is None
    assert ui.save_dispatches == []
    with pytest.raises(WorkflowReview, match="Save intention already"):
        save(ui, ledger, tmp_path)
    assert ui.save_dispatches == []


def test_existing_journal_blocks_save_with_a_fresh_in_memory_ledger(tmp_path):
    path = journal_path(tmp_path)
    previous = '{"prior_save_outcome":"unknown"}\n'
    path.write_text(previous)
    ui = FakeUi(FakeDesktop())
    ledger = SaveLedger()
    with pytest.raises(FileExistsError):
        save(ui, ledger, tmp_path)
    assert path.read_text() == previous
    assert ui.save_dispatches == []


def test_journal_write_failure_never_dispatches_save_and_keeps_in_memory_intent(tmp_path, monkeypatch):
    ui = FakeUi(FakeDesktop())
    ledger = SaveLedger()
    original_open = Path.open

    def refuse_intent_write(path, *args, **kwargs):
        if path == journal_path(tmp_path):
            raise PermissionError("Cannot write Save intention.")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_intent_write)
    with pytest.raises(PermissionError):
        save(ui, ledger, tmp_path)
    assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 0}
    assert not journal_path(tmp_path).exists()
    assert ui.save_dispatches == []
    with pytest.raises(WorkflowReview, match="Save intention already"):
        save(ui, ledger, tmp_path)
    assert ui.save_dispatches == []


def test_one_dispatch_is_journaled_first_and_is_not_persistence_proof(tmp_path):
    ui = FakeUi(FakeDesktop())
    ledger = SaveLedger()

    def inspect_before_dispatch():
        saved = json.loads(journal_path(tmp_path).read_text())
        assert saved["intent"]["kind"] == "Payment"
        assert saved["intent"]["identity"] == IDENTITY
        assert saved["intent"]["save_intent"] is True
        assert saved["intent"]["persistence_evidence"] is None
        assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 0}
        assert not ui.save_dispatches

    ui.before_press = inspect_before_dispatch
    returned = save(ui, ledger, tmp_path)
    assert ui.save_dispatches == [{"title": "Save", "active_editor": TITLE}]
    assert returned["persistence_evidence"] is None
    assert ledger.counts() == {"save_intents": 1, "with_persistence_evidence": 0}
    assert json.loads(journal_path(tmp_path).read_text())["editor_title"] == TITLE
    with pytest.raises(WorkflowReview, match="Save intention already"):
        save(ui, ledger, tmp_path)
    assert len(ui.save_dispatches) == 1


def test_uncertain_dispatch_result_retains_intent_and_forbids_retry(tmp_path):
    ui = FakeUi(FakeDesktop())
    ui.fail_after_dispatch = True
    ledger = SaveLedger()
    with pytest.raises(DesktopError, match="unknown"):
        save(ui, ledger, tmp_path)
    assert len(ui.save_dispatches) == 1
    assert journal_path(tmp_path).exists()
    assert ledger.as_json()[0]["persistence_evidence"] is None
    with pytest.raises(WorkflowReview, match="Save intention already"):
        save(ui, ledger, tmp_path)
    assert len(ui.save_dispatches) == 1


def test_readable_inactive_editor_is_focused_before_global_save(tmp_path):
    ui = FakeUi(FakeDesktop(active_editor="New Order"))
    ledger = SaveLedger()
    save(ui, ledger, tmp_path)
    # The current target can be readable while a different Eclipse part remains
    # active. Focusing its exact identity field must establish the Save target.
    assert ui.save_dispatches == [{"title": "Save", "active_editor": TITLE}]
    assert ui.desktop.identity["AXFocused"] is True


def test_identity_field_focus_loss_before_save_callback_refuses_dispatch(tmp_path):
    ui = FakeUi(FakeDesktop())
    ledger = SaveLedger()

    def switch_active_part_without_changing_readable_values():
        ui.desktop.active_editor = "New Order"
        ui.desktop.identity["AXFocused"] = False

    ui.before_press = switch_active_part_without_changing_readable_values
    with pytest.raises(DesktopError, match="context changed"):
        save(ui, ledger, tmp_path)
    assert journal_path(tmp_path).exists()
    assert ledger.as_json()[0]["persistence_evidence"] is None
    assert ui.save_dispatches == []


@pytest.mark.parametrize("change", ["title", "identity"])
def test_changed_title_or_identity_at_save_callback_refuses_dispatch(tmp_path, change):
    ui = FakeUi(FakeDesktop())
    ledger = SaveLedger()

    def change_record():
        if change == "title":
            ui.desktop.group["AXTitle"] = "Different editor"
        else:
            ui.desktop.identity["AXValue"] = "Different payment"

    ui.before_press = change_record
    with pytest.raises(DesktopError, match="context changed"):
        save(ui, ledger, tmp_path)
    assert journal_path(tmp_path).exists()
    assert ui.save_dispatches == []


def test_dirty_marker_title_is_the_same_intended_editor(tmp_path):
    ui = FakeUi(FakeDesktop(title="*" + TITLE))
    ledger = SaveLedger()
    save(ui, ledger, tmp_path)
    assert len(ui.save_dispatches) == 1
    assert ledger.counts()["with_persistence_evidence"] == 0


def test_document_identity_is_scoped_above_addresses_not_item_number(tmp_path):
    class DocumentUi(FakeUi):
        def scope(self, capture, title):
            assert title == 'New Order'
            return (0, 0, 800, 600)

        def label(self, capture, label, *, scope):
            assert label == 'Addresses'
            return (100, 200, 180, 220)

        def field(self, capture, label, *, scope, roles):
            assert label == 'No.'
            if scope != (0, 0, 800, 200):
                raise DesktopError('Item No. is outside the document identity header.')
            return self.desktop._control(self.desktop.identity)

    ui = DocumentUi(FakeDesktop(identity='PO000001', title='New Order'))
    ledger = SaveLedger()
    with pytest.raises(DesktopError, match='Item No.'):
        save_once(ui, ledger, kind='Order', identity='PO000001', title='New Order',
                  identity_label='No.', journal_directory=tmp_path)
    assert ledger.as_json() == [] and ui.save_dispatches == []
    save_once(ui, ledger, kind='Order', identity='PO000001', title='New Order',
              identity_label='No.', journal_directory=tmp_path, identity_before='Addresses')
    assert len(ui.save_dispatches) == 1
    assert ledger.as_json()[0]['identity'] == 'PO000001'
