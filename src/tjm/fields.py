"""Read labeled English fields using the positions observed in the input image.

This intentionally supports the assignment's labeled form and ruled item table,
not arbitrary invoices. Missing labels and uncertain address structure stop the
extraction. No source values or sample pixel coordinates appear here.
"""

import re
import math
from pathlib import Path

from PIL import Image, ImageOps

from .errors import ExtractionError
from .ocr import Token, group_lines, line_text, ocr_tokens, token_evidence

LABELS = {
    "external_reference": "EXTERNAL REFERENCE", "order_date": "ORDER DATE",
    "source_customer_id": "CUSTOMER ID", "currency": "CURRENCY",
    "company": "COMPANY", "contact_name": "CONTACT NAME",
    "first_name": "FIRST NAME", "last_name": "LAST NAME",
    "alias": "CUSTOMER ALIAS", "email": "EMAIL", "telephone": "PHONE",
    "billing": "BILLING ADDRESS", "delivery": "DELIVERY ADDRESS",
    "method": "PAYMENT METHOD", "status": "PAID STATUS", "date": "PAYMENT DATE",
    "source_amount": "PAYMENT AMOUNT", "net": "NET TOTAL", "vat": "VAT TOTAL",
    "gross": "GROSS TOTAL", "overall_discount_percent": "OVERALL DISCOUNT",
    "shipping_net": "SHIPPING NET", "shipping_vat_percent": "SHIPPING VAT",
}
SECTIONS = ["ORDER", "CUSTOMER AND CONTACT", "ADDRESSES", "PAYMENT", "ITEMS"]


def normalized(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def find_phrases(tokens: list[Token], phrase: str) -> list[tuple[int, int, int, int]]:
    wanted = [normalized(word) for word in phrase.split()]
    found = []
    for line in group_lines(tokens):
        words = [normalized(token.text) for token in line]
        for index in range(len(words) - len(wanted) + 1):
            if words[index:index + len(wanted)] != wanted:
                continue
            selected = line[index:index + len(wanted)]
            # A phrase must be adjacent on the image as well as in token order.
            height = max(t.height for t in selected)
            if any(b.left - a.right > height * 4 for a, b in zip(selected, selected[1:])):
                continue
            found.append((min(t.left for t in selected), min(t.top for t in selected),
                          max(t.right for t in selected), max(t.bottom for t in selected)))
    return found


class FieldReader:
    def __init__(self, image: Image.Image, tokens: list[Token], evidence_dir: Path):
        self.image, self.tokens, self.evidence_dir = image, tokens, evidence_dir
        self.evidence = {}
        items = find_phrases(tokens, "ITEMS")
        if len(items) != 1:
            raise ExtractionError("One visible ITEMS heading is required.",
                                  code="missing_label", field="items")
        self.items_top = items[0][1]
        self.labels = {}
        for key, phrase in LABELS.items():
            candidates = find_phrases(tokens, phrase)
            if key in {"net", "vat", "gross"}:
                candidates = [box for box in candidates if box[1] > self.items_top]
            elif key not in {"overall_discount_percent", "shipping_net", "shipping_vat_percent"}:
                candidates = [box for box in candidates if box[1] < self.items_top]
            if len(candidates) > 1:
                raise ExtractionError(f"Multiple {phrase} labels require review.",
                                      code="ambiguous_label", field=key)
            if candidates:
                self.labels[key] = candidates[0]
        self.sections = [box for phrase in SECTIONS for box in find_phrases(tokens, phrase)]
        source_regions = [self.value_region(name) for name in self.labels
                          if name not in {"net", "vat", "gross", "shipping_net",
                                          "shipping_vat_percent", "overall_discount_percent"}]
        totals_top = min((self.labels[key][1] for key in ("net", "vat", "gross")
                          if key in self.labels), default=self.items_top)
        def source_value(token):
            return (self.items_top < token.cy < totals_top or any(
                box[0] <= token.cx <= box[2] and box[1] <= token.cy <= box[3]
                for box in source_regions))
        for word, key in [("SHIPPING", "shipping_net"), ("FREIGHT", "shipping_net"),
                          ("SURCHARGE", "shipping_net"), ("DISCOUNT", "overall_discount_percent")]:
            occurrences = [t for t in tokens if normalized(t.text) == word and not source_value(t)]
            known_boxes = [box for name, box in self.labels.items()
                           if name in {"shipping_net", "shipping_vat_percent", "overall_discount_percent"}]
            if any(not any(box[0] <= t.cx <= box[2] and box[1] <= t.cy <= box[3]
                           for box in known_boxes) for t in occurrences):
                raise ExtractionError("A source charge/discount label has unsupported wording; review it rather than use zero.",
                                      code="unsupported_charge", field=key)

    def value_region(self, key):
        x, y, right, bottom = self.labels[key]
        height = bottom - y
        pad = max(2, round(height * .4))
        peers = [(other, box) for other, box in self.labels.items()
                 if other != key and abs(box[1] - y) <= height * 1.5]
        if key in {"net", "vat", "gross"}:
            center = (x + right) / 2
            centers = sorted((box[0] + box[2]) / 2 for _, box in peers)
            left_centers = [c for c in centers if c < center]
            right_centers = [c for c in centers if c > center]
            left = round((max(left_centers) + center) / 2) if left_centers else 0
            edge = round((min(right_centers) + center) / 2) if right_centers else self.image.width
        else:
            left = max(0, x - pad)
            section_start = max((box[1] for box in self.sections if box[1] < y), default=0)
            section_end = min((box[1] for box in self.sections if box[1] > bottom),
                              default=self.image.height)
            next_columns = [box[0] for box in self.labels.values()
                            if section_start < box[1] < section_end and box[0] > right]
            edge = min(next_columns) - pad if next_columns else self.image.width
        below = [box[1] for other, box in self.labels.items()
                 if other != key and box[1] > bottom and left <= box[0] < edge]
        below += [box[1] for box in self.sections if box[1] > bottom]
        lower = min(below) - pad if below else self.image.height
        return (left, bottom + max(1, round(height * .15)), edge, lower)

    def read(self, key, *, field=None, required=True, address=False):
        field = field or key
        if key not in self.labels:
            if not required:
                self.evidence[field] = {"present": False, "reason": "label absent"}
                return None
            raise ExtractionError(f"The {LABELS[key]} label was not found.",
                                  code="missing_label", field=field)
        _, y, _, bottom = self.labels[key]
        height = bottom - y
        pad = max(2, round(height * .4))
        left, _, edge, lower = self.value_region(key)
        if not address:
            candidates = [t for t in self.tokens if left <= t.cx < edge
                          and bottom + height * .15 <= t.top < lower]
            lines = group_lines(candidates)
            relevant_lines = lines
            if key in {"net", "vat", "gross"} and lines:
                first_bottom = max(t.bottom for t in lines[0])
                value_height = max(t.height for t in lines[0])
                # Final totals are followed by a separate distant footer. Only
                # that final region needs a text-band bound. Nearby extra lines
                # still make the total ambiguous, rather than being truncated.
                relevant_lines = [line for line in lines if min(t.top for t in line)
                                  <= first_bottom + value_height * 3]
            if len(relevant_lines) > 1:
                full_box = self.value_region(key)
                path = self.evidence_dir / (field.replace(".", "-") + ".png")
                self.image.crop(full_box).save(path)
                self.evidence[field] = {"label_box": list(self.labels[key]),
                                        "value_box": list(full_box), "crop": str(path),
                                        "raw_text": "\n".join(line_text(line) for line in relevant_lines)}
                raise ExtractionError(f"Wrapped or multiple values under {LABELS[key]} require review.",
                                      code="ambiguous_layout", field=field)
            if lines:
                lower = min(lower, max(t.bottom for t in lines[0]) + pad * 2)
            else:
                # An optional blank field has no text band; avoid the card rule.
                lower = min(lower, round(bottom + height * 4.5))
        box = (left, bottom + max(1, round(height * .15)), edge, lower)
        if box[3] <= box[1] or box[2] <= box[0]:
            raise ExtractionError(f"The area under {LABELS[key]} is ambiguous.",
                                  field=field, code="ambiguous_layout")
        crop = self.image.crop(box)
        path = self.evidence_dir / (field.replace(".", "-") + ".png")
        crop.save(path)
        # A border separates touching rules from text. Upscaling uses observed
        # label glyph height, so moved/scaled inputs keep the same behavior.
        scale = max(2, math.ceil(28 / height))
        # The supported form uses dark text on pale cards. Test for dark ink
        # before OCR so an empty payment-date card is not treated as a letter.
        # Keep the original and resized images for review.
        ink = ImageOps.grayscale(crop).point(lambda value: 255 if value < 180 else 0)
        attempts = []
        for attempt_scale in [1, scale, scale + 1]:
            prepared = ImageOps.expand(crop.resize((crop.width * attempt_scale,
                                                    crop.height * attempt_scale),
                                                   Image.Resampling.LANCZOS), border=12, fill="white")
            prepared_path = path.with_stem(path.stem + f"-ocr-{attempt_scale}")
            prepared.save(prepared_path)
            trial = ocr_tokens(prepared, psm=6) if ink.getbbox() else []
            trial_lines = [line_text(line) for line in group_lines(trial)]
            trial_text = "\n".join(trial_lines).strip()
            attempts.append({"raw_text": trial_text, "scale": attempt_scale,
                             "ocr_image": str(prepared_path), "ocr_tokens": token_evidence(trial),
                             "minimum_confidence": min((t.confidence for t in trial), default=None)})
            matches = [a for a in attempts if a["raw_text"] == trial_text
                       and (a["minimum_confidence"] is None or a["minimum_confidence"] >= 60)]
            if len(matches) >= 2:
                cropped_tokens = trial
                break
        else:
            self.evidence[field] = {"label_box": list(self.labels[key]), "value_box": list(box),
                                    "crop": str(path), "ocr_attempts": attempts}
            raise ExtractionError(f"OCR readings under {LABELS[key]} disagree or have low confidence.",
                                  code="ocr_disagreement", field=field, evidence=self.evidence)
        lines = [line_text(line) for line in group_lines(cropped_tokens)]
        text = "\n".join(lines).strip()
        self.evidence[field] = {"present": True, "label_box": list(self.labels[key]),
                                "value_box": list(box), "crop": str(path), "raw_text": text,
                                "ocr_tokens": token_evidence(cropped_tokens),
                                "crop_ocr_scale": attempt_scale, "ocr_attempts": attempts,
                                "preprocessing": "integer Lanczos resize and white border; two identical readings >=60 confidence required; no OCR if no dark ink (<180 grayscale)"}
        if not text and required:
            raise ExtractionError(f"The value under {LABELS[key]} is empty or unreadable.",
                                  code="unreadable", field=field, evidence=self.evidence)
        if any(token.confidence < 60 for token in cropped_tokens):
            raise ExtractionError(f"Low OCR confidence under {LABELS[key]} requires review.",
                                  code="low_confidence", field=field, evidence=self.evidence)
        if address:
            return lines
        if len(lines) > 1:
            raise ExtractionError(f"Multiple value lines under {LABELS[key]} require review.",
                                  code="ambiguous_layout", field=field, evidence=self.evidence)
        return text or None

    def address(self, key):
        field = "customer." + key
        lines = self.read(key, field=field, address=True)
        if len(lines) != 4:
            raise ExtractionError("An address needs four unambiguous lines: recipient, street, "
                                  "postal code/city and country. Extra lines need review.",
                                  field=field, code="address_structure", evidence=self.evidence)
        postal = re.fullmatch(r"([0-9]{5})\s+([^0-9\s].*)", lines[2])
        if not postal:
            raise ExtractionError("Local OCR requires a five-digit postal code followed by a city. Other postal structures need review.",
                                  field=field + ".postal_code", code="address_structure",
                                  evidence=self.evidence)
        result = dict(recipient=lines[0], street=lines[1], postal_code=postal[1], city=postal[2],
                      country=lines[3], additional_name=None, address_specification=None, district=None)
        self.evidence[field]["parsed_components"] = result
        self.evidence[field]["normalization"] = "Four lines; third line parsed as five-digit postal code plus city; postal code kept as text"
        return result


def decimal_text(text: str | None, *, currency=False, percent=False) -> str | None:
    """Strip printed units only; ambiguous separators remain for validation."""
    if text is None:
        return None
    value = re.sub(r"^EUR\s+", "", text) if currency else text
    return re.sub(r"\s*%$", "", value).strip() if percent else value.strip()


def extract_fields(image: Image.Image, tokens: list[Token], evidence_dir: Path):
    reader = FieldReader(image, tokens, evidence_dir)
    try:
        customer = {key: reader.read(key, field="customer." + key)
                    for key in ("company", "contact_name", "alias", "email", "telephone")}
        customer.update({key: reader.read(key, field="customer." + key, required=False)
                         for key in ("first_name", "last_name", "source_customer_id")})
        customer.update({key: reader.address(key) for key in ("billing", "delivery")})
        payment = {key: reader.read(key, field="payment." + key, required=key in {"method", "status"})
                   for key in ("method", "status", "date", "source_amount")}
        payment["source_amount"] = decimal_text(payment["source_amount"], currency=True)
        totals = {key: decimal_text(reader.read(key, field="totals." + key), currency=True)
                  for key in ("net", "vat", "gross")}
        for key, value in totals.items():
            reader.evidence["totals." + key]["parsed_value"] = value
            reader.evidence["totals." + key]["normalization"] = "Remove one explicit leading EUR marker only"
        fields = {key: reader.read(key) for key in ("order_date", "external_reference", "currency")}
        fields.update(customer=customer, payment=payment, totals=totals)
        for key, default in [("overall_discount_percent", "0"), ("shipping_net", "0"),
                             ("shipping_vat_percent", None)]:
            value = reader.read(key, required=key in reader.labels)
            fields[key] = decimal_text(value, currency=key == "shipping_net",
                                       percent=key != "shipping_net") if value is not None else default
            if key not in reader.labels:
                reader.evidence[key]["default"] = default
                reader.evidence[key]["reason"] = "No labeled source value; prescribed default"
        return fields, reader.evidence
    except ExtractionError as exc:
        exc.evidence = reader.evidence
        raise
