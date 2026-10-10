"""Runner finalization tests with simulated UI adapters."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm import workflow
from tjm.desktop import DesktopError
from tjm.models import RawOrder
from tjm.validation import validate_order
from tjm.workflow_report import (
    complete_run, completion_expectations, record_check, set_generated_number,
)


@pytest.fixture
def runner_scene(monkeypatch, tmp_path):
    raw = json.loads((Path(__file__).parent / 'fixtures/sample_expected.json').read_text())['raw']
    raw['customer']['email'] = 'marta.klein@example.com'
    validation = validate_order(RawOrder.model_validate(raw), name_order='given-family')
    folder = tmp_path / 'run'
    folder.mkdir()
    extraction = {**validation.model_dump(mode='json'), 'raw': raw,
                  'evidence': {'input': {'path': '/demo.png', 'sha256': 'a' * 64},
                               'name_order': 'given-family', 'run_directory': str(folder)}}
    monkeypatch.setattr('tjm.extract.extract_order', lambda *a, **k: extraction)
    original = tmp_path / 'original.png'
    original.write_bytes(b'original source fixture')
    workspace = tmp_path / 'prepared-data'
    workspace.mkdir()
    calls = []
    desktop = SimpleNamespace(activate=lambda: calls.append('activate'))
    ui = SimpleNamespace(write_actions=lambda: calls.append('write_actions'))
    monkeypatch.setattr('tjm.readiness._select_workspace_desktop', lambda *a: (desktop, {}))
    monkeypatch.setattr('tjm.readiness.connect_readiness', lambda *a, **k: (ui, {}))
    return SimpleNamespace(folder=folder, original=original, workspace=workspace, calls=calls)


def run(scene):
    return workflow.run_image('/demo.png', workspace=scene.workspace,
                              output_dir=scene.folder.parent, demonstration=True,
                              original_image=scene.original, prepared_empty_demo=True)


def test_successful_runner_returns_and_writes_verified_without_resyncing_ledger(
        monkeypatch, runner_scene):
    verified_snapshot = {}

    def transaction(ui, order, report, ledger, folder, **options):
        set_generated_number(report, 'order', 'O-A')
        set_generated_number(report, 'invoice', 'I-Z')
        for kind, identity in (('payment', 'Bank Transfer'), ('Order', 'O-A'), ('Invoice', 'I-Z')):
            ledger.record_intent(kind, identity)
            ledger.record_verified(kind, identity, evidence={'passed': True, 'ui_double': True})
        for name, expected in completion_expectations(report).items():
            evidence = {'ui_double': True}
            if name == 'invoice_order_linkage':
                identities = {key: expected[key] for key in ('order_number', 'invoice_number')}
                evidence = {
                    'followup_action': {**identities, 'area': 'Create a follow-up document',
                                        'action': 'Invoice', 'passed': True,
                                        'ui_evidence': {'ui_double': True}},
                    'persisted_linkage': {**identities, 'method': 'This transaction',
                                          'passed': True, 'ui_evidence': {'ui_double': True}},
                }
            record_check(report, name, expected=expected, observed=expected,
                         passed=True, evidence=evidence, stage='reopened')
        assert complete_run(report, ledger)['status'] == 'verified'
        verified_snapshot.update(deepcopy(report))

    monkeypatch.setattr('tjm.live_workflow.execute_transaction', transaction)
    report = run(runner_scene)
    assert report == verified_snapshot
    assert report['status'] == 'verified'
    assert report['save_counts'] == {'save_intents': 3, 'with_persistence_evidence': 3}
    assert report['provenance']['input_kind'] == 'demonstration'
    assert json.loads((runner_scene.folder / 'workflow-report.json').read_text()) == report
    assert runner_scene.calls == ['activate', 'write_actions']


def test_stopped_runner_still_syncs_one_uncertain_save_and_writes_actions(
        monkeypatch, runner_scene):
    calls = []

    def transaction(ui, order, report, ledger, folder, **options):
        calls.append('transaction')
        ledger.record_intent('Order', 'O-A')
        # No persistence proof and no report sync: finalization must retain this intention.
        raise DesktopError('Unknown first Save outcome')

    monkeypatch.setattr('tjm.live_workflow.execute_transaction', transaction)
    report = run(runner_scene)
    assert calls == ['transaction']
    assert report['status'] == 'review_required'
    assert report['save_counts'] == {'save_intents': 1, 'with_persistence_evidence': 0}
    assert report['save_ledger'][0]['identity'] == 'O-A'
    assert report['save_ledger'][0]['persistence_evidence'] is None
    assert report['issues'][0]['message'] == 'Unknown first Save outcome'
    assert json.loads((runner_scene.folder / 'workflow-report.json').read_text()) == report
    assert runner_scene.calls == ['activate', 'write_actions']
