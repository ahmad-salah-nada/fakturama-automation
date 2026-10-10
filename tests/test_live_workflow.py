"""Sequential workflow tests with simulated UI adapters."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm import live_workflow as module
from tjm.desktop import DesktopError
from tjm.models import RawOrder
from tjm.validation import validate_order
from tjm.workflow_report import content_expectations, new_run_report
from tjm.workflow_rules import SaveLedger


@pytest.fixture
def scene(monkeypatch, tmp_path):
    raw = json.loads((Path(__file__).parent / 'fixtures/sample_expected.json').read_text())['raw']
    raw['customer']['email'] = 'marta.klein@example.com'
    validation = validate_order(RawOrder.model_validate(raw), name_order='given-family')
    extraction = {**validation.model_dump(mode='json'), 'raw': raw,
                  'evidence': {'input': {'path': '/demo.png', 'sha256': 'a' * 64},
                               'name_order': 'given-family'}}
    report = new_run_report(extraction, workspace=str(tmp_path), input_kind='demonstration',
                             demo_label='test', original_input_sha256='b' * 64)
    report['evidence'] = {}
    order = validation.order
    calls = []
    header = {'number': 'O-A', 'date': 'Jul 14, 2026', 'reference': order.external_reference,
              'price_mode': 'Net', 'vat_mode': 'With VAT'}
    proposed = header | {'number': 'I-Z', 'date': 'Oct 8, 2026',
                         'service_date': 'Oct 8, 2026', 'order_date': 'Jul 14, 2026'}
    state = SimpleNamespace(order=order, report=report, ledger=SaveLedger(), folder=tmp_path,
                              calls=calls, cold_bad=False, save_uncertain=False,
                              payment_method=order.payment.method)
    ui = SimpleNamespace(click_label=lambda *a, **k: None)
    state.ui = ui
    monkeypatch.setattr(module, 'require_no_documents', lambda *a, **k: calls.append('absence') or {'passed': True})
    monkeypatch.setattr(module, 'begin_order', lambda *a: calls.append('begin') or SimpleNamespace(proposed_number='O-A'))
    monkeypatch.setattr(module, 'resolve_contact', lambda *a, **k: {'customer_id': 'C-A', 'created': False,
                                                                   'selector_evidence': {'actual': True}})
    monkeypatch.setattr(module, 'read_addresses', lambda *a, **k: {
        'billing': module.source_address(order.customer.billing), 'delivery': module.source_address(order.customer.delivery)})
    monkeypatch.setattr(module, 'read_document_header', lambda *a, **k:
                        deepcopy(proposed if k.get('invoice') else header))
    monkeypatch.setattr(module, 'add_source_product', lambda ui, item, *a, **k:
                        calls.append(('product', item.sku)) or {'created': False})
    monkeypatch.setattr(module, 'set_item_cell', lambda *a, **k: {'actual': k['expected_reopened']})
    monkeypatch.setattr(module, 'read_painted_item_cell', lambda *a, row_index, **k:
                        {'text': 'EUR' + f'{order.items[row_index].line_net:.2f}', 'scope': 'painted line Price read double'})
    monkeypatch.setattr(module, 'ensure_item_vat', lambda *a, **k: {'verified': True, 'ui_double': True})
    monkeypatch.setattr(module, 'prepare_document_presentation', lambda *a, **k: {'passed': True})

    def collect(ui, order, *, title, retained_number, invoice=False):
        kind = 'invoice' if invoice else 'order'
        observed = content_expectations({'order': order.model_dump(mode='json'),
                                          'generated_numbers': {kind: retained_number}})[kind + '_content']
        if state.cold_bad and title == 'O-A':
            observed['totals']['gross'] = '1.00'
        return {'passed': True, 'report': 'actual-content.json', 'observed': observed}
    monkeypatch.setattr(module, 'collect_document_content', collect)

    def save(ui, ledger, *, kind, identity, **kw):
        ledger.record_intent(kind, identity)
        calls.append(('save', kind, identity))
        if state.save_uncertain:
            raise DesktopError('Unknown first Save outcome')
        return identity
    monkeypatch.setattr(module, '_save_new_master', save)
    monkeypatch.setattr(module, 'close_saved_document', lambda *a, **k: {'remaining_editor': 'Fakturama', 'passed': True})
    monkeypatch.setattr(module, 'close_verified_inspection', lambda *a, **k: {'remaining_editor': 'Fakturama', 'passed': True})
    order_state = {'number': 'O-A', 'date': '2026-07-14', 'external_reference': order.external_reference,
                   'state': 'Open', 'total': '678.30'}
    invoice_state = {'number': 'I-Z', 'external_reference': order.external_reference, 'paid': True, 'total': '678.30'}
    monkeypatch.setattr(module, 'reopen_saved_document_by_native_identity', lambda *a, **k: {
        'passed': True, 'header': deepcopy(proposed if k['category'] == 'Invoices' else header),
        'report': 'actual-retrieval.json', 'observation': deepcopy(invoice_state if k['category'] == 'Invoices' else order_state)})
    monkeypatch.setattr(module, 'create_invoice_followup', lambda *a, **k:
                        calls.append('followup') or {'proposed_header': deepcopy(proposed),
                                                     'action_proof': {'requested': True}})
    monkeypatch.setattr(module, 'set_invoice_payment', lambda *a, **k: {'passed': True})
    monkeypatch.setattr(module, 'read_invoice_payment', lambda *a, **k:
                        {'method': state.payment_method, 'paid': True, 'date': 'Jul 18, 2026', 'amount': '678.30'})
    monkeypatch.setattr(module, 'read_transaction_documents_by_reference', lambda *a, **k: {
        'passed': True, 'method': 'This transaction', 'order_number': 'O-A', 'invoice_number': 'I-Z',
        'order_observation': deepcopy(order_state), 'invoice_observation': deepcopy(invoice_state),
        'ui_evidence': {'invoice_native_identity': {'native_header': deepcopy(proposed)}}})
    return state


def execute(scene):
    return module.execute_transaction(scene.ui, scene.order, scene.report, scene.ledger, scene.folder,
                                       readiness={'tabs': {'groups': [{'selected_title': 'Fakturama', 'tabs': ['Fakturama']}]}})


def test_complete_sequential_run_uses_one_order_and_one_invoice_save(scene):
    result = execute(scene)
    assert result['status'] == 'verified'
    assert result['provenance']['input_kind'] == 'demonstration'
    assert [call for call in scene.calls if isinstance(call, tuple) and call[0] == 'save'] == [
        ('save', 'Order', 'O-A'), ('save', 'Invoice', 'I-Z')]
    assert scene.calls.index(('save', 'Order', 'O-A')) < scene.calls.index('followup')
    assert result['save_counts'] == {'save_intents': 2, 'with_persistence_evidence': 2}
    assert [(row['line_price']['expected'], row['line_price']['observed'], row['line_price']['passed'])
            for row in result['evidence']['products']] == [
        ('450.00', '450.00', True), ('120.00', '120.00', True)]
    assert len(result['checks']) == 11


def test_uncertain_first_save_never_repeats_or_creates_followup(scene):
    scene.save_uncertain = True
    with pytest.raises(DesktopError, match='Unknown first Save'):
        execute(scene)
    assert scene.ledger.as_json()[0]['persistence_evidence'] is None
    assert [call for call in scene.calls if isinstance(call, tuple) and call[0] == 'save'] == [('save', 'Order', 'O-A')]
    assert 'followup' not in scene.calls


def test_wrong_first_calculated_price_stops_before_next_product_or_any_save(scene, monkeypatch):
    monkeypatch.setattr(module, 'read_painted_item_cell', lambda *a, **k: {'text': 'EUR449.00'})
    with pytest.raises(DesktopError, match='before selecting the next Product'):
        execute(scene)
    assert [call for call in scene.calls if isinstance(call, tuple) and call[0] == 'product'] == [
        ('product', scene.order.items[0].sku)]
    assert scene.ledger.as_json() == [] and 'followup' not in scene.calls
    price = scene.report['evidence']['products'][0]['line_price']
    assert price['expected'] == '450.00' and price['observed'] == '449.00' and price['passed'] is False


def test_cold_order_mismatch_stops_before_followup(scene):
    scene.cold_bad = True
    with pytest.raises(DesktopError, match='order_content'):
        execute(scene)
    assert 'followup' not in scene.calls


def test_actual_wrong_payment_is_not_replaced_with_source_method(scene):
    scene.payment_method = 'Pay Cash'
    with pytest.raises(DesktopError, match='invoice_payment'):
        execute(scene)
    assert scene.report['checks'][-1]['observed']['method'] == 'Pay Cash'
    assert scene.report['checks'][-1]['passed'] is False


def prepared_readiness():
    return {'tabs': {'groups': [
        {'selected_title': 'Documents', 'tabs': ['Documents']},
        {'selected_title': 'Fakturama', 'tabs': ['Fakturama']},
    ]}}


def test_explicit_prepared_demo_retains_unverified_cross_type_scope(scene, monkeypatch):
    def reject_guard(*a, **k):
        pytest.fail('The expressly excluded cross-type guard must not run for this demo.')
    monkeypatch.setattr(module, 'require_no_documents', reject_guard)
    monkeypatch.setattr(module, 'check_prepared_demo_query', lambda *a, **k: {
        'visible_query_empty_verified': True, 'cross_type_document_absence_verified': False})
    result = module.execute_transaction(scene.ui, scene.order, scene.report, scene.ledger, scene.folder,
                                        readiness=prepared_readiness(), prepared_empty_demo=True)
    assert result['status'] == 'verified'
    assert result['provenance']['input_kind'] == 'demonstration'
    assert result['evidence']['prepared_demo_environment']['cross_type_document_absence_verified'] is False
    assert 'document_absence' not in result['evidence']


def test_prepared_workspace_override_cannot_apply_to_original_input(scene):
    scene.report['provenance']['input_kind'] = 'original'
    with pytest.raises(DesktopError, match='labelled demo'):
        module.execute_transaction(scene.ui, scene.order, scene.report, scene.ledger, scene.folder,
                                    readiness=prepared_readiness(), prepared_empty_demo=True)
    assert scene.calls == []


def test_prepared_demo_refuses_open_editor_before_input(scene):
    ready = prepared_readiness()
    ready['tabs']['groups'][1]['tabs'].append('PO-EXISTING')
    with pytest.raises(DesktopError, match='only the clean'):
        module.execute_transaction(scene.ui, scene.order, scene.report, scene.ledger, scene.folder,
                                    readiness=ready, prepared_empty_demo=True)
    assert scene.calls == []


def test_prepared_demo_cannot_be_combined_with_automatic_initialization(scene):
    with pytest.raises(DesktopError, match='separate environment setup'):
        module.execute_transaction(scene.ui, scene.order, scene.report, scene.ledger, scene.folder,
                                    readiness=prepared_readiness(), prepared_empty_demo=True, fresh_launch={})
    assert scene.calls == []


def test_reused_products_keep_masters_and_apply_source_description_price_and_vat(
        scene, monkeypatch, tmp_path):
    """Real product-reuse/VAT writer/content comparison; native field reads doubled."""
    from test_document_content import actual_reads
    from test_item_vat import SelectionNative
    from tjm import document_content, item_vat, native_items, product_sequence

    raw = actual_reads()
    raw["header"]["number"] = "O-A"
    masters = {item.sku: {"sku": item.sku, "description": "Existing master description",
                          "gross": "EUR 1.00", "vat": "VAT 7%"} for item in scene.order.items}
    before = deepcopy(masters)
    for row in raw["items"]:
        row.update(Description="Existing master description", **{"U.Price": "EUR1.00", "Qty.": "99.00"})
        row["VAT"]["text"] = "VAT 7% (7.0%)"
    native = SelectionNative()
    ui = scene.ui
    ui.desktop, ui.title, ui.evidence_dir = native, "Main", tmp_path
    ui.actions, ui.discoveries = [], []
    active_row = None
    changes = []
    selected_skus, presentations = [], []
    original_action = native.perform_action

    def open_popup(actual_ui, **options):
        nonlocal active_row
        active_row = options["row_index"]
        selected = 0 if raw["items"][active_row]["VAT"]["text"] == "VAT 7% (7.0%)" else 1
        native.table.attributes["AXSelectedRows"] = [native.rows[selected]]
        return native.open(actual_ui, **options)

    def select_vat(element, action):
        result = original_action(element, action)
        raw["items"][active_row]["VAT"]["text"] = "VAT 19% (19.0%)"
        # Simulate VAT-dependent repricing: source price must be entered later.
        raw["items"][active_row]["U.Price"] = "EUR99.00"
        changes.append((active_row, "VAT"))
        return result

    def set_cell(actual_ui, *, row_index, header, expected_reopened, **options):
        changes.append((row_index, header))
        raw["items"][row_index][header] = expected_reopened
        return {"actual": expected_reopened, "scope": "native field edit double"}

    def no_master_write(*args, **options):
        pytest.fail("Reused Product master fields must not be written or saved.")

    def select_product(ui, item, **options):
        selected_skus.append(item.sku)
        return {"Item No.": masters[item.sku]["sku"]}, {"scope": "exact existing SKU selection double"}

    def prepare(ui, order, *, title, expected_row_count=2):
        presentations.append((title, expected_row_count))
        if title == "New Order":
            assert selected_skus == [item.sku for item in order.items[:expected_row_count]]
            assert changes == [(index, field) for index in range(expected_row_count - 1)
                               for field in ("VAT", "Qty.", "Description", "U.Price", "Discount")]
        return {"passed": True, "scope": "complete two-row presentation double"}

    native.perform_action = select_vat
    monkeypatch.setattr(native_items, "open_item_vat_popup", open_popup)
    monkeypatch.setattr(native_items, "read_painted_item_cell", lambda *a, row_index, **k:
                        raw["items"][row_index]["VAT"])
    monkeypatch.setattr(product_sequence, "_select_product", select_product)
    monkeypatch.setattr(product_sequence, "fill_product_definition", no_master_write)
    monkeypatch.setattr(product_sequence, "_save_new_master", no_master_write)
    monkeypatch.setattr(product_sequence, "resolve_vat", no_master_write)
    monkeypatch.setattr(module, "add_source_product", product_sequence.add_source_product)
    monkeypatch.setattr(module, "ensure_item_vat", item_vat.ensure_item_vat)
    monkeypatch.setattr(module, "prepare_document_presentation", prepare)
    monkeypatch.setattr(module, "set_item_cell", set_cell)
    monkeypatch.setattr(module, "read_painted_item_cell", lambda *a, row_index, **k:
                        raw["items"][row_index]["Price"])
    monkeypatch.setattr(module, "collect_document_content", document_content.collect_document_content)
    monkeypatch.setattr(document_content, "read_document_header", lambda *a, **k:
                        raw["header"] | {"number": "I-Z", "order_date": "Jul 14, 2026"}
                        if k.get("invoice") else raw["header"])
    monkeypatch.setattr(document_content, "read_addresses", lambda *a, **k: raw["addresses"])
    monkeypatch.setattr(document_content, "read_document_totals", lambda *a, **k: raw["summary"])
    monkeypatch.setattr(document_content, "_row_coverage", lambda *a: {"passed": True, "observed_count": 2})
    monkeypatch.setattr(document_content, "read_item_cell", lambda *a, row_index, header, **k:
                        raw["items"][row_index][header])
    monkeypatch.setattr(document_content, "read_painted_item_cell", lambda *a, row_index, header, **k:
                        raw["items"][row_index][header])
    result = execute(scene)
    assert result["status"] == "verified" and masters == before
    assert all(row["master"]["created"] is False and row["vat"]["verified"] is True
               for row in result["evidence"]["products"])
    assert changes == [(index, field) for index in (0, 1)
                       for field in ("VAT", "Qty.", "Description", "U.Price", "Discount")]
    assert len(native.dispatches) == 2
    assert [count for title, count in presentations if title == "New Order"] == [1, 2]
    assert [entry["kind"] for entry in scene.ledger.as_json()] == ["Order", "Invoice"]
    for kind in ("order_content", "invoice_content"):
        check = next(check for check in result["checks"] if check["name"] == kind)
        assert check["passed"] is True
        assert check["observed"]["items"][0]["vat_percent"] == "19"
