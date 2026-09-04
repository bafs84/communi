from __future__ import annotations

"""Render German per-participant invoice documents from vZEV output.

This is intentionally a wrapper around ``vzev_invoice.py``. It does not
reimplement allocation or pricing; it parses the same input CSV, asks the
calculator for the billing values, then writes one Markdown invoice per
consumer plus a small index file for the vZEV operator.
"""

import argparse
import json
import re
import sys
import types
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Cm, Pt

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# The imported pricing module only needs duckdb for database helpers, not for
# tariff arithmetic. Keep this wrapper usable in a plain Python+pandas shell.
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))

from ckw_script.vzev_invoice import (  # noqa: E402
    DEFAULT_VAT_RATE,
    PEAK_ALLOCATION_MODES,
    allocate_ckw_invoice_totals,
    calculate_vzev_invoices,
    parse_vzev_csv,
)
from ckw_script.vzev_community_overview import create_community_overview_docx  # noqa: E402
from ckw_script.vzev_feed_in import calculate_feed_in_compensation, select_feed_in_tariff  # noqa: E402

PV_TARIFF_METHODS = (
    "80%-pauschale",
    "effektive-kosten",
    "manuell",
)


@dataclass(frozen=True)
class WrittenInvoice:
    participant: str
    metering_code: str
    invoice_number: str
    path: Path
    consumption_kwh: float
    pv_payment_chf: float
    ckw_payment_chf: float
    total_chf: float


@dataclass(frozen=True)
class PvTariffConfig:
    local_energy_factor: Decimal
    tariff_rp_per_kwh: Decimal | None
    cap_rp_per_kwh: Decimal | None
    method: str
    note: str | None
    was_capped: bool


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "participant"


def _money(value: float) -> str:
    return f"CHF {value:,.2f}".replace(",", "'")


def _kwh(value: float) -> str:
    return f"{value:,.3f} kWh".replace(",", "'")


def _kvarh(value: float) -> str:
    return f"{value:,.3f} kvarh".replace(",", "'")


def _kw(value: float) -> str:
    return f"{value:,.3f} kW".replace(",", "'")


def _peak_mode_label(mode: str) -> str:
    labels = {
        "community_peak_share": "Gemäss internem Verteilschlüssel",
        "individual_peak_pro_rata": "Pro rata nach individueller Monatsleistung",
        "peak_in_grid_kwh": "Leistungspreis im Arbeitspreis eingerechnet",
    }
    return labels.get(mode, mode)


def _pv_method_label(method: str) -> str:
    labels = {
        "80%-pauschale": "80%-Pauschale",
        "effektive-kosten": "Effektive Kosten",
        "manuell": "Manuell festgelegt",
    }
    return labels.get(method, method)


def _pv_tariff_description(pv_tariff: PvTariffConfig, consumer: dict[str, Any]) -> str:
    """Describe the participant-specific price and its legal reference."""

    tariff = float(consumer["local_energy_rp_per_kwh"])
    reference = float(consumer["standard_product_reference_rp_per_kwh"])
    if pv_tariff.method == "80%-pauschale":
        factor_percent = float(consumer["local_energy_factor"]) * 100
        return (
            f"{factor_percent:.0f}% × vollständiger CKW-Standardproduktpreis {reference:.2f} Rp./kWh "
            f"= {tariff:.2f} Rp./kWh"
        )
    return (
        f"{tariff:.2f} Rp./kWh "
        f"({_pv_method_label(pv_tariff.method)}; Obergrenze Standardprodukt {reference:.2f} Rp./kWh)"
    )


def _build_pv_tariff_config(
    *,
    year: int,
    local_energy_factor: Decimal,
    pv_tariff_rp_per_kwh: Decimal | None,
    pv_tariff_method: str,
    pv_tariff_note: str | None,
) -> PvTariffConfig:
    del year  # The legal cap is participant-specific and is calculated later.
    if pv_tariff_method not in PV_TARIFF_METHODS:
        raise ValueError(f"Unsupported pv_tariff_method: {pv_tariff_method}")
    if local_energy_factor < 0:
        raise ValueError("local_energy_factor must be non-negative")
    if pv_tariff_rp_per_kwh is not None and pv_tariff_rp_per_kwh < 0:
        raise ValueError("pv_tariff_rp_per_kwh must be non-negative")
    return PvTariffConfig(
        local_energy_factor=local_energy_factor,
        tariff_rp_per_kwh=pv_tariff_rp_per_kwh,
        cap_rp_per_kwh=None,
        method=pv_tariff_method,
        note=pv_tariff_note,
        was_capped=False,
    )


def _period_key(result: dict[str, Any]) -> str:
    return str(result["period_start"])[:7].replace("-", "")


def _invoice_number(*, prefix: str, period_key: str, metering_code: str) -> str:
    suffix = metering_code[-6:] if metering_code else "unknown"
    return f"{prefix}-{period_key}-{suffix}"


def _line_items(consumer: dict[str, Any], rates: dict[str, Any]) -> list[dict[str, str]]:
    items = [
        {
            "description": "Energie aus dem CKW-Netz",
            "quantity": _kwh(consumer["allocated_grid_import_kwh"]),
            "rate": f"{rates['energy_rp_per_kwh']:.2f} Rp./kWh",
            "amount": _money(consumer["grid_energy_chf"]),
        },
        {
            "description": "Lokal genutzte PV-Energie",
            "quantity": _kwh(consumer["allocated_local_pv_kwh"]),
            "rate": f"{consumer['local_energy_rp_per_kwh']:.2f} Rp./kWh inkl. MWST",
            "amount": _money(consumer["local_energy_chf"]),
        },
    ]

    grid_components = [
        ("Netznutzung Einheitstarif", "grid_unit_rp_per_kwh", "grid_unit_chf"),
        ("Systemdienstleistungen Swissgrid", "grid_swissgrid_rp_per_kwh", "grid_swissgrid_chf"),
        ("Stromreserve", "grid_stromreserve_rp_per_kwh", "grid_stromreserve_chf"),
        ("Solidarisierte Kosten", "grid_solidarity_rp_per_kwh", "grid_solidarity_chf"),
    ]
    for description, rate_key, amount_key in grid_components:
        if float(rates.get(rate_key, 0)) == 0 and float(consumer.get(amount_key, 0)) == 0:
            continue
        items.append(
            {
                "description": description,
                "quantity": _kwh(consumer["allocated_grid_import_kwh"]),
                "rate": f"{rates[rate_key]:.2f} Rp./kWh",
                "amount": _money(consumer[amount_key]),
            }
        )

    if consumer.get("blindenergie_chf", 0) > 0:
        reactive_meter_allocation = consumer.get("blindenergie_allocation_mode") == "reactive_meter"
        items.append(
            {
                "description": "Blindenergie" if reactive_meter_allocation else "Blindenergieanteil",
                "quantity": (
                    _kvarh(consumer.get("billing_blindenergie_kvarh", consumer.get("blindenergie_kvarh", 0)))
                    if reactive_meter_allocation
                    else "pro rata"
                ),
                "rate": f"{rates['blindenergie_rp_per_kvarh']:.2f} Rp./kvarh",
                "amount": _money(consumer["blindenergie_chf"]),
            }
        )

    if consumer["embedded_peak_in_grid_chf"] > 0:
        items.append(
            {
                "description": "Leistungspreis im Arbeitspreis enthalten",
                "quantity": _kwh(consumer["allocated_grid_import_kwh"]),
                "rate": f"{rates['embedded_peak_rp_per_kwh']:.4f} Rp./kWh",
                "amount": _money(consumer["embedded_peak_in_grid_chf"]),
            }
        )
    else:
        items.append(
            {
                "description": "Leistungspreisanteil",
                "quantity": _kw(consumer["allocated_peak_kw"]),
                "rate": f"{rates['peak_price_chf_per_kw_month']:.2f} CHF/kW-Monat",
                "amount": _money(consumer["peak_price_chf"]),
            }
        )

    if consumer.get("public_netzzuschlag_chf", 0) > 0:
        items.append(
            {
                "description": "Netzzuschlag Förderung erneuerbare Energien",
                "quantity": _kwh(consumer["allocated_grid_import_kwh"]),
                "rate": f"{rates['netzzuschlag_rp_per_kwh']:.2f} Rp./kWh",
                "amount": _money(consumer["public_netzzuschlag_chf"]),
            }
        )
    if consumer.get("public_gemeinde_chf", 0) > 0:
        items.append(
            {
                "description": "Abgabe an die Gemeinde",
                "quantity": _kwh(consumer["allocated_grid_import_kwh"]),
                "rate": f"{rates['gemeinde_abgabe_rp_per_kwh']:.2f} Rp./kWh",
                "amount": _money(consumer["public_gemeinde_chf"]),
            }
        )

    if consumer.get("metering_chf", 0) > 0:
        items.append(
            {
                "description": "Messtarifanteil",
                "quantity": "pro rata",
                "rate": "gemäss CKW-Messtarif",
                "amount": _money(consumer["metering_chf"]),
            }
        )

    if consumer.get("vat_chf", 0) > 0:
        items.append(
            {
                "description": "MWST",
                "quantity": "",
                "rate": f"{rates['vat_rate']:.1%}" if rates.get("vat_rate") is not None else "",
                "amount": _money(consumer["vat_chf"]),
            }
        )

    if abs(float(consumer.get("ckw_rounding_adjustment_chf", 0))) >= 0.005:
        items.append(
            {
                "description": "CKW-Rechnungsabgleich",
                "quantity": "",
                "rate": "Rundung Gesamtbeleg",
                "amount": _money(consumer["ckw_rounding_adjustment_chf"]),
            }
        )

    return items


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_invoice_markdown(
    *,
    result: dict[str, Any],
    consumer: dict[str, Any],
    invoice_number: str,
    invoice_date: str,
    issuer: str,
    payment_terms: str,
    pv_tariff: PvTariffConfig,
) -> str:
    rates = result["rates"]
    line_items = _line_items(consumer, rates)
    line_item_rows = [
        [item["description"], item["quantity"], item["rate"], item["amount"]]
        for item in line_items
    ]

    details_rows = [
        ["Teilnehmer", consumer["display_name"]],
        ["Messpunkt", consumer["metering_code"]],
        ["Abrechnungsperiode", f"{result['period_start']} bis {result['period_end']}"],
        ["Tarifjahr", str(result["tariff_year"])],
        ["Leistungspreis-Modell", _peak_mode_label(str(result["peak_allocation_mode"]))],
        [
            "Preis lokal genutzte PV-Energie",
            _pv_tariff_description(pv_tariff, consumer),
        ],
        [
            "Rechtsgrundlage PV-Faktor",
            "80% des vollständigen Standardproduktpreises inkl. Netznutzung, Leistung, Messung, Abgaben und MWST",
        ],
        ["CKW-Tarifquelle", consumer["standard_product_reference"]["source_url"]],
        ["Rechtsgrundlage", consumer["standard_product_reference"]["legal_source_url"]],
        ["BFE-Leitfaden", consumer["standard_product_reference"]["guidance_source_url"]],
    ]
    if consumer.get("local_energy_tariff_was_capped"):
        details_rows.append(["PV-Tarif Deckelung", "Der eingegebene PV-Tarif wurde auf die Obergrenze gekappt."])
    if pv_tariff.note:
        details_rows.append(["PV-Tarif Bemerkung", pv_tariff.note])

    meter_rows = [
        ["Gemessener Verbrauch", _kwh(consumer["consumption_kwh"])],
        ["Abgerechneter CKW-Netzbezug", _kwh(consumer["allocated_grid_import_kwh"])],
        ["Lokal genutzte PV-Energie", _kwh(consumer["allocated_local_pv_kwh"])],
        ["Individuelle Monatsleistung", _kw(consumer["individual_peak_kw"])],
        ["Abgerechnete Leistung", _kw(consumer["allocated_peak_kw"])],
    ]

    reference = consumer["standard_product_reference"]
    reference_kwh = float(reference["consumption_kwh"])
    demand_equivalent_rp = float(reference["demand_chf"]) * 100 / reference_kwh if reference_kwh else 0.0
    fixed_equivalent_rp = (
        float(reference["fixed_and_metering_chf"]) * 100 / reference_kwh if reference_kwh else 0.0
    )
    vat_equivalent_rp = float(reference["vat_chf"]) * 100 / reference_kwh if reference_kwh else 0.0
    reference_rows = [
        [
            f"Energie {reference['product_name']}",
            "Publizierter CKW-Standardtarif",
            f"{reference['energy_rp_per_kwh']:.2f} Rp./kWh",
        ],
        [
            "Variable Netznutzung inkl. Systemdienste",
            "Publizierte kWh-basierte Netztarife",
            f"{reference['network_variable_rp_per_kwh']:.2f} Rp./kWh",
        ],
        [
            "Leistungspreis",
            f"{_kw(reference['peak_kw'])} × {reference['demand_chf_per_kw_month']:.2f} CHF/kW-Monat ÷ {_kwh(reference_kwh)}",
            f"{demand_equivalent_rp:.2f} Rp./kWh",
        ],
        [
            "Grund- und Messpreis",
            f"{reference['fixed_and_metering_chf_per_month']:.2f} CHF/Monat × {reference['months']:.3f} Monate ÷ {_kwh(reference_kwh)}",
            f"{fixed_equivalent_rp:.2f} Rp./kWh",
        ],
        [
            "Öffentliche Abgaben",
            "Netzzuschlag und Gemeindeabgabe",
            f"{reference['public_charges_rp_per_kwh']:.2f} Rp./kWh",
        ],
        ["MWST", f"{reference['vat_rate']:.1%} auf den Referenzpreis exkl. MWST", f"{vat_equivalent_rp:.2f} Rp./kWh"],
        [
            "Vollständiger Standardproduktpreis",
            "Summe der obigen Preisbestandteile",
            f"{reference['equivalent_rp_per_kwh_incl_vat']:.2f} Rp./kWh inkl. MWST",
        ],
        [
            "Interner PV-Preis",
            f"{consumer['local_energy_factor']:.0%} × {reference['equivalent_rp_per_kwh_incl_vat']:.2f} Rp./kWh",
            f"{consumer['local_energy_rp_per_kwh']:.2f} Rp./kWh inkl. MWST",
        ],
    ]

    invoice_metadata = [
        f"**Rechnungssteller:** {issuer}",
        f"**Rechnungsdatum:** {invoice_date}",
        f"**Teilnehmer:** {consumer['display_name']}",
    ]
    if payment_terms.strip():
        invoice_metadata.append(f"**Zahlungsfrist:** {payment_terms.strip()}")

    return "\n\n".join(
        [
            f"# Rechnung {invoice_number}",
            "  \n".join(invoice_metadata),
            "## Rechnungspositionen\n" + _markdown_table(["Beschreibung", "Menge", "Tarif", "Betrag"], line_item_rows),
            (
                "## Total\n"
                f"**Externe CKW-Kosten exkl. MWST:** {_money(consumer['external_subtotal_excl_vat_chf'])}  \n"
                f"**MWST auf externe CKW-Kosten:** {_money(consumer['vat_chf'])}  \n"
                f"**Lokale PV-Energie inkl. MWST im Referenzpreis:** {_money(consumer['local_energy_chf'])}  \n"
                f"**CKW-Rechnungsabgleich:** {_money(consumer.get('ckw_rounding_adjustment_chf', 0))}  \n"
                f"**Rechnungsbetrag:** {_money(consumer['invoice_total_chf'])}"
            ),
            "## Rechnungsdetails\n" + _markdown_table(["Feld", "Wert"], details_rows),
            "## Mess- und Abrechnungswerte\n" + _markdown_table(["Position", "Wert"], meter_rows),
            "## Herleitung des internen PV-Preises\n" + _markdown_table(
                ["Bestandteil", "Berechnung", "Beitrag zum Referenzpreis"], reference_rows
            ),
        ]
    ) + "\n"


def _add_markdown_runs(paragraph, text: str) -> None:
    """Add the small inline-Markdown subset used by the invoice renderer."""

    for part in re.split(r"(\*\*.*?\*\*)", text.rstrip()):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        else:
            paragraph.add_run(part)


def render_invoice_docx(
    *,
    result: dict[str, Any],
    consumer: dict[str, Any],
    invoice_number: str,
    invoice_date: str,
    issuer: str,
    payment_terms: str,
    pv_tariff: PvTariffConfig,
) -> bytes:
    """Render an editable Word invoice from the canonical Markdown content."""

    markdown = render_invoice_markdown(
        result=result,
        consumer=consumer,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        issuer=issuer,
        payment_terms=payment_terms,
        pv_tariff=pv_tariff,
    )
    document = Document()
    document.core_properties.title = f"Rechnung {invoice_number}"
    document.core_properties.subject = f"vZEV-Abrechnung für {consumer['display_name']}"
    section = document.sections[0]
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(1.8)
    section.right_margin = Cm(1.8)
    document.styles["Normal"].font.name = "Aptos"
    document.styles["Normal"].font.size = Pt(9)

    lines = markdown.splitlines()
    index = 0
    current_section: str | None = None
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("# "):
            document.add_heading(line[2:], level=0)
            index += 1
            continue
        if line.startswith("## "):
            if line == "## Rechnungsdetails":
                document.add_page_break()
            current_section = line[3:]
            document.add_heading(current_section, level=1)
            index += 1
            continue
        if line.startswith("| "):
            table_lines: list[str] = []
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            rows = [
                [cell.strip() for cell in table_line.strip().strip("|").split("|")]
                for table_line in table_lines
            ]
            rows = [
                row
                for row in rows
                if not all(re.fullmatch(r"-+", cell.replace(" ", "")) for cell in row)
            ]
            has_header = True
            if current_section == "Rechnungsdetails" and rows[:1] == [["Feld", "Wert"]]:
                rows = rows[1:]
                has_header = False
            if rows:
                table = document.add_table(rows=len(rows), cols=len(rows[0]))
                table.style = "Table Grid"
                if current_section == "Rechnungsdetails":
                    table.autofit = False
                    column_widths = (Cm(4.2), Cm(13.2))
                    for column, width in zip(table.columns, column_widths, strict=True):
                        column.width = width
                        for cell in column.cells:
                            cell.width = width
                for row_number, row in enumerate(rows):
                    for column_number, value in enumerate(row):
                        cell = table.cell(row_number, column_number)
                        cell.text = ""
                        _add_markdown_runs(cell.paragraphs[0], value)
                        if has_header and row_number == 0:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
            continue
        if line.startswith("- "):
            paragraph = document.add_paragraph(style="List Bullet")
            _add_markdown_runs(paragraph, line[2:])
            index += 1
            continue

        paragraph = document.add_paragraph()
        _add_markdown_runs(paragraph, line)
        index += 1

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def write_invoice_documents(
    *,
    result: dict[str, Any],
    output_dir: Path,
    issuer: str,
    invoice_date: str,
    payment_terms: str,
    invoice_prefix: str,
    pv_tariff: PvTariffConfig,
    document_format: str = "markdown",
) -> list[WrittenInvoice]:
    if document_format not in {"docx", "markdown"}:
        raise ValueError("document_format must be 'docx' or 'markdown'")

    period_key = _period_key(result)
    target_dir = output_dir / f"vzev-{period_key}"
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[WrittenInvoice] = []
    allocations = allocate_ckw_invoice_totals(result)
    for original_consumer in result["consumers"]:
        consumer = dict(original_consumer)
        allocation_key = str(consumer["metering_code"] or consumer["display_name"])
        allocation = allocations[allocation_key]
        consumer.update(allocation)
        consumer["invoice_total_chf"] = allocation["participant_invoice_total_chf"]
        invoice_number = _invoice_number(
            prefix=invoice_prefix,
            period_key=period_key,
            metering_code=consumer["metering_code"],
        )
        extension = "docx" if document_format == "docx" else "md"
        filename = f"{invoice_number}-{_slugify(consumer['display_name'])}.{extension}"
        path = target_dir / filename
        render_args = {
            "result": result,
            "consumer": consumer,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "issuer": issuer,
            "payment_terms": payment_terms,
            "pv_tariff": pv_tariff,
        }
        if document_format == "docx":
            path.write_bytes(render_invoice_docx(**render_args))
        else:
            path.write_text(
                render_invoice_markdown(**render_args),
                encoding="utf-8",
            )
        written.append(
            WrittenInvoice(
                participant=consumer["display_name"],
                metering_code=consumer["metering_code"],
                invoice_number=invoice_number,
                path=path,
                consumption_kwh=consumer["consumption_kwh"],
                pv_payment_chf=allocation["local_energy_chf"],
                ckw_payment_chf=allocation["ckw_invoice_share_chf"],
                total_chf=consumer["invoice_total_chf"],
            )
        )

    index = {
        "source_file": result["source_file"],
        "period_start": result["period_start"],
        "period_end": result["period_end"],
        "invoice_count": len(written),
        "total_internal_invoices_chf": round(sum(item.total_chf for item in written), 2),
        "aggregated_ckw_invoice_chf": result["aggregated_invoice"]["invoice_total_chf"],
        "local_pv_energy_chf": result["community"]["community_local_energy_chf"],
        "pv_tariff_method": pv_tariff.method,
        "pv_tariff_rp_per_kwh": (
            float(pv_tariff.tariff_rp_per_kwh) if pv_tariff.tariff_rp_per_kwh is not None else None
        ),
        "pv_tariff_cap_rp_per_kwh": None,
        "pv_tariff_was_capped": any(
            bool(consumer.get("local_energy_tariff_was_capped")) for consumer in result["consumers"]
        ),
        "pv_tariff_note": pv_tariff.note,
        "peak_allocation_mode": result["peak_allocation_mode"],
        "document_format": document_format,
        "invoices": [
            {
                "participant": item.participant,
                "metering_code": item.metering_code,
                "invoice_number": item.invoice_number,
                "path": str(Path(target_dir.name) / item.path.name),
                "consumption_kwh": item.consumption_kwh,
                "pv_payment_chf": item.pv_payment_chf,
                "ckw_payment_chf": item.ckw_payment_chf,
                "total_chf": item.total_chf,
            }
            for item in written
        ],
    }
    (target_dir / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Erstellt je vZEV-Teilnehmer eine editierbare deutschsprachige Rechnung "
            "aus einem monatlichen CKW-vZEV-CSV-Export. Die Berechnung erfolgt "
            "durch ckw_script.vzev_invoice; dieses Skript rendert nur die Dokumente."
        ),
        epilog=(
            "Beispiele:\n"
            "  ckw invoices data/vzev/example.csv\n"
            "  ckw invoices data/vzev/example.csv --issuer 'vZEV Allmendstrasse 4 Sursee'\n"
            "  ckw invoices data/vzev/example.csv --pv-tariff-rp-per-kwh 9.6 --pv-tariff-method 80%-pauschale\n"
            "  ckw invoices data/vzev/example.csv --peak-allocation-mode individual_peak_pro_rata"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("csv_path", help="Pfad zum monatlichen vZEV-CSV-Export")
    parser.add_argument("--output-dir", default="outputs/invoices", help="Zielordner für die Rechnungsdokumente")
    parser.add_argument("--issuer", default="vZEV", help="Rechnungssteller auf jeder Rechnung")
    parser.add_argument("--invoice-date", default=date.today().isoformat(), help="Rechnungsdatum, Standard: heute")
    parser.add_argument("--payment-terms", default="Zahlbar gemäss vZEV-Vereinbarung", help="Text für die Zahlungsfrist")
    parser.add_argument("--invoice-prefix", default="VZEV", help="Präfix für generierte Rechnungsnummern")
    parser.add_argument(
        "--format",
        choices=("docx", "markdown"),
        default="docx",
        help="Ausgabeformat. Standard: docx (editierbare Word-Dateien)",
    )
    parser.add_argument("--encoding", help="CSV-Encoding explizit setzen")
    parser.add_argument("--year", type=int, help="Tarifjahr überschreiben. Standard: aus Zeitstempeln ableiten")
    parser.add_argument(
        "--tariff-tier",
        choices=("auto", "lt_50000", "gt_50000"),
        default="auto",
        help="Tarifklasse für die zugrundeliegende Berechnung überschreiben",
    )
    parser.add_argument(
        "--annual-kwh-for-tariff",
        type=Decimal,
        help="Jährlicher Netzbezug in kWh nur für die Auswahl der Tarifklasse",
    )
    parser.add_argument(
        "--local-energy-factor",
        type=Decimal,
        default=Decimal("0.80"),
        help="Faktor auf dem vollständigen Standardproduktpreis für lokal genutzte PV-Energie. Standard: 0.80",
    )
    parser.add_argument(
        "--pv-tariff-rp-per-kwh",
        type=Decimal,
        help=(
            "PV-Tarif direkt in Rp./kWh. Praktisch für einen im Webportal festgelegten Wert. "
            "Wird je Teilnehmer automatisch auf die rechtliche Obergrenze des vollständigen Standardproduktpreises gekappt."
        ),
    )
    parser.add_argument(
        "--pv-tariff-method",
        choices=PV_TARIFF_METHODS,
        default="80%-pauschale",
        help="Methode zur Herleitung des PV-Tarifs, wird auf der Rechnung ausgewiesen.",
    )
    parser.add_argument(
        "--pv-tariff-note",
        help="Optionale Bemerkung zur Herleitung des PV-Tarifs, wird auf der Rechnung ausgewiesen.",
    )
    parser.add_argument(
        "--peak-allocation-mode",
        choices=PEAK_ALLOCATION_MODES,
        default="community_peak_share",
        help="Internes Modell für die Verteilung des Leistungspreises",
    )
    parser.add_argument(
        "--blindenergie-kvarh",
        type=Decimal,
        default=None,
        help=(
            "Blindenergie in kvarh gemäss CKW-Rechnung. "
            "Standard: aus der aggregierten Blindenergie-Spalte im CSV, wenn vorhanden."
        ),
    )
    parser.add_argument(
        "--direct-meter-count",
        type=int,
        help="Anzahl direkter physischer Verbrauchszähler. Standard: Teilnehmerzahl minus indirekte Zähler.",
    )
    parser.add_argument(
        "--indirect-meter-count",
        type=int,
        default=None,
        help="Anzahl indirekter physischer Verbrauchszähler. Standard: aus Blindenergiezählern ableiten.",
    )
    parser.add_argument(
        "--virtual-meter-count",
        type=int,
        help="Anzahl virtueller Messungen. Standard: PV-Teilnehmer plus ein vZEV-Aggregat.",
    )
    parser.add_argument(
        "--feed-in-energy-rp-per-kwh",
        type=Decimal,
        help="Rückliefervergütung Energie in Rp./kWh. Standard: publizierter Quartalssatz, falls bekannt.",
    )
    parser.add_argument(
        "--feed-in-hkn-rp-per-kwh",
        type=Decimal,
        help="HKN-Vergütung in Rp./kWh. Standard: 2.00 Rp./kWh.",
    )
    parser.add_argument(
        "--vat-rate",
        type=Decimal,
        default=DEFAULT_VAT_RATE,
        help="MWST-Satz als Dezimalzahl. Standard: 0.081.",
    )
    parser.add_argument("--no-vat", action="store_true", help="Keine MWST ausweisen.")
    parser.add_argument(
        "--exact-billing-basis",
        action="store_true",
        help="Exakte CSV-kWh/kW statt CKW-gerundeter Rechnungsbasis verwenden.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    parsed = parse_vzev_csv(args.csv_path, encoding=args.encoding)
    tariff_year = args.year or int(parsed.dataframe["timestamp"].dt.year.mode().iloc[0])
    pv_tariff = _build_pv_tariff_config(
        year=tariff_year,
        local_energy_factor=args.local_energy_factor,
        pv_tariff_rp_per_kwh=args.pv_tariff_rp_per_kwh,
        pv_tariff_method=args.pv_tariff_method,
        pv_tariff_note=args.pv_tariff_note,
    )

    result = calculate_vzev_invoices(
        parsed,
        year=args.year,
        tariff_tier=args.tariff_tier,
        annual_kwh_for_tariff=args.annual_kwh_for_tariff,
        local_energy_factor=pv_tariff.local_energy_factor,
        local_energy_rp_per_kwh=pv_tariff.tariff_rp_per_kwh,
        local_energy_pricing_method=pv_tariff.method,
        peak_allocation_mode=args.peak_allocation_mode,
        blindenergie_kvarh=args.blindenergie_kvarh,
        direct_meter_count=args.direct_meter_count,
        indirect_meter_count=args.indirect_meter_count,
        virtual_meter_count=args.virtual_meter_count,
        vat_rate=None if args.no_vat else args.vat_rate,
        ckw_invoice_rounding=not args.exact_billing_basis,
    )
    feed_in_tariff = select_feed_in_tariff(
        period_start=result["period_start"],
        energy_rp_per_kwh=args.feed_in_energy_rp_per_kwh,
        hkn_rp_per_kwh=args.feed_in_hkn_rp_per_kwh,
    )
    result["feed_in_compensation"] = calculate_feed_in_compensation(
        excess_kwh=Decimal(str(result["community"]["community_excess_total_kwh"])),
        gross_ckw_total_chf=Decimal(str(result["aggregated_invoice"]["invoice_total_chf"])),
        vat_rate=None if args.no_vat else args.vat_rate,
        tariff=feed_in_tariff,
        ckw_invoice_rounding=not args.exact_billing_basis,
    )

    written = write_invoice_documents(
        result=result,
        output_dir=Path(args.output_dir),
        issuer=args.issuer,
        invoice_date=args.invoice_date,
        payment_terms=args.payment_terms,
        invoice_prefix=args.invoice_prefix,
        pv_tariff=pv_tariff,
        document_format=args.format,
    )

    target_dir = Path(args.output_dir) / ("vzev-" + _period_key(result))
    overview_path = target_dir / f"VZEV-{_period_key(result)}-gemeinschaftsuebersicht.docx"
    overview_path.write_bytes(create_community_overview_docx(result=result, parsed=parsed, issuer=args.issuer))
    index_path = target_dir / "_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index.update(
        {
            "community_overview": str(Path(target_dir.name) / overview_path.name),
            "gross_ckw_invoice_chf": result["aggregated_invoice"]["invoice_total_chf"],
            "feed_in_compensation": result["feed_in_compensation"],
            "net_ckw_payable_chf": result["feed_in_compensation"]["net_ckw_payable_chf"],
        }
    )
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{len(written)} Rechnungen geschrieben nach {target_dir}")
    for item in written:
        print(f"- {item.invoice_number}: {item.participant} -> {_money(item.total_chf)} ({item.path})")
    print(f"- Gemeinschaftsübersicht: {overview_path}")
    print(f"- Berechneter CKW-Zahlbetrag: {_money(result['feed_in_compensation']['net_ckw_payable_chf'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
