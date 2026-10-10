"""Generate fictional OCR test images; none of these values are application data.

The expected values are specified independently of the production parser. Images
keep the documented English labels and a ruled table, while values, row counts,
addresses, payment state and placement vary. Pillow is the only dependency.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent


def font_path(bold: bool = False) -> Path:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
             "/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Install Arial, Liberation Sans or DejaVu Sans to regenerate the fixtures.")


def address(recipient, street, postal_code, city, country="Germany"):
    return dict(recipient=recipient, street=street, postal_code=postal_code,
                city=city, country=country, additional_name=None,
                address_specification=None, district=None)


def item(sku, description, quantity, unit_net, vat_percent,
         discount_percent, line_net):
    return dict(sku=sku, description=description, quantity=quantity, unit="pcs",
                unit_net=unit_net, vat_percent=vat_percent,
                discount_percent=discount_percent, line_net=line_net)


def order(reference, date, source_id, company, contact_name, alias, billing,
          delivery, email, telephone, payment, items, totals):
    return dict(external_reference=reference, order_date=date, currency="EUR",
                customer=dict(company=company, contact_name=contact_name,
                              first_name=None, last_name=None, alias=alias,
                              billing=billing, delivery=delivery, email=email,
                              telephone=telephone, source_customer_id=source_id),
                payment=dict(**payment, source_amount=None), items=items,
                totals=totals, overall_discount_percent="0", shipping_net="0",
                shipping_vat_percent=None)


def cases():
    one = order(
        "ALT-2026-0803-X09", "2026-08-03", "CLIENT-0024", "Harbor Studio Ltd",
        "Lena Weber", "HARBOR-MAIN",
        address("Harbor Studio Ltd", "Lindenweg 12", "00421", "Dresden"),
        address("Harbor Studio Ltd", "Lindenweg 12", "00421", "Dresden"),
        "lena.weber@example.test", "+49 351 1230 450",
        dict(method="Credit Card", status="UNPAID", date=None),
        [item("LMP-TASK-77", "Task Lamp", "4", "25.00", "19", "0", "100.00")],
        dict(net="100.00", vat="19.00", gross="119.00"),
    )
    three = order(
        "PO-2026-0902-B32", "2026-09-02", "ACCOUNT-0871", "Grove Workshop GmbH",
        "Jonas Meyer", "GROVE-HQ",
        address("Grove Workshop GmbH", "Parkstrasse 21", "50667", "Cologne"),
        address("Grove Storage Depot", "Hafenweg 9", "50735", "Cologne"),
        "jonas.meyer@example.test", "+49 221 9800 622",
        dict(method="SEPA Direct Debit", status="PAID", date="2026-09-06"),
        [item("BND-PACK-88", "Binder Pack", "5", "12.00", "19", "0", "60.00"),
         item("PAD-SKETCH-90", "Sketch Pad", "4", "15.00", "7", "0", "60.00"),
         item("CAB-STOR-06", "Storage Cabinet", "2", "100.00", "19", "10", "180.00")],
        dict(net="300.00", vat="49.80", gross="349.80"),
    )
    repeated = order(
        "SHOP-2026-1010-R55", "2026-10-10", "BUYER-0052", "Market Systems AG",
        "Pia Schulz", "MARKET-LEIPZIG",
        address("Market Systems AG", "Marktweg 8", "04109", "Leipzig"),
        address("Market Systems Stores", "Industrieweg 36", "04347", "Leipzig"),
        "pia.schulz@example.test", "+49 341 2200 711",
        dict(method="Bank Transfer", status="PAID", date="2026-10-13"),
        [item("CAB-USB-09", "USB Cable", "2", "10.00", "19", "0", "20.00"),
         item("CAB-USB-09", "USB Cable", "3", "10.00", "19", "20", "24.00")],
        dict(net="44.00", vat="8.36", gross="52.36"),
    )
    output = {
        "one_row_unpaid": dict(raw=one, expected_status="validated",
                               first_name="Lena", last_name="Weber",
                               product_gross=["29.75"], paid_amount=None),
        "three_rows_mixed_vat": dict(raw=three, expected_status="validated",
                                    first_name="Jonas", last_name="Meyer",
                                    product_gross=["14.28", "16.05", "119.00"],
                                    paid_amount="349.80"),
        "repeated_sku": dict(raw=repeated, expected_status="validated",
                             first_name="Pia", last_name="Schulz",
                             product_gross=["11.90", "11.90"], paid_amount="52.36"),
        "scaled_moved": dict(raw=copy.deepcopy(three), expected_status="validated",
                             first_name="Jonas", last_name="Meyer",
                             product_gross=["14.28", "16.05", "119.00"],
                             paid_amount="349.80", scale="0.8", offset=[170, 130]),
    }
    def invalid(name, mutation, *, field, render_options=None):
        raw = copy.deepcopy(one)
        mutation(raw)
        output[name] = dict(raw=raw, expected_status="review_required",
                            issue_field=field, render_options=render_options or {})
    invalid("missing_vat", lambda raw: raw["items"][0].update(vat_percent="?"),
            field="items[0].vat_percent")
    invalid("unreadable_row", lambda raw: raw["items"][0].update(description="[UNREADABLE]"),
            field="items[0].description")
    invalid("ambiguous_name", lambda raw: raw["customer"].update(contact_name="Ana Maria Silva"),
            field="customer.contact_name")
    invalid("contradictory_total", lambda raw: raw["totals"].update(gross="420.00"),
            field="totals.gross")
    invalid("swapped_date_reference", lambda raw: raw.update(order_date="ALT-2026-0803-X09",
                                                             external_reference="2026-08-03"),
            field="order_date")
    invalid("missing_reference_label", lambda raw: None, field="external_reference",
            render_options={"omit_reference_label": True})
    return output


def render(raw: dict, *, omit_reference_label=False) -> Image.Image:
    image = Image.new("RGB", (1760, 2040), "white")
    draw = ImageDraw.Draw(image)
    regular = ImageFont.truetype(str(font_path()), 25)
    heading = ImageFont.truetype(str(font_path(True)), 27)
    label_font = ImageFont.truetype(str(font_path(True)), 20)
    title_font = ImageFont.truetype(str(font_path(True)), 45)
    table_font = ImageFont.truetype(str(font_path()), 23)
    table_bold = ImageFont.truetype(str(font_path(True)), 23)
    navy, blue, gray = "#16324B", "#286BA4", "#596778"
    left, right = 110, 1650
    draw.rectangle((0, 0, 1760, 86), fill=navy)
    draw.text((110, 29), "FICTIONAL OCR TEST FIXTURE", font=heading, fill="white")
    draw.text((110, 135), "SALES ORDER INPUT", font=title_font, fill=navy)
    draw.text((110, 197), "Independent generated test data - not a real order", font=regular, fill=gray)

    def section(text, y):
        draw.text((left, y), text, font=heading, fill=blue)

    def box(y, bottom, tint=False):
        draw.rounded_rectangle((left, y, right, bottom), radius=12,
                               fill="#EDF4FA" if tint else "white", outline="#BDCCDB", width=2)

    def field(label, value, x, y):
        if label:
            draw.text((x, y), label, font=label_font, fill=gray)
        if value is not None:
            draw.text((x, y+34), str(value), font=regular, fill=navy)

    section("ORDER", 270)
    box(308, 431, True)
    field("" if omit_reference_label else "EXTERNAL REFERENCE", raw["external_reference"], 140, 328)
    field("ORDER DATE", raw["order_date"], 600, 328)
    field("CUSTOMER ID", raw["customer"]["source_customer_id"], 980, 328)
    field("CURRENCY", raw["currency"], 1360, 328)
    section("CUSTOMER AND CONTACT", 480)
    box(520, 776)
    draw.line((875, 543, 875, 752), fill="#BDCCDB", width=2)
    customer = raw["customer"]
    field("COMPANY", customer["company"], 140, 542)
    field("CUSTOMER ALIAS", customer["alias"], 140, 645)
    field("CONTACT NAME", customer["contact_name"], 915, 542)
    field("EMAIL", customer["email"], 915, 619)
    field("PHONE", customer["telephone"], 915, 694)
    section("ADDRESSES", 822)
    box(862, 1116)
    draw.line((875, 882, 875, 1094), fill="#BDCCDB", width=2)
    for key, x in (("billing", 140), ("delivery", 915)):
        draw.text((x, 888), key.upper()+" ADDRESS", font=label_font, fill=gray)
        addr = customer[key]
        lines = [addr["recipient"], addr["street"], addr["postal_code"]+" "+addr["city"], addr["country"]]
        for i, value in enumerate(lines):
            draw.text((x, 933+i*38), value, font=regular, fill=navy)
    section("PAYMENT", 1160)
    box(1200, 1321, True)
    payment = raw["payment"]
    field("PAYMENT METHOD", payment["method"], 140, 1221)
    field("PAID STATUS", payment["status"], 775, 1221)
    field("PAYMENT DATE", payment["date"], 1210, 1221)
    section("ITEMS", 1360)
    columns = [60, 220, 505, 85, 95, 160, 105, 100, 210]
    boundaries = [left]
    for width in columns:
        boundaries.append(boundaries[-1]+width)
    top, header_height, row_height = 1405, 64, 78
    bottom = top + header_height + row_height*len(raw["items"])
    draw.rectangle((left, top, right, top+header_height), fill=navy)
    for i, label in enumerate(("#", "SKU", "Description", "Qty", "Unit", "Unit net\n(EUR)", "Disc.", "VAT", "Line net\n(EUR)")):
        center = (boundaries[i]+boundaries[i+1]) / 2
        draw.multiline_text((center, top+31), label, font=label_font, fill="white",
                            anchor="mm", align="center", spacing=0)
    for index, value in enumerate(raw["items"]):
        row_top = top+header_height+index*row_height
        draw.rectangle((left, row_top, right, row_top+row_height), fill="#F3F7FA" if index % 2 else "white")
        texts = [str(index+1), value["sku"], value["description"], value["quantity"],
                 value["unit"], value["unit_net"], value["discount_percent"]+"%",
                 value["vat_percent"]+"%", value["line_net"]]
        for i, text in enumerate(texts):
            if i == 2:
                draw.text((boundaries[i]+12, row_top+25), text, font=table_font, fill=navy)
            else:
                center = (boundaries[i]+boundaries[i+1])/2
                draw.text((center, row_top+39), text, font=table_bold if i in (1, 8) else table_font,
                          fill=navy, anchor="mm")
    for x in boundaries:
        draw.line((x, top, x, bottom), fill="#BDCCDB", width=2)
    for y in [top, top+header_height]+[top+header_height+i*row_height for i in range(1, len(raw["items"])+1)]:
        draw.line((left, y, right, y), fill="#BDCCDB", width=2)
    totals_top = bottom+36
    box(totals_top, totals_top+124, True)
    for x, key in ((365, "net"), (875, "vat"), (1385, "gross")):
        draw.text((x, totals_top+28), key.upper()+" TOTAL", font=label_font, fill=gray, anchor="mm")
        draw.text((x, totals_top+78), "EUR "+raw["totals"][key], font=heading, fill=navy, anchor="mm")
    draw.text((left, 1970), "Clearly synthetic test data | Prices are net | Dates: YYYY-MM-DD", font=label_font, fill=gray)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT)
    arguments = parser.parse_args()
    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    manifest = cases()
    for name, case in manifest.items():
        image = render(case["raw"], **case.get("render_options", {}))
        if name == "scaled_moved":
            image = image.resize((1408, 1632), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (1798, 1842), "#EEEEEE")
            canvas.paste(image, tuple(case["offset"]))
            image = canvas
        image.save(output / (name+".png"))
    (output / "synthetic_expected.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
