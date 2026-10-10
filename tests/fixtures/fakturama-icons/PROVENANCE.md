# Fakturama State icon fixtures

These three PNGs, icon mappings and manifest are unchanged entries extracted
from the installed Fakturama 2.2.0 icon resource bundle:

`com.sebulli.fakturama.resources.icons_2.2.0.202605290905.jar`

- `checked.png` ← `icons/16x16/checked.png`
- `order_pending.png` ← `icons/16x16/order_pending.png`
- `error.png` ← `icons/16x16/error.png`
- `iconset.properties` ← `iconset.properties`
- `MANIFEST.MF` ← `META-INF/MANIFEST.MF`

The selected-header regression builds a temporary minimal resource JAR from
these three images and the mappings. It operates on an included screenshot,
without launching or requiring an installed Fakturama app. It preserves the
original image recognition and independently read state-text assertions.

The source bundle contains no license/about/notice/copyright text entry, and
its preserved manifest contains no license reference. The manifest identifies
SEBULLI as bundle vendor. No separate licensing terms are inferred or invented
for these images. The source application was obtained from the official
Fakturama download, https://www.fakturama.info/download/. This provenance records
origin and does not assign a new license to upstream assets.

Exact entry hashes are recorded in the preparation audit. The full upstream
application and full resource JAR are not distributed in this fixture.
