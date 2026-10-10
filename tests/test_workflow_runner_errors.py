"""The ordinary runner retains startup stops, including numeric native codes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tjm import workflow
from tjm.desktop import DesktopError, _AXReadError
from tjm.models import RawOrder
from tjm.validation import validate_order


def test_numeric_native_startup_error_returns_durable_stop_before_any_save(monkeypatch, tmp_path):
    raw = json.loads((Path(__file__).parent / 'fixtures/sample_expected.json').read_text())['raw']
    raw['customer']['email'] = 'marta.klein@example.com'
    validated = validate_order(RawOrder.model_validate(raw), name_order='given-family')
    folder = tmp_path / 'run'
    folder.mkdir()
    extraction = {**validated.model_dump(mode='json'), 'raw': raw,
                  'evidence': {'input': {'path': '/demo.png', 'sha256': 'a' * 64},
                               'name_order': 'given-family', 'run_directory': str(folder)}}
    monkeypatch.setattr('tjm.extract.extract_order', lambda *a, **k: extraction)
    original = tmp_path / 'original.png'
    original.write_bytes(b'original source fixture')
    calls = []
    def unavailable(*args):
        calls.append('launch')
        raise _AXReadError('AXWindows', -25204)
    monkeypatch.setattr('tjm.demo_setup.launch_fresh_demo', unavailable)
    monkeypatch.setattr('tjm.live_workflow.execute_transaction',
                        lambda *a, **k: calls.append('UNAUTHORIZED BUSINESS INPUT'))
    report = workflow.run_image('/demo.png', workspace=tmp_path / 'data', output_dir=tmp_path,
                                 demonstration=True, original_image=original, fresh_demo=True)
    assert calls == ['launch']
    assert report['status'] == 'review_required'
    assert report['issues'][0]['code'] == 'workflow_stopped'
    assert '-25204' in report['issues'][0]['message']
    assert report['save_counts'] == {'save_intents': 0, 'with_persistence_evidence': 0}
    assert json.loads((folder / 'workflow-report.json').read_text()) == report


@pytest.mark.parametrize('prepared', [False, True])
def test_prepared_workspace_opt_in_reaches_transaction_without_launching(monkeypatch, tmp_path, prepared):
    raw = json.loads((Path(__file__).parent / 'fixtures/sample_expected.json').read_text())['raw']
    raw['customer']['email'] = 'marta.klein@example.com'
    validated = validate_order(RawOrder.model_validate(raw), name_order='given-family')
    folder = tmp_path / 'run'
    folder.mkdir()
    extraction = {**validated.model_dump(mode='json'), 'raw': raw,
                  'evidence': {'input': {'path': '/demo.png', 'sha256': 'a' * 64},
                               'name_order': 'given-family', 'run_directory': str(folder)}}
    monkeypatch.setattr('tjm.extract.extract_order', lambda *a, **k: extraction)
    original = tmp_path / 'original.png'
    original.write_bytes(b'original source fixture')
    workspace = tmp_path / 'prepared-data'
    workspace.mkdir()
    calls = []
    desktop = SimpleNamespace(activate=lambda: calls.append('activate double'))
    ui = SimpleNamespace(write_actions=lambda: calls.append('write actions double'))
    monkeypatch.setattr('tjm.readiness._select_workspace_desktop', lambda *a: (desktop, {}))
    monkeypatch.setattr('tjm.readiness.connect_readiness', lambda *a, **k: (ui, {'readiness double': True}))
    monkeypatch.setattr('tjm.demo_setup.launch_fresh_demo', lambda *a: pytest.fail('Prepared workspace must not launch a process.'))
    def transaction(*args, **options):
        calls.append(options)
        raise DesktopError('Stop in transaction routing double before any business input.')
    monkeypatch.setattr('tjm.live_workflow.execute_transaction', transaction)
    report = workflow.run_image('/demo.png', workspace=workspace, output_dir=tmp_path,
                                 demonstration=True, original_image=original, prepared_empty_demo=prepared)
    options = next(call for call in calls if isinstance(call, dict))
    assert options['fresh_launch'] is None
    assert options.get('prepared_empty_demo', False) is prepared
    if not prepared:
        assert 'prepared_empty_demo' not in options
        assert 'declared_prepared_empty_demo' not in report
    else:
        assert report['declared_prepared_empty_demo'] is True
    assert report['status'] == 'review_required'
    assert report['save_counts'] == {'save_intents': 0, 'with_persistence_evidence': 0}


@pytest.mark.parametrize(('options', 'message'), [
    ({'prepared_empty_demo': True}, 'restricted to a labelled demonstration'),
    ({'prepared_empty_demo': True, 'fresh_demo': True, 'demonstration': True,
      'original_image': '/original.png'}, 'either fresh demo setup or a prepared empty demo'),
])
def test_invalid_prepared_workspace_options_refuse_before_extraction(monkeypatch, options, message):
    monkeypatch.setattr('tjm.extract.extract_order', lambda *a, **k: pytest.fail('Invalid options must stop before extraction.'))
    with pytest.raises(ValueError, match=message):
        workflow.run_image('/source.png', workspace='/workspace', **options)
