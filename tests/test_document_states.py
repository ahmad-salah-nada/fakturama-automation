"""State icons cannot replace independent, strict text observations."""

import pytest
from PIL import Image

from tjm import document_states as module
from tjm.desktop import DesktopError


@pytest.fixture
def icons():
    result = {}
    for state, color in (("paid", (30, 180, 40)), ("open", (200, 150, 25)),
                         ("unpaid", (200, 35, 25))):
        image = Image.new("RGBA", (16, 16), (*color, 255))
        result[state] = image, {"resource": state}
    return result


def cell(icons, state):
    image = Image.new("RGBA", (180, 24), (80, 150, 220, 255))
    image.alpha_composite(icons[state][0].resize((32, 32), Image.Resampling.BILINEAR), (0, -4))
    return image.convert("RGB")


@pytest.mark.parametrize("state", ["paid", "open", "unpaid"])
def test_icon_match_requires_independent_text_and_keeps_evidence(monkeypatch, tmp_path, icons, state):
    calls = []
    def text(image, box, path):
        calls.append(box)
        return state, {"raw_text": state, "confidence": 95}
    monkeypatch.setattr(module, "_cell", text)
    actual, proof = module.read_state_cell(cell(icons, state), (0, 0, 180, 24), tmp_path / 'state.png', icons)
    assert actual == state and calls == [(35, 0, 180, 24)]
    assert proof['state_icon']['mean_absolute_error'] < 8
    assert proof['raw_text'] == state


def test_wrong_or_unknown_icon_stops_before_text(monkeypatch, tmp_path, icons):
    monkeypatch.setattr(module, "_cell", lambda *args: pytest.fail("Unmatched icon must stop."))
    with pytest.raises(DesktopError, match="unambiguous installed icon"):
        module.read_state_cell(Image.new('RGB', (180, 24), 'white'), (0, 0, 180, 24), tmp_path / 'state.png', icons)


def test_ambiguous_identical_resources_stop(monkeypatch, tmp_path, icons):
    icons['open'] = icons['paid']
    monkeypatch.setattr(module, "_cell", lambda *args: pytest.fail("Ambiguous icon must stop."))
    with pytest.raises(DesktopError, match="unambiguous installed icon"):
        module.read_state_cell(cell(icons, 'paid'), (0, 0, 180, 24), tmp_path / 'state.png', icons)


def test_icon_does_not_override_conflicting_actual_text(monkeypatch, tmp_path, icons):
    monkeypatch.setattr(module, "_cell", lambda *args: ('unpaid', {}))
    with pytest.raises(DesktopError, match="disagree"):
        module.read_state_cell(cell(icons, 'paid'), (0, 0, 180, 24), tmp_path / 'state.png', icons)


def test_low_confidence_text_still_stops(monkeypatch, tmp_path, icons):
    def fail(*args):
        raise DesktopError('Low-confidence text')
    monkeypatch.setattr(module, "_cell", fail)
    with pytest.raises(DesktopError, match="Low-confidence"):
        module.read_state_cell(cell(icons, 'paid'), (0, 0, 180, 24), tmp_path / 'state.png', icons)


def test_unsupported_scale_stops(tmp_path, icons):
    with pytest.raises(DesktopError, match="scale-2"):
        module.read_state_cell(Image.new('RGB', (180, 25)), (0, 0, 180, 25), tmp_path / 'state.png', icons)
