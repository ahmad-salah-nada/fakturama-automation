"""Contract tests with fake responses; no live model quality is asserted."""

import base64
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from tjm.ai_extractor import AIExtractionError, extract_with_openai


def order_values():
    address = {
        "recipient": "Sample GmbH",
        "street": "Straße 1",
        "postal_code": "10115",
        "city": "Berlin",
        "country": "Germany",
        "additional_name": None,
        "address_specification": None,
        "district": None,
    }
    item = {
        "sku": "SKU-01",
        "description": "Müller chair",
        "quantity": "2",
        "unit_net": "10.00",
        "vat_percent": "19",
        "discount_percent": "10",
        "line_net": "18.00",
        "unit": None,
    }
    return {
        "order_date": "2026-07-14",
        "external_reference": "WEB-01",
        "currency": "EUR",
        "customer": {
            "company": "Sample GmbH",
            "contact_name": "Marta Klein",
            "alias": "Sample",
            "billing": address,
            "delivery": deepcopy(address),
            "email": "marta@example.test",
            "telephone": "+49 30 123",
            "first_name": None,
            "last_name": None,
            "source_customer_id": None,
        },
        "payment": {"method": "Bank transfer", "status": "PAID", "date": "2026-07-18", "source_amount": None},
        "items": [item],
        "totals": {"net": "18.00", "vat": "3.42", "gross": "21.42"},
        "overall_discount_percent": "0",
        "shipping_net": "0",
        "shipping_vat_percent": None,
    }


def response_for(values=None, **overrides):
    response = {
        "id": "resp_mock",
        "model": "explicit-test-model",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [],
        "output_text": json.dumps(order_values() if values is None else values, ensure_ascii=False),
    }
    response.update(overrides)
    return SimpleNamespace(**response)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "order.png"
    Image.new("RGB", (40, 30), "white").save(path)
    return path


def test_request_contains_exact_image_explicit_model_and_strict_complete_schema(image_path):
    client = FakeClient(response_for())
    raw, evidence = extract_with_openai(image_path, model="explicit-test-model", client=client)
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["model"] == "explicit-test-model"
    assert request["store"] is False
    image = request["input"][0]["content"][0]
    assert image["type"] == "input_image"
    assert image["detail"] == "high"
    prefix, encoded = image["image_url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(encoded) == image_path.read_bytes()
    assert "Never infer, repair, calculate" in request["instructions"]
    assert "Do not split one contact-name field" in request["instructions"]

    output_format = request["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True

    def check_schema(node):
        if isinstance(node, dict):
            assert "default" not in node
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for value in node.values():
                check_schema(value)
        elif isinstance(node, list):
            for value in node:
                check_schema(value)

    check_schema(output_format["schema"])
    assert raw.customer.contact_name == "Marta Klein"
    assert raw.customer.first_name is None
    assert raw.items[0].description == "Müller chair"
    assert evidence["image"]["sha256"] == sha256(image_path.read_bytes()).hexdigest()
    assert evidence["status"] == "extracted"
    assert evidence["business_validation_run"] is False
    json.dumps(evidence)


def test_adapter_preserves_repeated_rows_and_ambiguity_for_common_validation(image_path):
    values = order_values()
    values["order_date"] = "01/02/2026"
    values["payment"].update(status="UNPAID", date="18/07/2026", source_amount="20,00")
    values["items"].append(deepcopy(values["items"][0]))
    values["items"][1]["quantity"] = "3"
    values["items"][1]["unit_net"] = "1,000"
    raw, _ = extract_with_openai(image_path, model="test-model", client=FakeClient(response_for(values)))
    assert raw.order_date == "01/02/2026"
    assert raw.payment.date == "18/07/2026"
    assert raw.payment.source_amount == "20,00"
    assert [item.sku for item in raw.items] == ["SKU-01", "SKU-01"]
    assert [item.quantity for item in raw.items] == ["2", "3"]
    assert raw.items[1].unit_net == "1,000"


@pytest.mark.parametrize("overrides", [
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
    {"status": None},
    {"status": "failed", "error": {"code": "server_error"}},
    {"error": {"code": "server_error"}},
    {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]},
    {"output_text": ""},
    {"output_text": None},
    {"output_text": "not JSON"},
    {"output_text": "```json\n{}\n```"},
    {"output_text": "[]"},
    {"output_text": '{"currency":"EUR","currency":"USD"}'},
])
def test_unusable_response_requires_review_and_does_not_fallback(image_path, overrides):
    client = FakeClient(response_for(**overrides))
    with pytest.raises(AIExtractionError) as error:
        extract_with_openai(image_path, model="test-model", client=client)
    assert error.value.evidence["status"] == "review_required"
    assert len(client.calls) == 1
    json.dumps(error.value.evidence)


@pytest.mark.parametrize("case", ["missing_required", "missing_optional", "extra", "numeric_json"])
def test_schema_violations_are_rejected_locally(image_path, case):
    values = order_values()
    if case == "missing_required":
        del values["totals"]["gross"]
    elif case == "missing_optional":
        del values["customer"]["first_name"]
    elif case == "extra":
        values["items"][0]["invented"] = "value"
    elif case == "numeric_json":
        values["items"][0]["quantity"] = 2
    with pytest.raises(AIExtractionError, match="invalid transcription"):
        extract_with_openai(image_path, model="test-model", client=FakeClient(response_for(values)))


@pytest.mark.parametrize("model", ["", " ", " model", "model ", None])
def test_missing_or_malformed_model_fails_before_reading_or_requesting(model, tmp_path):
    client = FakeClient(response_for())
    with pytest.raises(AIExtractionError) as error:
        extract_with_openai(tmp_path / "absent.png", model=model, client=client)
    assert error.value.evidence["status"] == "failed"
    assert error.value.evidence["remote_request_attempted"] is False
    assert client.calls == []


@pytest.mark.parametrize("image_format,mime", [
    ("PNG", "image/png"), ("JPEG", "image/jpeg"),
    ("WEBP", "image/webp"), ("GIF", "image/gif"),
])
def test_mime_comes_from_contents_instead_of_filename(tmp_path, image_format, mime):
    path = tmp_path / "image.unrelated"
    Image.new("RGB", (8, 8), "white").save(path, format=image_format)
    client = FakeClient(response_for())
    _, evidence = extract_with_openai(path, model="test-model", client=client)
    assert evidence["image"]["mime_type"] == mime
    assert client.calls[0]["input"][0]["content"][0]["image_url"].startswith(f"data:{mime};base64,")


@pytest.mark.parametrize("kind", ["missing", "corrupt", "tiff", "animated"])
def test_unsupported_or_invalid_image_fails_before_request(tmp_path, kind):
    path = tmp_path / "input.png"
    if kind == "corrupt":
        path.write_bytes(b"not an image")
    elif kind == "tiff":
        Image.new("RGB", (8, 8), "white").save(path, format="TIFF")
    elif kind == "animated":
        frames = [Image.new("RGB", (8, 8), colour) for colour in ("white", "black")]
        frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:])
    client = FakeClient(response_for())
    with pytest.raises(AIExtractionError) as error:
        extract_with_openai(path, model="test-model", client=client)
    assert error.value.evidence["remote_request_attempted"] is False
    assert error.value.evidence["status"] == "failed"
    assert client.calls == []


def test_app_size_limit_fails_before_request(image_path, monkeypatch):
    monkeypatch.setattr("tjm.ai_extractor.MAX_IMAGE_BYTES", 10)
    client = FakeClient(response_for())
    with pytest.raises(AIExtractionError, match="20 MiB"):
        extract_with_openai(image_path, model="test-model", client=client)
    assert client.calls == []


def test_truncated_jpeg_fails_before_request_even_when_structure_verifies(tmp_path):
    path = tmp_path / "truncated.jpg"
    Image.new("RGB", (40, 30), "white").save(path, format="JPEG")
    path.write_bytes(path.read_bytes()[:-2])
    # Pillow's JPEG verify() accepts this; full pixel decoding must still fail.
    with Image.open(path) as image:
        image.verify()
    client = FakeClient(response_for())
    with pytest.raises(AIExtractionError, match="Cannot read a supported image") as error:
        extract_with_openai(path, model="test-model", client=client)
    assert error.value.evidence["status"] == "failed"
    assert error.value.evidence["remote_request_attempted"] is False
    assert client.calls == []


def test_request_error_does_not_expose_sdk_exception_body(image_path):
    class FailingClient:
        responses = None

        def __init__(self):
            self.responses = self

        def create(self, **kwargs):
            raise RuntimeError("sensitive request body")

    with pytest.raises(AIExtractionError) as error:
        extract_with_openai(image_path, model="test-model", client=FailingClient())
    assert "RuntimeError" in str(error.value)
    assert "sensitive request body" not in json.dumps(error.value.evidence)
    assert error.value.evidence["status"] == "failed"


def test_real_client_configuration_is_bounded_without_automatic_retries(image_path, monkeypatch):
    import sys

    client = FakeClient(response_for())
    options = []

    def create_client(**kwargs):
        options.append(kwargs)
        return client

    class SetupError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=create_client, OpenAIError=SetupError))
    extract_with_openai(image_path, model="test-model")
    assert options == [{"timeout": 60.0, "max_retries": 0}]
    assert len(client.calls) == 1


def test_missing_credentials_are_preflight_failure(image_path, monkeypatch):
    import sys

    class SetupError(Exception):
        pass

    def create_client(**kwargs):
        raise SetupError("missing credentials")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=create_client, OpenAIError=SetupError))
    with pytest.raises(AIExtractionError, match="OPENAI_API_KEY") as error:
        extract_with_openai(image_path, model="test-model")
    assert error.value.evidence["status"] == "failed"
    assert error.value.evidence["remote_request_attempted"] is False


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_non_sdk_client_configuration_failure_is_sanitized_before_request(
    image_path, monkeypatch, failure
):
    import sys

    def create_client(**kwargs):
        raise failure("sensitive network configuration")

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=create_client))
    with pytest.raises(AIExtractionError, match="client setup failed") as error:
        extract_with_openai(image_path, model="test-model")
    assert error.value.evidence["status"] == "failed"
    assert error.value.evidence["remote_request_attempted"] is False
    assert failure.__name__ in str(error.value)
    assert "sensitive network configuration" not in json.dumps(error.value.evidence)


def test_missing_optional_sdk_is_preflight_failure(image_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def without_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("test has no optional SDK")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_openai)
    with pytest.raises(AIExtractionError, match=r"optional \[ai\]") as error:
        extract_with_openai(image_path, model="test-model")
    assert error.value.evidence["remote_request_attempted"] is False
