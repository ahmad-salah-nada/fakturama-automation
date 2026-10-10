"""Small local Tesseract wrapper and geometry derived from observed tokens."""

import csv
import io
import statistics
import subprocess
import shutil
from dataclasses import dataclass

from PIL import Image

from .errors import ExtractionError


@dataclass(frozen=True)
class Token:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float

    @property
    def right(self):
        return self.left + self.width

    @property
    def bottom(self):
        return self.top + self.height

    @property
    def cx(self):
        return self.left + self.width / 2

    @property
    def cy(self):
        return self.top + self.height / 2

    @property
    def box(self):
        return self.left, self.top, self.right, self.bottom


def engine_info():
    executable = shutil.which("tesseract")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, check=True, timeout=5) if executable else None
        return {"executable": executable,
                "version": result.stdout.splitlines()[0] if result else None,
                "language": "eng"}
    except (OSError, subprocess.SubprocessError):
        return {"executable": executable, "version": None, "language": "eng"}


def ocr_tokens(image: Image.Image, *, psm=6) -> list[Token]:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", "eng", "tsv"],
            input=buffer.getvalue(), capture_output=True, check=True, timeout=45,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ExtractionError(
            "Tesseract did not complete. Check the installed executable and English language data.",
            code="ocr_runtime",
        ) from exc
    return parse_tsv(result.stdout.decode("utf-8"))


def parse_tsv(text: str) -> list[Token]:
    tokens = []
    # Tesseract's text column is literal text, not a CSV-quoted field. In
    # particular, quotes in a product identifier must survive transcription.
    for row in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5" or not row.get("text", "").strip():
            continue
        tokens.append(Token(
            row["text"].strip(), int(row["left"]), int(row["top"]),
            int(row["width"]), int(row["height"]), float(row["conf"]),
        ))
    return tokens


def group_lines(tokens: list[Token]) -> list[list[Token]]:
    """Group by observed vertical centers, retaining separate horizontal fields."""
    lines: list[list[Token]] = []
    for token in sorted(tokens, key=lambda t: (t.cy, t.left)):
        matched = False
        for line in reversed(lines):
            center = statistics.median(t.cy for t in line)
            height = statistics.median(t.height for t in line)
            if abs(token.cy - center) <= max(token.height, height) * .48:
                line.append(token)
                matched = True
                break
            if token.cy - center > max(token.height, height) * 2:
                break
        if not matched:
            lines.append([token])
    return [sorted(line, key=lambda t: t.left) for line in lines]


def line_text(tokens: list[Token]) -> str:
    return " ".join(t.text for t in sorted(tokens, key=lambda t: t.left))


def token_evidence(tokens: list[Token]) -> list[dict]:
    return [{"text": t.text, "box": list(t.box), "confidence": t.confidence} for t in tokens]
