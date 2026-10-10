from decimal import Decimal
from types import SimpleNamespace

import pytest

from tjm.desktop import DesktopError
from tjm.models import Item
from tjm.product_workflow import (
    VAT_CODE_LABEL, fill_product_definition, fill_vat_definition,
    read_product_definition, require_matching_product,
)


class Form:
    def __init__(self, *, vat=False):
        self.calls = []
        self.standard_changes = False
        self.fields = ({
            "Name": "", "Description": "", "Value": "0%", "Category": "",
            VAT_CODE_LABEL: "S (Standard rate)", "Standard": "Tax-free",
        } if vat else {
            "Item Number": "", "Name": "", "Description": "", "Category": "",
            "GTIN": "", "supplier code": "", "allowance": "",
            "user defined field 1": "", "Price (gross)": "EUR0.00",
            "cost price (net)": "EUR0.00", "Stock": "0.00", "VAT": "Tax-free",
        })

    def snapshot(self, label):
        return object()

    def field(self, capture, label, *, scope, roles):
        return SimpleNamespace(value=self.fields[label])

    def section(self, capture, editor, start, end):
        return "Description field only"

    def set_text(self, label, value, *, scope, expected=None):
        self.calls.append(("set_text", label, value))
        if label in {"Price (gross)", "cost price (net)"}:
            observed = "EUR" + value
        elif label == "Value":
            observed = value + "%"
        else:
            observed = value
        targets = (value,) if expected is None else (expected,) if isinstance(expected, str) else expected
        assert observed in targets
        self.fields[label] = observed
        if self.standard_changes and label == "Value":
            self.fields["Standard"] = "VAT 19%"

    def choose(self, label, value, *, scope):
        self.calls.append(("choose", label, value))
        self.fields[label] = value
        if label == "VAT":
            # Model the observed binding: VAT selection can recalculate gross.
            gross = Decimal(self.fields["Price (gross)"][3:]) * Decimal("1.19")
            self.fields["Price (gross)"] = f"EUR{gross:.2f}"


def item():
    return Item(sku="OTHER-123", description="Source description", quantity=Decimal("2"),
                unit_net=Decimal("10"), vat_percent=Decimal("19"),
                discount_percent=Decimal("20"), line_net=Decimal("16"),
                product_gross=Decimal("11.90"))


def test_final_gross_survives_vat_recalculation_and_excludes_line_discount():
    ui = Form()
    values = fill_product_definition(ui, item())
    assert values["gross"] == "EUR11.90"
    assert values["sku"] == "OTHER-123"
    assert values["description"] == "Source description"
    vat_call = next(i for i, call in enumerate(ui.calls) if call[:2] == ("choose", "VAT"))
    gross_call = next(i for i, call in enumerate(ui.calls)
                      if call[:2] == ("set_text", "Price (gross)"))
    assert vat_call < gross_call
    assert all(call[1] not in {"Category", "GTIN", "supplier code", "allowance",
                              "user defined field 1", "Picture", "Discount", "Save"}
               for call in ui.calls)


def test_incorrect_gross_is_rejected_before_caller_saves():
    ui = Form()
    values = fill_product_definition(ui, item())
    values["gross"] = "EUR14.16"
    with pytest.raises(DesktopError, match="gross"):
        require_matching_product(values, item())


def test_observed_euro_symbol_display_preserves_exact_amounts():
    values = fill_product_definition(Form(), item())
    values.update(gross="€11.90", cost_net="€0.00")
    assert require_matching_product(values, item()) == values
    values["gross"] = "USD11.90"
    with pytest.raises(DesktopError, match="gross"):
        require_matching_product(values, item())


def test_reopened_product_can_use_its_saved_editor_title():
    ui = Form()
    fill_product_definition(ui, item())
    values = read_product_definition(ui, title="Source description")
    assert require_matching_product(values, item()) == values


def test_vat_creation_checks_exact_definition_and_preserves_standard():
    ui = Form(vat=True)
    values, standard = fill_vat_definition(ui, Decimal("19.00"))
    assert values == {"name": "VAT 19%", "description": "VAT 19%",
                      "value": "19%", "code": "S (Standard rate)"}
    assert standard == "Tax-free"
    assert not any(call[1] in {"Standard", "Category", "Save"} for call in ui.calls)


def test_unexpected_standard_change_requires_review_before_save():
    ui = Form(vat=True)
    ui.standard_changes = True
    with pytest.raises(DesktopError, match="standard VAT changed"):
        fill_vat_definition(ui, Decimal("19"))
