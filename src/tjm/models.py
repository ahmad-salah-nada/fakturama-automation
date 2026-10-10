"""The boundary between transcribed strings and checked business values.

Raw models deliberately reject number-to-string coercion and unknown fields.
Screenshots, OCR confidence and provenance belong to the extraction report.
"""

from datetime import date as Date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class RawAddress(StrictModel):
    recipient: str
    street: str
    postal_code: str
    city: str
    country: str
    additional_name: str | None = None
    address_specification: str | None = None
    district: str | None = None


class RawCustomer(StrictModel):
    company: str
    contact_name: str
    alias: str
    billing: RawAddress
    delivery: RawAddress
    email: str
    telephone: str
    first_name: str | None = None
    last_name: str | None = None
    source_customer_id: str | None = None


class RawPayment(StrictModel):
    method: str
    status: str
    date: str | None = None
    source_amount: str | None = None


class RawItem(StrictModel):
    sku: str
    description: str
    quantity: str
    unit_net: str
    vat_percent: str
    discount_percent: str
    line_net: str
    unit: str | None = None


class RawTotals(StrictModel):
    net: str
    vat: str
    gross: str


class RawOrder(StrictModel):
    order_date: str
    external_reference: str
    currency: str
    customer: RawCustomer
    payment: RawPayment
    items: list[RawItem]
    totals: RawTotals
    overall_discount_percent: str = "0"
    shipping_net: str = "0"
    shipping_vat_percent: str | None = None


class Address(StrictModel):
    recipient: str
    street: str
    postal_code: str
    city: str
    country: str
    additional_name: str | None = None
    address_specification: str | None = None
    district: str | None = None


class Customer(StrictModel):
    company: str
    contact_name: str
    first_name: str
    last_name: str
    alias: str
    billing: Address
    delivery: Address
    email: str
    telephone: str
    source_customer_id: str | None = None


class Payment(StrictModel):
    method: str
    status: str
    paid: bool
    date: Date | None
    amount: Decimal | None
    source_date: Date | None = None
    source_amount: Decimal | None = None


class Item(StrictModel):
    sku: str
    description: str
    quantity: Decimal
    unit_net: Decimal
    vat_percent: Decimal
    discount_percent: Decimal
    line_net: Decimal
    product_gross: Decimal
    unit: str | None = None


class Totals(StrictModel):
    net: Decimal
    vat: Decimal
    gross: Decimal


class ValidatedOrder(StrictModel):
    order_date: Date
    external_reference: str
    currency: str
    customer: Customer
    payment: Payment
    items: list[Item]
    totals: Totals
    overall_discount_percent: Decimal
    shipping_net: Decimal
    shipping_vat_percent: Decimal | None = None


class ValidationResult(StrictModel):
    status: Literal["validated", "review_required"]
    order: ValidatedOrder | None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
