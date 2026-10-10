"""One sequential source transaction using the demonstrated UI operations."""

from .contact_sequence import _save_new_master, _select_contact, resolve_contact
from .desktop import DesktopError
from .document_cleanup import close_verified_inspection
from .document_content import collect_document_content, parse_item_money
from .document_presentation import prepare_document_presentation
from .document_sequence import (
    check_prepared_demo_query, parse_document_date, read_transaction_documents_by_reference,
    reopen_saved_document_by_native_identity, require_no_documents,
)
from .document_workflow import (
    candidate_delivery_text, close_saved_document, create_invoice_followup,
    read_addresses, read_document_header, read_invoice_payment, set_invoice_payment,
    source_address,
)
from .item_vat import ensure_item_vat
from .native_items import read_painted_item_cell, set_item_cell
from .product_sequence import add_source_product
from .workflow import begin_order, editor_scope
from .workflow_report import (
    complete_run, completion_expectations, content_expectations, record_check,
    set_generated_number, sync_ledger, write_run_report,
)


def execute_transaction(ui, order, report, ledger, folder, *, readiness,
                         allow_demo_payment_sequence=False, fresh_launch=None,
                         prepared_empty_demo=False):
    """No recovery loop: each operation runs once; the caller retains failures."""
    evidence = report['evidence']
    evidence['readiness'] = readiness
    # Retain read-only process re-observations without replaying business input.
    evidence['process_readiness_rechecks'] = getattr(getattr(ui, 'desktop', None), 'process_rechecks', [])
    journal = folder / 'save-intents'

    def phase(name):
        report['phase'] = name
        sync_ledger(report, ledger)
        write_run_report(report, folder / 'workflow-report.json')

    def check(name, observed, expected, proof, stage='pre_save'):
        record_check(report, name, expected=expected, observed=observed,
                     passed=observed == expected, evidence=proof, stage=stage)
        if observed != expected:
            raise DesktopError('Actual ' + name + ' differs from the validated source.')

    current = next(group['selected_title'] for group in readiness['tabs']['groups']
                   if 'Fakturama' in group['tabs'])
    if prepared_empty_demo:
        if report.get('provenance', {}).get('input_kind') != 'demonstration' or fresh_launch is not None:
            raise DesktopError('The prepared empty workspace route requires a labelled demo and separate environment setup.')
        groups = readiness['tabs']['groups']
        if (len(groups) != 2 or sorted(group['tabs'] for group in groups) != [['Documents'], ['Fakturama']]):
            raise DesktopError('The prepared demo must start with only the clean Fakturama home and Documents tabs.')
        phase('verify visible query in the declared prepared empty demo environment')
        evidence['prepared_demo_environment'] = check_prepared_demo_query(
            ui, order.external_reference, current_title=current)
    else:
        phase('repeat protection before business input')
        evidence['document_absence'] = require_no_documents(
            ui, order.external_reference, current_title=current, fresh_launch=fresh_launch)
    phase('open and retain original Order')
    editor = begin_order(ui, order)
    set_generated_number(report, 'order', editor.proposed_number)
    phase('resolve source customer and payment')
    contact = resolve_contact(ui, order, ledger, journal,
                              allow_demo_payment_sequence=allow_demo_payment_sequence)
    evidence['contact'] = contact
    addresses = read_addresses(ui, title='New Order')
    expected_addresses = {'billing': source_address(order.customer.billing),
                          'delivery': source_address(order.customer.delivery)}
    if contact['created'] and addresses != expected_addresses:
        # read_addresses leaves Delivery active. The same exact billing contact
        # selection registers its binding; the source warehouse text follows.
        selected, selection = _select_contact(ui, order.customer, expected_id=contact['customer_id'])
        if selected != contact['customer_id']:
            raise DesktopError('Delivery binding reselected another customer.')
        evidence['delivery_binding_selection'] = selection
        addresses = candidate_delivery_text(ui, order.customer, title='New Order')
        capture = ui.snapshot('commit-source-delivery-focus')
        identity = ui.field(capture, 'No.', scope=editor_scope(ui, capture, 'New Order', before='Addresses'))
        ui.desktop.click(capture, identity.bounds.x + identity.bounds.width / 2,
                         identity.bounds.y + identity.bounds.height / 2)
    check('source_addresses_after_selection', addresses, expected_addresses,
          {'contact_selection': contact['selector_evidence'], 'created': contact['created']})
    header = read_document_header(ui, title='New Order')
    check('retained_order_header',
          {key: header[key] for key in ('number', 'date', 'reference', 'price_mode', 'vat_mode')},
          {'number': editor.proposed_number, 'date': order.order_date.strftime('%b %d, %Y'),
           'reference': order.external_reference, 'price_mode': 'Net', 'vat_mode': 'With VAT'},
          {'native_header': header})
    evidence['products'] = []
    for index, item in enumerate(order.items):
        phase('resolve source Product ' + str(index + 1) + ': ' + item.sku)
        master = add_source_product(ui, item, ledger, journal, currency=order.currency)
        row = {'source_index': index, 'master': master, 'cells': {}}
        evidence['products'].append(row)
        phase('prepare VAT display for source item ' + str(index + 1))
        row['presentation'] = prepare_document_presentation(
            ui, order, title='New Order', expected_row_count=index + 1)
        phase('complete source item ' + str(index + 1) + ': ' + item.sku)
        row['vat'] = ensure_item_vat(ui, item, ledger, journal, row_index=index, title='New Order')
        common = {'row_index': index, 'expected_sku': item.sku, 'title': 'New Order'}
        for label, value, reopened in (
            ('Qty.', str(item.quantity), f'{item.quantity:.2f}'),
            ('Description', item.description, item.description),
            ('U.Price', f'{item.unit_net:.2f}', 'EUR ' + format(item.unit_net.normalize(), 'f')),
            ('Discount', str(item.discount_percent),
             f'{-item.discount_percent:.2f} %' if item.discount_percent else '0.00 %'),
        ):
            row['cells'][label] = set_item_cell(ui, header=label, value=value,
                                               expected_reopened=reopened, **common)
        price = read_painted_item_cell(ui, header='Price', **common)
        expected_price, actual_price = f'{item.line_net:.2f}', parse_item_money(price['text'])
        row['line_price'] = {'expected': expected_price, 'observed': actual_price,
                             'evidence': price, 'passed': actual_price == expected_price}
        if actual_price != expected_price:
            raise DesktopError('Actual source line Price differs before selecting the next Product.')
    phase('verify complete source Order before its single Save')
    evidence['order_presentation'] = evidence['products'][-1]['presentation']
    hot_order = collect_document_content(ui, order, title='New Order', retained_number=editor.proposed_number)
    evidence['order_pre_save_content'] = hot_order
    # Keep the edited Delivery binding active. Selecting Invoice here is not a
    # substitute for processing the newly entered warehouse address.
    ui.click_label('Delivery address', scope='New Order')
    phase('single Order Save')
    order_title = _save_new_master(ui, ledger, kind='Order', identity=editor.proposed_number,
                                   title='New Order', label='No.', journal_directory=journal,
                                   identity_before='Addresses')
    evidence['order_close'] = close_saved_document(
        ui, title=order_title, number=editor.proposed_number, reference=order.external_reference)
    phase('reopen saved Order and verify state and complete content')
    retrieved = reopen_saved_document_by_native_identity(
        ui, retained_number=editor.proposed_number, external_reference=order.external_reference,
        category='Orders', current_title=evidence['order_close']['remaining_editor'], saved_title=order_title)
    evidence['order_retrieval'] = retrieved
    expected_order_state = {'number': editor.proposed_number, 'date': order.order_date.isoformat(),
                            'external_reference': order.external_reference, 'state': 'Open',
                            'total': f'{order.totals.gross:.2f}'}
    check('order_persistence', retrieved['observation'], expected_order_state, retrieved, 'saved_list')
    evidence['order_cold_presentation'] = prepare_document_presentation(ui, order, title=order_title)
    cold_order = collect_document_content(ui, order, title=order_title, retained_number=editor.proposed_number)
    check('order_content', cold_order['observed'], content_expectations(report)['order_content'], cold_order, 'reopened')
    ledger.record_verified('Order', editor.proposed_number,
                           evidence={'passed': True, 'closed_and_reopened': True,
                                     'retrieval_report': retrieved['report'], 'content_report': cold_order['report']})
    evidence['order_inspection_close'] = close_verified_inspection(
        ui, title=order_title, number=editor.proposed_number, reference=order.external_reference,
        content_evidence=cold_order)
    evidence['clean_order_reopen'] = reopen_saved_document_by_native_identity(
        ui, retained_number=editor.proposed_number, external_reference=order.external_reference,
        category='Orders', current_title=evidence['order_inspection_close']['remaining_editor'], saved_title=order_title)
    phase('create linked Invoice from saved Order')
    followup = create_invoice_followup(ui, saved_title=order_title, number=editor.proposed_number,
                                       reference=order.external_reference)
    evidence['followup_action'] = followup
    proposed = followup['proposed_header']
    invoice_number = proposed['number']
    followup_evidence = {
        'passed': followup['action_proof']['requested'] is True,
        'area': 'Create a follow-up document', 'action': 'Invoice',
        'order_number': evidence['clean_order_reopen']['header']['number'],
        'invoice_number': proposed['number'], 'ui_evidence': followup,
    }
    evidence['followup_action'] = followup_evidence
    set_generated_number(report, 'invoice', invoice_number)
    phase('verify copied Invoice content and apply source payment')
    evidence['invoice_presentation'] = prepare_document_presentation(ui, order, title='New Invoice')
    evidence['invoice_pre_save_content'] = collect_document_content(
        ui, order, title='New Invoice', retained_number=invoice_number, invoice=True)
    evidence['invoice_payment_before_save'] = set_invoice_payment(ui, order.payment, title='New Invoice', currency=order.currency)
    check('invoice_proposed_header_before_save', read_document_header(ui, title='New Invoice', invoice=True),
          proposed, {'followup_action': followup})
    # Addresses are copied and unedited. The normal Invoice binding was proved
    # by the stage run; require both complete addresses again after reopening.
    ui.click_label('Invoice address', scope='New Invoice')
    phase('single Invoice Save')
    invoice_title = _save_new_master(ui, ledger, kind='Invoice', identity=invoice_number,
                                     title='New Invoice', label='No.', journal_directory=journal,
                                     identity_before='Addresses')
    evidence['invoice_close'] = close_saved_document(
        ui, title=invoice_title, number=invoice_number, reference=order.external_reference)
    phase('reopen Invoice and verify all content, proposed dates and payment')
    retrieval = reopen_saved_document_by_native_identity(
        ui, retained_number=invoice_number, external_reference=order.external_reference,
        category='Invoices', current_title=evidence['invoice_close']['remaining_editor'], saved_title=invoice_title)
    evidence['invoice_retrieval'] = retrieval
    evidence['invoice_cold_presentation'] = prepare_document_presentation(ui, order, title=invoice_title)
    cold_invoice = collect_document_content(ui, order, title=invoice_title, retained_number=invoice_number, invoice=True)
    check('invoice_content', cold_invoice['observed'], content_expectations(report)['invoice_content'], cold_invoice, 'reopened')
    cold_header = read_document_header(ui, title=invoice_title, invoice=True)
    check('invoice_proposed_header_after_reopen', cold_header, proposed, {'content_report': cold_invoice['report']}, 'reopened')
    payment = read_invoice_payment(ui, title=invoice_title, currency=order.currency)
    actual_payment = {'number': cold_header['number'], 'method': payment['method'], 'paid': payment['paid'],
                      'date': parse_document_date(payment['date']) if payment['paid'] else None,
                      'amount': payment['amount'] if payment['paid'] else None}
    expected = completion_expectations(report)
    check('invoice_payment', actual_payment, expected['invoice_payment'], payment, 'reopened')
    ledger.record_verified('Invoice', invoice_number,
                           evidence={'passed': True, 'closed_and_reopened': True,
                                     'retrieval_report': retrieval['report'], 'content_report': cold_invoice['report'],
                                     'payment': payment})
    evidence['invoice_inspection_close'] = close_verified_inspection(
        ui, title=invoice_title, number=invoice_number, reference=order.external_reference,
        content_evidence=cold_invoice)
    phase('verify paid Invoice, open source Order and persisted transaction link')
    transaction = read_transaction_documents_by_reference(
        ui, invoice_number=invoice_number, order_number=editor.proposed_number,
        external_reference=order.external_reference,
        current_title=evidence['invoice_inspection_close']['remaining_editor'],
        invoice_title=invoice_title, order_title=order_title)
    evidence['persisted_linkage'] = transaction
    check('invoice_persistence', transaction['invoice_observation'], expected['invoice_persistence'], transaction, 'saved_list')
    order_open = {key: value for key, value in transaction['order_observation'].items() if key != 'date'}
    check('order_open', order_open, expected['order_open'], transaction, 'saved_list')
    actual_invoice_header = transaction['ui_evidence']['invoice_native_identity']['native_header']
    linkage = {'invoice_number': transaction['invoice_number'], 'order_number': transaction['order_number'],
               'order_date': parse_document_date(actual_invoice_header['order_date']),
               'external_reference': actual_invoice_header['reference']}
    check('invoice_order_linkage', linkage, expected['invoice_order_linkage'],
          {'followup_action': followup_evidence, 'persisted_linkage': transaction}, 'reopened')
    phase('all required transaction observations collected')
    return complete_run(report, ledger)
