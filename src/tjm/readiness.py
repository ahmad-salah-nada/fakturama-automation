"""Read-only connection checks before starting a new Fakturama transaction.

The supported native layout exposes all tab captions as direct AXRadioButton
children of perspective AXTabGroups. Unknown shapes stop instead of searching
document fields or assuming an omitted tab is clean. No function launches,
activates, closes, clicks, types, or saves an application record.
"""

from pathlib import Path
import plistlib
import time

from .desktop import Desktop, DesktopError, _load_native


SUPPORTED_BUNDLE_ID = "Fakturama.ID"
SUPPORTED_VERSION = "2.2.0"


def expected_workspace_title(workspace):
    if not isinstance(workspace, (str, Path)) or not str(workspace).strip():
        raise ValueError("Readiness requires an explicit Fakturama workspace directory.")
    try:
        resolved = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DesktopError("The requested Fakturama workspace does not exist or cannot be resolved.") from exc
    if not resolved.is_dir():
        raise DesktopError("The requested Fakturama workspace is not a directory.")
    return resolved, "Fakturama - " + str(resolved)


def read_application_metadata(identity):
    """Read the plist of the actual connected bundle, never a configured path."""
    if (not isinstance(identity, tuple) or len(identity) != 4
            or identity[1] != SUPPORTED_BUNDLE_ID or not isinstance(identity[2], str)):
        raise DesktopError("Connected application identity does not identify Fakturama.")
    try:
        bundle = Path(identity[2]).resolve(strict=True)
        if not bundle.is_dir() or bundle.suffix != ".app":
            raise DesktopError("Connected application does not identify a macOS app bundle.")
        with (bundle / "Contents" / "Info.plist").open("rb") as stream:
            metadata = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise DesktopError("The connected application's Info.plist could not be read.") from exc
    if not isinstance(metadata, dict) or metadata.get("CFBundleIdentifier") != identity[1]:
        raise DesktopError("Connected process and installed bundle identifiers disagree.")
    version = metadata.get("CFBundleShortVersionString")
    if version != SUPPORTED_VERSION:
        raise DesktopError(f"Unsupported Fakturama version {version!r}; require {SUPPORTED_VERSION}.")
    return {"bundle_path": str(bundle), "bundle_id": identity[1],
            "version": version, "build": metadata.get("CFBundleVersion"),
            "plist_path": str(bundle / "Contents" / "Info.plist")}


def require_workspace_window(windows, expected_title):
    matches = [window for window in windows if window.title == expected_title]
    if len(matches) != 1:
        raise DesktopError(f"Requested workspace needs one exact main window {expected_title!r}; found {len(matches)}.")
    if any(window.title and window.title != expected_title for window in windows):
        raise DesktopError("Another named Fakturama window is open; clean main-window readiness is not established.")
    return matches[0]


def _select_workspace_desktop(expected_title):
    """Read every running instance; select one exact workspace without activation."""
    _, cocoa, _, _ = _load_native()
    apps = [app for app in cocoa.NSWorkspace.sharedWorkspace().runningApplications()
            if app.bundleIdentifier() == SUPPORTED_BUNDLE_ID and not app.isTerminated()]
    inventory, matches = [], []
    for app in apps:
        identity = Desktop._identity(app)
        desktop = Desktop(pid=identity[0], bundle_id=SUPPORTED_BUNDLE_ID)
        if desktop.identity != identity:
            raise DesktopError("A Fakturama process changed during workspace discovery.")
        # Never skip an instance whose native inventory cannot be read: it may
        # contain a second copy of the requested workspace.
        windows = desktop.windows()
        inventory.append({"process_identity": list(identity),
                          "window_titles": [window.title for window in windows]})
        for window in windows:
            if window.title == expected_title:
                matches.append((desktop, windows))
    if len(matches) != 1:
        raise DesktopError("Requested workspace needs one exact running Fakturama main window; "
                           f"found {len(matches)} across {len(apps)} applications.")
    desktop, windows = matches[0]
    require_workspace_window(windows, expected_title)
    return desktop, {"method": "unique exact workspace window",
                     "expected_window_title": expected_title,
                     "matching_workspace_windows": 1,
                     "application_count": len(apps), "application_inventory": inventory}


def read_clean_tabs(desktop, capture):
    """Inventory exposed tabs in the observed two-level perspective layout."""
    desktop.guard(capture)
    deadline, seen = time.monotonic() + 2, []

    def caption(element):
        if time.monotonic() >= deadline or len(seen) >= 128:
            raise DesktopError("Native tab inventory limit reached; cleanliness is unproved.")
        if any(desktop.cf.CFEqual(element, old) for old in seen):
            raise DesktopError("Native tab inventory contains a repeated element; completeness is unproved.")
        seen.append(element)
        code, pid = desktop.ax.AXUIElementGetPid(element, None)
        if code != desktop.ax.kAXErrorSuccess or int(pid) != desktop.pid:
            raise DesktopError("Native tab inventory element is not owned by Fakturama.")
        role = desktop._read(element, "AXRole", required=True)
        title = desktop._read(element, "AXTitle")
        if not isinstance(role, str) or (title is not None and not isinstance(title, str)):
            raise DesktopError("Native tab inventory has an unreadable role or caption.")
        return role, title

    def children(element):
        values = desktop._read(element, "AXChildren", required=True)
        if isinstance(values, (str, bytes)):
            raise DesktopError("Native tab inventory children are not an element collection.")
        try:
            result = list(values)
        except TypeError as exc:
            raise DesktopError("Native tab inventory children are not an element collection.") from exc
        if not result or len(result) + len(seen) > 128:
            raise DesktopError("Native tab inventory is empty or exceeds its complete-read limit.")
        return result

    perspectives = []
    for element in children(capture.window_element):
        role, title = caption(element)
        if role == "SWTComposite" and title == "Fakturama Perspective":
            perspectives.append(element)
        elif role not in {"AXToolbar", "AXButton", "AXStaticText"}:
            raise DesktopError("Main-window native layout is unsupported; tab inventory is unproved.")
    if len(perspectives) != 1:
        raise DesktopError("Readiness needs one exposed Fakturama Perspective.")

    groups = []
    for element in children(perspectives[0]):
        role, title = caption(element)
        if role == "SWTComposite" and title == "Navigation View":
            continue
        if role != "AXTabGroup" or not title:
            raise DesktopError("Perspective contains an unsupported child; tab inventory is unproved.")
        tabs = []
        selected = desktop._read(element, "AXValue", required=True)
        selected_indexes = []
        for tab in children(element):
            tab_role, tab_title = caption(tab)
            if tab_role != "AXRadioButton" or not tab_title:
                raise DesktopError("A tab group contains an unreadable or non-tab child.")
            if tab_title.startswith("*") or tab_title in {"New Order", "New Invoice"}:
                raise DesktopError(f"Pending editor {tab_title!r} blocks a new transaction.")
            if desktop.cf.CFEqual(tab, selected):
                selected_indexes.append(len(tabs))
            tabs.append(tab_title)
        if len(selected_indexes) != 1 or tabs[selected_indexes[0]] != title:
            raise DesktopError("Tab-group caption and exact selected native tab disagree.")
        groups.append({"group_title": title, "selected_title": title, "tabs": tabs})
    if not groups:
        raise DesktopError("No exposed Fakturama tab groups were found.")
    desktop.guard(capture)
    if time.monotonic() >= deadline:
        raise DesktopError("Native tab inventory exceeded its complete-read deadline.")
    return {"complete": True, "layout": "direct Fakturama Perspective tab groups",
            "groups": groups, "native_elements_read": len(seen), "dirty_tab_titles": []}


def connect_readiness(workspace, evidence_dir, *, text_input="unicode"):
    """Return (Ui, evidence) after read-only app/workspace/clean-tab checks.

    Caller must have focused the expected main window. Duplicate references,
    business settings, and persistence remain separate workflow checks.
    """
    from .ui import Ui

    resolved, title = expected_workspace_title(workspace)
    desktop, selection = _select_workspace_desktop(title)
    metadata = read_application_metadata(desktop.identity)
    permissions = desktop.permissions()
    if permissions != {"accessibility": True, "screen_recording": True}:
        raise DesktopError("Both Accessibility and Screen Recording must already be available.")
    desktop._require_focus()
    require_workspace_window(desktop.windows(), title)
    capture = desktop.capture(title)
    inventory = read_clean_tabs(desktop, capture)
    # Re-read all instances after clean-tab observation. PID reuse or another
    # exact workspace window refuses this run; unrelated workspaces remain open.
    rechecked, final_selection = _select_workspace_desktop(title)
    if rechecked.identity != desktop.identity:
        raise DesktopError("The selected Fakturama workspace process changed during readiness.")
    if read_application_metadata(rechecked.identity) != metadata:
        raise DesktopError("The selected Fakturama bundle metadata changed during readiness.")
    desktop.guard(capture)
    ui = Ui(desktop, title, evidence_dir, text_input=text_input)
    image_path = ui.evidence_dir / "readiness.png"
    capture.save(image_path)
    evidence = {"workspace": str(resolved), "window_title": title, "application": metadata,
                "process_identity": list(desktop.identity),
                "application_count": final_selection["application_count"],
                "workspace_selection": {"initial": selection, "rechecked": final_selection},
                "permissions": permissions, "capture": capture.metadata(),
                "capture_path": str(image_path), "tabs": inventory,
                "scope": "App identity/version, workspace, permissions, focused capture and clean exposed tabs."}
    ui.discoveries.append({"method": "read-only readiness", **evidence})
    return ui, evidence
