from __future__ import annotations

"""Application services for turning monthly CKW vZEV exports into invoices."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .vzev_invoice import DEFAULT_VAT_RATE, calculate_vzev_invoices, parse_vzev_csv
from .vzev_invoice_documents import (
    _build_pv_tariff_config,
    _period_key,
    write_invoice_documents,
)
from .vzev_community_overview import create_community_overview_docx
from .vzev_feed_in import calculate_feed_in_compensation, select_feed_in_tariff
from .vzev_multi_month_overview import create_multi_month_overview_docx


@dataclass(frozen=True)
class InvoiceBundle:
    content: bytes
    filename: str
    invoice_count: int
    period_key: str


@dataclass(frozen=True)
class InvoiceSource:
    content: bytes
    original_filename: str


@dataclass(frozen=True)
class InvoiceBatchBundle:
    content: bytes
    filename: str
    invoice_count: int
    period_keys: tuple[str, ...]

    @property
    def period_count(self) -> int:
        return len(self.period_keys)


def create_invoice_bundle(
    *,
    csv_content: bytes,
    original_filename: str,
    issuer: str = "vZEV",
    invoice_date: str | None = None,
    payment_terms: str = "Zahlbar gemäss vZEV-Vereinbarung",
    invoice_prefix: str = "VZEV",
    year: int | None = None,
    tariff_tier: str = "auto",
    local_energy_factor: Decimal = Decimal("0.80"),
    pv_tariff_rp_per_kwh: Decimal | None = None,
    pv_tariff_method: str = "80%-pauschale",
    pv_tariff_note: str | None = None,
    peak_allocation_mode: str = "community_peak_share",
    blindenergie_kvarh: Decimal | None = None,
    direct_meter_count: int | None = None,
    indirect_meter_count: int | None = None,
    virtual_meter_count: int | None = None,
    feed_in_energy_rp_per_kwh: Decimal | None = None,
    feed_in_hkn_rp_per_kwh: Decimal | None = None,
    vat_rate: Decimal | None = DEFAULT_VAT_RATE,
    ckw_invoice_rounding: bool = True,
) -> InvoiceBundle:
    """Create a ZIP containing one editable DOCX invoice per participant."""

    if not csv_content:
        raise ValueError("Die hochgeladene CSV-Datei ist leer.")

    safe_source_name = Path(original_filename).name or "vzev.csv"
    with TemporaryDirectory(prefix="ckw-vzev-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        source_path = temporary_path / safe_source_name
        source_path.write_bytes(csv_content)

        parsed = parse_vzev_csv(source_path)
        tariff_year = year or int(parsed.dataframe["timestamp"].dt.year.mode().iloc[0])
        pv_tariff = _build_pv_tariff_config(
            year=tariff_year,
            local_energy_factor=local_energy_factor,
            pv_tariff_rp_per_kwh=pv_tariff_rp_per_kwh,
            pv_tariff_method=pv_tariff_method,
            pv_tariff_note=pv_tariff_note,
        )
        result = calculate_vzev_invoices(
            parsed,
            year=year,
            tariff_tier=tariff_tier,
            local_energy_factor=pv_tariff.local_energy_factor,
            local_energy_rp_per_kwh=pv_tariff.tariff_rp_per_kwh,
            local_energy_pricing_method=pv_tariff.method,
            peak_allocation_mode=peak_allocation_mode,
            blindenergie_kvarh=blindenergie_kvarh,
            direct_meter_count=direct_meter_count,
            indirect_meter_count=indirect_meter_count,
            virtual_meter_count=virtual_meter_count,
            vat_rate=vat_rate,
            ckw_invoice_rounding=ckw_invoice_rounding,
        )
        result["source_file"] = safe_source_name

        feed_in_tariff = select_feed_in_tariff(
            period_start=result["period_start"],
            energy_rp_per_kwh=feed_in_energy_rp_per_kwh,
            hkn_rp_per_kwh=feed_in_hkn_rp_per_kwh,
        )
        result["feed_in_compensation"] = calculate_feed_in_compensation(
            excess_kwh=Decimal(str(result["community"]["community_excess_total_kwh"])),
            gross_ckw_total_chf=Decimal(str(result["aggregated_invoice"]["invoice_total_chf"])),
            vat_rate=vat_rate,
            tariff=feed_in_tariff,
            ckw_invoice_rounding=ckw_invoice_rounding,
        )

        output_directory = temporary_path / "invoices"
        written = write_invoice_documents(
            result=result,
            output_dir=output_directory,
            issuer=issuer.strip() or "vZEV",
            invoice_date=invoice_date or date.today().isoformat(),
            payment_terms=payment_terms.strip(),
            invoice_prefix=invoice_prefix.strip() or "VZEV",
            pv_tariff=pv_tariff,
            document_format="docx",
        )

        period_key = _period_key(result)
        target_directory = output_directory / f"vzev-{period_key}"
        overview_filename = f"VZEV-{period_key}-gemeinschaftsuebersicht.docx"
        (target_directory / overview_filename).write_bytes(
            create_community_overview_docx(result=result, parsed=parsed, issuer=issuer.strip() or "vZEV")
        )
        index_path = target_directory / "_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index.update(
            {
                "community_overview": str(Path(target_directory.name) / overview_filename),
                "gross_ckw_invoice_chf": result["aggregated_invoice"]["invoice_total_chf"],
                "feed_in_compensation": result["feed_in_compensation"],
                "net_ckw_payable_chf": result["feed_in_compensation"]["net_ckw_payable_chf"],
            }
        )
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        buffer = BytesIO()
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(target_directory.iterdir()):
                archive.write(path, arcname=f"vzev-{period_key}/{path.name}")

        return InvoiceBundle(
            content=buffer.getvalue(),
            filename=f"vzev-rechnungen-{period_key}.zip",
            invoice_count=len(written),
            period_key=period_key,
        )


def create_invoice_batch(
    *,
    csv_files: Sequence[InvoiceSource],
    issuer: str = "vZEV",
    invoice_date: str | None = None,
    payment_terms: str = "Zahlbar gemäss vZEV-Vereinbarung",
    invoice_prefix: str = "VZEV",
    year: int | None = None,
    tariff_tier: str = "auto",
    local_energy_factor: Decimal = Decimal("0.80"),
    pv_tariff_rp_per_kwh: Decimal | None = None,
    pv_tariff_method: str = "80%-pauschale",
    pv_tariff_note: str | None = None,
    peak_allocation_mode: str = "community_peak_share",
    blindenergie_kvarh: Decimal | None = None,
    direct_meter_count: int | None = None,
    indirect_meter_count: int | None = None,
    virtual_meter_count: int | None = None,
    feed_in_energy_rp_per_kwh: Decimal | None = None,
    feed_in_hkn_rp_per_kwh: Decimal | None = None,
    vat_rate: Decimal | None = DEFAULT_VAT_RATE,
    ckw_invoice_rounding: bool = True,
) -> InvoiceBatchBundle:
    """Validate monthly inputs and package all successful periods into one ZIP."""

    if not csv_files:
        raise ValueError("Es wurde keine CKW-Monatsdatei hochgeladen.")

    processed: list[tuple[InvoiceBundle, dict[str, Any]]] = []
    errors: list[str] = []
    period_sources: dict[str, str] = {}
    shared_arguments: dict[str, Any] = {
        "issuer": issuer,
        "invoice_date": invoice_date,
        "payment_terms": payment_terms,
        "invoice_prefix": invoice_prefix,
        "year": year,
        "tariff_tier": tariff_tier,
        "local_energy_factor": local_energy_factor,
        "pv_tariff_rp_per_kwh": pv_tariff_rp_per_kwh,
        "pv_tariff_method": pv_tariff_method,
        "pv_tariff_note": pv_tariff_note,
        "peak_allocation_mode": peak_allocation_mode,
        "blindenergie_kvarh": blindenergie_kvarh,
        "direct_meter_count": direct_meter_count,
        "indirect_meter_count": indirect_meter_count,
        "virtual_meter_count": virtual_meter_count,
        "feed_in_energy_rp_per_kwh": feed_in_energy_rp_per_kwh,
        "feed_in_hkn_rp_per_kwh": feed_in_hkn_rp_per_kwh,
        "vat_rate": vat_rate,
        "ckw_invoice_rounding": ckw_invoice_rounding,
    }

    for source in csv_files:
        source_name = Path(source.original_filename).name or "vzev.csv"
        try:
            bundle = create_invoice_bundle(
                csv_content=source.content,
                original_filename=source_name,
                **shared_arguments,
            )
            previous_source = period_sources.get(bundle.period_key)
            if previous_source is not None:
                errors.append(
                    f"{source_name}: Abrechnungsperiode {bundle.period_key} wurde bereits durch "
                    f"{previous_source} geliefert."
                )
                continue

            with ZipFile(BytesIO(bundle.content)) as monthly_archive:
                index_name = f"vzev-{bundle.period_key}/_index.json"
                month_index = json.loads(monthly_archive.read(index_name))
            period_sources[bundle.period_key] = source_name
            processed.append((bundle, month_index))
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")

    if errors:
        formatted_errors = "\n".join(f"- {error}" for error in errors)
        raise ValueError(f"Der Rechnungs-Batch konnte nicht verarbeitet werden:\n{formatted_errors}")

    processed.sort(key=lambda item: item[0].period_key)
    period_keys = tuple(bundle.period_key for bundle, _ in processed)
    invoice_count = sum(bundle.invoice_count for bundle, _ in processed)

    if len(processed) == 1:
        single_bundle = processed[0][0]
        return InvoiceBatchBundle(
            content=single_bundle.content,
            filename=single_bundle.filename,
            invoice_count=single_bundle.invoice_count,
            period_keys=period_keys,
        )

    period_summaries = [
        {
            "period_key": bundle.period_key,
            "source_file": month_index["source_file"],
            "directory": f"vzev-{bundle.period_key}",
            "invoice_count": month_index["invoice_count"],
            "total_internal_invoices_chf": month_index["total_internal_invoices_chf"],
            "gross_ckw_invoice_chf": month_index["gross_ckw_invoice_chf"],
            "feed_in_compensation_chf": month_index["feed_in_compensation"]["total_chf"],
            "net_ckw_payable_chf": month_index["net_ckw_payable_chf"],
            "local_pv_energy_chf": month_index["local_pv_energy_chf"],
        }
        for bundle, month_index in processed
    ]
    participants_by_key: dict[str, dict[str, Any]] = {}
    for bundle, month_index in processed:
        for invoice in month_index["invoices"]:
            participant_key = str(invoice["metering_code"] or invoice["participant"])
            participant = participants_by_key.setdefault(
                participant_key,
                {
                    "participant": invoice["participant"],
                    "metering_code": invoice["metering_code"],
                    "months": [],
                },
            )
            participant["months"].append(
                {
                    "period_key": bundle.period_key,
                    "consumption_kwh": invoice["consumption_kwh"],
                    "pv_payment_chf": invoice["pv_payment_chf"],
                    "ckw_payment_chf": invoice["ckw_payment_chf"],
                    "total_chf": invoice["total_chf"],
                }
            )

    participants = sorted(
        participants_by_key.values(),
        key=lambda participant: str(participant["participant"]).casefold(),
    )
    participant_total_fields = (
        "consumption_kwh",
        "pv_payment_chf",
        "ckw_payment_chf",
        "total_chf",
    )
    for participant in participants:
        participant["totals"] = {
            field: round(
                sum(float(month[field]) for month in participant["months"]),
                3 if field == "consumption_kwh" else 2,
            )
            for field in participant_total_fields
        }

    total_fields = (
        "total_internal_invoices_chf",
        "gross_ckw_invoice_chf",
        "feed_in_compensation_chf",
        "net_ckw_payable_chf",
        "local_pv_energy_chf",
    )
    overview_filename = f"VZEV-{period_keys[0]}-bis-{period_keys[-1]}-mehrmonatsuebersicht.docx"
    batch_index = {
        "period_count": len(period_keys),
        "invoice_count": invoice_count,
        "multi_month_overview": overview_filename,
        "periods": period_summaries,
        "participants": participants,
        "totals": {
            field: round(sum(float(period[field]) for period in period_summaries), 2)
            for field in total_fields
        },
    }

    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as batch_archive:
        for bundle, _ in processed:
            with ZipFile(BytesIO(bundle.content)) as monthly_archive:
                for member_name in sorted(monthly_archive.namelist()):
                    batch_archive.writestr(member_name, monthly_archive.read(member_name))
        batch_archive.writestr(
            overview_filename,
            create_multi_month_overview_docx(
                period_keys=period_keys,
                participants=participants,
                issuer=issuer.strip() or "vZEV",
            ),
        )
        batch_archive.writestr(
            "_batch_index.json",
            json.dumps(batch_index, ensure_ascii=False, indent=2),
        )

    return InvoiceBatchBundle(
        content=buffer.getvalue(),
        filename=f"vzev-rechnungen-{period_keys[0]}-bis-{period_keys[-1]}.zip",
        invoice_count=invoice_count,
        period_keys=period_keys,
    )
