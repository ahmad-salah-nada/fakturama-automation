"""Small business rules for the prescribed UI workflow; no desktop operations.

The caller supplies fully observed, stable selector rows with the canonical keys
documented below. These functions cannot establish that scrolling is complete,
that a Save was dispatched, or that a record persisted. Those are UI checks.

Requirement references are to the supplied assignment's PDF page numbers:
customer pp.4-6 §§2.3/2.10.2-2.10.6; product p.6 §3.3;
VAT p.7 §§3.4-3.6; one Save pp.5-10 §§2.10.6/2.11/3.6/3.11/4.4/5.4.
"""

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from .models import Customer, Item


class WorkflowReview(RuntimeError):
    """A rule cannot safely choose a record or permit another Save request."""

    def __init__(self, code: str, message: str, **evidence: Any):
        super().__init__(message)
        self.code = code
        self.evidence = evidence


CUSTOMER_KEYS = ("company", "first_name", "last_name", "postal_code", "city")
PAYMENT_CODES = {
    "Bank Transfer": "Credit transfer",
    "Credit Card": "Credit card",
    "SEPA Direct Debit": "SEPA direct debit",
}
DECIMAL_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


def _required_text(record: Mapping, keys: tuple[str, ...], *, context: str) -> dict:
    """Preserve case, punctuation and leading zeros; do not fuzzy-match."""
    result = {}
    if not isinstance(record, Mapping):
        raise WorkflowReview("incomplete_identity", f"{context} is not a readable record.")
    for key in keys:
        value = record.get(key)
        if (not isinstance(value, str) or not value.strip()
                or value.strip().upper() in {"?", "??", "???", "[UNREADABLE]", "[ILLEGIBLE]"}):
            raise WorkflowReview("incomplete_identity", f"{context}: {key} is unreadable or missing.",
                                 field=key)
        result[key] = value
    return result


def require_unique_customer(rows: list[dict], expected: Customer | Mapping) -> dict | None:
    """Choose one exact Company/First Name/Name/ZIP/City row, or report absence.

    The UI adapter maps visible Name to ``last_name`` and ZIP to ``postal_code``.
    One exact five-field row is selected even when other locations for that
    named customer appear; the selector can contain billing and delivery rows.
    With no exact row, a same-company, same-name row at a different location
    conservatively requires review. That boundary is an engineering safeguard
    under §2.3, not an additional explicit assignment instruction. Addresses
    still require checking after selection; this function does not check them.
    """
    if isinstance(expected, Customer):
        wanted = {key: getattr(expected, key) for key in CUSTOMER_KEYS[:3]}
        wanted.update(postal_code=expected.billing.postal_code, city=expected.billing.city)
    else:
        wanted = dict(expected)
        billing = wanted.get("billing")
        if isinstance(billing, Mapping):
            wanted.update(postal_code=billing.get("postal_code"), city=billing.get("city"))
    wanted = _required_text(wanted, CUSTOMER_KEYS, context="Expected customer")
    observed = [_required_text(row, CUSTOMER_KEYS, context="Customer result") for row in rows]
    exact = [row for row, identity in zip(rows, observed) if identity == wanted]
    conflicting = [identity for identity in observed
                   if all(identity[key] == wanted[key] for key in CUSTOMER_KEYS[:3])
                   and identity != wanted]
    if len(exact) > 1 or (not exact and conflicting):
        raise WorkflowReview("conflicting_customer", "Customer results are ambiguous or conflicting.",
                             expected=wanted, exact_count=len(exact), conflicting=conflicting)
    return exact[0] if exact else None


def require_unique_product(rows: list[dict], item: Item | Mapping) -> dict | None:
    """Choose one full, exact SKU; an existing master price is not a match key."""
    wanted = item.sku if isinstance(item, Item) else item.get("sku")
    _required_text({"sku": wanted}, ("sku",), context="Expected Product")
    identities = [_required_text(row, ("sku",), context="Product result") for row in rows]
    exact = [row for row, identity in zip(rows, identities) if identity["sku"] == wanted]
    if len(exact) > 1:
        raise WorkflowReview("conflicting_product", "Multiple Products have the exact source SKU.",
                             sku=wanted, exact_count=len(exact))
    return exact[0] if exact else None


def _number(value: Any, *, field: str, percent_display: bool = False) -> Decimal:
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if percent_display and text.endswith("%"):
            text = text[:-1].rstrip()
        if not DECIMAL_TEXT.fullmatch(text) or len(text.replace(".", "")) > 18:
            raise WorkflowReview("invalid_definition", f"{field} is not an unambiguous decimal.",
                                 field=field, observed=value)
        try:
            number = Decimal(text)
        except InvalidOperation as error:
            raise WorkflowReview("invalid_definition", f"{field} is unreadable.", field=field) from error
    else:
        raise WorkflowReview("invalid_definition", f"{field} requires Decimal or decimal text.",
                             field=field)
    if not number.is_finite() or number < 0:
        raise WorkflowReview("invalid_definition", f"{field} must be finite and nonnegative.",
                             field=field)
    digits = number.as_tuple().digits
    exponent = number.as_tuple().exponent
    if len(digits) + max(exponent, 0) > 18 or exponent < -6:
        raise WorkflowReview("invalid_definition", f"{field} exceeds supported numeric precision.",
                             field=field)
    return number


def required_vat_definition(rate: Decimal | str) -> dict:
    """Creation fields from p.7 §§3.4-3.6; Standard VAT is deliberately absent."""
    value = _number(rate, field="vat_percent")
    if value > 100:
        raise WorkflowReview("invalid_definition", "VAT percentage exceeds 100.", field="vat_percent")
    text = "0" if value == 0 else format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    name = f"VAT {text}%"
    return {"name": name, "description": name, "value": text, "code": "S"}


def require_unique_vat(rows: list[dict], rate: Decimal | str) -> dict | None:
    """Reuse only exact Name, numeric Value and S code (p.7 §3.5).

    Description is prescribed on creation, but is not one of §3.5's reuse keys.
    The adapter may supply code S or its exact visible label S (Standard rate).
    """
    wanted = required_vat_definition(rate)
    names = [_required_text(row, ("name",), context="VAT result")["name"] for row in rows]
    exact = [row for row, name in zip(rows, names) if name == wanted["name"]]
    if len(exact) > 1:
        raise WorkflowReview("conflicting_vat", "Multiple VAT definitions have the required exact Name.",
                             name=wanted["name"], exact_count=len(exact))
    if not exact:
        return None
    return require_matching_vat(exact[0], rate)


def require_matching_vat(row: dict, rate: Decimal | str) -> dict:
    """Check one observed VAT definition, including its exact required Name."""
    wanted = required_vat_definition(rate)
    name = _required_text(row, ("name",), context="VAT definition")["name"]
    value = _number(row.get("value"), field="value", percent_display=True)
    if (name != wanted["name"] or value != Decimal(wanted["value"])
            or row.get("code") not in ("S", "S (Standard rate)")):
        raise WorkflowReview("conflicting_vat", "The exact VAT Name has a conflicting Value or code.",
                             expected=wanted, observed=deepcopy(row))
    return row


def payment_definition(method: str) -> dict:
    """Creation fields from pp.5-6 §§2.10.3-2.10.5; never Set as standard."""
    if method not in PAYMENT_CODES:
        raise WorkflowReview("unsupported_payment_code", "No assignment payment-code mapping exists.",
                             method=method)
    return {
        "name": method, "description": method, "account": "", "code": PAYMENT_CODES[method],
        "cash_discount": "0", "discount_days": "0", "net_days": "0",
        "text_unpaid": "", "text_deposit": "", "text_paid": "",
    }


def require_unique_payment(rows: list[dict], method: str) -> dict | None:
    """Select one exact required method, rejecting a conflicting definition.

    For the three specified methods, comparison uses all mandatory creation
    fields. The standard-method flag is not changed or used as a match key.
    """
    payment_definition(method)  # reject an unsupported creation mapping before reporting absence
    names = [_required_text(row, ("name",), context="Payment result")["name"] for row in rows]
    exact = [row for row, name in zip(rows, names) if name == method]
    if len(exact) > 1:
        raise WorkflowReview("conflicting_payment", "Multiple Payment Methods have the exact Name.",
                             method=method, exact_count=len(exact))
    if not exact:
        return None
    return require_matching_payment(exact[0], method)


def require_matching_payment(row: dict, method: str) -> dict:
    """Check one exact Payment Method against mandatory creation fields."""
    wanted = payment_definition(method)
    _required_text(row, ("name",), context="Payment definition")
    conflicts = []
    for field, expected in wanted.items():
        if field in {"cash_discount", "discount_days", "net_days"}:
            equal = _number(row.get(field), field=field,
                            percent_display=field == "cash_discount") == Decimal(expected)
        else:
            equal = row.get(field) == expected
        if not equal:
            conflicts.append(field)
    if conflicts:
        raise WorkflowReview("conflicting_payment", "The exact Payment Method has a conflicting definition.",
                             expected=wanted, observed=deepcopy(row), fields=conflicts)
    return row


class SaveLedger:
    """An in-memory list of Save intentions and separately supplied readback.

    Record intent immediately before dispatch. Even when dispatch fails or its
    outcome is unknown, that record remains: this class never permits a blind
    second Save for the same kind/identity. It is not an automatic resume engine.
    Verification evidence must describe a caller's saved-record retrieval and
    have ``passed`` set to the boolean True. This class only records supplied
    evidence and does not independently prove persistence.
    """

    def __init__(self) -> None:
        self._entries: list[dict] = []

    def _find(self, kind: str, identity: str) -> dict | None:
        _required_text({"kind": kind, "identity": identity}, ("kind", "identity"),
                       context="Save identity")
        return next((row for row in self._entries
                     if row["kind"] == kind and row["identity"] == identity), None)

    def record_intent(self, kind: str, identity: str) -> dict:
        if self._find(kind, identity) is not None:
            raise WorkflowReview("repeated_save", "A Save intention already exists for this record; review its outcome.",
                                 kind=kind, identity=identity)
        entry = {"kind": kind, "identity": identity, "save_intent": True,
                 "persistence_evidence": None}
        self._entries.append(entry)
        return deepcopy(entry)

    def record_verified(self, kind: str, identity: str, *, evidence: Mapping) -> dict:
        entry = self._find(kind, identity)
        if entry is None:
            raise WorkflowReview("missing_save_intent", "Persistence evidence has no preceding Save intention.",
                                 kind=kind, identity=identity)
        if not isinstance(evidence, Mapping) or not evidence:
            raise WorkflowReview("missing_persistence_evidence", "Supply saved-record retrieval evidence.",
                                 kind=kind, identity=identity)
        if evidence.get("passed") is not True:
            raise WorkflowReview("failed_persistence_verification",
                                 "Persistence readback must explicitly pass before recording verification.",
                                 kind=kind, identity=identity)
        try:
            copied = json.loads(json.dumps(dict(evidence), allow_nan=False))
        except (TypeError, ValueError) as error:
            raise WorkflowReview("invalid_persistence_evidence", "Persistence evidence must be JSON data.",
                                 kind=kind, identity=identity) from error
        if entry["persistence_evidence"] is not None:
            raise WorkflowReview("repeated_verification", "Persistence evidence is already recorded.",
                                 kind=kind, identity=identity)
        entry["persistence_evidence"] = copied
        return deepcopy(entry)

    def as_json(self) -> list[dict]:
        """Return isolated reporting data, without exposing mutable ledger state."""
        return deepcopy(self._entries)

    def counts(self) -> dict:
        """Count recorded intentions/evidence; these are not successful-click counts."""
        return {"save_intents": len(self._entries),
                "with_persistence_evidence": sum(row["persistence_evidence"] is not None
                                                 for row in self._entries)}
