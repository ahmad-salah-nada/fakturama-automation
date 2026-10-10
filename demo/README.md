# Demo

The [annotated screenshots](screenshots.pdf) show the Order, the Invoice's payment
fields and their transaction view from one completed run on October 10, 2026.

| Result | Recorded value |
| --- | --- |
| Run | One continuous command; exit 0, `verified`; about 33 minutes. |
| Order | PO000001; Open; July 14, 2026; reference WEB-2026-0714-A17. |
| Invoice | Linked INV000001; Paid; Fakturama's proposed Invoice and service dates of October 10, 2026 retained. |
| Amount | EUR 570.00 net + 108.30 VAT = **678.30**. |
| Payment | Bank Transfer; July 18, 2026; **678.30**. |
| Records created | Payment method, customer, VAT, two Products, Order and Invoice; each saved once, then closed and reopened. |

Both item rows were checked: chairs `2 × 250.00 × 90% = 450.00` and mats
`3 × 40.00 = 120.00`, each with 19% VAT. The billing and warehouse addresses
matched the image in both documents. The checks also covered dates, reference,
document states, payment and the Order–Invoice link.

The [summary](summary.json) lists the expected and observed values. The
[workflow report](workflow-report.json) and [execution record](execution.json)
contain the run's details and command. The [test output](tests.txt) records the
offline reference run described in the [README](../README.md#tests).

## Fakturama 2.2.0 Quirk Workarounds

The original image contains `marta.klein@example.test`; extraction keeps it.
Fakturama 2.2.0 dropped an email with the same `.test` domain in a
[contact probe](email-probe.json). The demo therefore uses
`marta.klein@example.com`, its only business-data change. The original image
remains available for extraction and comparison.

The payment dropdown in an open customer editor does not refresh after creating
a missing method. The demo creates Bank Transfer before opening New Contact,
so the method appears in the choices. This is the workaround for the creation
sequence in assignment §2.10.

The customer's additional Delivery address is kept, and the Order's delivery
text is set to the warehouse address so the recipient displays correctly.
The linked Invoice retains that text. Implementation details and remaining
coverage are in [limitations](../docs/LIMITATIONS.md).
