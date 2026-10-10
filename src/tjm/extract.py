"""An extraction-only run: preserve source, transcribe, validate, write evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .ai_extractor import AIExtractionError, MAX_IMAGE_BYTES, extract_with_openai
from .errors import ExtractionError
from .fields import extract_fields
from .image_input import decode_image
from .models import RawOrder
from .ocr import engine_info, ocr_tokens, token_evidence
from .validation import validate_order


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runtime_versions():
    result = {}
    for package in ("tjm-fakturama", "pydantic", "Pillow", "numpy", "openai"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def extract_order(image_path, *, name_order=None, extractor="ocr", output_dir=None, model=None):
    """Return a JSON-safe report. This function never operates Fakturama.

    Every invocation creates a new directory, including failed runs. Optional
    OpenAI extraction is only selected by its explicit backend name; it shares
    the same business validation. 'validated' proves extraction checks, not a
    saved document, which can only be established by the later UI workflow.
    """
    parent = Path(output_dir or "runs").expanduser().resolve()
    run_dir = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12])
    run_dir.mkdir(parents=True, exist_ok=False)
    crops = run_dir / "crops"
    crops.mkdir()
    report = {
        "status": "failed", "raw": None, "order": None, "checks": [], "issues": [],
        "evidence": {"run_directory": str(run_dir), "report": str(run_dir / "report.json"),
                     "backend": extractor, "name_order": name_order,
                     "runtime_versions": runtime_versions(), "application_actions": 0},
    }
    try:
        if extractor not in {"ocr", "openai"}:
            raise ExtractionError("Choose ocr or openai explicitly.", code="configuration")
        if name_order not in {None, "given-family"}:
            raise ExtractionError("The supported explicit name convention is given-family.",
                                  code="configuration", field="customer.contact_name")
        path = Path(image_path).expanduser().resolve()
        report["evidence"]["input"] = {"path": str(path)}
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ExtractionError("The input image cannot be read.", code="input_file") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise ExtractionError("Image exceeds this application's 20 MiB limit.", code="invalid_image")
        source = run_dir / "source-image.bin"
        source.write_bytes(data)
        report["evidence"]["input"].update(
            sha256=sha256(data).hexdigest(), bytes=len(data), original_copy=str(source))
        image, image_format, composited = decode_image(data)
        preview = run_dir / "source.png"
        image.save(preview)
        report["evidence"]["input"].update(width=image.width, height=image.height,
                                            format=image_format, preview=str(preview),
                                            transparency_policy="composite_on_white",
                                            transparency_composited=composited)
        if extractor == "openai":
            raw, evidence = extract_with_openai(source, model=model)
            report["evidence"]["extraction"] = evidence
        else:
            from .table import extract_items

            report["evidence"]["ocr_engine"] = engine_info()
            tokens = ocr_tokens(image, psm=6)
            write_json(run_dir / "ocr-tokens.json", token_evidence(tokens))
            report["evidence"]["ocr_tokens"] = str(run_dir / "ocr-tokens.json")
            fields, field_evidence = extract_fields(image, tokens, crops)
            report["evidence"]["fields"] = field_evidence
            items, table_evidence = extract_items(image, tokens, crops)
            report["evidence"]["table"] = table_evidence
            raw = RawOrder.model_validate({**fields, "items": items})
        report["raw"] = raw.model_dump(mode="json")
        write_json(run_dir / "raw.json", report["raw"])
        validation = validate_order(raw, name_order=name_order)
        report.update(validation.model_dump(mode="json"))
        if report["order"] is not None:
            write_json(run_dir / "validated-order.json", report["order"])
    except AIExtractionError as exc:
        report["status"] = exc.evidence.get("status", "failed")
        report["evidence"]["extraction"] = exc.evidence
        report["issues"] = [{"code": "ai_extraction", "message": str(exc)}]
    except ExtractionError as exc:
        runtime_errors = {"configuration", "input_file", "invalid_image", "ocr_runtime"}
        report["status"] = "failed" if exc.code in runtime_errors else "review_required"
        report["issues"] = [{"code": exc.code, "field": exc.field, "message": str(exc)}]
        if exc.evidence:
            report["evidence"]["partial_extraction"] = exc.evidence
    except ValidationError as exc:
        report["status"] = "review_required"
        report["issues"] = [{"code": "schema", "field": ".".join(map(str, error["loc"])),
                              "message": error["msg"]} for error in exc.errors()]
    write_json(run_dir / "report.json", report)
    return report
