# Setup

Extraction can run without opening Fakturama. The desktop example below uses
a new workspace, as in the [demo](../demo/README.md).

## 1. Install dependencies and check extraction

The desktop demo ran on macOS ARM with English **Fakturama 2.2.0**,
**Java 22**, Python 3.11.16 and **Tesseract 5.5.0** with English data. Install
Fakturama and a compatible Java runtime separately. This is the reference
environment; other platforms, versions and themes are outside the current
desktop support.

Install uv and Tesseract. For Homebrew users:

```sh
brew install uv tesseract
tesseract --version
tesseract --list-langs
```

Confirm `eng` appears. From the repository root:

```sh
uv sync --frozen --python 3.11
.venv/bin/tjm doctor
.venv/bin/tjm extract fixtures/original.png --name-order given-family --output runs/extraction-check
```

`doctor` checks the extraction dependencies. Desktop permissions are checked
separately. `given-family` treats Marta as the first name and Klein as the last
name; an ambiguous name needs review. `validated` means the extracted data passed
its checks, without creating Fakturama records.

A `.test` email became blank when saving a contact in Fakturama 2.2.0, so the
desktop command stops before creating records for the original image. This
example uses the `.com` demo image. [Workaround details](../demo/README.md#fakturama-220-quirk-workarounds)

## 2. Prepare a new workspace

Install the desktop dependencies and the optional AI dependencies used by the
offline tests:

```sh
uv sync --frozen --extra desktop --extra ai --python 3.11
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests tools
TJM_DEMO_DATA="$(pwd)/demo-data/demo-01"
.venv/bin/python tools/prepare_demo.py --workspace "$TJM_DEMO_DATA"
TJM_DEMO_SETTINGS="${TJM_DEMO_DATA}-settings"
```

The helper creates empty directories and writes the demo's preferences. It
refuses existing paths and does not launch Fakturama or create business records.
If either path exists, choose a new name and keep the previous data.

Local OCR is the default and needs no API key. Optional AI extraction can be
selected through the CLI; see [cli.py](../src/tjm/cli.py) and
[ai_extractor.py](../src/tjm/ai_extractor.py).

## 3. Launch and check the desktop

Close any existing Fakturama instance normally. Replace these example paths with
your installed bundle and Java executable:

```sh
TJM_APP_BUNDLE="/absolute/path/to/Fakturama2.app"
TJM_JAVA="/Library/Java/JavaVirtualMachines/temurin-22.jdk/Contents/Home/bin/java"
open -n "$TJM_APP_BUNDLE" --args -vm "$TJM_JAVA" \
  --workspace "$TJM_DEMO_DATA" -data "$TJM_DEMO_SETTINGS" -nl en_US
```

In Initialization, confirm the new working directory and default database
settings, then acknowledge the workspace-restart message. In the recorded setup,
Fakturama's internal restart did not retain all the launch information the
program checks. Quitting normally and repeating the same launch command resolved
that setup step. If startup remains incomplete or an unexpected prompt appears,
inspect it before running.

In System Settings → Privacy & Security, grant **Accessibility** and **Screen
Recording** to the terminal or editor application launching Python. These
permissions allow it to read the screen and operate controls; the program checks
them and stops when they are unavailable.

Use an empty workspace with only the Documents/Fakturama tabs open and no unsaved
changes. Keep the entire window visible on one display. The demo used a
**1500 × 900 window in screen units, at scale 2**. Other complete layouts have
not been tested. Controls are located from the current screen and accessibility
information.

The helper's preferences use EUR code display, no thousands separator, visible
descriptions, per-line discounts and no quantity-unit column. In Preferences → Documents,
confirm **immediately take over a clearly found item number** is disabled, then
cancel. Contacts address format is:

```text
{company}<br>{street}<br>{zip} {city}<br>{country}
```

Confirm these preferences before running. The image's `pcs` unit stays in the
extracted data; the assignment's numbered steps do not require entering the
optional GUI unit field.

## 4. Execute once and read the result

```sh
.venv/bin/tjm run fixtures/demo-example-com.png \
  --workspace "$TJM_DEMO_DATA" --output runs/demo-01 \
  --name-order given-family --workarounds fakturama-2.2.0 \
  --original-image fixtures/original.png
```

The demo image has a regenerated layout; its only business-data change is the
email from `.test` to `.com`. The `fakturama-2.2.0` workaround profile retains both
input hashes and creates a missing payment method before opening the customer editor.
It requires the new empty workspace prepared above.
[Demo notes](../demo/README.md#fakturama-220-quirk-workarounds) explain why.

`run` exits **0** only for `verified`; `review_required`/`failed` exit **2**.
The command prints the path to `workflow-report.json`, which contains the checks,
any issues, screenshot references and record history. If a run stops, keep its
data and inspect the report before repeating the command: it may have created
records, and automatic rollback or resume is not implemented. The recorded
run took about 33 minutes; other runs may take a different amount of time.

The desktop workflow supports exactly two rows, up to two effective decimal
places, four-line addresses, and no shipping or overall discount. Other input
and environment limits are listed in [limitations](LIMITATIONS.md).
