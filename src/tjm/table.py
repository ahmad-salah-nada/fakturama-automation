"""Read the supported printed, dark-header item table from observed geometry.

The input contract requires an ITEMS label, a ruled table with readable English
headings and numbered rows, followed by NET TOTAL. Pixel rules find this table;
they are not application coordinates or a vendor-independent document parser.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .errors import ExtractionError
from .ocr import Token, group_lines, line_text, ocr_tokens, token_evidence


HEADER_FIELDS = {
    "#": "row_number",
    "sku": "sku",
    "description": "description",
    "qty": "quantity",
    "unit": "unit",
    "unit net eur": "unit_net",
    "disc": "discount_percent",
    "vat": "vat_percent",
    "line net eur": "line_net",
}
REQUIRED_FIELDS = {
    "row_number", "sku", "description", "quantity", "unit_net",
    "discount_percent", "vat_percent", "line_net",
}
NUMERIC_FIELDS = {
    "quantity", "unit_net", "discount_percent", "vat_percent", "line_net",
}


def _runs(indices):
    """Return half-open intervals for adjacent observed pixels."""
    result = []
    for value in indices:
        value = int(value)
        if result and value == result[-1][1]:
            result[-1][1] = value + 1
        else:
            result.append([value, value + 1])
    return result


def _anchor(tokens, words):
    matches = []
    for line in group_lines(tokens):
        for index in range(len(line) - len(words) + 1):
            selected = line[index:index + len(words)]
            if [t.text.upper() for t in selected] == list(words):
                matches.append(selected)
    if len(matches) != 1:
        raise ExtractionError(
            f"Expected one {' '.join(words)} table anchor; found {len(matches)}.",
            code="unsupported_table", field="items",
        )
    return matches[0]


def _header_geometry(image, tokens):
    items = _anchor(tokens, ("ITEMS",))
    totals = _anchor(tokens, ("NET", "TOTAL"))
    start = max(t.bottom for t in items)
    limit = min(t.top for t in totals)
    if start >= limit:
        raise ExtractionError("ITEMS must precede NET TOTAL.",
                              code="unsupported_table", field="items")
    rgb = np.asarray(image.convert("RGB"))
    dark = rgb[start:limit].max(axis=2) < 100
    bands = _runs(np.flatnonzero(dark.mean(axis=1) > .5))
    if len(bands) != 1:
        raise ExtractionError("Expected one dark table header between ITEMS and NET TOTAL.",
                              code="unsupported_table", field="items")
    top, bottom = (start + v for v in bands[0])
    header = rgb[top:bottom]
    extent = np.flatnonzero((header.max(axis=2) < 100).mean(axis=0) > .5)
    if not len(extent):
        raise ExtractionError("The table header has no measurable extent.",
                              code="unsupported_table", field="items")
    left, right = int(extent[0]), int(extent[-1]) + 1
    # Light vertical rules span the header. Letters occupy only part of its height.
    separators = _runs(np.flatnonzero(
        (header.min(axis=2) > 150).mean(axis=0)[left:right] > .85
    ) + left)
    boundaries = [left] + [round((a + b - 1) / 2) for a, b in separators] + [right]
    boundaries = sorted(set(boundaries))
    if len(boundaries) < 3:
        raise ExtractionError("Column rules are missing or unreadable.",
                              code="unsupported_table", field="items")
    # Follow the same rules below the header to find the real table bottom.
    x_points = boundaries[1:-1]
    pixels = rgb[bottom:limit, x_points].astype(np.int16)
    gray_rule = ((pixels.min(axis=2) > 120) & (pixels.max(axis=2) < 245)
                 & ((pixels.max(axis=2) - pixels.min(axis=2)) < 60))
    present = gray_rule.mean(axis=1) > .5
    missing_runs = _runs(np.flatnonzero(~present))
    ending = next((a for a, b in missing_runs if b - a >= 3), None)
    if ending is None or ending < bottom - top:
        raise ExtractionError("The ruled table body cannot be bounded.",
                              code="unsupported_table", field="items")
    return boundaries, top, bottom, bottom + ending


def _crop_read(image, box, path, *, invert=False, psm=6, scale=None):
    crop = image.crop(box).convert("RGB")
    crop.save(path)
    prepared = crop.convert("L")
    if invert:
        prepared = ImageOps.invert(prepared)
    scale = scale or (3 if invert else 2)
    prepared = prepared.resize((prepared.width * scale, prepared.height * scale),
                               Image.Resampling.LANCZOS)
    padding = 15
    prepared = ImageOps.expand(prepared, border=padding, fill="white")
    tokens = ocr_tokens(prepared, psm=psm)
    lines = group_lines(tokens)
    text = " ".join(line_text(line) for line in lines).strip()
    # The raw OCR coordinates are retained with their transform, rather than
    # pretending that crop coordinates already refer to the source image.
    return text, {
        "raw_text": text,
        "source_box": list(box),
        "crop": str(path),
        "ocr_tokens": token_evidence(tokens),
        "ocr_transform": {"scale": scale, "padding": padding},
        "minimum_confidence": min((t.confidence for t in tokens), default=None),
    }


def _header_key(text):
    return re.sub(r"[^a-z0-9#]+", " ", text.lower()).strip()


def _percentage_characters(image, box, path):
    """Observe isolated glyphs, preserving explicit unknown-rate punctuation.

    Whole-word OCR can confidently read ?% as 2%. Isolated-character OCR is an
    additional observation, not a VAT guess from the totals or a character map.
    It also resolves an observed T% reading when the isolated glyph reads 7.
    """
    crop = image.crop(box).convert("L")
    dark = np.asarray(crop) < 150
    columns = _runs(np.flatnonzero(dark.any(axis=0)))
    readings = []
    for index, (left, right) in enumerate(columns):
        rows = np.flatnonzero(dark[:, left:right].any(axis=1))
        char_box = (box[0] + left, box[1] + int(rows[0]),
                    box[0] + right, box[1] + int(rows[-1]) + 1)
        # A small character needs less enlargement than a complete table header.
        text, evidence = _crop_read(
            image, char_box, path.with_name(path.stem + f"-char-{index}.png"),
            psm=10, scale=2,
        )
        readings.append(evidence)
    return readings


def _row_centers(image, boundaries, body_top, body_bottom, row_column):
    left, right = boundaries[row_column:row_column + 2]
    # The ordinal column is mandatory in this format. Its ink finds row centers
    # even if whole-page OCR merges a row or misreads another cell.
    padding = max(2, round((right - left) * .08))
    rgb = np.asarray(image.crop((left + padding, body_top,
                                 right - padding, body_bottom)).convert("RGB"))
    ink_rows = np.flatnonzero((rgb.max(axis=2) < 130).any(axis=1))
    bands = _runs(ink_rows)
    # Antialiased glyphs can contain a one-pixel horizontal gap.
    merged = []
    for start, end in bands:
        if merged and start - merged[-1][1] <= 2:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    centers = [body_top + (a + b) / 2 for a, b in merged if b - a >= 3]
    if not centers:
        raise ExtractionError("No visible numbered item row was found.",
                              code="unreadable", field="items")
    # A row whose ordinal is missing must not be silently absorbed into a
    # neighbor. Compare all body ink with the observed numbered text lines.
    body = np.asarray(image.crop((boundaries[0] + padding, body_top,
                                  boundaries[-1] - padding, body_bottom)).convert("RGB"))
    ink_bands = _runs(np.flatnonzero((body.max(axis=2) < 130).any(axis=1)))
    ordinal_height = max(b - a for a, b in merged)
    for start, end in ink_bands:
        if end - start < 3:
            continue
        center = body_top + (start + end) / 2
        tolerance = max(ordinal_height, end - start) * 1.3
        if min(abs(center - candidate) for candidate in centers) > tolerance:
            raise ExtractionError("A visible item text line has no readable row ordinal.",
                                  code="unreadable", field="items")
    return centers


def extract_items(image: Image.Image, tokens: list[Token], evidence_dir: Path):
    """Return every visible ordered row and its observed table/cell evidence."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    boundaries, top, body_top, body_bottom = _header_geometry(image, tokens)
    evidence = {
        "method": "labelled dark-header grid; per-cell local OCR",
        "table_box": [boundaries[0], top, boundaries[-1], body_bottom],
        "column_boundaries": boundaries, "headers": [], "rows": [],
    }
    columns = []
    inset = max(2, round((body_top - top) * .05))
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        text, cell = _crop_read(
            image, (left + inset, top + inset, right - inset, body_top - inset),
            evidence_dir / f"header-{index}.png", invert=True,
        )
        key = _header_key(text)
        field = HEADER_FIELDS.get(key)
        cell["field"] = field
        evidence["headers"].append(cell)
        if field is None or field in columns:
            raise ExtractionError(
                f"Unsupported or repeated item heading: {text!r}.",
                code="unsupported_table", field="items", evidence=evidence,
            )
        columns.append(field)
    if not REQUIRED_FIELDS.issubset(columns):
        raise ExtractionError("Required item column headings are missing.",
                              code="unsupported_table", field="items", evidence=evidence)
    centers = _row_centers(image, boundaries, body_top, body_bottom,
                           columns.index("row_number"))
    row_limits = [body_top] + [round((a + b) / 2) for a, b in zip(centers, centers[1:])]
    row_limits.append(body_bottom)
    items = []
    for index, (row_top, row_bottom) in enumerate(zip(row_limits, row_limits[1:])):
        row = {"unit": None}
        row_evidence = {"source_y": [row_top, row_bottom], "cells": {}}
        evidence["rows"].append(row_evidence)
        for column, field in enumerate(columns):
            left, right = boundaries[column:column + 2]
            box = (left + inset, row_top + inset, right - inset, row_bottom - inset)
            path = evidence_dir / f"row-{index}-{field}.png"
            text, cell = _crop_read(image, box, path, scale=3 if field == "row_number" else 2)
            row_evidence["cells"][field] = cell
            issue_field = f"items[{index}].{field}"
            if not text or "UNREADABLE" in text.upper():
                raise ExtractionError(f"Item {index + 1} {field} is unreadable.",
                                      field=issue_field, evidence=evidence)
            if field == "row_number":
                if not re.fullmatch(r"\d+", text) or int(text) != index + 1:
                    raise ExtractionError("Item ordinals must be readable and sequential.",
                                          field="items", evidence=evidence)
                continue
            if field in NUMERIC_FIELDS:
                # Removing printed units is transcription; repairing O/0, ?/7,
                # signs, decimal separators or missing numbers would be guessing.
                value = text
                if field in {"vat_percent", "discount_percent"}:
                    # Only one final marker belongs to a percentage. Do this
                    # before alternate OCR so malformed 1%9% cannot become 19.
                    if (text.count("%") > 1 or ("%" in text and not text.endswith("%"))
                            or "EUR" in text):
                        raise ExtractionError(
                            f"Item {index + 1} {field} has misplaced or repeated unit markers.",
                            code="invalid_numeric_unit", field=issue_field, evidence=evidence,
                        )
                    value = text.removesuffix("%").strip()
                    readings = _percentage_characters(image, box, path)
                    cell["character_readings"] = readings
                    if any("?" in reading["raw_text"] for reading in readings):
                        raise ExtractionError(
                            f"Item {index + 1} {field} contains an unreadable-rate marker.",
                            field=issue_field, evidence=evidence,
                        )
                    alternative = "".join(reading["raw_text"] for reading in readings)
                    if alternative.count("%") > 1 or "%" in alternative[:-1]:
                        raise ExtractionError(
                            f"Item {index + 1} {field} has misplaced or repeated unit markers.",
                            code="invalid_numeric_unit", field=issue_field, evidence=evidence,
                        )
                    confident = all((reading["minimum_confidence"] or 0) >= 60
                                    for reading in readings)
                    if (confident and re.fullmatch(r"\d+(?:\.\d+)?%", alternative)
                            and re.fullmatch(r"\d+(?:\.\d+)?", value)
                            and value != alternative[:-1]):
                        raise ExtractionError(
                            f"Item {index + 1} {field} has conflicting OCR readings.",
                            code="ambiguous_ocr", field=issue_field, evidence=evidence,
                        )
                    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
                        if confident and re.fullmatch(r"\d+(?:\.\d+)?%", alternative):
                            value = alternative[:-1]
                            cell["selected_reading"] = "isolated_characters"
                            cell["selected_text"] = alternative
                elif field in {"unit_net", "line_net"}:
                    value = text.removeprefix("EUR").strip()
                if not re.fullmatch(r"\d+(?:\.\d+)?", value):
                    raise ExtractionError(f"Item {index + 1} {field} is not a readable number.",
                                          field=issue_field, evidence=evidence)
                row[field] = value
            else:
                row[field] = text
            if (cell.get("selected_reading") != "isolated_characters"
                    and (cell["minimum_confidence"] or 0) < 60):
                raise ExtractionError(
                    f"Item {index + 1} {field} has low OCR confidence and needs review.",
                    code="low_confidence", field=issue_field, evidence=evidence,
                )
        items.append(row)
    evidence["row_count"] = len(items)
    return items, evidence
