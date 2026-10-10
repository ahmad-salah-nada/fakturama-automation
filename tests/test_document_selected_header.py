"""Regression for the real transaction view whose selected header hides rules."""

from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image

from tjm.document_states import state_cell_reader
from tjm.ocr import ocr_tokens
from tjm.ui_table import _selected_document_header, read_table
from tjm.desktop import DesktopError


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / 'tests/fixtures/gui/selected-document-header.png'
ICONS = ROOT / 'tests/fixtures/fakturama-icons'


def observed():
    if not TRACE.is_file():
        pytest.skip('Local desktop trace is unavailable.')
    return Image.open(TRACE)


def test_real_selected_header_reads_both_states_and_totals_from_pixels(tmp_path):
    image = observed()
    app = tmp_path / 'Fakturama2.app'
    plugins = app / 'Contents/Eclipse/plugins'
    plugins.mkdir(parents=True)
    bundle = plugins / 'com.sebulli.fakturama.resources.icons_2.2.0.test.jar'
    with ZipFile(bundle, 'w') as archive:
        archive.write(ICONS / 'iconset.properties', 'iconset.properties')
        for filename in ('checked.png', 'order_pending.png', 'error.png'):
            archive.write(ICONS / filename, 'icons/16x16/' + filename)
    reader = state_cell_reader(SimpleNamespace(identity=(0, 'Fakturama.ID', str(app), 0)))
    result = read_table(image, (606, 1110, 3000, 1800), ['State', 'Total'], tmp_path,
                        observed_table_box=(962, 1232, 2992, 1792),
                        cell_readers={'State': reader})
    assert [row['values'] for row in result['rows']] == [
        {'State': 'paid', 'Total': 'EUR678.30'}, {'State': 'open', 'Total': 'EUR678.30'}]
    assert result['evidence']['geometry']['selected_header_body_rules'] is True


def test_selected_header_route_requires_native_boundary(tmp_path):
    with pytest.raises(DesktopError):
        read_table(observed(), (962, 1232, 2992, 1792), ['State', 'Total'], tmp_path)


def test_selected_header_route_cannot_read_other_business_columns():
    crop = observed().crop((962, 1232, 2992, 1792))
    with pytest.raises(DesktopError, match='restricted'):
        _selected_document_header(crop, ocr_tokens(crop, psm=11), ['Cust.Ref.'])


def test_partial_native_frame_stops(tmp_path):
    with pytest.raises(DesktopError):
        read_table(observed(), (606, 1110, 3000, 1800), ['State', 'Total'], tmp_path,
                    observed_table_box=(962, 1232, 2800, 1792))
