"""Collect actual document content through the demonstrated English/EUR UI.

The source supplies occurrence identities and comparisons. It never supplies an
observed field. This narrow reader does not edit, resize, retry, scroll or save.
"""

import json
import re
from decimal import Decimal
from uuid import uuid4

import numpy as np
from PIL import ImageChops

from .desktop import DesktopError
from .document_sequence import parse_document_date
from .document_workflow import read_addresses, read_document_header, read_document_totals
from .models import ValidatedOrder
from .native_items import (
    _observe_item, _same_window, read_item_cell, read_painted_item_cell, visible_row_bands,
)
from .ocr import ocr_tokens, token_evidence
from .ui_table import _foreground
from .ui_tables import _no_scrollbars
from .workflow_report import content_expectations
from .workflow_rules import WorkflowReview


def _decimal(text, *, field, positive=False):
    if not isinstance(text, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text):
        raise DesktopError(f"Unsupported English decimal in {field}: {text!r}.")
    value = Decimal(text)
    if positive and value <= 0:
        raise DesktopError(f"The observed {field} must be positive.")
    return value


def _normalized(value):
    return format(value.normalize(), "f") if value else "0"


def parse_item_money(text):
    """Inline U.Price can omit decimal places; never guess separators/currency."""
    if not isinstance(text, str):
        raise DesktopError("Item money must be readable English EUR text.")
    match = re.fullmatch(r"(?:EUR|€)\s*((?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?)", text.strip())
    if match is None:
        raise DesktopError(f"Unsupported English EUR item amount: {text!r}.")
    return f"{Decimal(match[1]):.2f}"


def parse_item_discount(text):
    """Fakturama stores a reduction as a nonpositive signed percentage."""
    if not isinstance(text, str):
        raise DesktopError("Item Discount must be readable signed percentage text.")
    match = re.fullmatch(r"(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?)\s*%", text.strip())
    if match is None:
        raise DesktopError(f"Unsupported item Discount: {text!r}.")
    value = Decimal(match[1])
    if not Decimal("-100") <= value <= 0:
        raise DesktopError("Item Discount must be a zero or negative reduction up to 100%.")
    return _normalized(-value)


def parse_painted_vat(text):
    """Read the converter's complete Name (percentage) display, retaining Name."""
    if not isinstance(text, str):
        raise DesktopError("Painted VAT must be readable complete text.")
    match = re.fullmatch(r"([^\r\n()]+?)\s+\(((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)%\)", text.strip())
    if match is None or not match[1].strip():
        raise DesktopError(f"Unsupported or incomplete painted VAT: {text!r}.")
    value = Decimal(match[2])
    if not 0 <= value <= 100:
        raise DesktopError("Painted VAT percentage is outside 0–100%.")
    return {"name": match[1].strip(), "vat_percent": _normalized(value)}


def parse_four_line_address(text):
    """Interpret only the approved recipient/street/ZIP city/country display."""
    if not isinstance(text, str):
        raise DesktopError("Document address must be readable text.")
    lines = text.split("\n")
    if len(lines) != 4 or any(not line or line != line.strip() or "\r" in line for line in lines):
        raise DesktopError("Document address must contain four complete, unpadded source lines.")
    match = re.fullmatch(r"([A-Za-z0-9-]+) (\S(?:.*\S)?)", lines[2])
    if match is None:
        raise DesktopError("The observed address needs an unambiguous one-token ZIP followed by city.")
    return {"recipient": lines[0], "street": lines[1], "postal_code": match[1],
            "city": match[2], "country": lines[3]}


def _row_has_content(image, box, *, column_rules=()):
    """Ignore only continuous grid rules; unreadable content still counts as ink."""
    left, top, right, bottom = map(round, box)
    if right - left < 5 or bottom - top < 5:
        raise DesktopError("A visible item row fragment is too small to establish emptiness.")
    crop = image.crop((left + 1, top + 1, right - 1, bottom - 1)).convert("RGB")
    pixels = np.asarray(crop).astype(int)
    ink = _foreground(crop)
    neutral = pixels.max(axis=2) - pixels.min(axis=2) <= 4
    grey = neutral & (pixels.mean(axis=2) < 225)
    # Glyph stems cannot span the full row's height in this bounded grid.
    ink[:, grey.mean(axis=0) >= .85] = False
    # The final partial row's rules can stop at the closing frame. Project
    # only the same grey column edges observed across the preceding full row.
    for start, stop in column_rules:
        x0, x1 = max(0, start - left - 1), min(ink.shape[1], stop - left - 1)
        if x0 < x1:
            ink[:, x0:x1] &= ~grey[:, x0:x1]
    # Thin horizontal rules and a uniform closing frame are not row content.
    ink[ink.mean(axis=1) >= .85, :] = False
    return bool(ink.any())


def _column_rules(image, box):
    left, top, right, bottom = map(round, box)
    pixels = np.asarray(image.crop((left + 1, top + 1, right - 1, bottom - 1)).convert("RGB")).astype(int)
    grey = (pixels.max(axis=2) - pixels.min(axis=2) <= 4) & (pixels.mean(axis=2) < 225)
    positions = np.flatnonzero(grey.mean(axis=0) >= .85) + left + 1
    groups = []
    for position in positions.tolist():
        if groups and position == groups[-1][-1] + 1:
            groups[-1].append(position)
        else:
            groups.append([position])
    return [(group[0], group[-1] + 1) for group in groups]


def _row_coverage(ui, title, expected_count):
    """Prove the visible occupied prefix, source ordinals and blank remainder."""
    observation = _observe_item(ui, "Qty.", title, 0)
    d = ui.desktop
    native = _no_scrollbars(d, observation.table)
    image, boxes = observation.capture.image, observation.boxes
    bands = visible_row_bands(image, boxes, observation.scope[3])
    left = max(observation.scope[0], observation.table_box[0])
    right = min(observation.scope[2], observation.table_box[2])
    occupied = [_row_has_content(image, (left, low, right, high)) for low, high in bands]
    count = sum(occupied)
    if occupied != [True] * count + [False] * (len(bands) - count):
        raise DesktopError("Nonempty content follows a blank item row; occurrence order is ambiguous.")
    if count == len(bands):
        raise DesktopError("All complete visible item rows are occupied; a blank remainder is required.")
    tail = (left, bands[-1][1], right, observation.scope[3])
    closing_column_rules = _column_rules(image, (left, bands[-1][0], right, bands[-1][1]))
    if 0 < tail[3] - tail[1] < 5:
        raise DesktopError("The last item row fragment is too short to establish emptiness.")
    if tail[3] - tail[1] >= 5 and _row_has_content(image, tail, column_rules=closing_column_rules):
        raise DesktopError("Content remains in the partial item row below the complete visible rows.")
    if count != expected_count:
        raise WorkflowReview("document_item_count_mismatch",
                             "Actual occupied document rows differ from the source row count.",
                             expected=expected_count, observed=count, occupied_rows=occupied)
    ordinals = []
    font = boxes["Qty."][3] - boxes["Qty."][1]
    for index, (low, high) in enumerate(bands[:count]):
        crop = image.crop((round(left), low + 1, round(boxes["Qty."][0] - font), high - 1))
        path = ui.evidence_dir / f"content-ordinal-{ui.sequence:04d}-{index}.png"
        crop.save(path)
        tokens = ocr_tokens(crop, psm=7)
        if (len(tokens) != 1 or tokens[0].text != str(index + 1)
                or tokens[0].confidence < 85):
            raise DesktopError("The actual row ordinal does not prove its source occurrence.")
        ordinals.append({"row_index": index, "ordinal": tokens[0].text,
                         "tokens": token_evidence(tokens), "crop": str(path)})
    fresh = ui.snapshot("after-document-row-coverage", ocr=False)
    _same_window(d, observation.capture, fresh)
    current = d._control(observation.table.element)
    if current.role != "AXScrollArea" or current.bounds != observation.table.bounds:
        raise DesktopError("The native item table changed during complete row-count proof.")
    area = (round(left), bands[0][0], round(right), round(observation.scope[3]))
    if ImageChops.difference(image.crop(area), fresh.image.crop(area)).getbbox() is not None:
        raise DesktopError("The visible item rows changed during row-count proof.")
    d.guard(fresh)
    return {"passed": True, "observed_count": count, "expected_count": expected_count,
            "visible_row_bands": [list(band) for band in bands], "occupied_rows": occupied,
            "blank_remaining_complete_rows": len(bands) - count,
            "partial_tail_checked": tail[3] - tail[1] >= 5,
            "partial_tail_column_rules": [list(span) for span in closing_column_rules],
            "ordinals": ordinals, "native": native,
            "capture": observation.capture.metadata(), "capture_path": observation.capture_path}


def adapt_document_content(raw, *, invoice=False):
    """Build the report observation from actual reads; no source argument exists."""
    header, summary = raw["header"], raw["summary"]
    observed = {"number": header["number"],
                "order_date": parse_document_date(header["order_date"] if invoice else header["date"]),
                "external_reference": header["reference"],
                "billing": parse_four_line_address(raw["addresses"]["billing"]),
                "delivery": parse_four_line_address(raw["addresses"]["delivery"]),
                "items": [], "totals": {}, "currency": "EUR",
                "overall_discount_percent": _normalized(_decimal(
                    summary["overall_discount_percent"], field="overall Discount")),
                "shipping_net": _normalized(_decimal(summary["shipping_net"], field="Shipping")),
                "price_mode": header["price_mode"], "vat_mode": header["vat_mode"]}
    for row in raw["items"]:
        vat = parse_painted_vat(row["VAT"]["text"])
        observed["items"].append({"sku": row["Item No."], "description": row["Description"],
                                  "quantity": _normalized(_decimal(row["Qty."], field="Qty.", positive=True)),
                                  "unit_net": parse_item_money(row["U.Price"]),
                                  "vat_percent": vat["vat_percent"],
                                  "discount_percent": parse_item_discount(row["Discount"]),
                                  "line_net": parse_item_money(row["Price"]["text"])})
    for key in ("net", "vat", "gross"):
        value = _decimal(summary[key], field="Total " + key)
        if value != value.quantize(Decimal("0.01")):
            raise DesktopError("The observed document total has unsupported sub-cent precision.")
        observed["totals"][key] = f"{value:.2f}"
    return observed


def collect_document_content(ui, order, *, title, retained_number, invoice=False):
    """Read once, compare every prescribed field and retain a durable raw report."""
    if (not isinstance(order, ValidatedOrder) or type(invoice) is not bool
            or not isinstance(title, str) or not title.strip()
            or not isinstance(retained_number, str) or not retained_number.strip()):
        raise ValueError("Content collection requires validated source and exact editor/number.")
    if order.currency != "EUR" or not order.items:
        raise WorkflowReview("document_content_format_unsupported", "Only nonempty English EUR documents are supported.")
    optional = ("additional_name", "address_specification", "district")
    if (order.shipping_vat_percent is not None
            or any(getattr(address, key) is not None for address in
                   (order.customer.billing, order.customer.delivery) for key in optional)):
        raise WorkflowReview("document_content_format_unsupported",
                             "Additional address lines or Shipping VAT need a demonstrated reader.")
    result = {"passed": False, "scope": "actual prescribed document content; no Save",
              "raw": {}, "evidence": {}}
    action_start, discovery_start = len(ui.actions), len(ui.discoveries)
    path = ui.evidence_dir / ("document-content-" + uuid4().hex[:12] + ".json")
    result["report"] = str(path)
    try:
        header = read_document_header(ui, title=title, invoice=invoice)
        result["raw"]["header"] = header
        if header["number"] != retained_number or header["reference"] != order.external_reference:
            raise DesktopError("Document content collection found another number or source reference.")
        result["raw"]["addresses"] = read_addresses(ui, title=title)
        result["evidence"]["initial_row_coverage"] = _row_coverage(ui, title, len(order.items))
        result["raw"]["items"] = []
        for index, item in enumerate(order.items):
            common = {"row_index": index, "expected_sku": item.sku, "title": title}
            row = {"source_index": index}
            result["raw"]["items"].append(row)
            for label in ("Item No.", "Qty.", "Name", "Description", "U.Price", "Discount"):
                row[label] = read_item_cell(ui, header=label, **common)
            for label in ("VAT", "Price"):
                row[label] = read_painted_item_cell(ui, header=label, **common)
        result["raw"]["summary"] = read_document_totals(ui, title=title, currency="EUR")
        result["evidence"]["final_row_coverage"] = _row_coverage(ui, title, len(order.items))
        final_header = read_document_header(ui, title=title, invoice=invoice)
        result["raw"]["final_header"] = final_header
        if final_header != header:
            raise DesktopError("The actual document header changed during content collection.")
        observed = adapt_document_content(result["raw"], invoice=invoice)
        result["observed"] = observed
        kind = "invoice" if invoice else "order"
        expected = content_expectations({"order": order.model_dump(mode="json"),
                                         "generated_numbers": {kind: retained_number}})[kind + "_content"]
        result["expected"] = expected
        if observed != expected:
            raise WorkflowReview("document_content_mismatch",
                                 "Actual document content differs from the validated source.",
                                 expected=expected, observed=observed, report=str(path))
        result["passed"] = True
        return result
    except Exception as error:
        result["error"] = str(error)
        error.document_content_evidence = str(path)
        raise
    finally:
        result["evidence"]["ui_actions"] = ui.actions[action_start:]
        result["evidence"]["ui_discoveries"] = ui.discoveries[discovery_start:]
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
