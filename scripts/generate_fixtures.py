"""Generate the committed fixture set.

Everything this writes is synthetic. No supplier name, tax number or bank
detail here belongs to a real company, per rule 5.6.

The script is committed alongside its output so the fixtures can be
regenerated or extended without hand editing JSON. Run it with:

    uv run python scripts/generate_fixtures.py

Source documents are real, openable files: the PDFs have a correct cross
reference table and the images have valid headers. Cached Document AI
responses are hand shaped to match the real Invoice Parser output, because
calling the live processor to build a fixture set would cost money for no
benefit.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import struct
import zlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
SOURCE_DIR = FIXTURES / "source"
EXTRACTION_DIR = FIXTURES / "extractions"
GMAIL_DIR = FIXTURES / "gmail"
PUBSUB_DIR = FIXTURES / "pubsub"


# --------------------------------------------------------------------------
# Source document builders
# --------------------------------------------------------------------------


def build_pdf(pages: list[list[str]]) -> bytes:
    """A valid multi page PDF with a correct cross reference table."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = 0
    page_ids: list[int] = []
    content_ids: list[int] = []

    # Reserve ids 1 and 2 for the catalogue and page tree.
    add(b"")  # 1: catalogue, filled in later
    add(b"")  # 2: page tree, filled in later
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for lines in pages:
        stream_parts = ["BT", "/F1 11 Tf", "50 790 Td", "14 TL"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream_parts.append(f"({escaped}) Tj")
            stream_parts.append("T*")
        stream_parts.append("ET")
        stream = "\n".join(stream_parts).encode("latin-1", errors="replace")
        content_id = add(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        content_ids.append(content_id)
        page_ids.append(0)  # placeholder, replaced below

    for index, content_id in enumerate(content_ids):
        page_id = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 "
            + str(font_id).encode()
            + b" 0 R >> >> /Contents "
            + str(content_id).encode()
            + b" 0 R >>"
        )
        page_ids[index] = page_id

    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + kids
        + b"] /Count "
        + str(len(page_ids)).encode()
        + b" >>"
    )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def build_png(width: int = 64, height: int = 64, shade: int = 200) -> bytes:
    """A valid greyscale PNG.

    Pixels carry deterministic noise rather than a gradient, so the file
    compresses to a size a photographed invoice would plausibly have. A
    smooth gradient deflates to a couple of kilobytes, which would sit
    below the attachment size floor and make the fixture
    unrepresentative.
    """
    rng = random.Random(shade)
    raw = b"".join(
        b"\x00"
        + bytes([(shade + x + y + rng.randrange(0, 96)) % 256 for x in range(width)])
        for y in range(height)
    )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def build_tiff(width: int = 64, height: int = 64) -> bytes:
    """A valid little endian greyscale TIFF."""
    pixels = bytes((x + y) % 256 for y in range(height) for x in range(width))
    entries = [
        (256, 3, 1, width),  # ImageWidth
        (257, 3, 1, height),  # ImageLength
        (258, 3, 1, 8),  # BitsPerSample
        (259, 3, 1, 1),  # Compression, none
        (262, 3, 1, 1),  # PhotometricInterpretation, black is zero
        (273, 4, 1, 8 + 2 + len(entries_placeholder := []) * 0),  # patched below
        (277, 3, 1, 1),  # SamplesPerPixel
        (278, 3, 1, height),  # RowsPerStrip
        (279, 4, 1, len(pixels)),  # StripByteCounts
    ]
    del entries_placeholder
    ifd_offset = 8
    ifd_size = 2 + len(entries) * 12 + 4
    strip_offset = ifd_offset + ifd_size
    entries[5] = (273, 4, 1, strip_offset)

    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_offset))
    out += struct.pack("<H", len(entries))
    for tag, kind, count, value in entries:
        out += struct.pack("<HHI", tag, kind, count)
        out += struct.pack("<I", value) if kind == 4 else struct.pack("<HH", value, 0)
    out += struct.pack("<I", 0)
    out += pixels
    return bytes(out)


# --------------------------------------------------------------------------
# Invoice definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One invoice line."""

    description: str
    quantity: str
    unit_price: str
    line_total: str
    confidence: float = 0.95


@dataclass(frozen=True)
class Invoice:
    """A synthetic invoice and the confidences its extraction should carry."""

    slug: str
    supplier: str
    tax_id: str
    number: str
    invoice_date: str
    due_date: str
    currency: str
    net: str
    tax: str
    total: str
    lines: list[Line]
    media_type: str = "application/pdf"
    pages: int = 1
    confidence: float = 0.97
    note: str = ""
    # Fields the extractor failed to read at all. Present with a null value,
    # which is deliberately different from the key being absent.
    missing: tuple[str, ...] = field(default_factory=tuple)
    language: str = "en"


INVOICES: list[Invoice] = [
    Invoice(
        "acme-office-supplies",
        "Acme Office Supplies Pte Ltd",
        "200812345K",
        "INV-1001",
        "2026-08-03",
        "2026-09-02",
        "SGD",
        "1200.00",
        "108.00",
        "1308.00",
        [
            Line("A4 copier paper, box of 5 reams", "12", "42.00", "504.00"),
            Line("Whiteboard markers, pack of 10", "24", "12.00", "288.00"),
            Line("Desk organiser, mesh", "16", "25.50", "408.00"),
        ],
    ),
    Invoice(
        "bluewave-logistics",
        "Bluewave Logistics Pte Ltd",
        "199905678M",
        "BW-2026-0442",
        "2026-08-05",
        "2026-09-04",
        "SGD",
        "3450.00",
        "310.50",
        "3760.50",
        [
            Line("Freight forwarding, Singapore to Penang", "1", "2200.00", "2200.00"),
            Line("Customs clearance handling", "1", "450.00", "450.00"),
            Line("Warehouse storage, 10 days", "10", "80.00", "800.00"),
        ],
    ),
    Invoice(
        "northgate-consulting",
        "Northgate Consulting Ltd",
        "GB443221190",
        "NGC-7781",
        "2026-07-28",
        "2026-08-27",
        "GBP",
        "8000.00",
        "1600.00",
        "9600.00",
        [
            Line("Process review, senior consultant", "40", "150.00", "6000.00"),
            Line("Workshop facilitation", "2", "1000.00", "2000.00"),
        ],
    ),
    Invoice(
        "harbourpoint-it",
        "Harbourpoint IT Services",
        "201234567W",
        "HP-INV-0098",
        "2026-08-11",
        "2026-08-25",
        "SGD",
        "980.00",
        "88.20",
        "1068.20",
        [
            Line("Managed endpoint support, monthly", "35", "28.00", "980.00"),
        ],
    ),
    Invoice(
        "meridian-facilities",
        "Meridian Facilities Management",
        "200validated",
        "MFM-4410",
        "2026-08-01",
        "2026-08-31",
        "SGD",
        "5600.00",
        "504.00",
        "6104.00",
        [
            Line("Office cleaning, August", "1", "3200.00", "3200.00"),
            Line("Pantry restocking", "1", "900.00", "900.00"),
            Line("Pest control, quarterly", "1", "1500.00", "1500.00"),
        ],
    ),
    Invoice(
        "cedarline-print",
        "Cedarline Print Works",
        "198800321H",
        "CPW-5567",
        "2026-08-14",
        "2026-09-13",
        "SGD",
        "742.50",
        "66.83",
        "809.33",
        [
            Line("Business cards, 500 units", "5", "45.00", "225.00"),
            Line("Brochure printing, A5 gloss", "750", "0.69", "517.50"),
        ],
    ),
    Invoice(
        "vantage-power",
        "Vantage Power Utilities",
        "197600998C",
        "VPU-20260812",
        "2026-08-12",
        "2026-08-26",
        "SGD",
        "2310.40",
        "207.94",
        "2518.34",
        [
            Line("Electricity supply, July", "18320", "0.126", "2308.32"),
            Line("Meter service charge", "1", "2.08", "2.08"),
        ],
    ),
    Invoice(
        "quayside-catering",
        "Quayside Catering Services",
        "201599887E",
        "QC-3321",
        "2026-08-19",
        "2026-09-18",
        "SGD",
        "1875.00",
        "168.75",
        "2043.75",
        [
            Line("Staff lunch catering, 75 pax", "75", "22.00", "1650.00"),
            Line("Beverage station", "1", "225.00", "225.00"),
        ],
    ),
    Invoice(
        "stellar-software",
        "Stellar Software Inc",
        "US880042113",
        "SSI-99120",
        "2026-07-31",
        "2026-08-30",
        "USD",
        "12000.00",
        "0.00",
        "12000.00",
        [
            Line("Platform licence, annual, 40 seats", "40", "300.00", "12000.00"),
        ],
    ),
    Invoice(
        "ironbridge-hardware",
        "Ironbridge Hardware Supply",
        "200455667D",
        "IBH-0771",
        "2026-08-08",
        "2026-09-07",
        "SGD",
        "634.20",
        "57.08",
        "691.28",
        [
            Line("Safety helmets, white", "18", "18.90", "340.20"),
            Line("Hi-vis vests, large", "21", "14.00", "294.00"),
        ],
    ),
    Invoice(
        "lumen-marketing",
        "Lumen Marketing Collective",
        "201877665B",
        "LMC-2026-118",
        "2026-08-16",
        "2026-09-15",
        "SGD",
        "4400.00",
        "396.00",
        "4796.00",
        [
            Line("Campaign design retainer", "1", "3200.00", "3200.00"),
            Line("Copywriting, per article", "6", "200.00", "1200.00"),
        ],
    ),
    Invoice(
        "portside-security",
        "Portside Security Pte Ltd",
        "200322114A",
        "PSS-8890",
        "2026-08-21",
        "2026-09-20",
        "SGD",
        "7280.00",
        "655.20",
        "7935.20",
        [
            Line("Security officer, day shift", "160", "26.00", "4160.00"),
            Line("Security officer, night shift", "104", "30.00", "3120.00"),
        ],
    ),
    # Multi page.
    Invoice(
        "grandmere-equipment",
        "Grandmere Equipment Leasing",
        "199111223F",
        "GEL-6654",
        "2026-08-02",
        "2026-09-01",
        "SGD",
        "18450.00",
        "1660.50",
        "20110.50",
        [
            Line("Forklift lease, unit A, monthly", "1", "3200.00", "3200.00"),
            Line("Forklift lease, unit B, monthly", "1", "3200.00", "3200.00"),
            Line("Scissor lift lease, monthly", "1", "2850.00", "2850.00"),
            Line("Pallet jack lease, monthly", "6", "180.00", "1080.00"),
            Line("Operator training, per session", "4", "520.00", "2080.00"),
            Line("Preventive maintenance, quarterly", "1", "3040.00", "3040.00"),
            Line("Insurance surcharge", "1", "3000.00", "3000.00"),
        ],
        pages=3,
        note="Multi page invoice: line items continue across three pages.",
    ),
    Invoice(
        "atlas-freight",
        "Atlas Freight Partners",
        "200766554J",
        "AFP-11902",
        "2026-08-06",
        "2026-09-05",
        "SGD",
        "9600.00",
        "864.00",
        "10464.00",
        [
            Line("Sea freight, 40ft container", "3", "2200.00", "6600.00"),
            Line("Inland haulage", "3", "600.00", "1800.00"),
            Line("Documentation fee", "3", "400.00", "1200.00"),
        ],
        pages=2,
        note="Multi page invoice with a continuation sheet.",
    ),
    # Non-English.
    Invoice(
        "pelletier-fournitures",
        "Pelletier Fournitures SARL",
        "FR76512348901",
        "FAC-2026-0331",
        "2026-08-09",
        "2026-09-08",
        "EUR",
        "2450.00",
        "490.00",
        "2940.00",
        [
            Line("Papier A4, carton de 5 rames", "10", "38.00", "380.00"),
            Line("Cartouches d'encre, noir", "14", "72.50", "1015.00"),
            Line("Chaises de bureau ergonomiques", "5", "211.00", "1055.00"),
        ],
        language="fr",
        note="Non-English invoice, French, EUR.",
    ),
    # Scanned.
    Invoice(
        "westmoor-timber",
        "Westmoor Timber Merchants",
        "198922334G",
        "WTM-0450",
        "2026-07-24",
        "2026-08-23",
        "SGD",
        "3180.00",
        "286.20",
        "3466.20",
        [
            Line("Plywood sheets, 18mm", "60", "34.00", "2040.00", confidence=0.82),
            Line("Timber battens, 2.4m", "120", "9.50", "1140.00", confidence=0.80),
        ],
        confidence=0.86,
        note="Scanned document: confidences reflect OCR rather than native text.",
    ),
    # Poor quality scan, with fields the extractor could not read.
    Invoice(
        "dockside-marine",
        "Dockside Marine Repairs",
        "",
        "DMR-2219",
        "2026-07-19",
        "",
        "SGD",
        "",
        "",
        "1890.00",
        [
            Line("Hull inspection", "1", "700.00", "700.00", confidence=0.44),
            Line("Anode replacement", "4", "", "", confidence=0.31),
        ],
        confidence=0.52,
        missing=("supplier_tax_id", "due_date", "net_amount", "tax_amount"),
        note="Deliberately poor quality scan: low confidence, unreadable fields.",
    ),
    # Image formats.
    Invoice(
        "kingsway-couriers",
        "Kingsway Couriers Pte Ltd",
        "201044556N",
        "KC-7788",
        "2026-08-13",
        "2026-09-12",
        "SGD",
        "418.00",
        "37.62",
        "455.62",
        [
            Line("Same day delivery, island wide", "22", "19.00", "418.00"),
        ],
        media_type="image/png",
        confidence=0.88,
        note="Photographed invoice supplied as a PNG.",
    ),
    Invoice(
        "orchid-labs",
        "Orchid Laboratory Services",
        "201988776P",
        "OLS-1130",
        "2026-08-18",
        "2026-09-17",
        "SGD",
        "5250.00",
        "472.50",
        "5722.50",
        [
            Line("Water quality analysis, per sample", "35", "150.00", "5250.00"),
        ],
        media_type="image/tiff",
        confidence=0.90,
        note="Faxed invoice supplied as a TIFF.",
    ),
]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_LABELS = {
    "en": {
        "invoice": "TAX INVOICE",
        "number": "Invoice number",
        "date": "Invoice date",
        "due": "Due date",
        "tax_id": "GST registration",
        "desc": "Description",
        "qty": "Qty",
        "unit": "Unit price",
        "total": "Amount",
        "net": "Subtotal",
        "tax_line": "Tax",
        "grand": "Total due",
        "page": "Page",
    },
    "fr": {
        "invoice": "FACTURE",
        "number": "Numero de facture",
        "date": "Date de facture",
        "due": "Date d'echeance",
        "tax_id": "Numero de TVA",
        "desc": "Designation",
        "qty": "Qte",
        "unit": "Prix unitaire",
        "total": "Montant",
        "net": "Total HT",
        "tax_line": "TVA",
        "grand": "Total TTC",
        "page": "Page",
    },
}


def render_pages(invoice: Invoice) -> list[list[str]]:
    """Lay the invoice out across its pages."""
    labels = _LABELS[invoice.language]
    per_page = max(1, -(-len(invoice.lines) // invoice.pages))
    pages: list[list[str]] = []

    for page_index in range(invoice.pages):
        lines = [
            invoice.supplier,
            f"{labels['tax_id']}: {invoice.tax_id or 'not shown'}",
            "",
            labels["invoice"],
            f"{labels['number']}: {invoice.number}",
            f"{labels['date']}: {invoice.invoice_date}",
            f"{labels['due']}: {invoice.due_date or 'not shown'}",
            "",
            f"{labels['desc']:<44}{labels['qty']:>8}"
            f"{labels['unit']:>14}{labels['total']:>14}",
            "-" * 80,
        ]
        chunk = invoice.lines[page_index * per_page : (page_index + 1) * per_page]
        for line in chunk:
            lines.append(
                f"{line.description[:44]:<44}{line.quantity:>8}"
                f"{line.unit_price or '?':>14}{line.line_total or '?':>14}"
            )
        lines.append("")
        if page_index == invoice.pages - 1:
            lines += [
                f"{labels['net']:>60}{invoice.net or '?':>18}",
                f"{labels['tax_line']:>60}{invoice.tax or '?':>18}",
                f"{labels['grand']:>60}{invoice.total:>18}",
                "",
                f"Currency: {invoice.currency}",
            ]
        lines.append("")
        lines.append(f"{labels['page']} {page_index + 1} / {invoice.pages}")
        if invoice.note:
            lines.append(f"Fixture note: {invoice.note}")
        lines.append("Synthetic document. Not a real supplier or invoice.")
        # Pad so the file clears the Gmail minimum attachment size. A real
        # invoice PDF is comfortably over 8 KB; a signature logo is not, and
        # the size floor is what separates them.
        lines += [
            f"Reference {invoice.slug}-{n:04d} continuation line for padding"
            for n in range(300)
        ]
        pages.append(lines)
    return pages


def source_bytes(invoice: Invoice) -> bytes:
    """Build the source document for an invoice."""
    if invoice.media_type == "image/png":
        return build_png(640, 480, shade=90)
    if invoice.media_type == "image/tiff":
        return build_tiff(640, 480)
    return build_pdf(render_pages(invoice))


_EXTENSION = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/jpeg": ".jpg",
}


# --------------------------------------------------------------------------
# Document AI response shaping
# --------------------------------------------------------------------------


def money_value(amount: str, currency: str) -> dict[str, object]:
    """Document AI reports money as integer units plus nanos, which is exact."""
    value = Decimal(amount)
    units = int(value)
    nanos = int((value - units) * Decimal(1_000_000_000))
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


def entity(
    entity_type: str,
    mention: str,
    confidence: float,
    *,
    normalised: dict[str, object] | None = None,
    page: int = 0,
) -> dict[str, object]:
    node: dict[str, object] = {
        "type": entity_type,
        "mentionText": mention,
        "confidence": round(confidence, 4),
        "pageAnchor": {"pageRefs": [{"page": str(page)}]},
    }
    if normalised is not None:
        node["normalizedValue"] = normalised
    return node


def build_response(invoice: Invoice) -> dict[str, object]:
    """Shape a cached Document AI response for one invoice."""
    entities: list[dict[str, object]] = []
    base = invoice.confidence

    def add_scalar(
        entity_type: str,
        value: str,
        field_name: str,
        normalised: dict[str, object] | None = None,
        bump: float = 0.0,
    ) -> None:
        # A field the extractor could not read is simply absent from the
        # response, which is how Document AI reports it.
        if field_name in invoice.missing or not value:
            return
        entities.append(
            entity(
                entity_type,
                value,
                min(base + bump, 0.9999),
                normalised=normalised,
            )
        )

    add_scalar("supplier_name", invoice.supplier, "supplier_name", bump=0.01)
    add_scalar("supplier_tax_id", invoice.tax_id, "supplier_tax_id")
    add_scalar("invoice_id", invoice.number, "invoice_number", bump=0.02)
    add_scalar(
        "invoice_date",
        invoice.invoice_date,
        "invoice_date",
        normalised={
            "dateValue": {
                "year": int(invoice.invoice_date[:4]),
                "month": int(invoice.invoice_date[5:7]),
                "day": int(invoice.invoice_date[8:10]),
            }
        },
    )
    if invoice.due_date and "due_date" not in invoice.missing:
        add_scalar(
            "due_date",
            invoice.due_date,
            "due_date",
            normalised={
                "dateValue": {
                    "year": int(invoice.due_date[:4]),
                    "month": int(invoice.due_date[5:7]),
                    "day": int(invoice.due_date[8:10]),
                }
            },
        )
    add_scalar(
        "net_amount",
        invoice.net,
        "net_amount",
        normalised={"moneyValue": money_value(invoice.net, invoice.currency)}
        if invoice.net
        else None,
    )
    add_scalar(
        "total_tax_amount",
        invoice.tax,
        "tax_amount",
        normalised={"moneyValue": money_value(invoice.tax, invoice.currency)}
        if invoice.tax
        else None,
    )
    add_scalar(
        "total_amount",
        invoice.total,
        "total_amount",
        normalised={"moneyValue": money_value(invoice.total, invoice.currency)},
        bump=0.02,
    )
    entities.append(entity("currency", invoice.currency, min(base + 0.01, 0.9999)))

    per_page = max(1, -(-len(invoice.lines) // invoice.pages))
    for index, line in enumerate(invoice.lines):
        page = index // per_page
        properties: list[dict[str, object]] = [
            entity(
                "line_item/description",
                line.description,
                line.confidence,
                page=page,
            ),
            entity("line_item/quantity", line.quantity, line.confidence, page=page),
        ]
        if line.unit_price:
            properties.append(
                entity(
                    "line_item/unit_price",
                    line.unit_price,
                    line.confidence,
                    normalised={
                        "moneyValue": money_value(line.unit_price, invoice.currency)
                    },
                    page=page,
                )
            )
        if line.line_total:
            properties.append(
                entity(
                    "line_item/amount",
                    line.line_total,
                    line.confidence,
                    normalised={
                        "moneyValue": money_value(line.line_total, invoice.currency)
                    },
                    page=page,
                )
            )
        entities.append(
            {
                "type": "line_item",
                "mentionText": line.description,
                "confidence": round(line.confidence, 4),
                "pageAnchor": {"pageRefs": [{"page": str(page)}]},
                "properties": properties,
            }
        )

    return {
        "mimeType": invoice.media_type,
        "text": f"{invoice.supplier}\n{invoice.number}\n{invoice.total}\n",
        "pages": [
            {
                "pageNumber": number,
                "dimension": {"width": 595, "height": 842, "unit": "points"},
            }
            for number in range(1, invoice.pages + 1)
        ],
        "entities": entities,
    }


# --------------------------------------------------------------------------
# Gmail and Pub/Sub fixtures
# --------------------------------------------------------------------------


def gmail_message(
    message_id: str,
    history_ms: int,
    attachments: list[tuple[str, str, str, int]],
    *,
    with_inline_logo: bool = False,
) -> dict[str, object]:
    """A Gmail message with the parts shape the API actually returns."""
    parts: list[dict[str, object]] = [
        {
            "partId": "0",
            "mimeType": "text/plain",
            "filename": "",
            "body": {"size": 220},
        }
    ]
    if with_inline_logo:
        # An embedded signature logo. It arrives as an attachment part and
        # would become a document and a paid extraction without the filter.
        parts.append(
            {
                "partId": "1",
                "mimeType": "image/png",
                "filename": "signature-logo.png",
                "headers": [
                    {"name": "Content-ID", "value": "<logo@example.test>"},
                    {
                        "name": "Content-Disposition",
                        "value": 'inline; filename="signature-logo.png"',
                    },
                ],
                "body": {"attachmentId": "att-logo", "size": 4096},
            }
        )
    for index, (attachment_id, filename, media_type, size) in enumerate(attachments):
        parts.append(
            {
                "partId": str(index + 2),
                "mimeType": media_type,
                "filename": filename,
                "headers": [
                    {
                        "name": "Content-Disposition",
                        "value": f'attachment; filename="{filename}"',
                    }
                ],
                "body": {"attachmentId": attachment_id, "size": size},
            }
        )
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["ap-inbox", "INBOX"],
        "internalDate": str(history_ms),
        "payload": {"mimeType": "multipart/mixed", "parts": parts},
    }


def pubsub_envelope(
    message_id: str, data: dict[str, object], subscription: str
) -> dict[str, object]:
    """A Pub/Sub push envelope."""
    import base64

    return {
        "message": {
            "messageId": message_id,
            "publishTime": "2026-08-20T02:00:00.000Z",
            "data": base64.b64encode(
                json.dumps(data, separators=(",", ":")).encode()
            ).decode(),
            "attributes": {},
        },
        "subscription": subscription,
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    for directory in (SOURCE_DIR, EXTRACTION_DIR, PUBSUB_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("history", "messages", "attachments"):
        (GMAIL_DIR / name).mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, object]] = []
    for invoice in INVOICES:
        data = source_bytes(invoice)
        digest = hashlib.sha256(data).hexdigest()
        filename = f"{invoice.slug}{_EXTENSION[invoice.media_type]}"
        (SOURCE_DIR / filename).write_bytes(data)
        (EXTRACTION_DIR / f"{digest}.json").write_text(
            json.dumps(build_response(invoice), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        manifest.append(
            {
                "slug": invoice.slug,
                "file": filename,
                "media_type": invoice.media_type,
                "content_hash": digest,
                "bytes": len(data),
                "pages": invoice.pages,
                "currency": invoice.currency,
                "total_amount": invoice.total,
                "language": invoice.language,
                "note": invoice.note,
            }
        )

    # A deliberately malformed response, for the schema failure test. The
    # confidence is out of range, which normalisation passes through
    # unchanged, so validation is what has to catch it.
    malformed_source = build_pdf(
        [
            ["Broken Invoice Ltd", "This fixture exists to fail validation."]
            + [
                f"Padding line {n:04d} to clear the attachment floor"
                for n in range(300)
            ]
        ]
    )
    malformed_hash = hashlib.sha256(malformed_source).hexdigest()
    (SOURCE_DIR / "malformed-invoice.pdf").write_bytes(malformed_source)
    (EXTRACTION_DIR / f"{malformed_hash}.json").write_text(
        json.dumps(
            {
                "mimeType": "application/pdf",
                "pages": [{"pageNumber": 1}],
                "entities": [
                    entity("invoice_id", "INV-BROKEN", 1.5),
                    entity(
                        "total_amount",
                        "10.00",
                        0.9,
                        normalised={
                            "moneyValue": {
                                "currencyCode": "SGD",
                                "units": "10",
                                "nanos": 0,
                            }
                        },
                    ),
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest.append(
        {
            "slug": "malformed-invoice",
            "file": "malformed-invoice.pdf",
            "media_type": "application/pdf",
            "content_hash": malformed_hash,
            "bytes": len(malformed_source),
            "pages": 1,
            "currency": "SGD",
            "total_amount": "10.00",
            "language": "en",
            "note": "Deliberately malformed: confidence above 1 fails validation.",
            "malformed": True,
        }
    )

    (FIXTURES / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # Gmail: one message with three attachments, one with an inline logo that
    # must be filtered out, and one ordinary single attachment message.
    by_slug = {item["slug"]: item for item in manifest}
    groups = [
        (
            "msg-0001",
            1_755_600_000_000,
            ["acme-office-supplies", "bluewave-logistics", "northgate-consulting"],
            False,
        ),
        ("msg-0002", 1_755_686_400_000, ["harbourpoint-it"], True),
        ("msg-0003", 1_755_772_800_000, ["grandmere-equipment"], False),
        ("msg-0004", 1_755_859_200_000, ["pelletier-fournitures"], False),
    ]
    message_ids: list[str] = []
    for message_id, stamp, slugs, inline in groups:
        attachments: list[tuple[str, str, str, int]] = []
        for slug in slugs:
            item = by_slug[slug]
            attachment_id = f"att-{slug}"
            attachments.append(
                (
                    attachment_id,
                    str(item["file"]),
                    str(item["media_type"]),
                    int(item["bytes"]),
                )
            )
            shutil.copyfile(
                SOURCE_DIR / str(item["file"]),
                GMAIL_DIR / "attachments" / attachment_id,
            )
        (GMAIL_DIR / "messages" / f"{message_id}.json").write_text(
            json.dumps(
                gmail_message(message_id, stamp, attachments, with_inline_logo=inline),
                indent=2,
            ),
            encoding="utf-8",
        )
        message_ids.append(message_id)

    (GMAIL_DIR / "history" / "1000.json").write_text(
        json.dumps({"message_ids": message_ids, "new_history_id": "1400"}, indent=2),
        encoding="utf-8",
    )
    # A range containing exactly one message, which itself carries one
    # qualifying attachment and one inline logo that must be filtered out.
    (GMAIL_DIR / "history" / "2000.json").write_text(
        json.dumps({"message_ids": ["msg-0002"], "new_history_id": "2100"}, indent=2),
        encoding="utf-8",
    )
    (GMAIL_DIR / "history" / "2100.json").write_text(
        json.dumps({"message_ids": [], "new_history_id": "2100"}, indent=2),
        encoding="utf-8",
    )
    (GMAIL_DIR / "history" / "1400.json").write_text(
        json.dumps({"message_ids": [], "new_history_id": "1400"}, indent=2),
        encoding="utf-8",
    )
    (GMAIL_DIR / "history" / "resync.json").write_text(
        json.dumps({"message_ids": message_ids, "new_history_id": "1500"}, indent=2),
        encoding="utf-8",
    )

    (PUBSUB_DIR / "gmail_notify.json").write_text(
        json.dumps(
            pubsub_envelope(
                "pubsub-msg-0001",
                {"emailAddress": "ap@example.test", "historyId": "1400"},
                "projects/ap-autopilot/subscriptions/gmail-notifications-push",
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    (PUBSUB_DIR / "extraction_complete.json").write_text(
        json.dumps(
            pubsub_envelope(
                "pubsub-msg-0002",
                {
                    "bucket": "ap-autopilot-docs",
                    "name": "extractions/JOB_ID_HERE/output-0-to-1.json",
                },
                "projects/ap-autopilot/subscriptions/extraction-complete-push",
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    (PUBSUB_DIR / "malformed.json").write_text(
        json.dumps(
            {
                "message": {
                    "messageId": "pubsub-msg-0003",
                    "publishTime": "2026-08-20T02:00:00.000Z",
                    "data": "bm90IGpzb24gYXQgYWxs",
                    "attributes": {},
                },
                "subscription": (
                    "projects/ap-autopilot/subscriptions/gmail-notifications-push"
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"{len(manifest)} source documents and cached responses")
    print(f"{len(message_ids)} Gmail messages")
    total = sum(int(item["bytes"]) for item in manifest)
    print(f"{total} bytes of synthetic source material")


if __name__ == "__main__":
    main()
