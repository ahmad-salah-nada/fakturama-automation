"""Read Fakturama's State cell as its installed icon followed by strict text OCR."""

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from PIL import Image

from .desktop import DesktopError
from .ui_table import _cell


STATE_RESOURCES = {"paid": "checked.png", "open": "order_pending.png", "unpaid": "error.png"}


def state_cell_reader(desktop):
    """Load the actual connected application's 16px State-painter resources."""
    plugins = Path(desktop.identity[2]) / "Contents/Eclipse/plugins"
    bundles = list(plugins.glob("com.sebulli.fakturama.resources.icons_2.2.0.*.jar"))
    if len(bundles) != 1:
        raise DesktopError("State reading requires one connected 2.2.0 icon bundle.")
    icons = {}
    with ZipFile(bundles[0]) as archive:
        properties = archive.read("iconset.properties").decode()
        for state, filename in STATE_RESOURCES.items():
            command = {"paid": "COMMAND_CHECKED", "open": "COMMAND_ORDER_PENDING",
                       "unpaid": "COMMAND_ERROR"}[state]
            if command + "=" + filename not in properties.splitlines():
                raise DesktopError("The installed State resource mapping differs.")
            resource = "icons/16x16/" + filename
            data = archive.read(resource)
            icon = Image.open(BytesIO(data)).convert("RGBA")
            if icon.size != (16, 16):
                raise DesktopError("Unsupported installed State icon dimensions.")
            icons[state] = (icon, {"bundle": str(bundles[0]), "resource": resource,
                                   "sha256": hashlib.sha256(data).hexdigest()})

    def reader(image, box, path):
        return read_state_cell(image, box, path, icons)
    return reader


def read_state_cell(image, box, path, icons):
    """Match only the leading icon; all remaining pixels use the strict reader.

    The supported retina table has 28px rows, with 2px vertical cell insets.
    Its 32px rendered icon is vertically clipped by the row. The matching
    window is derived from the observed cell height; no screen position is used.
    A matched icon never supplies the state: independently read text must agree.
    """
    crop = image.crop(box).convert("RGB")
    crop.save(path)
    if crop.height != 24 or crop.width < 60:
        raise DesktopError("State cell needs the demonstrated scale-2 24px inset row.")
    pixels = np.asarray(crop)
    background = tuple(map(int, np.median(pixels.reshape(-1, 3), axis=0)))
    scores = []
    width = 35
    for state, (source, evidence) in icons.items():
        icon = source.resize((32, 32), Image.Resampling.BILINEAR)
        best = None
        for x in range(-2, 4):
            for y in range(-8, 1):
                candidate = Image.new("RGBA", (width, crop.height), (*background, 255))
                candidate.alpha_composite(icon, (x, y))
                difference = np.abs(np.asarray(candidate.convert("RGB")).astype(float)
                                    - pixels[:, :width].astype(float))
                score = float(difference.mean())
                if best is None or score < best[0]:
                    best = score, x, y
        scores.append((best[0], state, best, evidence))
    scores.sort(key=lambda row: row[0])
    if scores[0][0] > 8 or scores[1][0] - scores[0][0] < 20:
        raise DesktopError("The leading State image is not one unambiguous installed icon.")
    _, icon_state, alignment, resource = scores[0]
    text_box = (box[0] + width, box[1], box[2], box[3])
    text, record = _cell(image, text_box, Path(path).with_name(Path(path).stem + "-text.png"))
    if text != icon_state:
        raise DesktopError("The independently read State text and installed icon disagree.")
    record.update(box=list(box), source_crop=str(path),
                  state_icon={"resource": resource, "mean_absolute_error": alignment[0],
                              "alignment": list(alignment[1:]), "excluded_width": width},
                  method="installed leading icon matched; remaining text read independently")
    return text, record
