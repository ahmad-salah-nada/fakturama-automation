"""Fast boundary checks: no OCR process, network call or Fakturama action.

Field/table mocks isolate run evidence, backend selection and shared validation.
Real image transcription is checked separately by test_extraction_images.py.
"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

import tjm.cli as cli
import tjm.extract as boundary
from tjm.models import RawOrder


FIXTURE = Path(__file__).parent / "fixtures" / "sample_expected.json"


@pytest.fixture
def raw_values():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["raw"]


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "input.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


@pytest.fixture
def mocked_ocr(monkeypatch, raw_values):
    from tjm import table

    fields = deepcopy(raw_values)
    items = fields.pop("items")
    monkeypatch.setattr(boundary, "engine_info", lambda: {"version": "mocked-tesseract"})
    monkeypatch.setattr(boundary, "ocr_tokens", lambda *args, **kwargs: [])
    monkeypatch.setattr(boundary, "extract_fields", lambda *args: (deepcopy(fields), {"mock": True}))
    monkeypatch.setattr(table, "extract_items", lambda *args: (deepcopy(items), {"mock": True}))


def persisted(report):
    path = Path(report["evidence"]["report"])
    assert path.is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == report
    assert saved["evidence"]["application_actions"] == 0
    return path


@pytest.mark.parametrize("kind,code", [("missing", "input_file"), ("invalid", "invalid_image")])
def test_bad_input_has_readable_persisted_failure(kind, code, tmp_path):
    source = tmp_path / "bad.png"
    if kind == "invalid":
        source.write_bytes(b"This is text, not an image.")
    report = boundary.extract_order(source, output_dir=tmp_path / "evidence")
    assert report["status"] == "failed"
    assert report["order"] is None and report["raw"] is None
    assert report["issues"][0]["code"] == code
    assert report["issues"][0]["message"]
    if kind == "invalid":
        original = source.read_bytes()
        identity = report["evidence"]["input"]
        assert identity["path"] == str(source.resolve())
        assert identity["sha256"] == sha256(original).hexdigest()
        assert identity["bytes"] == len(original)
        assert Path(identity["original_copy"]).read_bytes() == original
    persisted(report)


def test_each_run_is_unique_and_preserves_the_exact_original_bytes(
    image_path, mocked_ocr, tmp_path
):
    output = tmp_path / "evidence"
    first_bytes = image_path.read_bytes()
    first = boundary.extract_order(image_path, name_order="given-family", output_dir=output)
    first_report = persisted(first)
    first_saved = first_report.read_bytes()

    Image.new("RGB", (41, 31), "black").save(image_path)
    second_bytes = image_path.read_bytes()
    second = boundary.extract_order(image_path, name_order="given-family", output_dir=output)
    second_report = persisted(second)
    assert first["status"] == second["status"] == "validated"
    assert first_report.parent != second_report.parent
    assert first_report.read_bytes() == first_saved

    for report, original in ((first, first_bytes), (second, second_bytes)):
        identity = report["evidence"]["input"]
        copy = Path(identity["original_copy"])
        assert copy.read_bytes() == original
        assert identity["sha256"] == sha256(original).hexdigest()
        assert identity["sha256"] == sha256(copy.read_bytes()).hexdigest()
        assert identity["bytes"] == len(original)
        with Image.open(identity["preview"]) as preview:
            assert preview.size == (identity["width"], identity["height"])
    assert first["evidence"]["input"]["sha256"] != second["evidence"]["input"]["sha256"]


def test_default_ocr_never_invokes_ai_even_when_a_model_name_is_present(
    image_path, mocked_ocr, monkeypatch, tmp_path
):
    monkeypatch.setattr(boundary, "extract_with_openai", lambda *args, **kwargs:
                        pytest.fail("Default OCR must not invoke optional AI."))
    report = boundary.extract_order(image_path, name_order="given-family",
                                    model="not-an-opt-in", output_dir=tmp_path / "evidence")
    assert report["status"] == "validated", report["issues"]
    assert report["evidence"]["backend"] == "ocr"
    assert "extraction" not in report["evidence"]
    persisted(report)


def test_explicit_ai_transcription_cannot_bypass_shared_financial_validation(
    image_path, raw_values, monkeypatch, tmp_path
):
    from tjm import table

    values = deepcopy(raw_values)
    values["totals"]["gross"] = "420.00"
    calls = []

    def fake_ai(source, *, model):
        calls.append((Path(source).read_bytes(), model))
        return RawOrder.model_validate(values), {"backend": "openai", "mock": True}

    def no_ocr(*args, **kwargs):
        pytest.fail("Explicit AI must not silently route through OCR.")

    monkeypatch.setattr(boundary, "extract_with_openai", fake_ai)
    monkeypatch.setattr(boundary, "ocr_tokens", no_ocr)
    monkeypatch.setattr(boundary, "extract_fields", no_ocr)
    monkeypatch.setattr(table, "extract_items", no_ocr)
    report = boundary.extract_order(image_path, extractor="openai", model="mock-model",
                                    name_order="given-family", output_dir=tmp_path / "evidence")
    assert calls == [(image_path.read_bytes(), "mock-model")]
    assert report["status"] == "review_required"
    assert report["order"] is None
    assert report["raw"]["totals"]["gross"] == "420.00"
    assert any(issue["field"] == "totals.gross" and issue["code"] == "arithmetic_mismatch"
               for issue in report["issues"])
    persisted(report)


def test_missing_ai_model_fails_before_client_or_remote_request(
    image_path, monkeypatch, tmp_path
):
    calls = []

    def client_factory(**kwargs):
        calls.append(kwargs)
        pytest.fail("A missing model must fail before constructing any remote client.")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client_factory))
    report = boundary.extract_order(image_path, extractor="openai",
                                    output_dir=tmp_path / "evidence")
    assert calls == []
    assert report["status"] == "failed"
    assert report["order"] is None
    assert "explicit model" in report["issues"][0]["message"]
    assert report["evidence"]["extraction"]["remote_request_attempted"] is False
    persisted(report)


@pytest.mark.parametrize("installed", [False, True])
def test_doctor_reports_extraction_readiness_without_claiming_ui_readiness(
    installed, monkeypatch
):
    versions = {name: "test-version" for name in (
        "tjm-fakturama", "pydantic", "Pillow", "numpy"
    )}
    versions["openai"] = None
    monkeypatch.setattr(cli, "runtime_versions", lambda: versions)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/mock/tesseract" if installed else None)

    def fake_run(arguments, **kwargs):
        assert kwargs["timeout"] == 5
        if arguments[-1] == "--version":
            return SimpleNamespace(stdout="tesseract test-version\n")
        assert arguments[-1] == "--list-langs"
        return SimpleNamespace(stdout="List of available languages (1):\neng\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    report = cli.doctor()
    assert report["status"] == ("ready" if installed else "failed")
    assert report["scope"] == "extraction dependencies only; desktop readiness is checked when running the workflow"
    assert report["optional_openai_sdk"] is None
    names = {check["name"] for check in report["checks"]}
    assert names == {"python", "tesseract_english", "pydantic", "Pillow", "numpy"}


def test_cli_failure_exit_code_and_json_point_to_the_persisted_report(tmp_path, capsys):
    code = cli.main(["extract", str(tmp_path / "missing.png"), "--output", str(tmp_path / "evidence")])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "failed"
    assert report["issues"][0]["code"] == "input_file"
    persisted(report)


def test_cli_success_exit_code_is_extraction_validation_only(
    image_path, mocked_ocr, tmp_path, capsys
):
    code = cli.main(["extract", str(image_path), "--name-order", "given-family",
                     "--output", str(tmp_path / "evidence")])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "validated"
    assert report["evidence"]["application_actions"] == 0
    assert report["evidence"]["backend"] == "ocr"
    persisted(report)


def label_tokens(*lines):
    """Observed word boxes for constructor-only label classification tests."""
    from tjm.ocr import Token

    tokens = []
    for index, line in enumerate((*lines, "ITEMS")):
        x, y = 20, 20 + index * 28
        for word in line.split():
            width = len(word) * 8
            tokens.append(Token(word, x, y, width, 14, 95))
            x += width + 8
    return tokens


@pytest.mark.parametrize("word,field", [
    ("SHIPPING", "shipping_net"), ("FREIGHT", "shipping_net"),
    ("SURCHARGE", "shipping_net"), ("DISCOUNT", "overall_discount_percent"),
])
def test_unrecognized_charge_wording_requires_review_instead_of_zero(
    word, field, tmp_path
):
    from tjm.errors import ExtractionError
    from tjm.fields import FieldReader

    with pytest.raises(ExtractionError) as failure:
        FieldReader(Image.new("RGB", (600, 240), "white"),
                    label_tokens(word + " CHARGE"), tmp_path)
    assert failure.value.code == "unsupported_charge"
    assert failure.value.field == field
    assert "rather than use zero" in str(failure.value)


@pytest.mark.parametrize("label,key", [
    ("SHIPPING NET", "shipping_net"),
    ("SHIPPING VAT", "shipping_vat_percent"),
    ("OVERALL DISCOUNT", "overall_discount_percent"),
])
def test_supported_charge_labels_are_recognized_without_triggering_guard(
    label, key, tmp_path
):
    from tjm.fields import FieldReader

    reader = FieldReader(Image.new("RGB", (600, 240), "white"),
                         label_tokens(label), tmp_path)
    assert key in reader.labels


@pytest.mark.parametrize("known,unknown,field", [
    ("SHIPPING NET", "SHIPPING FEE", "shipping_net"),
    ("OVERALL DISCOUNT", "SPECIAL DISCOUNT", "overall_discount_percent"),
])
def test_one_known_label_does_not_whitelist_other_unknown_charge_occurrences(
    known, unknown, field, tmp_path
):
    from tjm.errors import ExtractionError
    from tjm.fields import FieldReader

    with pytest.raises(ExtractionError) as failure:
        FieldReader(Image.new("RGB", (600, 240), "white"),
                    label_tokens(known, unknown), tmp_path)
    assert failure.value.code == "unsupported_charge"
    assert failure.value.field == field
