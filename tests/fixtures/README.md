# Independently specified OCR fixtures

These files test extraction without opening or changing Fakturama. All supplied
and generated values are fictional. The original page-3 image is retained at
`fixtures/original.png`; `sample_expected.json` transcribes its
complete field and row values. The independently written expected master gross
prices are 297.50 and 47.60; the paid amount is the full 678.30 invoice total,
derived under the assignment rule rather than a separate printed payment amount.

The generator contains only test data and never belongs in the production import
path. `synthetic_expected.json` records exactly what each generated image prints
and the independently specified expected result. Run
`python tests/fixtures/generate_orders.py` to regenerate the PNGs and manifest;
Pillow and Arial, Liberation Sans or DejaVu Sans fonts are needed. The committed
PNGs can be tested without regenerating them. Font differences can change OCR,
so a regenerated fixture needs a fresh integration run.

| Image | Intended check |
| --- | --- |
| `one_row_unpaid.png` | One row, changed customer/addresses/SKU, leading-zero ZIP, Credit Card and UNPAID with no payment date. |
| `three_rows_mixed_vat.png` | Three separate rows, changed master details, 19% and 7% VAT, SEPA Direct Debit and separate delivery recipient. |
| `repeated_sku.png` | Two transactions for one SKU retain their source order, distinct quantities and distinct discounts. |
| `scaled_moved.png` | The three-row image reduced to 80% and moved within a larger canvas; values and positions must come from the image. |
| `missing_vat.png` | A visible `?%` cell must require review; totals cannot be used to invent the VAT rate. |
| `unreadable_row.png` | A literal `[UNREADABLE]` description must require review, with no silently discarded row. |
| `ambiguous_name.png` | A three-part unsplit name requires review even with the supported two-part convention selected. |
| `contradictory_total.png` | A printed 420.00 gross contradicts the 100.00 net and 19.00 VAT. |
| `swapped_date_reference.png` | Reference/date values printed under the opposite labels cannot be accepted as a real date. |
| `missing_reference_label.png` | A visible orphan value does not justify guessing which required field it represents. |

The integration tests compare **every** raw and validated field and every ordered
row for accepted images. Decimal representation differences, such as `19` versus
`19.00`, are compared numerically; names, SKUs, references, dates, addresses and
leading zeros are compared exactly. Rejection tests require the relevant issue
field and an absent validated Order, not just any failure or a crashed program.

This is a narrow input contract: English printed labels, readable text, separately
labelled addresses and a clearly ruled item table with explicit column headings.
These fixtures do not prove support for handwriting, arbitrary vendor layouts,
unlabelled fields, rotated/distorted scans or reliable Unicode OCR. The scaled
layout is supported only when an actual recorded integration run passes. These
tests also do not establish Fakturama's UI behavior or saved records.
