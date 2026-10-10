"""Real decoder failures and visible-pixel preservation, without GUI or network.

PNG/TSV regression fixtures are byte copies from the independent review probes;
the quoted order's expected text was supplied to its renderer before extraction.
"""

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from PIL import Image
import pytest

from tjm import cli
from tjm.ai_extractor import AIExtractionError, extract_with_openai
from tjm.extract import extract_order
from tjm.image_input import decode_image
from tjm.ocr import ocr_tokens, parse_tsv


FIXTURES = Path(__file__).parent / "fixtures" / "image-input"


def test_captured_tesseract_tsv_keeps_literal_sku_quotes():
    text = (FIXTURES / "quoted-sku-table-raw.tsv").read_text()
    assert [token.text for token in parse_tsv(text)] == ['"SKU"']


@pytest.mark.parametrize("literal", ['"SKU"-9', '"SKU', 'SKU"', '"', 'SKU""-9'])
def test_tsv_quotes_are_text_and_cannot_merge_rows(literal):
    header = "level\tleft\ttop\twidth\theight\tconf\ttext\n"
    # An unmatched quote must also leave the following physical TSV row intact.
    text = header + f"5\t10\t20\t80\t16\t95\t{literal}\n5\t10\t40\t80\t16\t94\tNEXT-SKU\n"
    tokens = parse_tsv(text)
    assert [token.text for token in tokens] == [literal, "NEXT-SKU"]
    assert [token.top for token in tokens] == [20, 40]


@pytest.mark.skipif(shutil.which("tesseract") is None,
                    reason="Real OCR requires the documented Tesseract executable")
def test_real_order_ocr_preserves_quoted_sku_and_every_other_raw_field(tmp_path):
    expected = json.loads((FIXTURES / "quoted-sku-expected.json").read_text())
    report = extract_order(FIXTURES / "quoted-sku-order.png", name_order="given-family",
                           output_dir=tmp_path)
    assert report["status"] == "validated", report["issues"]
    assert report["raw"] == expected
    assert report["order"]["items"][0]["sku"] == '"SKU"'


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "RGB"])
def test_transparency_composites_visible_pixels_on_white(mode):
    if mode == "RGBA":
        source = Image.new("RGBA", (3, 1))
        source.putdata([(0, 0, 0, 0), (0, 0, 0, 128), (12, 34, 56, 255)])
        expected = [(255, 255, 255), (127, 127, 127), (12, 34, 56)]
        options = {}
    elif mode == "LA":
        source = Image.new("LA", (3, 1))
        source.putdata([(0, 0), (0, 128), (80, 255)])
        expected = [(255, 255, 255), (127, 127, 127), (80, 80, 80)]
        options = {}
    elif mode == "P":
        source = Image.new("P", (3, 1))
        source.putpalette([0, 0, 0, 0, 0, 0, 12, 34, 56] + [0] * (768 - 9))
        source.putdata([0, 1, 2])
        options = {"transparency": bytes([0, 128, 255])}
        expected = [(255, 255, 255), (127, 127, 127), (12, 34, 56)]
    else:
        source = Image.new("RGB", (3, 1))
        source.putdata([(0, 0, 0), (90, 110, 130), (12, 34, 56)])
        options = {"transparency": (0, 0, 0)}
        expected = [(255, 255, 255), (90, 110, 130), (12, 34, 56)]
    data = BytesIO()
    source.save(data, format="PNG", **options)
    image, image_format, composited = decode_image(data.getvalue())
    assert image.mode == "RGB" and image_format == "PNG" and composited
    assert [image.getpixel((x, 0)) for x in range(image.width)] == expected


def test_opaque_image_pixels_are_unchanged():
    source = Image.new("RGB", (2, 1))
    source.putdata([(12, 34, 56), (78, 90, 123)])
    data = BytesIO()
    source.save(data, format="PNG")
    image, image_format, composited = decode_image(data.getvalue())
    assert image_format == "PNG" and not composited
    assert [image.getpixel((x, 0)) for x in range(image.width)] == [(12, 34, 56), (78, 90, 123)]


@pytest.mark.skipif(shutil.which("tesseract") is None,
                    reason="Real OCR requires the documented Tesseract executable")
def test_ordinary_partially_transparent_text_edges_remain_readable():
    with Image.open(FIXTURES / "quoted-text.png") as original:
        partial = original.convert("RGBA")
    partial.putalpha(128)
    data = BytesIO()
    partial.save(data, format="PNG")
    visible, _, composited = decode_image(data.getvalue())
    assert composited
    assert [token.text for token in ocr_tokens(visible, psm=6)] == ['"Q.01"']


@pytest.mark.skipif(shutil.which("tesseract") is None,
                    reason="Real OCR requires the documented Tesseract executable")
def test_fully_transparent_hidden_order_cannot_validate_and_original_is_preserved(tmp_path):
    source = FIXTURES / "fully-transparent-order.png"
    report = extract_order(source, name_order="given-family", output_dir=tmp_path)
    assert report["status"] == "review_required"
    assert report["raw"] is None and report["order"] is None
    assert report["evidence"]["application_actions"] == 0
    identity = report["evidence"]["input"]
    assert identity["transparency_policy"] == "composite_on_white"
    assert identity["transparency_composited"] is True
    assert Path(identity["original_copy"]).read_bytes() == source.read_bytes()
    with Image.open(identity["preview"]) as preview:
        assert preview.getextrema() == ((255, 255),) * 3
    assert json.loads(Path(report["evidence"]["ocr_tokens"]).read_text()) == []


class NeverCalledClient:
    def __init__(self):
        self.responses = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        pytest.fail("Malformed input must be refused before any remote request.")


@pytest.mark.parametrize("name,error_type", [
    ("malformed-metadata.png", SyntaxError), ("malformed-icc.png", IndexError),
])
def test_real_malformed_png_metadata_has_common_invalid_image_report(
    name, error_type, tmp_path, monkeypatch, capsys
):
    import tjm.extract as boundary

    source = FIXTURES / name
    original_bytes = source.read_bytes()
    # These valid-pixel PNGs reproduce actual Pillow decoder failures; they are
    # not fabricated exceptions from a mocked decoder.
    with pytest.raises(error_type):
        with Image.open(source) as image:
            image.load()
    monkeypatch.setattr(boundary, "extract_with_openai", lambda *args, **kwargs:
                        pytest.fail("The shared decoder must refuse before optional AI."))
    for backend in ("ocr", "openai"):
        code = cli.main(["extract", str(source), "--extractor", backend,
                         "--model", "offline-test", "--output", str(tmp_path / backend)])
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert code == 2 and captured.err == ""
        assert report["status"] == "failed"
        assert report["issues"][0]["code"] == "invalid_image"
        assert error_type.__name__ in report["issues"][0]["message"]
        assert report["raw"] is None and report["order"] is None
        identity = report["evidence"]["input"]
        assert identity["sha256"] == sha256(original_bytes).hexdigest()
        assert identity["bytes"] == len(original_bytes)
        assert Path(identity["original_copy"]).read_bytes() == original_bytes
        assert json.loads(Path(report["evidence"]["report"]).read_text()) == report
        assert report["evidence"]["application_actions"] == 0
    client = NeverCalledClient()
    with pytest.raises(AIExtractionError) as failure:
        extract_with_openai(source, model="offline-test", client=client)
    assert failure.value.evidence["status"] == "failed"
    assert failure.value.evidence["code"] == "invalid_image"
    assert failure.value.evidence["image"]["sha256"] == sha256(original_bytes).hexdigest()
    assert failure.value.evidence["remote_request_attempted"] is False
    assert client.calls == []


def test_optional_ai_submits_white_visible_pixels_and_keeps_original_identity(tmp_path):
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(source)
    values = json.loads((FIXTURES / "quoted-sku-expected.json").read_text())
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="completed", error=None, output=[],
                               output_text=json.dumps(values))

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    _, evidence = extract_with_openai(source, model="offline-test", client=client)
    submitted = base64.b64decode(calls[0]["input"][0]["content"][0]["image_url"].split(",", 1)[1])
    with Image.open(BytesIO(submitted)) as image:
        assert image.mode == "RGB"
        assert image.getextrema() == ((255, 255),) * 3
    identity = evidence["image"]
    assert identity["sha256"] == sha256(source.read_bytes()).hexdigest()
    assert identity["submitted_sha256"] == sha256(submitted).hexdigest()
    assert identity["submitted_bytes"] == len(submitted)
    assert identity["transparency_composited"] is True
    assert identity["transparency_policy"] == "composite_on_white"
    assert submitted != source.read_bytes()
