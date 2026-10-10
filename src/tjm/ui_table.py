"""Read the current English SWT table from pixels without operating the desktop.

Columns come from the visible header rules, not remembered coordinates. Only the
requested, fully visible columns are returned. The result describes this visible
table: callers must separately establish that it has not been scrolled before
using an empty result as proof that a record does not exist.
"""

import json
import math
import re
import statistics
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image, ImageOps

from .desktop import DesktopError
from .errors import ExtractionError
from .ocr import group_lines, line_text, ocr_tokens, token_evidence


def _label(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _groups(indices):
    groups = []
    for value in indices:
        value = int(value)
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def _spans(mask, max_gap):
    groups = _groups(np.flatnonzero(mask))
    merged = []
    for group in groups:
        if merged and group[0] - merged[-1][1] - 1 <= max_gap:
            merged[-1][1] = group[-1] + 1
        else:
            merged.append([group[0], group[-1] + 1])
    return merged


def _contains_words(text, expected):
    words, target = _label(text).split(), _label(expected).split()
    return any(words[i:i + len(target)] == target for i in range(len(words)))


def _cell_error(message, record):
    error = DesktopError(message)
    error.table_cell_evidence = record
    raise error


def _foreground(image):
    pixels = np.asarray(image.convert("RGB"), dtype=np.int16)
    background = np.median(pixels.reshape(-1, 3), axis=0)
    return np.max(np.abs(pixels - background), axis=2) > 45


def _read_tokens(image, *, psm):
    tokens = ocr_tokens(image, psm=psm)
    for token in tokens:
        if (token.left < 0 or token.top < 0 or token.width <= 0 or token.height <= 0
                or token.right > image.width or token.bottom > image.height
                or not math.isfinite(token.confidence) or not 0 <= token.confidence <= 100):
            raise DesktopError("OCR returned invalid table token geometry or confidence.")
    return tokens


def _cell(image, box, path, *, header=False):
    """Read an isolated cell, retain its exact OCR and flag visible truncation."""
    left, top, right, bottom = box
    if right - left < 5 or bottom - top < 5:
        raise DesktopError("A table cell is too narrow or short to read reliably.")
    crop = image.crop(box).convert("RGB")
    crop.save(path)
    # A border keeps a short, right-aligned amount away from Tesseract's image edge.
    pixels = np.asarray(crop)
    background = np.median(pixels.reshape(-1, 3), axis=0)
    colored = np.max(background) - np.min(background) > 25
    dark_neutral = not colored and np.mean(background) < 200
    # Selected rows can have white text on blue. Preserve the source crop and
    # turn contrast against its observed solid background into ordinary black ink.
    contrast = colored or dark_neutral
    ocr_crop = Image.fromarray(np.where(_foreground(crop), 0, 255).astype("uint8")) if contrast else crop
    prepared = ImageOps.expand(ocr_crop.resize((crop.width * 3, crop.height * 3)), border=15,
                               fill="white")
    tokens = _read_tokens(prepared, psm=7)
    raw = line_text(tokens)
    record = {"box": list(box), "crop": str(path), "raw_text": raw,
              "tokens": token_evidence(tokens), "ocr_scale": 3, "ocr_border": 15,
              "preparation": ("solid color background contrast" if colored else
                              "solid neutral dark background contrast" if dark_neutral else "RGB source")}
    ink = _foreground(crop)
    # Rule fragments are excluded by the caller's inset. Blank cells have no ink.
    if ink.sum() == 0:
        return "", record
    if not tokens:
        _cell_error(f"A visible {'header' if header else 'cell'} contains unreadable content.", record)
    if any(not math.isfinite(t.confidence) or t.confidence < 70 for t in tokens):
        _cell_error(f"Low-confidence table {'header' if header else 'cell'}: {raw!r}.", record)
    if "..." in raw or "…" in raw or re.search(r"\.{2,}", raw):
        _cell_error(f"A table {'header' if header else 'cell'} is clipped: {raw!r}.", record)
    edge_ink = ink.copy()
    edge_ink[np.mean(edge_ink, axis=1) > .85] = False  # residual horizontal rule, not a glyph
    edge_ink[:, np.mean(edge_ink, axis=0) > .85] = False  # residual vertical rule
    if edge_ink[:, :1].any() or edge_ink[:, -1:].any():
        _cell_error(f"A table {'header' if header else 'cell'} touches its horizontal crop edge.", record)
    return raw, record


def _header_geometry(image, tokens, expected):
    candidates = []
    for line in group_lines(tokens):
        text = line_text(line)
        score = sum(_contains_words(text, name) for name in expected)
        if score >= min(len(expected), max(2, math.ceil(len(expected) * .6))):
            candidates.append((score, line))
    if not candidates:
        raise DesktopError("No sufficiently complete expected table header is visible.")
    score = max(candidate[0] for candidate in candidates)
    best = [line for points, line in candidates if points == score]
    if len(best) != 1:
        raise DesktopError("More than one table header matches the requested columns.")
    line = best[0]
    semantic = [t for t in line if re.search(r"[A-Za-z0-9]", t.text)]
    # OCR can absorb header/row rules into most word boxes after a column resize.
    # Use the shortest observed fifth (at least two boxes) as the glyph estimate;
    # absorbed boxes are taller than the visible font. Full-height separators are
    # still qualified below, so short glyphs do not themselves create columns.
    heights = sorted(t.height for t in semantic)
    font = statistics.median(heights[:max(2, math.ceil(len(heights) * .2))])
    center = statistics.median(t.cy for t in semantic)
    gray = np.asarray(image.convert("L"))
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    neutral = np.max(rgb, axis=2) - np.min(rgb, axis=2) < 12
    neutral_rule = (gray < 225) & neutral
    required = [t for t in semantic if any(_contains_words(t.text, word)
                for name in expected for word in name.split())]
    if not required:
        raise DesktopError("Expected header words have no observable geometry.")
    span_left, span_right = min(t.left for t in required), max(t.right for t in required)
    baseline = min(image.height - 1, max(0, round(center)))
    background = float(np.median(gray[baseline, span_left:span_right]))
    dark_header = 80 < background < 210 and np.mean(neutral[baseline, span_left:span_right]) > .55 and np.mean(
        np.abs(gray[baseline, span_left:span_right].astype(float) - background) <= 3) > .55
    if dark_header:
        # Inactive/selected SWT headers can be white text on neutral dark gray.
        # Their lighter gray rules contrast with that solid background. The
        # ordinary '<225' mask would count the entire background as a rule.
        neutral_rule = neutral & (gray > background + 12) & (gray < background + 55)
    runs = []
    rule_start = max(0, round(center))
    if len(expected) == 1:
        # A single short label can have connected glyph strokes spanning its
        # full width. Its lower frame must start below the observed glyphs;
        # the existing full-height separator checks still establish the table.
        glyphs = [token for token in required if token.height <= font * 1.25]
        if not glyphs:
            raise DesktopError("The header glyph band cannot be separated from its rules.")
        rule_start = max(rule_start, max(token.bottom for token in glyphs))
    for y in range(rule_start, min(image.height, round(center + font * 3))):
        for start, stop in _spans(neutral_rule[y], max_gap=max(1, round(font * .25))):
            if start <= span_left and stop >= span_right:
                runs.append((y, start, stop))
    if not runs:
        raise DesktopError("The visible header has no complete lower rule.")
    first_y = min(y for y, _, _ in runs)
    same_rule = [item for item in runs if item[0] <= first_y + max(2, round(font * .3))]
    bottom = max(y for y, _, _ in same_rule) + 1
    left = min(start for _, start, _ in same_rule)
    right = max(stop for _, _, stop in same_rule)
    strip_top = max(0, bottom - round(font * 3))
    mask = neutral_rule[strip_top:bottom, left:right]
    run = np.zeros(mask.shape[1], dtype=int)
    active = np.ones(mask.shape[1], dtype=bool)
    for row in mask[::-1]:
        active &= row
        run += active
    groups = _groups(np.flatnonzero(run >= font * 1.15))
    # Descenders can touch the lower rule and create a long-looking vertical
    # stroke inside "Company" or "type". A separator starts above the glyphs
    # and extends through the complete header height. Use the unabsorbed word
    # boxes to locate the glyph band, then retain full-height consistent runs.
    glyphs = [token for token in required if token.height <= font * 1.25]
    if not glyphs:
        raise DesktopError("The header glyph band cannot be separated from its rules.")
    glyph_top = min(token.top for token in glyphs)
    qualified = []
    for group in groups:
        height = int(run[group[len(group) // 2]])
        if bottom - height <= glyph_top - max(1, font * .15):
            qualified.append((group, height))
    if len(qualified) < 2:
        raise DesktopError("The header's column boundaries cannot be established.")
    header_height = statistics.median(height for _, height in qualified)
    qualified = [(group, height) for group, height in qualified if height >= header_height * .9]
    if len(qualified) < 2:
        raise DesktopError("Full-height header separators are not sufficiently consistent.")
    rules = [(left + group[0], left + group[-1] + 1) for group, _ in qualified]
    heights = [height for _, height in qualified]
    top = bottom - round(statistics.median(heights))
    if top < 1:
        raise DesktopError("The table header is clipped at the top of the supplied scope.")
    edges = [left] + [round((a + b) / 2) for a, b in rules] + [right]
    edges = sorted(set(edges))
    columns = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > font]
    return {"top": top, "bottom": bottom, "left": left, "right": right,
            "font": font, "columns": columns, "column_rules": rules,
            "header_background": background, "dark_neutral_header": bool(dark_header)}


def _selected_document_header(image, tokens, expected):
    """Selected Documents headers lose their separators; use full body rules.

    This route requires a caller-supplied native table boundary and the actual
    State/Total captions. It reads no expected cell data and does not invent a
    hidden separator. Every column boundary is a continuous observed body rule.
    """
    if expected != ['State', 'Total']:
        raise DesktopError('Selected-header geometry is restricted to Documents State/Total.')
    lines = [line for line in group_lines(tokens)
             if all(_contains_words(line_text(line), name) for name in expected)]
    if len(lines) != 1:
        raise DesktopError('Selected document header needs one actual State/Total baseline.')
    required = [t for t in lines[0] if any(_contains_words(t.text, name) for name in expected)]
    if len(required) != 2 or any(t.confidence < 85 for t in required):
        raise DesktopError('Selected document header captions are uncertain.')
    font = statistics.median(t.height for t in required)
    gray = np.asarray(image.convert('L'))
    rgb = np.asarray(image.convert('RGB'), dtype=int)
    neutral = rgb.max(axis=2) - rgb.min(axis=2) < 12
    baseline = round(statistics.median(t.cy for t in required))
    background = float(np.median(gray[baseline]))
    if not 80 < background < 210 or np.mean(np.abs(gray[baseline].astype(float) - background) < 3) < .55:
        raise DesktopError('A solid selected gray document header is not established.')
    bottom = None
    for y in range(max(t.bottom for t in required), min(image.height, round(baseline + font * 3))):
        if np.mean(np.abs(gray[y].astype(float) - background) > 10) > .5:
            bottom = y
            break
    if bottom is None or bottom + font * 4 >= image.height:
        raise DesktopError('Selected header has no complete observed body transition.')
    start = bottom + round(font * 3)
    mask = neutral[start:] & (gray[start:] < 225)
    groups = _groups(np.flatnonzero(mask.mean(axis=0) >= .95))
    rules = [(group[0], group[-1] + 1) for group in groups if len(group) <= max(4, font * .3)]
    if len(rules) < 4 or np.median(gray[bottom:, -1]) >= 245:
        raise DesktopError('Selected-header body columns lack complete native edge frames.')
    edges = [1] + [round((a + b) / 2) for a, b in rules] + [image.width]
    columns = [(a, b) for a, b in zip(edges, edges[1:]) if b - a > font]
    if any(sum(a < t.left < t.right < b for a, b in columns) != 1 for t in required):
        raise DesktopError('Actual selected-header captions do not align with unique body columns.')
    return {'top': 1, 'bottom': bottom, 'left': edges[0], 'right': edges[-1],
            'font': font, 'columns': columns, 'column_rules': rules,
            'header_background': background, 'dark_neutral_header': True,
            'selected_header_body_rules': True}


def _body_geometry(image, header, *, observed_bottom=None):
    gray = np.asarray(image.convert("L"))
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    body_color = (np.max(rgb, axis=2) - np.min(rgb, axis=2) > 25) & (gray > 60)
    body_background = (gray > 245) | body_color
    font, top = header["font"], header["bottom"]
    center = round((header["left"] + header["right"]) / 2)
    seed = None
    # Find the white body through its blank stripes; gray surroundings are distinct.
    for y in range(top, image.height):
        for left, right in _spans(body_background[y], max_gap=max(1, round(font * .3))):
            if (left <= center < right and left <= header["left"] + font
                    and right >= header["right"] - font
                    and right - left >= header["right"] - header["left"] - font):
                seed = (y, left, right)
                break
        if seed:
            break
    if not seed:
        raise DesktopError("No bounded white table body is visible below the header.")
    _, left, right = seed
    if left > header["left"] + font or right < header["right"] - font:
        raise DesktopError("The table body does not cover its visible columns.")
    nonwhite = 0
    max_gap = max(3, round(font * .6))
    bottom = None
    for y in range(seed[0], image.height):
        white = np.mean(body_background[y, left:right])
        if white < .7:
            nonwhite += 1
        else:
            nonwhite = 0
        if nonwhite >= max_gap:
            bottom = y - nonwhite + 1
            break
    used_native_boundary = False
    if bottom is None:
        # SWT's inset pane can end in a thick closing rule, a white bevel and
        # another rule. Unlike an ordinary grid line, the closing rule is thicker.
        runs = _groups(np.flatnonzero(np.mean(gray[top:, left:right] < 225, axis=1) > .94))
        widths = [len(run) for run in runs if len(run) <= font * .5]
        normal = statistics.median(widths) if widths else 0
        closing = [run for run in runs if len(run) >= max(3, normal * 1.5)
                   and image.height - (top + run[-1]) <= font * 1.5]
        if len(closing) == 1:
            bottom = top + closing[0][0]
        elif observed_bottom is not None:
            # The caller observed this exact table's complete AXScrollArea on
            # the same capture. Native bounds can close a pane whose footer is
            # shorter than the pixel-only contrast guard. They do not establish
            # scroll position or waive any row/cell readback checks.
            bottom = observed_bottom
            used_native_boundary = True
        else:
            raise DesktopError("The table body reaches the scope edge; hidden or clipped rows are possible.")
    # A horizontal scrollbar/gray thumb or another nonwhite region stops observation.
    # It must follow a substantial blank tail rather than silently hide more rows.
    return (left, top, right, bottom), used_native_boundary


def _row_boxes(image, header, body):
    gray = np.asarray(image.convert("L"))
    rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
    neutral_rule = (gray < 225) & (np.max(rgb, axis=2) - np.min(rgb, axis=2) < 12)
    left, top, right, bottom = body
    font = header["font"]
    rule_left, rule_right = max(left, header["left"]), min(right, header["right"])
    lines = _groups(np.flatnonzero(np.mean(neutral_rule[top:bottom, rule_left:rule_right], axis=1) > .94))
    rules = [(top + line[0], top + line[-1] + 1) for line in lines if len(line) <= font * .3]
    ruled = len(rules) >= 2 or (len(rules) == 1 and bottom - top <= (rules[0][0] - top) * 3 + font)
    if ruled:
        boundaries = [(top, top)] + rules + [(bottom, bottom)]
        boxes = [(left, a[1], right, b[0]) for a, b in zip(boundaries, boundaries[1:])
                 if b[0] - a[1] > font * .7]
        heights = [box[3] - box[1] for box in boxes]
        median = statistics.median(heights)
        # An incomplete last grid row is harmless only if it is entirely blank.
        for box in boxes:
            if box[3] - box[1] < median * .8 and _row_ink(image, box, header):
                raise DesktopError("The last visible table row is vertically clipped.")
        return boxes, "body rules"
    # Selector dialogs show column rules only in the header, with an unruled body.
    tokens = _read_tokens(image.crop(body), psm=11)
    observed = group_lines(tokens)
    row_height = header["bottom"] - header["top"]
    boxes = []
    for line in observed:
        if any(t.confidence < 0 for t in line):
            raise DesktopError("Invalid OCR confidence in a table row.")
        y0 = round(top + min(t.top for t in line) - font * .2)
        y1 = round(top + max(t.bottom for t in line) + font * .3)
        if y0 < top or y1 >= bottom:
            raise DesktopError("An unruled table row is clipped at its visible boundary.")
        if boxes and y0 < boxes[-1][3] - font * .2:
            raise DesktopError("Unruled table rows overlap or contain ambiguous wrapped text.")
        boxes.append((left, y0, right, y1))
    if observed:
        last_ink = max(top + t.bottom for line in observed for t in line)
        if bottom - last_ink < row_height:
            raise DesktopError("There is no blank tail below the visible selector rows.")
    # OCR can miss icons or damaged text. Any unassigned ink makes absence unsafe.
    assigned = np.zeros((bottom - top, right - left), dtype=bool)
    for _, y0, _, y1 in boxes:
        assigned[y0 - top:y1 - top] = True
    unknown = _foreground(image.crop(body)) & ~assigned
    # Selector rows can have a closing rule without a grid in the blank tail.
    # Remove only the thin observed straight rules and projected column edges.
    for y0, y1 in rules:
        unknown[y0 - top:y1 - top] = False
    for x0, x1 in header["columns"]:
        for edge in (x0, x1):
            start, stop = max(0, edge - left - 2), min(unknown.shape[1], edge - left + 2)
            unknown[:, start:stop] = False
    threshold = max(4, font / 2)
    if unknown.sum() > threshold:
        ys, xs = np.nonzero(unknown)
        error = DesktopError("Unassigned visible content remains in the selector body.")
        error.evidence = {
            "selector_unassigned_content": {
                "pixel_count": int(unknown.sum()), "threshold": threshold,
                "body_bounds": list(body),
                "content_bounds": [int(left + xs.min()), int(top + ys.min()),
                                   int(left + xs.max() + 1), int(top + ys.max() + 1)],
            }
        }
        raise error
    return boxes, "observed row baselines"


def _row_ink(image, box, header):
    left, top, right, bottom = box
    mask = _foreground(image.crop((left + 2, top + 2, right - 2, bottom - 2)))
    for x0, x1 in header["columns"]:
        for edge in (x0, x1):
            start, stop = max(0, edge - left - 4), min(mask.shape[1], edge - left + 2)
            mask[:, start:stop] = False
    return bool(mask.any())


def read_table(image: Image.Image, scope, expected_headers: list[str], evidence_dir: Path,
               *, observed_table_box=None, cell_readers=None) -> dict:
    """Return exact requested cell text from one bounded visible SWT table.

    This function never reads a database, accesses application internals, clicks,
    scrolls, changes column widths or claims that unseen rows are absent.
    ``observed_table_box`` is an optional absolute pixel rectangle obtained by
    the caller from this exact owned native table on the current capture. It
    does not assert that the table is unscrolled or has no scrollbar.
    A caller may request a single identity column (for example a Product SKU);
    the complete observed table frame and row coverage are still required.
    """
    if not isinstance(image, Image.Image):
        raise DesktopError("Table reading requires a PIL image.")
    if (not isinstance(expected_headers, list) or not expected_headers
            or any(not isinstance(name, str) or not _label(name) for name in expected_headers)
            or len({_label(name) for name in expected_headers}) != len(expected_headers)):
        raise DesktopError("Distinct nonempty expected table headers are required.")
    if (not isinstance(scope, (tuple, list)) or len(scope) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in scope)):
        raise DesktopError("A finite four-coordinate pixel scope is required.")
    left, top, right, bottom = map(int, scope)
    if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
        raise DesktopError("Table scope is outside the captured image.")
    supplied_scope = [left, top, right, bottom]
    if observed_table_box is not None:
        if (not isinstance(observed_table_box, (tuple, list)) or len(observed_table_box) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in observed_table_box)):
            raise DesktopError("The observed native table boundary requires four finite pixel coordinates.")
        nl, nt, nr, nb = observed_table_box
        if not (left <= nl < nr <= right and top <= nt < nb <= bottom):
            raise DesktopError("The observed native table boundary is not contained in the supplied scope.")
        left, top, right, bottom = map(int, observed_table_box)
        if right <= left or bottom <= top:
            raise DesktopError("The observed native table boundary has no positive pixel area.")
    folder = Path(evidence_dir) / f"table-{uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=False)
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    crop.save(folder / "scope.png")
    evidence = {"scope": supplied_scope, "read_scope": [left, top, right, bottom],
                "scope_crop": str(folder / "scope.png"),
                "native_boundary_supplied": observed_table_box is not None,
                "observed_table_box": list(observed_table_box) if observed_table_box is not None else None,
                "visible_only": True, "not_scrolled_requires_caller_check": True,
                "cells": [], "headers": [], "status": "review_required"}
    try:
        tokens = _read_tokens(crop, psm=11)
        evidence["scope_tokens"] = token_evidence(tokens)
        try:
            header = _header_geometry(crop, tokens, expected_headers)
        except DesktopError:
            if observed_table_box is None or expected_headers != ['State', 'Total']:
                raise
            header = _selected_document_header(crop, tokens, expected_headers)
        if observed_table_box is not None:
            tolerance = max(2, header["font"] * .4)
            # SWT's final header column has no upright rule. Its native right
            # edge instead has a gray frame beside the white body. A rectangle
            # cut through the middle of a column lacks that observed frame.
            edge = np.asarray(crop.convert("L"))[header["bottom"]:, -1]
            gray_right_frame = bool(len(edge) and np.median(edge) < 245)
            # Selector dialogs are borderless and can have white filler after a
            # complete final header separator. This is observable independently
            # of a gray outer frame; a cut through a column has no final rule at
            # the end of its header's lower bar.
            last_rule = max(right for _, right in header["column_rules"])
            gray_crop = np.asarray(crop.convert("L"))
            filler = gray_crop[header["bottom"]:, header["right"]:]
            complete_filler = bool(filler.size and np.median(filler) > 245
                                   and header["right"] - last_rule <= tolerance)
            # The Product selector paints an extra white, unlabelled filler
            # column after its final header separator. Its lower bar continues
            # through that filler. Every interior header pixel must be white;
            # a cut through a named gray header cannot satisfy this condition.
            header_filler = gray_crop[header["top"] + 2:header["bottom"] - 4, last_rule:]
            blank_header_filler = bool(header_filler.size and header_filler.shape[1] >= header["font"]
                                       and np.all(header_filler > 245))
            right_frame = gray_right_frame or complete_filler or blank_header_filler
            evidence["native_header_alignment"] = {
                "top": header["top"], "left": header["left"], "tolerance": tolerance,
                "gray_right_frame": gray_right_frame,
                "white_filler_after_complete_final_separator": complete_filler,
                "blank_white_header_filler_after_final_separator": blank_header_filler,
            }
            if (header["top"] > tolerance or header["left"] > tolerance
                    or header["right"] > crop.width or not right_frame
                    or crop.height - header["bottom"] < header["font"]):
                raise DesktopError("Visible header geometry contradicts the observed native table boundary.")
        selected = {}
        headers = []
        for index, (x0, x1) in enumerate(header["columns"]):
            box = (x0 + 3, header["top"] + 2, x1 - 3, header["bottom"] - 3)
            # Read every candidate header, but clipped optional columns are not
            # required for the caller's requested identity/readback fields.
            try:
                text, record = _cell(crop, box, folder / f"header-{index:02d}.png", header=True)
            except DesktopError as exc:
                record = getattr(exc, "table_cell_evidence", {"box": list(box)})
                evidence["headers"].append({**record, "error": str(exc)})
                continue
            evidence["headers"].append(record)
            for name in expected_headers:
                if _label(text) == _label(name):
                    if name in selected:
                        raise DesktopError(f"The required header {name!r} occurs more than once.")
                    selected[name] = (x0, x1)
                    headers.append({"text": name, "observed_text": text,
                                    "box": [left + x0, top + header["top"],
                                            left + x1, top + header["bottom"]]})
        missing = [name for name in expected_headers if name not in selected]
        if missing:
            raise DesktopError(f"Required table headers are missing, clipped or unreadable: {missing}.")
        body, native_used = _body_geometry(crop, header,
                                          observed_bottom=crop.height if observed_table_box is not None else None)
        evidence["native_boundary_used"] = native_used
        boxes, method = _row_boxes(crop, header, body)
        rows, blank_after_data = [], False
        for index, row_box in enumerate(boxes):
            if not _row_ink(crop, row_box, header):
                if rows:
                    blank_after_data = True
                continue
            if blank_after_data:
                raise DesktopError("A nonempty row follows an empty grid row; row order is ambiguous.")
            values, cell_boxes = {}, {}
            for name in expected_headers:
                x0, x1 = selected[name]
                box = (x0 + 1, row_box[1] + 2, x1 - 1, row_box[3] - 2)
                reader = (cell_readers or {}).get(name, _cell)
                text, record = reader(crop, box, folder / f"row-{index:03d}-{_label(name).replace(' ', '-')}.png")
                record.update(header=name, row=index)
                evidence["cells"].append(record)
                values[name] = text
                cell_boxes[name] = [left + n if i % 2 == 0 else top + n for i, n in enumerate(box)]
            rows.append({"values": values, "cell_boxes": cell_boxes,
                         "row_box": [left + row_box[0], top + row_box[1],
                                     left + row_box[2], top + row_box[3]]})
        if rows and method == "body rules" and not blank_after_data:
            raise DesktopError("Every visible grid row is occupied; hidden overflow is possible.")
        evidence.update(status="read", bounded_body=True, row_geometry=method,
                        visible_rows=len(rows), geometry=header,
                        report=str(folder / "readback.json"))
        result = {"headers": sorted(headers, key=lambda item: item["box"][0]), "rows": rows,
                  "table_box": [left + body[0], top + header["top"],
                                left + body[2], top + body[3]], "evidence": evidence}
        (folder / "readback.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    except (DesktopError, ExtractionError) as exc:
        if hasattr(exc, "table_cell_evidence"):
            evidence["cells"].append(exc.table_cell_evidence)
        evidence["error"] = str(exc)
        (folder / "readback.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        if isinstance(exc, ExtractionError):
            raise DesktopError(f"Table OCR did not complete: {exc}") from exc
        raise
