"""Combine visible table text with the exact owned native table boundary.

This adapter supports only a fully visible table whose native vertical and
horizontal scrollbars are explicitly absent. It does not scroll or resize
columns. Unreadable cells, scrollbar presence, changed content and unknown
native attributes stop the caller; none authorizes record creation. Two equal
observations are a practical readiness check, not proof that an application has
finished every asynchronous operation. Selecting a row only dispatches input;
the workflow must separately verify the selection and its subsequent result.
"""

from dataclasses import dataclass, field, replace
import json
import math
import re
import statistics
import time
from uuid import uuid4

from PIL import Image, ImageChops

from .desktop import AXControl, Capture, DesktopError
from .fields import find_phrases, normalized
from .ui_table import read_table


SCROLL_ATTRIBUTES = ("AXVerticalScrollBar", "AXHorizontalScrollBar")


@dataclass(frozen=True)
class TableObservation:
    capture: Capture = field(repr=False, compare=False)
    area: AXControl = field(repr=False, compare=False)
    scope: tuple
    table_box: tuple
    header_boxes: dict
    native_evidence: dict


@dataclass(frozen=True)
class CompleteTable:
    observation: TableObservation = field(repr=False, compare=False)
    readback: dict
    evidence: dict
    _signature: str = field(repr=False)
    search_field: str | None = None
    search_value: str | None = None

    @property
    def rows(self):
        return self.readback["rows"]

    def __getitem__(self, key):
        return self.readback[key]


def _pixel_box(capture, bounds):
    return ((bounds.x - capture.screen_rect.x) * capture.scale_x,
            (bounds.y - capture.screen_rect.y) * capture.scale_y,
            (bounds.x + bounds.width - capture.screen_rect.x) * capture.scale_x,
            (bounds.y + bounds.height - capture.screen_rect.y) * capture.scale_y)


def _valid_box(box, outer, description):
    if (not isinstance(box, (tuple, list)) or len(box) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in box)):
        raise DesktopError(f"{description} requires four finite pixel coordinates.")
    left, top, right, bottom = box
    if not (outer[0] <= left < right <= outer[2]
            and outer[1] <= top < bottom <= outer[3]):
        raise DesktopError(f"{description} extends outside its observed scope.")
    return tuple(box)


def _no_scrollbars(desktop, area):
    code, names = desktop.ax.AXUIElementCopyAttributeNames(area.element, None)
    if code != desktop.ax.kAXErrorSuccess or names is None:
        raise DesktopError(f"Cannot establish table scrollbar attributes: {code}.")
    if not all(name in names for name in SCROLL_ATTRIBUTES):
        raise DesktopError("Native table does not expose both scrollbar attributes; completeness is unknown.")
    observed = {}
    for name in SCROLL_ATTRIBUTES:
        code, value = desktop.ax.AXUIElementCopyAttributeValue(area.element, name, None)
        if code not in {desktop.ax.kAXErrorSuccess, desktop.ax.kAXErrorNoValue}:
            raise DesktopError(f"Cannot establish native table {name}: {code}.")
        if value is not None:
            raise DesktopError(f"Native table has {name}; unseen or scrolled rows/columns are possible.")
        observed[name] = {"code": int(code), "value": None}
    return {"role": area.role, "bounds": area.bounds.as_dict(),
            "attribute_names": list(names), "scrollbars": observed,
            "no_vertical_scrollbar": True, "no_horizontal_scrollbar": True}


def _table_at(ui, capture, point):
    """Resolve only the observed table or its one same-bounds SWT wrapper."""
    d = ui.desktop
    hit = d.hit_test(capture, *point)
    if hit.enabled is False:
        raise DesktopError("The native table hit target is explicitly disabled.")
    if hit.role == "AXScrollArea":
        return hit, {"hit_role": hit.role, "parent_hops": 0}
    if hit.role != "SWTComposite":
        raise DesktopError(f"Native table hit has unsupported role {hit.role!r}.")
    parent_element = d._read(hit.element, "AXParent", required=True)
    parent = d._control(parent_element)
    if parent.role != "AXScrollArea" or parent.bounds != hit.bounds:
        raise DesktopError("SWT table wrapper requires one immediate AXScrollArea parent with identical bounds.")
    if parent.enabled is False:
        raise DesktopError("The immediate native table parent is explicitly disabled.")
    # Do not climb again to an enclosing Data pane. The one observed parent
    # must itself expose the absent-scrollbar proof needed for this table.
    _no_scrollbars(d, parent)
    return parent, {"hit_role": hit.role, "parent_role": parent.role,
                    "parent_hops": 1, "identical_bounds": True}


def _header_label(ui, capture, name, headers, scope, *, tokens_override=None):
    """Remove only a short phrase contained in another proven requested header."""
    def label(wanted_name):
        if tokens_override is None:
            return ui.label(capture, wanted_name, scope=scope)
        scoped = [token for token in tokens_override
                  if scope[0] <= token.cx < scope[2] and scope[1] <= token.cy < scope[3]]
        matches = find_phrases(scoped, wanted_name)
        if len(matches) != 1:
            raise DesktopError(f"Expected one visible {wanted_name!r} label in its current scope; found {len(matches)}.")
        return matches[0]
    try:
        return label(name)
    except DesktopError as exc:
        prefix = f"Expected one visible {name!r} label in its current scope; found "
        count = re.fullmatch(re.escape(prefix) + r"([0-9]+)\.", str(exc))
        if count is None or int(count[1]) <= 1:
            raise
    tokens = [token for token in (capture.tokens() if tokens_override is None else tokens_override)
              if scope[0] <= token.cx < scope[2] and scope[1] <= token.cy < scope[3]]
    candidates = find_phrases(tokens, name)
    wanted = [normalized(word) for word in name.split()]
    longer = []
    for header in headers:
        words = [normalized(word) for word in header.split()]
        if len(words) > len(wanted) and any(words[i:i + len(wanted)] == wanted
                                           for i in range(len(words) - len(wanted) + 1)):
            # This deliberately uses ordinary strict discovery: a duplicate
            # longer header cannot justify removing any short-name candidate.
            longer.append((header, label(header)))
    removed = [box for box in candidates if any(
        outer[0] <= box[0] and outer[1] <= box[1] and box[2] <= outer[2] and box[3] <= outer[3]
        for _, outer in longer)]
    remaining = [box for box in candidates if box not in removed]
    if not removed or len(remaining) != 1:
        raise DesktopError(f"Requested header {name!r} remains ambiguous after longer-header containment; "
                           f"found {len(remaining)}.")
    if hasattr(ui, "discoveries"):
        ui.discoveries.append({"method": "requested header containment", "header": name,
                               "longer_headers": [{"header": header, "box": list(box)}
                                                  for header, box in longer],
                               "removed_candidates": [list(box) for box in removed],
                               "pixel_box": list(remaining[0]), "scope": scope})
    return remaining[0]


def _dark_header_tokens(ui, capture, scope, geometry):
    """Read white-on-gray labels only within a geometrically proven header."""
    import numpy as np
    from .ocr import token_evidence
    from .ui_table import _read_tokens

    left, top, right, bottom = map(int, scope)
    original = capture.image.crop((left, top, right, bottom)).convert("RGB")
    gray = np.asarray(original.convert("L"), dtype=float)
    prepared = Image.fromarray(np.where(np.abs(gray - geometry["header_background"]) > 45,
                                        0, 255).astype("uint8"))
    tokens = _read_tokens(prepared, psm=6)
    absolute = [replace(token, left=token.left + left, top=token.top + top) for token in tokens]
    stem = ui.evidence_dir / f"dark-header-{uuid4().hex[:12]}"
    source_path, prepared_path = stem.with_suffix(".png"), stem.with_name(stem.name + "-contrast.png")
    original.save(source_path)
    prepared.save(prepared_path)
    if hasattr(ui, "discoveries"):
        ui.discoveries.append({"method": "observed neutral dark header contrast", "scope": list(scope),
                               "background": geometry["header_background"], "contrast_threshold": 45,
                               "source_crop": str(source_path), "prepared_crop": str(prepared_path),
                               "tokens": token_evidence(absolute)})
    return absolute


def _dark_header_boxes(ui, capture, headers, scope):
    from .ocr import ocr_tokens
    from .ui_table import _header_geometry, _selected_document_header

    region = tuple(map(int, scope))
    if headers == ['State', 'Total']:
        # Locate the actual white header captions, then hit-test their table.
        # The full Documents scope also contains a category tree.
        import numpy as np
        original = capture.image.crop(region)
        prepared = Image.fromarray(np.where(np.asarray(original.convert('L')) > 225,
                                             0, 255).astype('uint8'))
        visible = [replace(t, left=t.left + region[0], top=t.top + region[1])
                   for t in ocr_tokens(prepared, psm=11)]
        seeds = [_header_label(ui, capture, name, headers, scope, tokens_override=visible)
                 for name in headers]
        seed = seeds[0]
        area, _ = _table_at(ui, capture, capture.screen_point(
            (seed[0] + seed[2]) / 2, seed[3] + (seed[3] - seed[1])))
        region = tuple(map(int, _valid_box(_pixel_box(capture, area.bounds), scope,
                                         'Owned selected document table')))
    crop = capture.image.crop(region)
    tokens = ocr_tokens(crop, psm=11)
    try:
        geometry = _header_geometry(crop, tokens, headers)
    except DesktopError:
        if headers != ['State', 'Total']:
            raise
        geometry = _selected_document_header(crop, tokens, headers)
    if not geometry.get("dark_neutral_header"):
        raise DesktopError("A neutral dark header was not geometrically established.")
    band = (region[0], region[1] + geometry["top"], region[2], region[1] + geometry["bottom"])
    tokens = _dark_header_tokens(ui, capture, band, geometry)
    return {name: _header_label(ui, capture, name, headers, band, tokens_override=tokens)
            for name in headers}


def _header_boxes(ui, capture, headers, scope):
    """Exclude body phrases only through a coherent observed header baseline."""
    boxes, pending = {}, {}
    for name in headers:
        try:
            boxes[name] = _header_label(ui, capture, name, headers, scope)
        except DesktopError as exc:
            count = re.search(r"found ([0-9]+)\.", str(exc))
            if count is not None and int(count[1]) == 0:
                try:
                    return _dark_header_boxes(ui, capture, headers, scope)
                except DesktopError:
                    raise exc
            if count is None or int(count[1]) <= 1:
                raise
            pending[name] = exc
    if not pending:
        return boxes
    if len(boxes) < 2:
        raise next(iter(pending.values()))
    heights = sorted(box[3] - box[1] for box in boxes.values())
    font = statistics.median(heights[:max(1, math.ceil(len(heights) / 2))])
    centers = [(box[1] + box[3]) / 2 for box in boxes.values()]
    if max(centers) - min(centers) > font * .75:
        raise DesktopError("Unambiguous requested labels do not prove one table header baseline.")
    band = (scope[0], max(scope[1], min(box[1] for box in boxes.values()) - font * .25),
            scope[2], min(scope[3], max(box[3] for box in boxes.values()) + font * .25))
    if hasattr(ui, "discoveries"):
        ui.discoveries.append({"method": "coherent requested header baseline", "scope": list(band),
                               "anchors": {name: list(box) for name, box in boxes.items()},
                               "ambiguous_labels": list(pending)})
    for name in pending:
        boxes[name] = _header_label(ui, capture, name, headers, band)
    return boxes


def _native_header_cell_boxes(ui, capture, headers, table_box, raw_boxes):
    """Reread rule-absorbed labels inside the already observed native table.

    This is only called after raw label containment fails. It neither expands
    the native boundary nor uses a requested label as observed table text.
    """
    from .ui_table import _cell, _header_geometry, _label, _read_tokens

    folder = ui.evidence_dir / ("native-header-cells-" + uuid4().hex[:12])
    folder.mkdir(parents=True, exist_ok=False)
    region = tuple(map(int, table_box))
    crop = capture.image.crop(region).convert("RGB")
    crop.save(folder / "native-table.png")
    evidence = {"method": "native containment header cell reread",
                "table_box": list(table_box), "raw_boxes": raw_boxes,
                "source_crop": str(folder / "native-table.png"), "headers": [],
                "status": "review_required"}
    try:
        geometry = _header_geometry(crop, _read_tokens(crop, psm=11), headers)
        evidence["geometry"] = geometry
        if geometry["top"] > geometry["font"] * 2:
            raise DesktopError("Native header reread begins too far below the table boundary.")
        boxes = {}
        for index, (left, right) in enumerate(geometry["columns"]):
            cell_box = (left + 3, geometry["top"] + 2,
                        right - 3, geometry["bottom"] - 3)
            try:
                text, record = _cell(crop, cell_box, folder / f"header-{index:02d}.png",
                                     header=True)
            except DesktopError as exc:
                evidence["headers"].append({
                    **getattr(exc, "table_cell_evidence", {"box": list(cell_box)}),
                    "error": str(exc)})
                continue
            evidence["headers"].append(record)
            for name in headers:
                if _label(text) != _label(name):
                    continue
                if name in boxes:
                    raise DesktopError(f"The required header {name!r} occurs more than once.")
                tokens = record["tokens"]
                # _cell's actual OCR is enlarged by 3 with a 15px border.
                glyph_box = (region[0] + cell_box[0] + (min(t["box"][0] for t in tokens) - 15) / 3,
                             region[1] + cell_box[1] + (min(t["box"][1] for t in tokens) - 15) / 3,
                             region[0] + cell_box[0] + (max(t["box"][2] for t in tokens) - 15) / 3,
                             region[1] + cell_box[1] + (max(t["box"][3] for t in tokens) - 15) / 3)
                absolute_cell = (region[0] + cell_box[0], region[1] + cell_box[1],
                                 region[0] + cell_box[2], region[1] + cell_box[3])
                _valid_box(glyph_box, absolute_cell, f"Reread header {name!r} inside its cell")
                boxes[name] = _valid_box(glyph_box, table_box,
                                         f"Reread header {name!r} inside native table")
        missing = [name for name in headers if name not in boxes]
        if missing:
            raise DesktopError(f"Required reread headers are missing, clipped or unreadable: {missing}.")
        evidence.update(status="read", header_boxes=boxes)
        return boxes
    except DesktopError as exc:
        evidence["error"] = str(exc)
        raise
    finally:
        report = folder / "readback.json"
        report.write_text(json.dumps(evidence, indent=2) + "\n")
        if hasattr(ui, "discoveries"):
            ui.discoveries.append({**evidence, "report": str(report)})


def _observe_capture(ui, capture, headers, scope):
    if (not isinstance(headers, list) or not headers
            or any(not isinstance(name, str) or not name.strip() for name in headers)
            or len(set(headers)) != len(headers)):
        raise DesktopError("Table observation requires distinct nonempty header names.")
    image_box = (0, 0, capture.image.width, capture.image.height)
    scope = _valid_box(ui.scope(capture, scope) if scope is not None else image_box,
                       image_box, "Table semantic scope")
    boxes = {name: _valid_box(box, scope, f"Table header {name!r}")
             for name, box in _header_boxes(ui, capture, headers, scope).items()}
    if headers == ['State', 'Total']:
        # OCR takes time. Re-observe before native discovery rather than relax
        # capture age; exact header pixels and window geometry must survive.
        fresh = ui.snapshot('fresh-document-table-header', ocr=False)
        if (fresh.pid != capture.pid or fresh.window_title != capture.window_title
                or fresh.screen_rect != capture.screen_rect
                or fresh.scale_x != capture.scale_x or fresh.scale_y != capture.scale_y):
            raise DesktopError('Document table window changed during header reading.')
        band = (round(min(box[0] for box in boxes.values())),
                round(min(box[1] for box in boxes.values())),
                round(max(box[2] for box in boxes.values())),
                round(max(box[3] for box in boxes.values())))
        if ImageChops.difference(capture.image.crop(band), fresh.image.crop(band)).getbbox() is not None:
            raise DesktopError('Document table header pixels changed during reading.')
        capture = fresh
    heights = sorted(box[3] - box[1] for box in boxes.values())
    font = statistics.median(heights[:max(1, math.ceil(len(heights) / 2))])
    centers = [(box[1] + box[3]) / 2 for box in boxes.values()]
    if max(centers) - min(centers) > font * .75:
        raise DesktopError("Requested table headers are not on one observed header baseline.")
    ordered = sorted(boxes.values(), key=lambda box: box[0])
    if any(a[2] > b[0] for a, b in zip(ordered, ordered[1:])):
        raise DesktopError("Requested table header text overlaps; the table is ambiguous.")
    x = (ordered[0][0] + ordered[-1][2]) / 2
    y = max(box[3] for box in boxes.values()) + font
    if not (scope[0] <= x < scope[2] and scope[1] <= y < scope[3]):
        raise DesktopError("Observed header has no in-scope body point for native table discovery.")
    area, resolution = _table_at(ui, capture, capture.screen_point(x, y))
    table_box = _valid_box(_pixel_box(capture, area.bounds), scope, "Owned native table")
    # An enclosing Data pane can also be AXScrollArea. A table begins at its
    # observed header; a distant enclosing pane is not accepted as the table.
    if min(box[1] for box in boxes.values()) - table_box[1] > font * 2:
        raise DesktopError("Hit-tested scroll area begins above the table header; an outer pane is ambiguous.")
    try:
        for name, box in boxes.items():
            _valid_box(box, table_box, f"Header {name!r} inside native table")
    except DesktopError:
        boxes = _native_header_cell_boxes(ui, capture, headers, table_box, boxes)
        heights = sorted(box[3] - box[1] for box in boxes.values())
        font = statistics.median(heights[:max(1, math.ceil(len(heights) / 2))])
        centers = [(box[1] + box[3]) / 2 for box in boxes.values()]
        if max(centers) - min(centers) > font * .75:
            raise DesktopError("Reread table headers are not on one observed header baseline.")
        ordered = sorted(boxes.values(), key=lambda box: box[0])
        if any(a[2] > b[0] for a, b in zip(ordered, ordered[1:])):
            raise DesktopError("Reread table header text overlaps; the table is ambiguous.")
        y = max(box[3] for box in boxes.values()) + font
    if y >= table_box[3]:
        raise DesktopError("Native table does not contain the observed body point.")
    native = _no_scrollbars(ui.desktop, area)
    native["discovery"] = resolution
    return TableObservation(capture, area, scope, table_box, boxes, native)


def observe_table(ui, headers, scope=None):
    """Locate one native table from the current visible header and body point."""
    return _observe_capture(ui, ui.snapshot("table-observation"), headers, scope)


def _query(ui, observation, search_field, search_value):
    if search_field is None:
        return None
    control = ui.field(observation.capture, search_field, scope=observation.scope)
    if control.role != "AXTextField" or control.value != search_value:
        raise DesktopError("Current table search field does not exactly match the requested query.")
    return {"label": search_field, "expected": search_value,
            "observed": control.value, "bounds": control.bounds.as_dict(), "passed": True}


def _signature(readback):
    return json.dumps({"headers": readback["headers"], "rows": readback["rows"]},
                      sort_keys=True, ensure_ascii=False, allow_nan=False)


def _same_table(ui, first, second):
    return (ui.desktop.cf.CFEqual(first.area.element, second.area.element)
            and first.area.bounds == second.area.bounds
            and first.table_box == second.table_box and first.scope == second.scope
            and first.header_boxes == second.header_boxes)


def _refresh_observation(ui, old, label, search_field=None, search_value=None):
    """Check a new capture against the pixels and native table already read."""
    capture = ui.snapshot(label, ocr=False)
    d = ui.desktop
    d.guard(capture)
    if (capture.pid != old.capture.pid or capture.process_identity != old.capture.process_identity
            or capture.window_title != old.capture.window_title
            or not d.cf.CFEqual(capture.window_element, old.capture.window_element)):
        raise DesktopError("Table readback belongs to another process or window; selection refused.")
    current = d._control(old.area.element)
    if (current.role != "AXScrollArea" or current.bounds != old.area.bounds
            or not capture.window_bounds.contains_rect(current.bounds)):
        raise DesktopError("The exact native table role or geometry changed after observation.")
    box = _valid_box(_pixel_box(capture, current.bounds), (0, 0, *capture.image.size), "Current native table")
    if box != old.table_box:
        raise DesktopError("Table pixels moved in the current capture; rediscover before selection.")
    native = _no_scrollbars(d, current)
    fresh = TableObservation(capture, current, old.scope, box, old.header_boxes, native)
    _query(ui, fresh, search_field, search_value)
    old_pixels = old.capture.image.crop(old.table_box).convert("RGB")
    new_pixels = capture.image.crop(box).convert("RGB")
    if old_pixels.size != new_pixels.size or ImageChops.difference(old_pixels, new_pixels).getbbox() is not None:
        raise DesktopError("Visible table pixels changed after readback; reread before selecting.")
    return fresh


def read_complete_table(ui, headers, scope=None, *, search_field=None, search_value=None,
                        timeout=8, interval=.2, cell_readers=None):
    """Read two stable tables, requiring native evidence of absent scrollbars.

    ``search_field`` is the visible label (for example ``Search:``); when given,
    its exact ``search_value`` is checked on both captures. Returns CompleteTable
    with ``.rows``/``.readback`` and private native/capture observations. Evidence
    is JSON; the CompleteTable object itself is intentionally not serialized.
    """
    if (not math.isfinite(timeout) or not 0 < timeout <= 60
            or not math.isfinite(interval) or not 0 < interval <= timeout):
        raise ValueError("Table polling requires 0 < interval <= timeout <= 60 seconds.")
    if (search_field is None) != (search_value is None):
        raise ValueError("Supply both search_field and exact search_value, or neither.")
    if search_field is not None and (not isinstance(search_field, str) or not search_field.strip()
                                     or not isinstance(search_value, str)):
        raise ValueError("Table search label/value must be text with a nonempty label.")
    history = []
    try:
        return _read_stable(ui, headers, scope, search_field, search_value, timeout, interval,
                            history, cell_readers=cell_readers)
    except DesktopError as exc:
        evidence = {"status": "review_required", "error": str(exc), "observations": history,
                    "two_equal_observations": False, "complete_native_view": False}
        exc.table_adapter_evidence_path = str(_write_summary(ui, evidence))
        raise


def _write_summary(ui, evidence):
    path = ui.evidence_dir / f"complete-table-{uuid4().hex[:12]}.json"
    evidence["report"] = str(path)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _read_stable(ui, headers, scope, search_field, search_value, timeout, interval, history,
                 *, cell_readers=None):
    deadline = time.monotonic() + timeout
    previous = None
    while True:
        if time.monotonic() >= deadline:
            raise DesktopError("Timed out waiting for two stable, complete table observations.")
        current = observe_table(ui, headers, scope)
        query = _query(ui, current, search_field, search_value)
        entry = {"native": current.native_evidence, "initial_native": current.native_evidence,
                 "table_box": list(current.table_box),
                 "query": query, "capture": current.capture.metadata(),
                 "post_read_guard_passed": False}
        history.append(entry)
        options = {"observed_table_box": current.table_box}
        if cell_readers is not None:
            options["cell_readers"] = cell_readers
        readback = read_table(current.capture.image, current.scope, headers, ui.evidence_dir, **options)
        entry["readback_report"] = readback["evidence"]["report"]
        current = _refresh_observation(ui, current, "after-table-read", search_field, search_value)
        entry.update(native=current.native_evidence, refreshed_capture=current.capture.metadata(),
                     post_read_guard_passed=True)
        # Readiness is bounded between OCR calls. A single OCR process has its
        # own finite timeout; its runtime can make this wall-clock budget overrun.
        if time.monotonic() >= deadline:
            raise DesktopError("Table OCR exceeded the readiness deadline; completeness was not accepted.")
        signature = _signature(readback)
        if previous is not None and _same_table(ui, previous[0], current) and previous[2] == signature:
            evidence = {"status": "read", "observations": history,
                        "two_equal_observations": True, "complete_native_view": True,
                        "limitations": ["No scrolling or column resizing is implemented.",
                                        "Equal observations do not prove all asynchronous work has finished.",
                                        "A row click does not prove its selection or subsequent persistence."]}
            path = _write_summary(ui, evidence)
            readback["evidence"]["native_completeness_report"] = str(path)
            return CompleteTable(current, readback, evidence, signature, search_field, search_value)
        previous = current, readback, signature
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def rowselect(ui, result, row, *, clicks=1):
    """Dispatch one row click only while its exact observed table is unchanged."""
    if not isinstance(result, CompleteTable):
        raise DesktopError("Row selection requires a complete table observation.")
    if clicks not in (1, 2) or isinstance(clicks, bool):
        raise ValueError("Table row selection supports one click or one double click.")
    if not any(row is candidate for candidate in result.rows):
        raise DesktopError("Selected row must be the exact row object from this table readback.")
    if _signature(result.readback) != result._signature:
        raise DesktopError("Table readback was modified after observation; selection refused.")
    d, old = ui.desktop, result.observation
    fresh = _refresh_observation(ui, old, "before-row-selection", result.search_field, result.search_value)
    capture, current = fresh.capture, fresh.area
    if current.enabled is False:
        raise DesktopError("Native table is explicitly disabled; row selection refused.")
    row_box = _valid_box(row["row_box"], fresh.table_box, "Selected row")
    # The native table can extend far beyond its populated columns. SWT does
    # not necessarily open a record when its blank filler is double-clicked.
    # Ground selection in a requested, actually read cell of this exact row.
    if not row.get("cell_boxes"):
        raise DesktopError("Selected row has no observed requested cell.")
    cell_box = _valid_box(next(iter(row["cell_boxes"].values())), row_box, "Selected row cell")
    point = capture.screen_point((cell_box[0] + cell_box[2]) / 2,
                                 (cell_box[1] + cell_box[3]) / 2)
    hit, resolution = _table_at(ui, capture, point)
    if (not d.cf.CFEqual(hit.element, current.element) or hit.bounds != current.bounds
            or hit.enabled is False):
        raise DesktopError("The current row point does not belong to the exact observed native table.")
    d.click(capture, *point, clicks=clicks)
    ui.actions.append({"action": "table_row_click", "values": dict(row["values"]),
                       "row_box": list(row_box), "cell_box": list(cell_box), "clicks": clicks,
                       "native": fresh.native_evidence, "hit_resolution": resolution,
                       "selection_verified": False})


def fit_columns(ui, expected_values, *, scope, headers):
    """Fit known identity/value columns using observed glyphs and current rules.

    Widths are presentation settings, never record values. Shrink available
    short-value columns first, then widen long-value columns. Every boundary is
    rediscovered after the preceding drag; overflow or uncertain geometry stops.
    """
    from .ocr import ocr_tokens
    from .ui_table import _header_geometry

    if (not isinstance(expected_values, dict) or not expected_values
            or any(key not in headers or not isinstance(value, str)
                   for key, value in expected_values.items())):
        raise DesktopError("Column fitting needs textual expected values under requested headers.")

    def geometry():
        capture = ui.snapshot("before-column-fit")
        region = ui.scope(capture, scope)
        region = tuple(map(int, region or (0, 0, *capture.image.size)))
        crop = capture.image.crop(region)
        observed = _header_geometry(crop, ocr_tokens(crop, psm=11), headers)
        header_scope = (region[0], region[1] + observed["top"],
                        region[2], region[1] + observed["bottom"])
        header_tokens = (_dark_header_tokens(ui, capture, header_scope, observed)
                         if observed.get("dark_neutral_header") else None)
        columns = {}
        for name in expected_values:
            label = _header_label(ui, capture, name, headers, header_scope, tokens_override=header_tokens)
            center = (label[0] + label[2]) / 2 - region[0]
            candidates = [(left, right) for left, right in observed["columns"]
                          if left <= center < right]
            if len(candidates) != 1:
                raise DesktopError(f"Cannot establish one column boundary for {name!r}.")
            columns[name] = candidates[0]
        return capture, region, observed, columns

    _, _, observed, columns = geometry()
    font = observed["font"]
    targets = {name: math.ceil(max(len(name), len(value)) * font * .8 + font * 4)
               for name, value in expected_values.items()}
    names = sorted(targets, key=lambda name: targets[name] -
                   (columns[name][1] - columns[name][0]))
    for name in names:
        capture, region, observed, columns = geometry()
        left, right = columns[name]
        width, target = right - left, targets[name]
        if abs(width - target) <= 4:
            continue
        x = region[0] + right
        y = region[1] + (observed["top"] + observed["bottom"]) / 2
        end = x + target - width
        if not region[0] < end < region[2]:
            raise DesktopError("The fitted column boundary would leave its visible semantic scope.")
        hit = ui.desktop.hit_test(capture, *capture.screen_point(x, y))
        if hit.enabled is False:
            raise DesktopError("The current header boundary is explicitly disabled.")
        ui.desktop.drag(capture, *capture.screen_point(x, y), *capture.screen_point(end, y))
        _, _, _, changed = geometry()
        actual_width = changed[name][1] - changed[name][0]
        if abs(actual_width - target) > 4:
            raise DesktopError("Column drag did not produce its expected visible width; do not repeat blindly.")
        ui.actions.append({"action": "fit_table_column", "header": name,
                           "width_before_pixels": width, "requested_width_pixels": target,
                           "observed_width_pixels": actual_width, "passed": True})
