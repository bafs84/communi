from __future__ import annotations

"""Create an operator overview comparing all vZEV invoice calculation modes.

This script is a read-only wrapper around the existing vZEV calculation code.
It runs the current invoice calculator for every peak allocation mode and
writes operator-facing overview files without changing any invoice logic.
"""

import argparse
import csv
import json
import sys
import types
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Keep the overview usable in environments where duckdb is not installed. The
# imported tariff code only needs duckdb for unrelated database helper methods.
sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))

from ckw_script.vzev_invoice import (  # noqa: E402
    DEFAULT_VAT_RATE,
    PEAK_ALLOCATION_MODES,
    calculate_vzev_invoices,
    parse_vzev_csv,
)
from ckw_script.vzev_invoice_documents import _peak_mode_label  # noqa: E402


@dataclass(frozen=True)
class OverviewFiles:
    output_dir: Path
    markdown: Path
    participant_csv: Path
    mode_csv: Path
    pv_csv: Path
    json_path: Path


def _money(value: float) -> str:
    return f"CHF {value:,.2f}".replace(",", "'")


def _kwh(value: float) -> str:
    return f"{value:,.3f} kWh".replace(",", "'")


def _pct(value: float) -> str:
    return f"{value:.2%}"


def _period_key(result: dict[str, Any]) -> str:
    return str(result["period_start"])[:7].replace("-", "")


def _round_money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantize_kwh(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def calculate_allocated_ckw_bill_total(result: dict[str, Any], consumer: dict[str, Any]) -> float:
    aggregate = result["aggregated_invoice"]
    grid_import = Decimal(str(aggregate["grid_import_kwh"]))
    allocated_import = Decimal(str(consumer["allocated_grid_import_kwh"]))
    import_share = allocated_import / grid_import if grid_import > 0 else Decimal("0")

    import_shared_subtotal = (
        Decimal(str(aggregate["energy_chf"]))
        + Decimal(str(aggregate["grid_utilization_chf"]))
        + Decimal(str(aggregate["metering_chf"]))
        + Decimal(str(aggregate["public_charges_chf"]))
    )
    blindenergie_subtotal = Decimal(str(consumer.get("blindenergie_chf", 0)))

    peak_total = Decimal(str(aggregate["peak_price_chf"]))
    if result["peak_allocation_mode"] == "peak_in_grid_kwh":
        peak_share = import_share
    else:
        peak_kw = Decimal(str(aggregate["peak_kw"]))
        allocated_peak_kw = Decimal(str(consumer["allocated_peak_kw"]))
        peak_share = allocated_peak_kw / peak_kw if peak_kw > 0 else Decimal("0")

    subtotal = import_shared_subtotal * import_share + blindenergie_subtotal + peak_total * peak_share
    vat_rate = result["rates"].get("vat_rate")
    vat = subtotal * Decimal(str(vat_rate)) if vat_rate is not None else Decimal("0")
    return _round_money(subtotal + vat)


def calculate_without_vzev_cost(result: dict[str, Any], consumer: dict[str, Any]) -> dict[str, float | str]:
    del result
    reference = consumer["standard_product_reference"]
    energy_chf = float(reference["energy_chf"])
    grid_chf = float(reference["network_variable_chf"])
    public_chf = float(reference["public_charges_chf"])
    peak_chf = float(reference["demand_chf"])
    metering_chf = float(reference["fixed_and_metering_chf"])
    subtotal_chf = float(reference["subtotal_excl_vat_chf"])
    vat_chf = float(reference["vat_chf"])
    return {
        "standalone_product_name": str(reference["product_name"]),
        "standalone_tariff_tier": str(reference["tariff_tier"]),
        "standalone_annualized_kwh": float(reference["annualized_consumption_kwh"]),
        "standalone_equivalent_rp_per_kwh": float(reference["equivalent_rp_per_kwh_incl_vat"]),
        "without_vzev_energy_chf": energy_chf,
        "without_vzev_grid_chf": grid_chf,
        "without_vzev_peak_chf": peak_chf,
        "without_vzev_public_charges_chf": public_chf,
        "without_vzev_metering_chf": metering_chf,
        "without_vzev_subtotal_excl_vat_chf": subtotal_chf,
        "without_vzev_vat_chf": vat_chf,
        "without_vzev_total_chf": float(reference["total_incl_vat_chf"]),
    }


def _producer_totals(parsed) -> list[dict[str, Any]]:
    df = parsed.dataframe
    rows: list[dict[str, Any]] = []
    for producer in parsed.producers:
        production_kwh = float(df[producer.column_name].sum())
        rows.append(
            {
                "producer": producer.display_name,
                "metering_code": producer.metering_code,
                "production_kwh": production_kwh,
            }
        )
    total = sum(row["production_kwh"] for row in rows)
    for row in rows:
        row["production_share"] = (row["production_kwh"] / total) if total else 0.0
    return rows


def build_overview(
    *,
    csv_path: str | Path,
    encoding: str | None,
    year: int | None,
    tariff_tier: str,
    annual_kwh_for_tariff: Decimal | None,
    local_energy_factor: Decimal,
    blindenergie_kvarh: Decimal | None,
    direct_meter_count: int | None,
    indirect_meter_count: int | None,
    virtual_meter_count: int | None,
    vat_rate: Decimal | None,
    ckw_invoice_rounding: bool,
    excess_energy_chf_per_kwh: Decimal,
    excess_hkn_chf_per_kwh: Decimal,
) -> dict[str, Any]:
    parsed = parse_vzev_csv(csv_path, encoding=encoding)

    results_by_mode: dict[str, dict[str, Any]] = {}
    for mode in PEAK_ALLOCATION_MODES:
        results_by_mode[mode] = calculate_vzev_invoices(
            parsed,
            year=year,
            tariff_tier=tariff_tier,
            annual_kwh_for_tariff=annual_kwh_for_tariff,
            local_energy_factor=local_energy_factor,
            peak_allocation_mode=mode,
            blindenergie_kvarh=blindenergie_kvarh,
            direct_meter_count=direct_meter_count,
            indirect_meter_count=indirect_meter_count,
            virtual_meter_count=virtual_meter_count,
            vat_rate=vat_rate,
            ckw_invoice_rounding=ckw_invoice_rounding,
        )

    first_result = results_by_mode[PEAK_ALLOCATION_MODES[0]]
    participant_rows: list[dict[str, Any]] = []
    mode_rows: list[dict[str, Any]] = []

    for mode, result in results_by_mode.items():
        total_internal = 0.0
        total_ckw_split = 0.0
        total_without = 0.0
        savings_values: list[float] = []
        mode_participant_rows: list[dict[str, Any]] = []

        for consumer in result["consumers"]:
            without = calculate_without_vzev_cost(result, consumer)
            without_total = float(without["without_vzev_total_chf"])
            ckw_bill_share = calculate_allocated_ckw_bill_total(result, consumer)
            internal_total = round(ckw_bill_share + float(consumer["local_energy_chf"]), 2)
            savings_chf = without_total - internal_total
            total_ckw_split += ckw_bill_share
            total_internal += internal_total
            total_without += without_total
            savings_values.append(savings_chf)

            mode_participant_rows.append(
                {
                    "mode": mode,
                    "mode_label": _peak_mode_label(mode),
                    "participant": consumer["display_name"],
                    "metering_code": consumer["metering_code"],
                    "consumption_kwh": consumer["consumption_kwh"],
                    "saved_grid_import_kwh": consumer["allocated_local_pv_kwh"],
                    "grid_import_with_vzev_kwh": consumer["allocated_grid_import_kwh"],
                    "individual_peak_kw": consumer["individual_peak_kw"],
                    "allocated_peak_kw": consumer["allocated_peak_kw"],
                    "without_vzev_total_chf": round(without_total, 2),
                    "ckw_bill_share_chf": ckw_bill_share,
                    "local_pv_energy_chf": consumer["local_energy_chf"],
                    "with_vzev_total_chf": internal_total,
                    "savings_chf": round(savings_chf, 2),
                    "standalone_tariff_tier": without["standalone_tariff_tier"],
                    "standalone_annualized_kwh": round(float(without["standalone_annualized_kwh"]), 3),
                    "energy_chf": consumer["energy_chf"],
                    "grid_utilization_chf": consumer["grid_utilization_chf"],
                    "peak_price_chf": consumer["peak_price_chf"],
                }
            )

        aggregate = result["aggregated_invoice"]
        if mode_participant_rows:
            target_ckw_total = float(aggregate["invoice_total_chf"])
            rounded_delta = round(target_ckw_total - total_ckw_split, 2)
            if rounded_delta:
                last = mode_participant_rows[-1]
                last["ckw_bill_share_chf"] = round(float(last["ckw_bill_share_chf"]) + rounded_delta, 2)
                last["with_vzev_total_chf"] = round(float(last["with_vzev_total_chf"]) + rounded_delta, 2)
                last["savings_chf"] = round(float(last["without_vzev_total_chf"]) - float(last["with_vzev_total_chf"]), 2)
                total_ckw_split = round(total_ckw_split + rounded_delta, 2)
                total_internal = round(total_internal + rounded_delta, 2)
                savings_values[-1] = float(last["savings_chf"])
        participant_rows.extend(mode_participant_rows)

        local_pv_energy_chf = float(result["community"]["community_local_energy_chf"])
        mode_rows.append(
            {
                "mode": mode,
                "mode_label": _peak_mode_label(mode),
                "allocated_ckw_invoice_chf": round(total_ckw_split, 2),
                "total_internal_invoices_chf": round(total_internal, 2),
                "total_without_vzev_chf": round(total_without, 2),
                "total_participant_savings_chf": round(total_without - total_internal, 2),
                "participants_with_savings": sum(1 for value in savings_values if value >= 0),
                "participants_with_extra_cost": sum(1 for value in savings_values if value < 0),
                "lowest_individual_savings_chf": round(min(savings_values), 2) if savings_values else 0.0,
                "aggregated_ckw_invoice_chf": aggregate["invoice_total_chf"],
                "local_pv_energy_revenue_chf": round(local_pv_energy_chf, 2),
                "operator_balance_before_pv_payout_chf": round(total_internal - total_ckw_split, 2),
            }
        )

    producer_rows = _producer_totals(parsed)
    community = first_result["community"]
    local_pv_revenue = float(community["community_local_energy_chf"])
    local_pv_kwh = float(community["community_local_pv_kwh"])
    excess_kwh = float(community["community_excess_total_kwh"])
    excess_kwh_d = Decimal(str(excess_kwh))
    billing_excess_kwh = _quantize_kwh(excess_kwh_d) if ckw_invoice_rounding else excess_kwh_d
    excess_energy_chf = _quantize_money(billing_excess_kwh * excess_energy_chf_per_kwh)
    excess_hkn_chf = _quantize_money(billing_excess_kwh * excess_hkn_chf_per_kwh)
    excess_subtotal_chf = excess_energy_chf + excess_hkn_chf
    excess_vat_chf = _quantize_money(excess_subtotal_chf * vat_rate) if vat_rate is not None else Decimal("0")
    excess_total_chf = excess_subtotal_chf + excess_vat_chf

    allocated_excess_energy = Decimal("0")
    allocated_excess_hkn = Decimal("0")
    allocated_excess_vat = Decimal("0")
    for row in producer_rows:
        share = float(row["production_share"])
        row["allocated_local_pv_kwh"] = round(local_pv_kwh * share, 3)
        row["allocated_excess_pv_kwh"] = round(excess_kwh * share, 3)
        row["local_pv_revenue_chf"] = round(local_pv_revenue * share, 2)
        share_d = Decimal(str(share))
        row_energy = _quantize_money(excess_energy_chf * share_d)
        row_hkn = _quantize_money(excess_hkn_chf * share_d)
        row_vat = _quantize_money(excess_vat_chf * share_d)
        allocated_excess_energy += row_energy
        allocated_excess_hkn += row_hkn
        allocated_excess_vat += row_vat
        row["excess_energy_revenue_chf"] = float(row_energy)
        row["excess_hkn_revenue_chf"] = float(row_hkn)
        row["excess_subtotal_chf"] = float(row_energy + row_hkn)
        row["excess_vat_chf"] = float(row_vat)
        row["excess_total_chf"] = float(row_energy + row_hkn + row_vat)

    if producer_rows:
        last = producer_rows[-1]
        energy_delta = excess_energy_chf - allocated_excess_energy
        hkn_delta = excess_hkn_chf - allocated_excess_hkn
        vat_delta = excess_vat_chf - allocated_excess_vat
        if energy_delta or hkn_delta or vat_delta:
            row_energy = Decimal(str(last["excess_energy_revenue_chf"])) + energy_delta
            row_hkn = Decimal(str(last["excess_hkn_revenue_chf"])) + hkn_delta
            row_vat = Decimal(str(last["excess_vat_chf"])) + vat_delta
            last["excess_energy_revenue_chf"] = float(row_energy)
            last["excess_hkn_revenue_chf"] = float(row_hkn)
            last["excess_subtotal_chf"] = float(row_energy + row_hkn)
            last["excess_vat_chf"] = float(row_vat)
            last["excess_total_chf"] = float(row_energy + row_hkn + row_vat)

    return {
        "source_file": str(csv_path),
        "period_start": first_result["period_start"],
        "period_end": first_result["period_end"],
        "tariff_year": first_result["tariff_year"],
        "local_energy_factor": first_result["rates"]["local_energy_factor"],
        "local_energy_rp_per_kwh": first_result["rates"]["local_energy_rp_per_kwh"],
        "community": first_result["community"],
        "aggregated_invoice": first_result["aggregated_invoice"],
        "excess_pv_compensation": {
            "excess_pv_kwh": excess_kwh,
            "billing_excess_pv_kwh": float(billing_excess_kwh),
            "energy_chf_per_kwh": float(excess_energy_chf_per_kwh),
            "hkn_chf_per_kwh": float(excess_hkn_chf_per_kwh),
            "energy_revenue_chf": float(excess_energy_chf),
            "hkn_revenue_chf": float(excess_hkn_chf),
            "subtotal_chf": float(excess_subtotal_chf),
            "vat_chf": float(excess_vat_chf),
            "total_chf": float(excess_total_chf),
        },
        "mode_comparison": mode_rows,
        "participant_comparison": participant_rows,
        "pv_operator_earnings": producer_rows,
        "assumptions": [
            "Teilnehmervergleich ohne vZEV: Der gesamte gemessene Verbrauch wird als CKW-Netzbezug gerechnet.",
            "Teilnehmervergleich ohne vZEV: Die individuelle Monatsleistung wird mit dem standalone Tarif des Teilnehmers bewertet.",
            "PV-Ertrag: Einnahmen aus lokal genutzter PV-Energie werden mangels zeitgleicher Erzeugerzuordnung pro rata nach gemessener PV-Produktion verteilt.",
            "CKW-Vergütung für Überschussenergie wird pro rata nach gemessener PV-Produktion verteilt.",
            "Beträge enthalten MWST, wenn ein MWST-Satz konfiguriert ist.",
        ],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_markdown(overview: dict[str, Any]) -> str:
    mode_rows = [
        [
            row["mode_label"],
            _money(row["allocated_ckw_invoice_chf"]),
            _money(row["total_internal_invoices_chf"]),
            _money(row["total_without_vzev_chf"]),
            _money(row["total_participant_savings_chf"]),
            str(row["participants_with_savings"]),
            str(row["participants_with_extra_cost"]),
            _money(row["lowest_individual_savings_chf"]),
            _money(row["operator_balance_before_pv_payout_chf"]),
        ]
        for row in overview["mode_comparison"]
    ]

    participant_rows = [
        [
            row["mode_label"],
            row["participant"],
            _kwh(row["saved_grid_import_kwh"]),
            _money(row["without_vzev_total_chf"]),
            _money(row["ckw_bill_share_chf"]),
            _money(row["local_pv_energy_chf"]),
            _money(row["with_vzev_total_chf"]),
            _money(row["savings_chf"]),
        ]
        for row in overview["participant_comparison"]
    ]

    pv_rows = [
        [
            row["producer"],
            row["metering_code"],
            _kwh(row["production_kwh"]),
            _pct(row["production_share"]),
            _kwh(row["allocated_local_pv_kwh"]),
            _kwh(row["allocated_excess_pv_kwh"]),
            _money(row["local_pv_revenue_chf"]),
            _money(row["excess_energy_revenue_chf"]),
            _money(row["excess_hkn_revenue_chf"]),
            _money(row["excess_subtotal_chf"]),
            _money(row["excess_vat_chf"]),
            _money(row["excess_total_chf"]),
        ]
        for row in overview["pv_operator_earnings"]
    ]

    assumptions = "\n".join(f"- {item}" for item in overview["assumptions"])
    community = overview["community"]
    aggregate = overview["aggregated_invoice"]
    excess_comp = overview["excess_pv_compensation"]

    return "\n\n".join(
        [
            "# vZEV Betreiberübersicht",
            (
                f"**Quelle:** {overview['source_file']}  \n"
                f"**Periode:** {overview['period_start']} bis {overview['period_end']}  \n"
                f"**Tarifjahr:** {overview['tariff_year']}  \n"
                f"**Preis lokal genutzte PV-Energie:** {overview['local_energy_rp_per_kwh']:.2f} rp/kWh "
                f"(mengengewichteter Mittelwert; {overview['local_energy_factor']:.0%} des vollständigen "
                "Standardproduktpreises je Teilnehmer)"
            ),
            (
                "## vZEV Gesamtwerte\n"
                + _markdown_table(
                    ["Position", "Wert"],
                    [
                        ["CKW-Netzbezug", _kwh(aggregate["grid_import_kwh"])],
                        ["CKW-Rechnungsbasis Netzbezug", _kwh(aggregate["billing_grid_import_kwh"])],
                        ["Lokal genutzte PV-Energie", _kwh(community["community_local_pv_kwh"])],
                        ["Überschussenergie", _kwh(community["community_excess_total_kwh"])],
                        ["CKW-Rechnung vZEV exkl. MWST", _money(aggregate["subtotal_excl_vat_chf"])],
                        ["MWST", _money(aggregate["vat_chf"])],
                        ["CKW-Rechnung vZEV inkl. MWST", _money(aggregate["invoice_total_chf"])],
                        ["Interner PV-Energieertrag", _money(community["community_local_energy_chf"])],
                    ],
                )
            ),
            (
                "## CKW-Vergütung Überschussenergie\n"
                + _markdown_table(
                    ["Position", "Wert"],
                    [
                        ["Überschussenergie gemessen", _kwh(excess_comp["excess_pv_kwh"])],
                        ["CKW-Rechnungsbasis Überschuss", _kwh(excess_comp["billing_excess_pv_kwh"])],
                        ["Energievergütung", _money(excess_comp["energy_revenue_chf"])],
                        ["HKN", _money(excess_comp["hkn_revenue_chf"])],
                        ["Subtotal exkl. MWST", _money(excess_comp["subtotal_chf"])],
                        ["MWST", _money(excess_comp["vat_chf"])],
                        ["Total inkl. MWST", _money(excess_comp["total_chf"])],
                    ],
                )
            ),
            (
                "## Vergleich der Leistungspreis-Modelle\n"
                + _markdown_table(
                    [
                        "Modell",
                        "CKW-Rechnung verteilt",
                        "Interne Rechnungen inkl. PV",
                        "Ohne vZEV",
                        "Teilnehmer-Ersparnis",
                        "Teilnehmer mit Vorteil",
                        "Teilnehmer mit Mehrkosten",
                        "Tiefste Ersparnis",
                        "Saldo vor PV-Auszahlung",
                    ],
                    mode_rows,
                )
            ),
            (
                "## Teilnehmervergleich\n"
                + _markdown_table(
                    [
                        "Modell",
                        "Teilnehmer",
                        "Eingesparte kWh",
                        "Ohne vZEV",
                        "CKW-Anteil",
                        "Lokale PV",
                        "Mit vZEV",
                        "Ersparnis",
                    ],
                    participant_rows,
                )
            ),
            (
                "## Potenzielle PV-Erträge\n"
                + _markdown_table(
                    [
                        "PV-Anlage",
                        "Messpunkt",
                        "Produktion",
                        "Produktionsanteil",
                        "Zugeordnete lokale PV",
                        "Zugeordneter Überschuss",
                        "PV-Ertrag lokal",
                        "Überschuss Energie",
                        "Überschuss HKN",
                        "Überschuss exkl. MWST",
                        "MWST Überschuss",
                        "Überschuss inkl. MWST",
                    ],
                    pv_rows,
                )
            ),
            "## Annahmen\n" + assumptions,
        ]
    ) + "\n"


def write_overview_files(*, overview: dict[str, Any], output_dir: Path) -> OverviewFiles:
    period_key = str(overview["period_start"])[:7].replace("-", "")
    target_dir = output_dir / f"vzev-overview-{period_key}"
    target_dir.mkdir(parents=True, exist_ok=True)

    markdown = target_dir / "overview.md"
    participant_csv = target_dir / "participant_comparison.csv"
    mode_csv = target_dir / "mode_comparison.csv"
    pv_csv = target_dir / "pv_operator_earnings.csv"
    json_path = target_dir / "overview.json"

    markdown.write_text(render_markdown(overview), encoding="utf-8")
    _write_csv(participant_csv, overview["participant_comparison"])
    _write_csv(mode_csv, overview["mode_comparison"])
    _write_csv(pv_csv, overview["pv_operator_earnings"])
    json_path.write_text(json.dumps(overview, ensure_ascii=False, indent=2), encoding="utf-8")

    return OverviewFiles(
        output_dir=target_dir,
        markdown=markdown,
        participant_csv=participant_csv,
        mode_csv=mode_csv,
        pv_csv=pv_csv,
        json_path=json_path,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Erstellt eine Betreiberübersicht für alle drei vZEV-Leistungspreis-Modelle. "
            "Die Berechnung nutzt die bestehenden vZEV-Skripte; dieses Skript schreibt nur Übersichtsdokumente."
        ),
        epilog=(
            "Beispiele:\n"
            "  ckw overview data/vzev/example.csv\n"
            "  ckw overview data/vzev/example.csv --output-dir outputs/overviews\n"
            "  ckw overview data/vzev/example.csv --local-energy-factor 0.80"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("csv_path", help="Pfad zum monatlichen vZEV-CSV-Export")
    parser.add_argument("--output-dir", default="outputs/overviews", help="Zielordner für die Übersicht")
    parser.add_argument("--encoding", help="CSV-Encoding explizit setzen")
    parser.add_argument("--year", type=int, help="Tarifjahr überschreiben. Standard: aus Zeitstempeln ableiten")
    parser.add_argument(
        "--tariff-tier",
        choices=("auto", "lt_50000", "gt_50000"),
        default="auto",
        help="Tarifklasse für die vZEV-Rechnung überschreiben",
    )
    parser.add_argument(
        "--annual-kwh-for-tariff",
        type=Decimal,
        help="Jährlicher vZEV-Netzbezug in kWh nur für die Auswahl der Tarifklasse",
    )
    parser.add_argument(
        "--local-energy-factor",
        type=Decimal,
        default=Decimal("0.80"),
        help="Faktor auf dem vollständigen Standardproduktpreis je Teilnehmer. Standard: 0.80",
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
    parser.add_argument(
        "--excess-energy-chf-per-kwh",
        type=Decimal,
        default=Decimal("0"),
        help="CKW-Vergütung für Überschussenergie in CHF/kWh.",
    )
    parser.add_argument(
        "--excess-hkn-chf-per-kwh",
        type=Decimal,
        default=Decimal("0"),
        help="CKW-HKN-Vergütung für Überschussenergie in CHF/kWh.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    overview = build_overview(
        csv_path=args.csv_path,
        encoding=args.encoding,
        year=args.year,
        tariff_tier=args.tariff_tier,
        annual_kwh_for_tariff=args.annual_kwh_for_tariff,
        local_energy_factor=args.local_energy_factor,
        blindenergie_kvarh=args.blindenergie_kvarh,
        direct_meter_count=args.direct_meter_count,
        indirect_meter_count=args.indirect_meter_count,
        virtual_meter_count=args.virtual_meter_count,
        vat_rate=None if args.no_vat else args.vat_rate,
        ckw_invoice_rounding=not args.exact_billing_basis,
        excess_energy_chf_per_kwh=args.excess_energy_chf_per_kwh,
        excess_hkn_chf_per_kwh=args.excess_hkn_chf_per_kwh,
    )
    files = write_overview_files(overview=overview, output_dir=Path(args.output_dir))

    print(f"Betreiberübersicht geschrieben nach {files.output_dir}")
    print(f"- Markdown: {files.markdown}")
    print(f"- Modellvergleich CSV: {files.mode_csv}")
    print(f"- Teilnehmervergleich CSV: {files.participant_csv}")
    print(f"- PV-Erträge CSV: {files.pv_csv}")
    print(f"- JSON: {files.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
