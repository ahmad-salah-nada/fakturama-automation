from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError
from tjm.readiness import (
    _select_workspace_desktop, connect_readiness, expected_workspace_title,
    read_application_metadata, read_clean_tabs, require_workspace_window,
)


def app_identity(tmp_path, *, identifier="Fakturama.ID", version="2.2.0"):
    bundle = tmp_path / "An Arbitrary Installation" / "Fakturama2.app"
    contents = bundle / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": identifier, "CFBundleShortVersionString": version,
        "CFBundleVersion": "2.2.0.example-build",
    }))
    return 99, "Fakturama.ID", str(bundle), 1234.5


def test_version_is_read_from_connected_bundle_at_any_installation_path(tmp_path):
    identity = app_identity(tmp_path)
    actual = read_application_metadata(identity)
    assert actual["bundle_path"] == identity[2]
    assert actual["version"] == "2.2.0"
    assert actual["build"] == "2.2.0.example-build"
    assert Path(actual["plist_path"]).is_file()


@pytest.mark.parametrize("version", ["2.1.0", "2.2.1", "2.2.0.2026", None])
def test_unsupported_or_missing_version_cannot_inherit_assumed_supported_version(tmp_path, version):
    identity = app_identity(tmp_path)
    path = Path(identity[2]) / "Contents" / "Info.plist"
    metadata = plistlib.loads(path.read_bytes())
    if version is None:
        metadata.pop("CFBundleShortVersionString")
    else:
        metadata["CFBundleShortVersionString"] = version
    path.write_bytes(plistlib.dumps(metadata))
    with pytest.raises(DesktopError, match="Unsupported Fakturama version"):
        read_application_metadata(identity)


def test_bundle_identifier_must_match_running_process_identity(tmp_path):
    with pytest.raises(DesktopError, match="identifiers disagree"):
        read_application_metadata(app_identity(tmp_path, identifier="different.application"))


def test_workspace_title_uses_resolved_requested_path(tmp_path):
    workspace = tmp_path / "Company workspace"
    workspace.mkdir()
    alias = tmp_path / "shortcut"
    alias.symlink_to(workspace, target_is_directory=True)
    resolved, title = expected_workspace_title(alias)
    assert resolved == workspace.resolve()
    assert title == "Fakturama - " + str(workspace.resolve())


def test_another_workspace_is_not_accepted_by_a_similar_main_window_title(tmp_path):
    requested = tmp_path / "requested"
    requested.mkdir()
    _, title = expected_workspace_title(requested)
    with pytest.raises(DesktopError, match="one exact main window"):
        require_workspace_window([SimpleNamespace(title=title + "-old")], title)


def test_named_auxiliary_window_blocks_readiness_but_unnamed_inventory_entry_is_allowed(tmp_path):
    _, title = expected_workspace_title(tmp_path)
    main = SimpleNamespace(title=title)
    assert require_workspace_window([main, SimpleNamespace(title="")], title) is main
    with pytest.raises(DesktopError, match="Another named"):
        require_workspace_window([main, SimpleNamespace(title="Confirm Save")], title)


class NativeNode:
    def __init__(self, role, title=None, *, children=None, selected=None, owner=99):
        self.attributes = {"AXRole": role, "AXTitle": title,
                           "AXChildren": children, "AXValue": selected}
        self.owner = owner


class ReadOnlyNative:
    def __init__(self):
        self.pid = 99
        self.guards = 0
        self.cf = SimpleNamespace(CFEqual=lambda first, second: first is second)
        self.ax = SimpleNamespace(kAXErrorSuccess=0,
                                 AXUIElementGetPid=lambda element, _: (0, element.owner))

    def guard(self, capture):
        self.guards += 1

    @staticmethod
    def _read(element, attribute, *, required=False):
        value = element.attributes[attribute]
        if required and value is None:
            raise DesktopError("Required native collection/value is unavailable.")
        return value


def native_layout(editor_titles, *, selected_index=0):
    tabs = [NativeNode("AXRadioButton", title) for title in editor_titles]
    editors = NativeNode("AXTabGroup", tabs[selected_index].attributes["AXTitle"],
                         children=tabs, selected=tabs[selected_index])
    documents = NativeNode("AXRadioButton", "Documents")
    views = NativeNode("AXTabGroup", "Documents", children=[documents], selected=documents)
    perspective = NativeNode("SWTComposite", "Fakturama Perspective", children=[
        views, editors, NativeNode("SWTComposite", "Navigation View")])
    window = NativeNode("AXWindow", "main", children=[
        NativeNode("AXToolbar"), perspective, NativeNode("AXButton")])
    return SimpleNamespace(window_element=window), perspective, editors


def test_multiple_clean_tabs_can_start_a_new_transaction_without_closing_any():
    capture, _, _ = native_layout(["Fakturama", "Order ORD-123", "Existing product"], selected_index=1)
    desktop = ReadOnlyNative()
    evidence = read_clean_tabs(desktop, capture)
    assert evidence["complete"] is True
    assert evidence["groups"][1] == {"group_title": "Order ORD-123", "selected_title": "Order ORD-123",
                                      "tabs": ["Fakturama", "Order ORD-123", "Existing product"]}
    assert desktop.guards == 2


@pytest.mark.parametrize("pending_title", ["*Existing product", "*Order ORD-123", "New Order", "New Invoice"])
def test_unselected_pending_tab_blocks_readiness(pending_title):
    capture, _, _ = native_layout(["Fakturama", pending_title])
    with pytest.raises(DesktopError, match="Pending editor"):
        read_clean_tabs(ReadOnlyNative(), capture)


def test_selected_element_and_caption_must_agree():
    capture, _, group = native_layout(["Fakturama", "Order ORD-123"])
    group.attributes["AXValue"] = group.attributes["AXChildren"][1]
    with pytest.raises(DesktopError, match="exact selected native tab disagree"):
        read_clean_tabs(ReadOnlyNative(), capture)


@pytest.mark.parametrize("unproved_children", [None, [], "not elements"])
def test_unavailable_or_empty_tab_inventory_is_not_treated_as_clean(unproved_children):
    capture, _, group = native_layout(["Fakturama"])
    group.attributes["AXChildren"] = unproved_children
    with pytest.raises(DesktopError):
        read_clean_tabs(ReadOnlyNative(), capture)


def test_unknown_nested_container_stops_instead_of_omitting_possible_tabs():
    capture, perspective, _ = native_layout(["Fakturama"])
    perspective.attributes["AXChildren"].append(NativeNode("SWTComposite", "Unknown editor container"))
    with pytest.raises(DesktopError, match="unsupported child"):
        read_clean_tabs(ReadOnlyNative(), capture)


def test_inventory_over_limit_is_not_truncated_to_the_first_clean_tabs():
    capture, _, _ = native_layout(["Fakturama"] + [f"Clean tab {index}" for index in range(130)])
    with pytest.raises(DesktopError, match="complete-read limit"):
        read_clean_tabs(ReadOnlyNative(), capture)


def test_native_tab_from_another_process_is_refused():
    capture, _, group = native_layout(["Fakturama"])
    group.attributes["AXChildren"][0].owner = 100
    with pytest.raises(DesktopError, match="not owned by Fakturama"):
        read_clean_tabs(ReadOnlyNative(), capture)


@pytest.fixture
def running_workspaces(monkeypatch, tmp_path):
    """Fake read-only native inventories; activation/input would fail the test."""
    import tjm.readiness as readiness

    installed = app_identity(tmp_path)
    state = SimpleNamespace(apps=[], reads=[], focused=True, permissions=True)

    def application(pid, title, *, identity=None, windows=None):
        process = SimpleNamespace(
            identity=identity if identity is not None else (pid, *installed[1:]),
            window_inventory=windows if windows is not None else [SimpleNamespace(title=title)])
        process.bundleIdentifier = lambda: "Fakturama.ID"
        process.isTerminated = lambda: False
        return process

    class FakeDesktop:
        @staticmethod
        def _identity(app):
            if app.identity is None:
                raise DesktopError("Running application identity is incomplete.")
            return app.identity

        def __init__(self, *, pid, bundle_id):
            assert bundle_id == "Fakturama.ID"
            candidates = [app for app in state.apps if app.identity is not None and app.identity[0] == pid]
            assert len(candidates) == 1
            self.app = candidates[0]
            self.pid, self.identity = pid, self.app.identity

        def windows(self):
            state.reads.append(("windows", self.pid))
            if isinstance(self.app.window_inventory, Exception):
                raise self.app.window_inventory
            return self.app.window_inventory

        def permissions(self):
            return {"accessibility": state.permissions, "screen_recording": state.permissions}

        def _require_focus(self):
            state.reads.append(("focus", self.pid))
            if not state.focused:
                raise DesktopError("Target process is not freshly focused.")

        def capture(self, title):
            state.reads.append(("capture", self.pid, title))
            return SimpleNamespace(metadata=lambda: {"pid": self.pid},
                                   save=lambda path: Path(path).write_bytes(b"fake captured evidence"))

        def guard(self, capture):
            state.reads.append(("guard", self.pid))

        def activate(self):
            pytest.fail("Readiness must never activate any workspace.")

    native_workspace = SimpleNamespace(runningApplications=lambda: state.apps)
    cocoa = SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: native_workspace))
    monkeypatch.setattr(readiness, "_load_native", lambda: (None, cocoa, None, None))
    monkeypatch.setattr(readiness, "Desktop", FakeDesktop)
    monkeypatch.setattr(readiness, "read_clean_tabs", lambda desktop, capture:
                        {"complete": True, "target_pid": desktop.pid, "dirty_tab_titles": []})
    state.application = application
    state.installed = installed
    state.requested = tmp_path / "target business workspace"
    state.requested.mkdir()
    state.title = "Fakturama - " + str(state.requested.resolve())
    return state


def test_exact_workspace_selects_one_target_among_two_running_instances(running_workspaces):
    state = running_workspaces
    state.apps = [state.application(100, "Fakturama - another workspace"),
                  state.application(99, state.title)]
    desktop, selection = _select_workspace_desktop(state.title)
    assert desktop.pid == 99
    assert selection["application_count"] == 2
    assert selection["matching_workspace_windows"] == 1
    assert selection["method"] == "unique exact workspace window"
    assert state.reads == [("windows", 100), ("windows", 99)]


@pytest.mark.parametrize("matching_count", [0, 2])
def test_zero_or_two_exact_workspace_windows_refuse_selection(running_workspaces, matching_count):
    state = running_workspaces
    state.apps = [state.application(99, state.title if matching_count else state.title + " old"),
                  state.application(100, state.title if matching_count else "Fakturama - unrelated")]
    with pytest.raises(DesktopError, match=f"found {matching_count} across 2 applications"):
        _select_workspace_desktop(state.title)


def test_multiple_exact_windows_within_one_process_are_also_ambiguous(running_workspaces):
    state = running_workspaces
    state.apps = [state.application(99, state.title, windows=[SimpleNamespace(title=state.title)] * 2)]
    with pytest.raises(DesktopError, match="found 2 across 1 applications"):
        _select_workspace_desktop(state.title)


def test_an_unreadable_other_workspace_inventory_cannot_be_skipped(running_workspaces):
    state = running_workspaces
    state.apps = [state.application(99, state.title),
                  state.application(100, "Fakturama - other", windows=DesktopError("Unreadable inventory"))]
    with pytest.raises(DesktopError, match="Unreadable inventory"):
        _select_workspace_desktop(state.title)


def test_incomplete_other_process_identity_cannot_hide_another_matching_workspace(running_workspaces):
    state = running_workspaces
    other = state.application(100, "Fakturama - other")
    other.identity = None
    state.apps = [state.application(99, state.title), other]
    with pytest.raises(DesktopError, match="identity is incomplete"):
        _select_workspace_desktop(state.title)


def test_selected_workspace_auxiliary_window_still_blocks_readiness(running_workspaces):
    state = running_workspaces
    state.apps = [state.application(99, state.title, windows=[SimpleNamespace(title=state.title),
                                                           SimpleNamespace(title="Confirm Save")])]
    with pytest.raises(DesktopError, match="Another named"):
        _select_workspace_desktop(state.title)


def test_connection_rechecks_unique_workspace_and_reports_all_instances(running_workspaces, tmp_path):
    state = running_workspaces
    state.apps = [state.application(100, "Fakturama - other"), state.application(99, state.title)]
    ui, evidence = connect_readiness(state.requested, tmp_path / "readiness evidence")
    assert ui.desktop.pid == 99 and evidence["process_identity"][0] == 99
    assert evidence["application_count"] == 2 and evidence["tabs"]["target_pid"] == 99
    assert evidence["workspace_selection"]["initial"]["application_count"] == 2
    assert evidence["workspace_selection"]["rechecked"]["matching_workspace_windows"] == 1
    assert state.reads.count(("windows", 100)) == 2
    assert ("focus", 99) in state.reads and ("focus", 100) not in state.reads
    assert Path(evidence["capture_path"]).is_file()


def test_reused_target_pid_with_another_launch_identity_is_refused(running_workspaces, monkeypatch, tmp_path):
    import tjm.readiness as readiness

    state = running_workspaces
    target = state.application(99, state.title)
    state.apps = [target]

    def changed_after_inventory(desktop, capture):
        target.identity = (*target.identity[:3], target.identity[3] + 1)
        return {"complete": True}

    monkeypatch.setattr(readiness, "read_clean_tabs", changed_after_inventory)
    with pytest.raises(DesktopError, match="workspace process changed"):
        connect_readiness(state.requested, tmp_path / "refused readiness")


def test_second_matching_workspace_appearing_during_clean_tab_check_is_refused(
        running_workspaces, monkeypatch, tmp_path):
    import tjm.readiness as readiness

    state = running_workspaces
    state.apps = [state.application(99, state.title)]

    def second_workspace(desktop, capture):
        state.apps.append(state.application(100, state.title))
        return {"complete": True}

    monkeypatch.setattr(readiness, "read_clean_tabs", second_workspace)
    with pytest.raises(DesktopError, match="found 2 across 2 applications"):
        connect_readiness(state.requested, tmp_path / "ambiguous readiness")


@pytest.mark.parametrize("guard", ["focus", "permissions"])
def test_target_focus_and_permissions_remain_required(running_workspaces, tmp_path, guard):
    state = running_workspaces
    state.apps = [state.application(99, state.title), state.application(100, "Fakturama - other")]
    setattr(state, "focused" if guard == "focus" else "permissions", False)
    with pytest.raises(DesktopError):
        connect_readiness(state.requested, tmp_path / (guard + " refused"))
    assert not any(read[0] == "capture" for read in state.reads)


def test_missing_selected_bundle_metadata_is_not_assumed_supported(running_workspaces, tmp_path):
    state = running_workspaces
    state.apps = [state.application(99, state.title)]
    (Path(state.installed[2]) / "Contents" / "Info.plist").unlink()
    with pytest.raises(DesktopError, match="Info.plist could not be read"):
        connect_readiness(state.requested, tmp_path / "missing metadata")


def test_pending_target_tab_check_still_stops_connection(running_workspaces, monkeypatch, tmp_path):
    import tjm.readiness as readiness

    state = running_workspaces
    state.apps = [state.application(99, state.title)]

    def pending_tab(desktop, capture):
        raise DesktopError("Pending editor New Order blocks a new transaction.")

    monkeypatch.setattr(readiness, "read_clean_tabs", pending_tab)
    with pytest.raises(DesktopError, match="Pending editor"):
        connect_readiness(state.requested, tmp_path / "pending readiness")
