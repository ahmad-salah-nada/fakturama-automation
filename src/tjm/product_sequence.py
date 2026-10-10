"""Resolve VAT and Product masters while retaining the original Order editor.

This module uses visible lists, current controls and the shared one-Save helper.
It does not edit transaction lines or accept their values after selection.
"""

from uuid import uuid4

from .contact_sequence import (
    _close_clean, _left_scope, _save_new_master, _write_evidence,
)
from .desktop import DesktopError
from .document_workflow import open_product_selector
from .models import Item
from .product_workflow import (
    fill_product_definition, fill_vat_definition, read_product_definition,
    read_vat_definition, require_matching_product,
)
from .ui import Ui
from .ui_tables import fit_columns, read_complete_table, rowselect
from .workflow_rules import (
    WorkflowReview, required_vat_definition, require_matching_vat, require_unique_product,
)


# The assignment matches Products by SKU. Full creation fields are verified in
# the cold-opened editor; table VAT labels add display details and can clip.
PRODUCT_HEADERS = ["Item No."]
VAT_HEADERS = ["Name", "Description", "Value"]


def _product_row(table, item):
    mapped = [{"sku": row["values"]["Item No."], "visible_row": row}
              for row in table.rows]
    matched = require_unique_product(mapped, item)
    return matched["visible_row"] if matched is not None else None


def _product_columns(item, currency):
    return {"Item No.": item.sku}


def _product_table(ui, item, *, scope, currency):
    fit_columns(ui, _product_columns(item, currency), scope=scope, headers=PRODUCT_HEADERS)
    table = read_complete_table(ui, PRODUCT_HEADERS, scope=scope,
                                search_field="Search:", search_value=item.sku, timeout=25)
    return table, _product_row(table, item)


def _select_product(ui, item, *, currency):
    open_product_selector(ui, title="New Order")
    selector = Ui(ui.desktop, "Select a product",
                  ui.evidence_dir / ("product-selector-" + uuid4().hex[:12]), text_input=ui.text_input)
    try:
        # Initial SWT layout can place Search in the title bar. Each new modal
        # gets the one observed resize before visible-label discovery and input.
        selector.relayout_product_dialog()
        selector.search(item.sku)
        table, row = _product_table(selector, item, scope=None, currency=currency)
        if row is None:
            selector.press("Cancel")
            ui.wait_tab("New Order")
            return None, table.evidence
        selected_values = dict(row["values"])
        rowselect(selector, table, row)
        selector.press("OK")
        ui.wait_tab("New Order")
        return selected_values, table.evidence
    finally:
        selector.write_actions()


def _vat_table(ui, name):
    ui.set_text("Search:", name, scope="VATs")
    fit_columns(ui, {"Name": name, "Description": name}, scope="VATs", headers=VAT_HEADERS)
    table = read_complete_table(ui, VAT_HEADERS, scope="VATs", search_field="Search:",
                                search_value=name, timeout=25)
    exact = [row for row in table.rows if row["values"]["Name"] == name]
    if len(exact) > 1:
        raise WorkflowReview("conflicting_vat", "Multiple VAT definitions have the required exact Name.",
                             name=name, exact_count=len(exact))
    return table, exact[0] if exact else None


def resolve_vat(ui, item: Item, ledger, journal_directory):
    """Resolve one exact VAT, checking its code inside the actual editor.

    Starts and finishes in the retained New Order. A newly saved definition is
    closed and reopened before its ledger entry is marked verified.
    """
    name = required_vat_definition(item.vat_percent)["name"]
    ui.click_label("VATs", scope=_left_scope(ui, "New Order"))
    table, row = _vat_table(ui, name)
    created = row is None
    standard_before = None
    if created:
        ui.icon("plus.png", size=16, scope="VATs", expected_help="Create a new tax rate")
        ui.wait_tab("New TAX Rate")
        _, standard_before = fill_vat_definition(ui, item.vat_percent)
        saved_title = _save_new_master(ui, ledger, kind="vat", identity=name,
                                       title="New TAX Rate", label="Name",
                                       journal_directory=journal_directory)
        _close_clean(ui, saved_title, identity_label="Name", identity=name)
        table, row = _vat_table(ui, name)
        if row is None:
            raise WorkflowReview("vat_not_retrieved", "Saved VAT is absent from its complete list.",
                                 name=name)
    rowselect(ui, table, row, clicks=2)
    ui.wait_tab(name)
    fields, standard_after = read_vat_definition(ui, title=name)
    require_matching_vat(fields, item.vat_percent)
    if created and (fields["description"] != name or standard_after != standard_before):
        raise DesktopError("Saved VAT Description or preserved standard differs after reopening.")
    evidence = {"passed": True, "created": created, "closed_and_reopened": created,
                "fields": fields, "preserved_standard": standard_after,
                "table_evidence": table.evidence}
    evidence["report"] = _write_evidence(ui, "resolved-vat-" + uuid4().hex[:12] + ".json", evidence)
    if created:
        ledger.record_verified("vat", name, evidence=evidence)
    _close_clean(ui, name, identity_label="Name", identity=name)
    return evidence


def _reopen_product(ui, item, saved_title, *, currency):
    _close_clean(ui, saved_title, identity_label="Item Number", identity=item.sku)
    ui.click_label("Products", scope=_left_scope(ui, "New Order"))
    ui.set_text("Search:", item.sku, scope="Products")
    table, row = _product_table(ui, item, scope="Products", currency=currency)
    if row is None:
        raise WorkflowReview("product_not_retrieved", "Saved Product is absent from its complete list.",
                             sku=item.sku)
    rowselect(ui, table, row, clicks=2)
    ui.wait_tab(saved_title)
    return table.evidence


def add_source_product(ui, item: Item, ledger, journal_directory, *, currency="EUR"):
    """Select one exact SKU, creating and cold-checking missing masters first.

    Existing Product price/description/VAT are not rewritten. The caller must
    set and read back the new transaction line's source values separately.
    """
    selected_values, selector_evidence = _select_product(ui, item, currency=currency)
    if selected_values is not None:
        return {"sku": item.sku, "created": False, "selected_row": selected_values,
                "selector_evidence": selector_evidence, "line_verified": False}

    vat_evidence = resolve_vat(ui, item, ledger, journal_directory)
    ui.click_label("New product", scope=_left_scope(ui, "New Order"))
    ui.wait_tab("New product")
    before_save = fill_product_definition(ui, item, currency=currency)
    saved_title = _save_new_master(ui, ledger, kind="product", identity=item.sku,
                                   title="New product", label="Item Number",
                                   journal_directory=journal_directory)
    table_evidence = _reopen_product(ui, item, saved_title, currency=currency)
    fields = read_product_definition(ui, title=saved_title)
    require_matching_product(fields, item, currency=currency)
    if fields["unchanged_fields"] != before_save["unchanged_fields"]:
        raise DesktopError("An unsourced Product field differs after saving and reopening.")
    evidence = {"passed": True, "closed_and_reopened": True, "fields": fields,
                "table_evidence": table_evidence}
    evidence["report"] = _write_evidence(ui, "resolved-product-" + uuid4().hex[:12] + ".json", evidence)
    ledger.record_verified("product", item.sku, evidence=evidence)
    # Closing the cold-opened master also activates the original Order through
    # its observed native radio tab; it never opens a replacement Order.
    _close_clean(ui, saved_title, identity_label="Item Number", identity=item.sku)
    selected_values, selector_evidence = _select_product(ui, item, currency=currency)
    if selected_values is None:
        raise WorkflowReview("product_not_reselected", "Saved Product cannot be reselected for the retained Order.",
                             sku=item.sku)
    return {"sku": item.sku, "created": True, "selected_row": selected_values,
            "vat_evidence": vat_evidence, "product_readback": evidence,
            "selector_evidence": selector_evidence, "line_verified": False}
