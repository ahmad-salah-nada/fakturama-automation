"""CLI routing checks use a fake runner and never inspect or operate a desktop."""

import builtins
import json
import sys
from types import SimpleNamespace

import pytest

import tjm.cli as cli


@pytest.fixture
def fake_workflow(monkeypatch):
    calls = []
    report = {"status": "verified", "provenance": {"input_kind": "original"}}

    def run_image(image, **kwargs):
        calls.append((image, kwargs))
        return report

    monkeypatch.setitem(sys.modules, "tjm.workflow", SimpleNamespace(run_image=run_image))
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return calls, report


@pytest.fixture
def reject_workflow_import(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"workflow", "tjm.workflow", "desktop", "tjm.desktop"}:
            pytest.fail("This command must not import the desktop workflow.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_run_defaults_do_not_opt_in_to_demonstration_or_sequence_change(fake_workflow, capsys):
    calls, report = fake_workflow
    result = cli.main(["run", "source.png", "--workspace", "/demo-workspace"])
    assert result == 0
    assert json.loads(capsys.readouterr().out) == report
    assert calls == [("source.png", {
        "workspace": "/demo-workspace", "output_dir": "runs", "extractor": "ocr",
        "name_order": None, "model": None, "demonstration": False,
        "original_image": None, "allow_demo_payment_sequence": False,
    })]


def test_run_forwards_explicit_demo_provenance_and_sequence_opt_in(fake_workflow, capsys):
    calls, _ = fake_workflow
    result = cli.main([
        "run", "demo.png", "--workspace", "/isolated-workspace", "--output", "/evidence",
        "--extractor", "openai", "--name-order", "given-family", "--model", "chosen-model",
        "--demonstration", "--original-image", "original.png", "--allow-demo-payment-sequence",
    ])
    assert result == 0
    assert calls == [("demo.png", {
        "workspace": "/isolated-workspace", "output_dir": "/evidence", "extractor": "openai",
        "name_order": "given-family", "model": "chosen-model", "demonstration": True,
        "original_image": "original.png", "allow_demo_payment_sequence": True,
    })]
    capsys.readouterr()


def test_demo_label_does_not_implicitly_allow_payment_sequence_change(fake_workflow, capsys):
    calls, _ = fake_workflow
    cli.main(["run", "demo.png", "--workspace", "/workspace", "--demonstration",
              "--original-image", "original.png"])
    assert calls[0][1]["demonstration"] is True
    assert calls[0][1]["allow_demo_payment_sequence"] is False
    capsys.readouterr()


@pytest.mark.parametrize("status", ["review_required", "failed", "running", "ready", "validated"])
def test_run_returns_nonzero_until_persistence_is_verified(status, fake_workflow, capsys):
    calls, report = fake_workflow
    report["status"] = status
    assert cli.main(["run", "source.png", "--workspace", "/workspace"]) == 2
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(("arguments", "message"), [
    (["run", "source.png"], "--workspace"),
    (["run", "source.png", "--workspace", "  "], "--workspace must name"),
    (["run", "demo.png", "--workspace", "/workspace", "--demonstration"],
     "--demonstration requires --original-image"),
    (["run", "source.png", "--workspace", "/workspace", "--original-image", "original.png"],
     "--original-image requires --demonstration"),
    (["run", "source.png", "--workspace", "/workspace", "--allow-demo-payment-sequence"],
     "--allow-demo-payment-sequence requires --demonstration"),
    (["run", "source.png", "--workspace", "/workspace", "--prepared-empty-demo"],
     "--prepared-empty-demo requires --demonstration"),
    (["run", "source.png", "--workspace", "/workspace", "--demonstration",
      "--original-image", "original.png", "--prepared-empty-demo", "--fresh-demo"],
     "not allowed with argument"),
    (["run", "source.png", "--workspace", "/workspace", "--extractor", "unknown"],
     "invalid choice"),
    (["run", "source.png", "--workspace", "/workspace", "--name-order", "family-given"],
     "invalid choice"),
    (["run", "demo.png", "--workspace", "/workspace", "--workarounds", "fakturama-2.2.0"],
     "--workarounds requires --original-image"),
    (["run", "demo.png", "--workspace", "/workspace", "--workarounds", "fakturama-2.2.0",
      "--original-image", "original.png", "--fresh-demo"],
     "--workarounds uses a prepared workspace"),
])
def test_invalid_run_configuration_fails_before_workflow_import(
    arguments, message, reject_workflow_import, capsys
):
    with pytest.raises(SystemExit) as failure:
        cli.main(arguments)
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


@pytest.mark.parametrize(("explicit", "expected"), [(None, "environment-model"),
                                                     ("flag-model", "flag-model")])
def test_run_model_environment_fallback_is_preserved(
    explicit, expected, fake_workflow, monkeypatch, capsys
):
    calls, _ = fake_workflow
    monkeypatch.setenv("OPENAI_MODEL", "environment-model")
    arguments = ["run", "source.png", "--workspace", "/workspace", "--extractor", "openai"]
    if explicit:
        arguments.extend(["--model", explicit])
    cli.main(arguments)
    assert calls[0][1]["model"] == expected
    capsys.readouterr()


def test_extract_cli_keeps_defaults_and_never_imports_workflow(
    reject_workflow_import, monkeypatch, capsys
):
    calls = []
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    def extract_order(image, **kwargs):
        calls.append((image, kwargs))
        return {"status": "validated", "evidence": {"application_actions": 0}}

    monkeypatch.setattr(cli, "extract_order", extract_order)
    assert cli.main(["extract", "source.png"]) == 0
    assert calls == [("source.png", {"output_dir": "runs", "extractor": "ocr",
                                    "name_order": None, "model": None})]
    assert json.loads(capsys.readouterr().out)["evidence"]["application_actions"] == 0


def test_doctor_cli_never_imports_workflow(reject_workflow_import, monkeypatch, capsys):
    monkeypatch.setattr(cli, "doctor", lambda: {"status": "ready", "scope": "extraction only"})
    assert cli.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "extraction only"


def test_run_output_write_failure_has_nonzero_json_result(fake_workflow, monkeypatch, capsys):
    def failing_run(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(sys.modules["tjm.workflow"], "run_image", failing_run)
    assert cli.main(["run", "source.png", "--workspace", "/workspace"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["issues"][0]["code"] == "evidence_write"
    assert "PermissionError" in report["issues"][0]["message"]


def test_run_forwards_prepared_empty_demo_only_when_explicit(fake_workflow, capsys):
    calls, _ = fake_workflow
    assert cli.main(['run', 'demo.png', '--workspace', '/prepared-disposable-workspace',
                     '--demonstration', '--original-image', 'original.png',
                     '--prepared-empty-demo']) == 0
    assert calls[0][1]['prepared_empty_demo'] is True
    assert 'fresh_demo' not in calls[0][1]
    capsys.readouterr()


def test_run_help_describes_workarounds_and_empty_workspace(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(['run', '--help'])
    assert result.value.code == 0
    help_text = ' '.join(capsys.readouterr().out.split())
    assert '--workarounds {fakturama-2.2.0}' in help_text
    assert 'new empty workspace' in help_text
    assert '--original-image' in help_text
    assert '--demonstration' not in help_text
    assert '--allow-demo-payment-sequence' not in help_text
    assert '--prepared-empty-demo' not in help_text


def test_workaround_profile_forwards_the_recorded_settings(fake_workflow, capsys):
    calls, _ = fake_workflow
    assert cli.main([
        'run', 'demo.png', '--workspace', '/prepared-workspace',
        '--workarounds', 'fakturama-2.2.0', '--original-image', 'original.png',
    ]) == 0
    assert calls == [('demo.png', {
        'workspace': '/prepared-workspace', 'output_dir': 'runs', 'extractor': 'ocr',
        'name_order': None, 'model': None, 'demonstration': True,
        'original_image': 'original.png', 'allow_demo_payment_sequence': True,
        'prepared_empty_demo': True,
    })]
    capsys.readouterr()
