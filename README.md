# fakturama-automation

A Python program that reads an order image and enters its customer, products,
Order and Invoice through Fakturama's desktop interface. It checks the amounts
before entering data, then closes and reopens records to check the result.

The [design proposal](docs/design.pdf) outlines the workflow and its tradeoffs. The
[demo](demo/README.md) includes screenshots and the recorded result;
[setup](docs/SETUP.md) covers installation and running the program.

## Demo result

One complete run created **Order PO000001** and its linked **Invoice INV000001**
for **EUR 678.30**. The Order remained Open, and the Invoice was Paid by Bank
Transfer. Reopening the records confirmed the addresses, item quantities,
prices, VAT, discounts, totals, payment and Order–Invoice link.
[Screenshots](demo/screenshots.pdf) · [Recorded result](demo/summary.json)

The run uses a demo image with a `.com` email and a workaround for Fakturama
2.2.0's payment dropdown. [Demo notes](demo/README.md#fakturama-220-quirk-workarounds)
explain both; [limitations](docs/LIMITATIONS.md) describe the supported scope.

## Try extraction

With uv and Tesseract's English language data installed, run from this directory:

```sh
uv sync --frozen --python 3.11
.venv/bin/tjm doctor
.venv/bin/tjm extract fixtures/original.png --name-order given-family
```

This reads the original image without creating Fakturama records. It preserves
`marta.klein@example.test`. The name option treats Marta as the first
name and Klein as the last name; ambiguous names need review.

An extraction result of `validated` means the data passed its checks. A desktop
result of `verified` means the saved-record checks also passed. Running the
desktop workflow requires the extra dependencies, permissions and empty
workspace described in [setup](docs/SETUP.md).

## How it works

The code has four main responsibilities:

- **Read:** local OCR turns the image's printed text into structured order data.
  An optional AI extractor is also available.
- **Check:** required fields and decimal calculations must agree before desktop
  input begins. Decimal arithmetic avoids floating-point errors in money.
- **Enter:** OCR locates visible text, while macOS accessibility supplies control
  values and actions. The workflow checks the active window and selected record
  before changing them.
- **Confirm:** records are reopened and compared with the image. A JSON
  report records the checks and any problem that needs review.

If a control or outcome is uncertain, the program stops. It does not automatically
undo or resume a partly completed run. Desktop support currently covers the
tested two-item form on English Fakturama 2.2.0 on macOS; other inputs and layouts
are described in [limitations](docs/LIMITATIONS.md).

## Tests

```sh
uv sync --frozen --extra ai --extra desktop --python 3.11
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests tools
```

In the reference environment—macOS ARM, Python 3.11.16, Tesseract 5.5.0 and the
locked dependencies—**1,342 tests passed with zero skips**, and Ruff passed.
[Test output](demo/tests.txt) · [Check summary](demo/summary.json)

These tests cover calculations, OCR fixtures and simulated desktop controls.
They run offline without creating Fakturama records or calling an AI model.
The demo checks the resulting records in Fakturama. Environment differences are
covered in [limitations](docs/LIMITATIONS.md#test-environments).

## With three more hours

I would first investigate why the original `.test` email disappears after saving,
using controlled manual and automated entry tests. Next, I would work on creating
and selecting a missing payment method while keeping the Debtor editor open, as
the assignment requires. I would use the remaining time for a focused Fakturama
regression run, checking the warehouse address association, and recording which
steps are complete and which still need work.