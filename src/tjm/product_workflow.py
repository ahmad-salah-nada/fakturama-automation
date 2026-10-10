"""Fill the already-open VAT and Product editors, then read their visible values.

These helpers do not create editors or save records. The caller must establish
absence before creation and retrieve each saved record to prove persistence.
Field labels and the EUR display format were observed in the tested Fakturama UI
probes. Missing or ambiguous current controls stop the stage.
"""

from decimal import Decimal

from .desktop import DesktopError
from .models import Item
from .ui import Ui
from .workflow_rules import required_vat_definition, require_matching_vat


VAT_CODE_LABEL = "VAT code (E-Invoice)"
VAT_CODE_VALUE = "S (Standard rate)"
PRODUCT_FIELDS = {
    "sku": "Item Number", "name": "Name", "gross": "Price (gross)",
    "cost_net": "cost price (net)", "vat": "VAT", "stock": "Stock",
}
PRODUCT_UNSOURCED_FIELDS = {
    "category": "Category", "gtin": "GTIN", "supplier_code": "supplier code",
    "allowance": "allowance", "user_field_1": "user defined field 1",
}


def euro_display(amount, *, currency="EUR"):
    """The two exact EUR displays observed in the isolated M1/M4 workspaces."""
    if currency != "EUR":
        raise DesktopError("Only the observed EUR currency display is supported.")
    return (f"EUR{amount:.2f}", f"€{amount:.2f}")


def _read(ui, capture, label, *, scope,
          roles=("AXTextField", "AXTextArea", "AXPopUpButton", "AXComboBox")):
    return ui.field(capture, label, scope=scope, roles=roles).value


def read_vat_definition(ui: Ui, *, title="New TAX Rate"):
    """Return current VAT fields and the displayed standard VAT separately."""
    capture = ui.snapshot("vat-definition-readback")
    values = {
        "name": _read(ui, capture, "Name", scope=title),
        "description": _read(ui, capture, "Description", scope=title),
        "value": _read(ui, capture, "Value", scope=title),
        "code": _read(ui, capture, VAT_CODE_LABEL, scope=title,
                      roles=("AXPopUpButton",)),
    }
    standard = _read(ui, capture, "Standard", scope=title,
                     roles=("AXStaticText", "AXTextField"))
    return values, standard


def fill_vat_definition(ui: Ui, rate: Decimal | str, *, title="New TAX Rate"):
    """Fill one new VAT without changing its category or the standard VAT."""
    expected = required_vat_definition(rate)
    capture = ui.snapshot("vat-defaults-before-input")
    standard_before = _read(ui, capture, "Standard", scope=title,
                            roles=("AXStaticText", "AXTextField"))
    category_before = _read(ui, capture, "Category", scope=title)
    ui.set_text("Name", expected["name"], scope=title)
    ui.set_text("Description", expected["description"], scope=title)
    ui.choose(VAT_CODE_LABEL, VAT_CODE_VALUE, scope=title)
    ui.set_text("Value", expected["value"], expected=expected["value"] + "%", scope=title)
    values, standard_after = read_vat_definition(ui, title=title)
    require_matching_vat(values, rate)
    if values["description"] != expected["description"]:
        raise DesktopError("The new VAT Description does not match its required Name.")
    capture = ui.snapshot("vat-unchanged-defaults-readback")
    if (standard_after != standard_before
            or _read(ui, capture, "Category", scope=title) != category_before):
        raise DesktopError("The VAT category or standard VAT changed during creation.")
    return values, standard_before


def _description_scope(ui, capture, title):
    # Product has both a section heading and a field called Description.
    # Bound the field by its current neighboring labels to keep it unique.
    return ui.section(capture, title, "supplier code", "Price (gross)")


def read_product_definition(ui: Ui, *, title="New product"):
    """Read the currently displayed Product; this alone does not prove a Save."""
    capture = ui.snapshot("product-definition-readback")
    values = {key: _read(ui, capture, label, scope=title)
              for key, label in PRODUCT_FIELDS.items()}
    values["description"] = _read(ui, capture, "Description",
                                   scope=_description_scope(ui, capture, title))
    values["unchanged_fields"] = {
        key: _read(ui, capture, label, scope=title)
        for key, label in PRODUCT_UNSOURCED_FIELDS.items()
    }
    return values


def require_matching_product(values: dict, item: Item, *, currency="EUR"):
    """Compare all mandatory Product creation fields with validated source data."""
    expected = {
        "sku": item.sku, "name": item.description, "description": item.description,
        "stock": "0.00",
        "vat": required_vat_definition(item.vat_percent)["name"],
    }
    conflicts = [key for key, value in expected.items() if values.get(key) != value]
    if values.get("gross") not in euro_display(item.product_gross, currency=currency):
        conflicts.append("gross")
    if values.get("cost_net") not in euro_display(Decimal("0"), currency=currency):
        conflicts.append("cost_net")
    if conflicts:
        raise DesktopError("The Product differs from its required creation fields: "
                           + ", ".join(conflicts) + ".")
    return values


def fill_product_definition(ui: Ui, item: Item, *, currency="EUR", title="New product"):
    """Select VAT before entering the final gross price, then verify the master.

    The source line discount is deliberately absent: it belongs to the Order
    line, while product_gross has already been validated without that discount.
    Picture and every unsourced field receive no input.
    """
    before = read_product_definition(ui, title=title)
    ui.set_text("Item Number", item.sku, scope=title)
    ui.set_text("Name", item.description, scope=title)
    capture = ui.snapshot("before-product-description")
    ui.set_text("Description", item.description,
                scope=_description_scope(ui, capture, title))
    ui.choose("VAT", required_vat_definition(item.vat_percent)["name"], scope=title)
    # M1 product-persisted-readback.json showed that changing VAT can change
    # the displayed gross price. Entering it last avoids retaining that change.
    ui.set_text("Price (gross)", f"{item.product_gross:.2f}",
                expected=euro_display(item.product_gross, currency=currency), scope=title)
    ui.set_text("cost price (net)", "0.00",
                expected=euro_display(Decimal("0"), currency=currency), scope=title)
    ui.set_text("Stock", "0.00", scope=title)
    values = read_product_definition(ui, title=title)
    require_matching_product(values, item, currency=currency)
    if values["unchanged_fields"] != before["unchanged_fields"]:
        raise DesktopError("An unsourced Product field changed during creation.")
    return values
