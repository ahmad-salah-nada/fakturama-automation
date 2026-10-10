"""Reject ambiguous source data before it can become an application write.

HALF_UP is the proposed two-decimal Product-price rule. Fakturama's line
rounding and VAT aggregation are not yet calibrated: ties and differing
rounding stages require review, rather than being treated as proven behavior.
"""

import re
import unicodedata
from collections import defaultdict
from datetime import date
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_UP, localcontext

from .models import (
    Address, Customer, Item, Payment, RawAddress, RawOrder, Totals,
    ValidatedOrder, ValidationResult,
)

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")
DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
PAYMENT_STATUSES = {"PAID", "UNPAID", "NOT PAID", "PENDING", "PARTIALLY PAID"}
UNREADABLE_MARKERS = {"?", "??", "???", "[UNREADABLE]", "[ILLEGIBLE]"}


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def half_cent(value: Decimal) -> bool:
    return value % CENT == Decimal("0.005")


class _Checks:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.issues: list[dict] = []

    def issue(self, field: str, code: str, message: str, **details: str) -> None:
        self.issues.append({"field": field, "code": code, "message": message,
                            "details": details})

    def compare(self, field: str, expected: Decimal, observed: Decimal) -> None:
        passed = expected == observed
        self.checks.append({"field": field, "rule": "exact_decimal_equality",
                            "expected": str(expected), "observed": str(observed),
                            "passed": passed})
        if not passed:
            self.issue(field, "arithmetic_mismatch", "Source value does not match the calculation.",
                       expected=str(expected), observed=str(observed))

    def text(self, field: str, value: str | None, *, optional: bool = False) -> str | None:
        if value is None:
            if not optional:
                self.issue(field, "missing_field", "A required source field is missing.")
            return None
        cleaned = value.strip()
        if not cleaned:
            if not optional:
                self.issue(field, "missing_field", "A required source field is blank.")
            return None
        if cleaned.upper() in UNREADABLE_MARKERS:
            self.issue(field, "unreadable_field", "An explicit unreadable placeholder cannot become application data.")
            return None
        if any(unicodedata.category(ch) == "Cc" for ch in cleaned):
            self.issue(field, "control_character", "Review embedded control characters or unresolved line wrapping.")
        return cleaned

    def number(self, field: str, value: str | None, *, positive: bool = False,
               percent: bool = False, money: bool = False) -> Decimal | None:
        if value is None or not DECIMAL_TEXT.fullmatch(value.strip()):
            self.issue(field, "invalid_decimal", "Use a finite decimal string with '.' and no grouping, symbols or exponent.")
            return None
        cleaned = value.strip()
        digits = cleaned.lstrip("+-").replace(".", "")
        places = len(cleaned.split(".", 1)[1]) if "." in cleaned else 0
        if len(digits) > 18 or places > 6:
            self.issue(field, "unsupported_precision", "Initial support is at most 18 digits and six decimal places.")
            return None
        parsed = Decimal(cleaned)
        if parsed < ZERO or (positive and parsed == ZERO) or (percent and parsed > HUNDRED):
            self.issue(field, "out_of_range", "Value is outside the supported nonnegative range.",
                       observed=str(parsed), allowed="(0, infinity)" if positive else "[0, 100]" if percent else "[0, infinity)")
            return None
        if money and parsed != cents(parsed):
            self.issue(field, "fractional_cent", "A source line total, document total or payment amount must be exact cents.",
                       observed=str(parsed))
            return None
        return parsed

    def date(self, field: str, value: str | None, *, required: bool = True) -> date | None:
        if value is None:
            if required:
                self.issue(field, "missing_date", "A source date is required.")
            return None
        text = value.strip()
        try:
            if not ISO_DATE.fullmatch(text):
                raise ValueError
            return date.fromisoformat(text)
        except ValueError:
            self.issue(field, "invalid_date", "Use a real date in unambiguous YYYY-MM-DD format.", observed=text)
            return None

    def rounding(self, field: str, value: Decimal, code: str) -> None:
        self.issue(field, code, "This rounding case needs Fakturama calibration before automation.",
                   unrounded=str(value), proposed_half_up=str(cents(value)))


def _address(raw: RawAddress, field: str, checks: _Checks) -> Address | None:
    required = {key: checks.text(f"{field}.{key}", getattr(raw, key))
                for key in ("recipient", "street", "postal_code", "city", "country")}
    optional = {key: checks.text(f"{field}.{key}", getattr(raw, key), optional=True)
                for key in ("additional_name", "address_specification", "district")}
    return Address(**required, **optional) if all(required.values()) else None


def _latin_name_part(value: str) -> bool:
    """The explicitly selected convention supports two unabbreviated Latin parts."""
    value = unicodedata.normalize("NFC", value)
    if not value or not value[0].isalpha() or not value[-1].isalpha():
        return False
    return all((ch.isalpha() and "LATIN" in unicodedata.name(ch, ""))
               or ch in "-'’" for ch in value)


def _name_comparison(value: str) -> str:
    """Compare presentation variants without deleting name characters."""
    compact = " ".join(unicodedata.normalize("NFC", value).split())
    return re.sub(r"\s*,\s*", ", ", compact)


def _customer(raw, name_order: str | None, checks: _Checks) -> Customer | None:
    fields = {key: checks.text(f"customer.{key}", getattr(raw, key))
              for key in ("company", "contact_name", "alias", "email", "telephone")}
    first = checks.text("customer.first_name", raw.first_name, optional=True)
    last = checks.text("customer.last_name", raw.last_name, optional=True)
    if bool(first) != bool(last):
        checks.issue("customer.contact_name", "incomplete_name", "Explicit name parts require both First Name and Last Name.")
    elif first and last:
        forms = {"given_family": f"{first} {last}", "family_given": f"{last} {first}",
                 "family_comma_given": f"{last}, {first}", "given_comma_family": f"{first}, {last}"}
        matched = next((form for form, value in forms.items()
                        if _name_comparison(value) == _name_comparison(fields["contact_name"] or "")), None)
        checks.checks.append({"field": "customer.contact_name", "rule": "explicit_name_parts",
                             "passed": matched is not None, "matched_form": matched,
                             "source_contact_name": fields["contact_name"], "source_first_name": first,
                             "source_last_name": last, "comparison": "NFC, whitespace and comma spacing only; source name values are retained."})
        if matched is None:
            checks.issue("customer.contact_name", "conflicting_name_parts",
                         "Explicit name parts do not account for the complete source Contact Name. Review the conflicting interpretation.",
                         source_contact_name=fields["contact_name"] or "", first_name=first, last_name=last)
    else:
        parts = (fields["contact_name"] or "").split()
        if (name_order == "given-family" and len(parts) == 2
                and all(_latin_name_part(part) for part in parts)
                and parts[0].lower() not in {"mr", "mrs", "ms", "dr", "prof"}):
            first, last = parts
            checks.checks.append({"field": "customer.contact_name", "rule": "name_interpretation",
                                 "passed": True, "policy": "given-family", "first_name": first,
                                 "last_name": last, "assumption": "Caller selected a two-part Latin given/family convention; raw contact_name is retained."})
        else:
            checks.issue("customer.contact_name", "ambiguous_name", "Supply explicit name parts or select the supported convention for an unambiguous two-part Latin name.")
    billing = _address(raw.billing, "customer.billing", checks)
    delivery = _address(raw.delivery, "customer.delivery", checks)
    source_id = checks.text("customer.source_customer_id", raw.source_customer_id, optional=True)
    if fields["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fields["email"]):
        checks.issue("customer.email", "invalid_email", "Email must contain a local part, domain and domain suffix without spaces.")
    if fields["telephone"] and not any(ch in "0123456789" for ch in fields["telephone"]):
        checks.issue("customer.telephone", "invalid_telephone", "Telephone must contain digits.")
    if all(fields.values()) and first and last and billing and delivery:
        return Customer(**fields, first_name=first, last_name=last, billing=billing,
                        delivery=delivery, source_customer_id=source_id)
    return None


def validate_order(raw: RawOrder, *, name_order: str | None = None) -> ValidationResult:
    """Return typed data only when field and financial checks permit proceeding.

    Raw schema errors are Pydantic ValidationErrors, handled by the caller at
    extraction boundaries. Business ambiguity returns review_required here.
    """
    checks = _Checks()
    # Isolate arithmetic from caller precision, traps, exponent limits and
    # rounding. The bounded parser fits comfortably within this context.
    context = Context(prec=80, rounding=ROUND_HALF_UP, Emin=-999999, Emax=999999,
                      capitals=1, clamp=0, flags=[],
                      traps=[InvalidOperation, DivisionByZero, Overflow])
    with localcontext(context):
        return _validate(raw, name_order, checks)


def _validate(raw: RawOrder, name_order: str | None, checks: _Checks) -> ValidationResult:
    if name_order not in (None, "given-family"):
        checks.issue("name_order", "unsupported_name_policy", "Supported policy is given-family; omit it for explicit source name parts.")
    order_date = checks.date("order_date", raw.order_date)
    reference = checks.text("external_reference", raw.external_reference)
    currency = checks.text("currency", raw.currency)
    if currency != "EUR":
        checks.issue("currency", "unsupported_currency", "The tested initial currency is EUR.", observed=currency or "")
    customer = _customer(raw.customer, name_order, checks)
    discount = checks.number("overall_discount_percent", raw.overall_discount_percent, percent=True)
    shipping = checks.number("shipping_net", raw.shipping_net, money=True)
    shipping_rate = (checks.number("shipping_vat_percent", raw.shipping_vat_percent, percent=True)
                     if raw.shipping_vat_percent is not None else None)
    if shipping is not None and shipping > ZERO and shipping_rate is None:
        checks.issue("shipping_vat_percent", "unknown_shipping_tax", "Nonzero shipping needs an explicit source VAT rate.")

    items = []
    if not raw.items:
        checks.issue("items", "missing_items", "At least one readable source row is required.")
    for index, raw_item in enumerate(raw.items):
        field = f"items[{index}]"
        sku = checks.text(f"{field}.sku", raw_item.sku)
        description = checks.text(f"{field}.description", raw_item.description)
        unit = checks.text(f"{field}.unit", raw_item.unit, optional=True)
        values = {key: checks.number(f"{field}.{key}", getattr(raw_item, key),
                                   positive=key == "quantity", percent=key in ("vat_percent", "discount_percent"),
                                   money=key == "line_net")
                  for key in ("quantity", "unit_net", "vat_percent", "discount_percent", "line_net")}
        if not sku or not description or any(value is None for value in values.values()):
            continue
        quantity, price, vat, item_discount = (values[key] for key in ("quantity", "unit_net", "vat_percent", "discount_percent"))
        discounted_unit = price * (1 - item_discount / HUNDRED)
        exact_line = quantity * discounted_unit
        expected_line = cents(exact_line)
        checks.compare(f"{field}.line_net", expected_line, values["line_net"])
        if exact_line != expected_line:
            checks.rounding(f"{field}.line_net", exact_line, "uncalibrated_line_rounding")
        alternative_line = cents(quantity * cents(discounted_unit))
        if alternative_line != expected_line:
            checks.issue(f"{field}.line_net", "uncalibrated_discount_stage", "Rounding the discounted unit before quantity changes the line total.",
                         round_line=str(expected_line), round_unit_first=str(alternative_line))
        gross_exact = price * (1 + vat / HUNDRED)
        if half_cent(gross_exact):
            checks.rounding(f"{field}.product_gross", gross_exact, "uncalibrated_product_tie")
        items.append(Item(sku=sku, description=description, unit=unit, **values,
                          product_gross=cents(gross_exact)))

    totals_values = {key: checks.number(f"totals.{key}", getattr(raw.totals, key), money=True)
                     for key in ("net", "vat", "gross")}
    totals = Totals(**totals_values) if all(value is not None for value in totals_values.values()) else None
    expected_totals = None
    if len(items) == len(raw.items) and items and discount is not None and shipping is not None:
        rates = {item.vat_percent for item in items}
        if discount > ZERO and (len(rates) != 1 or shipping > ZERO):
            checks.issue("overall_discount_percent", "unknown_discount_tax", "Overall discount requires one item VAT rate and zero shipping until allocation is explicitly supported.")
        elif shipping == ZERO or shipping_rate is not None:
            base = sum((item.line_net for item in items), ZERO)
            exact_net = base * (1 - discount / HUNDRED) + shipping
            expected_net = cents(exact_net)
            if exact_net != expected_net:
                checks.rounding("totals.net", exact_net, "uncalibrated_document_discount_rounding")
            if discount > ZERO:
                components = [(item.line_net * (1 - discount / HUNDRED), item.vat_percent)
                              for item in items]
                rounded_line_sum = sum((cents(net) for net, _ in components), ZERO)
                if rounded_line_sum != expected_net or any(half_cent(net) for net, _ in components):
                    checks.issue("overall_discount_percent", "uncalibrated_document_discount_stage",
                                 "Allocating the document discount across lines can change rounding.",
                                 discount_subtotal=str(expected_net), discount_each_line=str(rounded_line_sum))
            else:
                components = [(item.line_net, item.vat_percent) for item in items]
                if shipping > ZERO:
                    components.append((shipping, shipping_rate))
            tax_values = [net * rate / HUNDRED for net, rate in components]
            grouped_net = defaultdict(lambda: ZERO)
            for net, rate in components:
                grouped_net[rate] += net
            grouped_tax = [net * rate / HUNDRED for rate, net in grouped_net.items()]
            variants = {"per_line": sum((cents(value) for value in tax_values), ZERO),
                        "per_rate": sum((cents(value) for value in grouped_tax), ZERO),
                        "document": cents(sum(tax_values, ZERO))}
            if any(half_cent(value) for value in tax_values + grouped_tax + [sum(tax_values, ZERO)]):
                checks.issue("totals.vat", "uncalibrated_vat_tie", "A VAT half-cent needs calibration even when source totals agree.",
                             **{key: str(value) for key, value in variants.items()})
            if len(set(variants.values())) != 1:
                checks.issue("totals.vat", "uncalibrated_vat_stage", "Plausible VAT rounding stages produce different totals.",
                             **{key: str(value) for key, value in variants.items()})
            expected_vat = variants["per_line"]
            expected_totals = Totals(net=expected_net, vat=expected_vat, gross=expected_net + expected_vat)
            checks.checks.append({"field": "totals.vat", "rule": "rounding_stage_comparison",
                                 "passed": len(set(variants.values())) == 1,
                                 "calibrated": False, **{key: str(value) for key, value in variants.items()}})
            if totals:
                for key in ("net", "vat", "gross"):
                    checks.compare(f"totals.{key}", getattr(expected_totals, key), getattr(totals, key))
    if totals:
        checks.compare("totals.gross_from_source_net_and_vat", totals.net + totals.vat, totals.gross)

    method = checks.text("payment.method", raw.payment.method)
    status = checks.text("payment.status", raw.payment.status)
    normalized_status = status.upper() if status else None
    if normalized_status not in PAYMENT_STATUSES:
        checks.issue("payment.status", "unsupported_payment_status", "Review an unknown or unreadable payment status; do not interpret it as unpaid.",
                     observed=status or "")
    paid = normalized_status == "PAID"
    payment_date = checks.date("payment.date", raw.payment.date, required=paid)
    source_amount = (checks.number("payment.source_amount", raw.payment.source_amount, money=True)
                     if raw.payment.source_amount is not None else None)
    payment = None
    if method and normalized_status in PAYMENT_STATUSES:
        amount = expected_totals.gross if paid and expected_totals else None
        if paid and amount is not None:
            if source_amount is not None:
                checks.compare("payment.source_amount", amount, source_amount)
            checks.checks.append({"field": "payment.amount", "rule": "paid_full_invoice_total",
                                 "passed": True, "derived": str(amount), "source_amount_supplied": raw.payment.source_amount is not None})
        if not paid:
            checks.checks.append({"field": "payment", "rule": "non_paid_leaves_payment_clear", "passed": True,
                                 "source_status": normalized_status,
                                 "supplied_date_or_amount_not_applied": raw.payment.date is not None or raw.payment.source_amount is not None})
        payment = Payment(method=method, status=status, paid=paid,
                          date=payment_date if paid else None, amount=amount,
                          source_date=payment_date, source_amount=source_amount)

    order = None
    if not checks.issues:
        order = ValidatedOrder(order_date=order_date, external_reference=reference, currency=currency,
                               customer=customer, payment=payment, items=items, totals=totals,
                               overall_discount_percent=discount, shipping_net=shipping,
                               shipping_vat_percent=shipping_rate)
    return ValidationResult(status="validated" if order else "review_required", order=order,
                            checks=checks.checks, issues=checks.issues)
