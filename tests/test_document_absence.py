"""Repeat protection must inspect both types or the exact newly owned launch."""

from types import SimpleNamespace

import pytest

from tjm import document_sequence as module
from tjm.desktop import DesktopError
from tjm.workflow_rules import WorkflowReview


@pytest.fixture
def ui(monkeypatch, tmp_path):
    calls = []
    actual = SimpleNamespace(title='observed workspace', evidence_dir=tmp_path,
                              desktop=SimpleNamespace(identity=(12, 'Fakturama.ID', '/app', 3)),
                              click_label=lambda *a, **k: calls.append('Documents'),
                              set_text=lambda label, value, **kw: calls.append(('query', value)))
    monkeypatch.setattr(module, '_left_scope', lambda *args: None)
    monkeypatch.setattr(module, 'open_documents', lambda *a, **k: calls.append(k['category']) or {'category': k['category']})
    actual.calls = calls
    return actual


def test_both_parents_required_even_when_first_is_empty(ui, monkeypatch):
    tables = [SimpleNamespace(rows=[], evidence={'complete': True}),
              SimpleNamespace(rows=[{'values': {'Total': 'EUR1.00'}}], evidence={'complete': True})]
    monkeypatch.setattr(module, 'read_complete_table', lambda *a, **k: tables.pop(0))
    with pytest.raises(WorkflowReview) as error:
        module.require_no_documents(ui, 'SOURCE', current_title='home')
    assert error.value.code == 'document_reference_exists'
    assert ui.calls == ['Orders', ('query', 'SOURCE'), 'Invoices', ('query', 'SOURCE')]


def test_two_empty_complete_parent_queries_pass(ui, monkeypatch):
    monkeypatch.setattr(module, 'read_complete_table', lambda *a, **k: SimpleNamespace(rows=[], evidence={'complete': True}))
    result = module.require_no_documents(ui, 'SOURCE', current_title='home')
    assert result['passed'] and len(result['categories']) == 2


def test_fresh_receipt_cannot_identify_another_process(ui):
    with pytest.raises(DesktopError, match='newly owned'):
        module.require_no_documents(ui, 'SOURCE', current_title='home', fresh_launch={
            'previously_absent': True, 'window_title': ui.title, 'process_identity': [99, 'Fakturama.ID', '/app', 3]})
    assert ui.calls == []


def test_owned_fresh_launch_still_requires_empty_complete_visible_query(ui, monkeypatch):
    monkeypatch.setattr(module, 'read_complete_table', lambda *a, **k: SimpleNamespace(rows=[{}], evidence={'complete': True}))
    with pytest.raises(WorkflowReview, match='query results'):
        module.require_no_documents(ui, 'SOURCE', current_title='home', fresh_launch={
            'previously_absent': True, 'window_title': ui.title, 'process_identity': list(ui.desktop.identity)})


def test_prepared_demo_query_reports_observation_without_claiming_cross_type_absence(ui, monkeypatch):
    monkeypatch.setattr(module, 'read_complete_table', lambda *a, **k: SimpleNamespace(rows=[], evidence={'complete': True}))
    result = module.check_prepared_demo_query(ui, 'SOURCE', current_title='home')
    assert result['visible_query_empty_verified'] is True
    assert result['cross_type_document_absence_verified'] is False
    assert 'environment_precondition' in result
    assert ui.calls == ['Documents', ('query', 'SOURCE')]


def test_prepared_demo_positive_query_stops_before_business_input(ui, monkeypatch):
    monkeypatch.setattr(module, 'read_complete_table', lambda *a, **k: SimpleNamespace(rows=[{}], evidence={'complete': True}))
    with pytest.raises(WorkflowReview, match='query results'):
        module.check_prepared_demo_query(ui, 'SOURCE', current_title='home')


def test_prepared_demo_empty_reference_is_refused_before_query(ui):
    with pytest.raises(ValueError, match='nonblank'):
        module.check_prepared_demo_query(ui, ' ', current_title='home')
    assert ui.calls == []
