# Limitations

The [demo](../demo/README.md) shows one complete run with two item rows.
The current desktop support is English Fakturama 2.2.0 on macOS ARM, with
EUR display and a new empty workspace. Installation and permissions are covered
in [setup](SETUP.md).

## Fakturama 2.2.0 Quirk Workarounds

- **Email:** a `.test` email became blank after saving a contact in Fakturama.
  The [contact probe](../demo/email-probe.json) records this behavior. Extraction
  keeps `marta.klein@example.test` from [the original image](../fixtures/original.png);
  the desktop preflight stops before creating records. The
  [demo image](../fixtures/demo-example-com.png) uses `.com` in a regenerated
  layout, with all other business values unchanged. The program records this
  substitution rather than changing the original input.
- **Payment dropdown:** an open customer editor does not refresh its choices
  after a payment method is created. The workaround confirms customer absence,
  creates the method, then opens New Contact. This differs from assignment
  §2.10's instruction to keep the Debtor editor open during method creation.
- **Warehouse address:** the customer has an additional address marked Delivery,
  with the warehouse recipient entered as its additional name. Fakturama's
  document display still uses the customer's company name, so the Order's
  delivery text is replaced with the image's four-line warehouse address. The
  linked Invoice inherits it, and both documents are checked after reopening.
  Those checks cover the displayed addresses and document link; internal
  contact/address associations are not inspected.

## Supported input and layout

Local OCR handles the tested English printed form. The desktop workflow requires
two item rows, four-line addresses without optional fields, and at most two
effective decimal places in quantity, net price, VAT and discount. Trailing
zeroes are allowed. Shipping and overall discount must be zero, with no shipping
VAT. Unsupported inputs stop before desktop interaction.
[Input checks](../src/tjm/workflow_report.py)

The demo window was 1500 × 900 screen units at scale 2. The item-table code
handles the observed VAT widths of 150 and 300 image pixels. Other layouts,
operating systems, versions and themes have not been tested. macOS accessibility
provides control values and actions alongside OCR.

## Test environments

The [test output](../demo/tests.txt) records the
[reference environment result](../README.md#tests). Table structure tests use
controlled or recorded OCR observations. Real OCR cases check that values are
correct or that uncertainty produces a review result.

Tesseract and its English language data are installed separately from the Python
lockfile. OCR output and platform-specific desktop dependencies can differ across
environments, so the archived result describes the reference setup. The tests
use OCR fixtures and simulated controls; the desktop demonstration is recorded
separately.

## Further coverage

Live coverage currently focuses on new customer and product records, Bank
Transfer and a Paid Invoice. Existing or conflicting customers/payment methods,
Credit Card or SEPA, unpaid invoices, identical addresses, repeated SKUs, and
broader VAT and rounding cases need further live tests. Salutation and Product
Picture are not automatically checked fields. Optional AI extraction has not
been tested with a live model.

Automatic rollback, resume and comprehensive duplicate prevention are not
implemented. If a run stops, preserve its data and inspect the report before
repeating the command. [Setup and running](SETUP.md)
