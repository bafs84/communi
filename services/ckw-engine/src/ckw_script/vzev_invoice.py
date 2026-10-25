from __future__ import annotations

"""Calculate internal invoices for a CKW monthly vZEV export.

The script treats the vZEV as one external CKW customer and derives internal
consumer invoices from the sub-meter data:
- grid import is allocated from the community import meter
- locally shared PV is priced from each participant's full standard-product
  comparison price (energy, grid, demand, metering, levies and VAT)
- the external peak charge can be allocated with different internal methods
"""

import argparse
import csv
import json
import sys
import types
import unicodedata
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ckw_script.billing_tariff import INVOICE_TARIFF_TIERS

try:
    from ckw_script.pricing_import_2025 import (
        calculate_standard_product_reference,
        rp_to_chf,
        select_import_tariff,
    )
except ModuleNotFoundError as exc:
    if exc.name != "duckdb":
        raise
    sys.modules.setdefault("duckdb", types.ModuleType("duckdb"))
    from ckw_script.pricing_import_2025 import (
        calculate_standard_product_reference,
        rp_to_chf,
        select_import_tariff,
    )


DEFAULT_ENCODINGS = ("cp1252", "iso-8859-1", "utf-8-sig")
TARIFF_THRESHOLD_KWH = Decimal("50000")
MIN_LONG_PERIOD_HOURS = Decimal("7200")
MONEY_QUANT = Decimal("0.01")
ENERGY_QUANT = Decimal("0.001")
PERCENT_QUANT = Decimal("0.0001")
PEAK_QUANT = Decimal("0.001")
BILLING_KWH_QUANT = Decimal("1")
BILLING_KW_QUANT = Decimal("0.1")
BILLING_KVARH_QUANT = Decimal("1")
DEFAULT_VAT_RATE = Decimal("0.081")
FLOAT_TOLERANCE = 1e-9
PEAK_ALLOCATION_MODES = (
    "community_peak_share",
    "individual_peak_pro_rata",
    "peak_in_grid_kwh",
)


@dataclass(frozen=True)
class MeterColumn:
    value_index: int
    display_name: str
    metering_code: str
    line_name: str
    obis_code: str
    unit: str
    initial_reading: str
    role: str
    dataframe_column: str | None = None

    @property
    def column_name(self) -> str:
        return self.dataframe_column or self.display_name


@dataclass(frozen=True)
class ParsedVzevCsv:
    path: Path
    dataframe: pd.DataFrame
    meters: list[MeterColumn]
    community_import: MeterColumn
    community_excess: MeterColumn | None
    producers: list[MeterColumn]
    consumers: list[MeterColumn]
    community_reactive: MeterColumn | None
    consumer_reactive: list[MeterColumn]
    source_encoding: str


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.casefold()


def _parse_decimal(value: str) -> Decimal:
    text = value.strip()
    if not text:
        return Decimal("0")
    text = text.replace("'", "").replace(",", ".")
    return Decimal(text)


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _decimal_to_float(value: Decimal, quantum: Decimal | None = None) -> float:
    if quantum is not None:
        value = _quantize(value, quantum)
    return float(value)


def _read_csv_rows(path: Path, encoding: str | None) -> tuple[list[list[str]], str]:
    encodings = (encoding,) if encoding else DEFAULT_ENCODINGS
    errors: list[str] = []

    for candidate in encodings:
        try:
            with path.open("r", encoding=candidate, newline="") as handle:
                return list(csv.reader(handle, delimiter=";")), candidate
        except UnicodeDecodeError as exc:
            errors.append(f"{candidate}: {exc}")

    raise ValueError(f"Could not decode {path} with any supported encoding: {errors}")


def _is_reactive_meter(*, line_name: str, obis_code: str, unit: str) -> bool:
    return _fold(unit) == "kvarh" or "blind" in _fold(line_name) or ":132." in obis_code


def _is_active_energy_meter(*, line_name: str, obis_code: str, unit: str) -> bool:
    return _fold(unit) == "kwh" and not _is_reactive_meter(
        line_name=line_name,
        obis_code=obis_code,
        unit=unit,
    )


def _classify_meter(*, display_name: str, line_name: str, obis_code: str, unit: str) -> str:
    folded = _fold(display_name)
    if _is_reactive_meter(line_name=line_name, obis_code=obis_code, unit=unit):
        if "vzev summe bezug" in folded:
            return "community_reactive"
        return "consumer_reactive"
    if not _is_active_energy_meter(line_name=line_name, obis_code=obis_code, unit=unit):
        return "other"
    if "vzev summe bezug" in folded:
        return "community_import"
    if "vzev summe uberschuss" in folded:
        return "community_excess"
    if "uberschuss pva" in folded:
        return "producer"
    return "consumer"


def _dataframe_column_name(
    *,
    display_name: str,
    line_name: str,
    unit: str,
    duplicate_display_names: set[str],
    assigned_names: set[str],
) -> str:
    base_name = display_name
    if display_name in duplicate_display_names:
        base_name = " | ".join(part for part in (display_name, line_name, unit) if part)

    column_name = base_name
    suffix = 2
    while column_name in assigned_names:
        column_name = f"{base_name} [{suffix}]"
        suffix += 1
    assigned_names.add(column_name)
    return column_name


def parse_vzev_csv(path: str | Path, *, encoding: str | None = None) -> ParsedVzevCsv:
    csv_path = Path(path)
    rows, used_encoding = _read_csv_rows(csv_path, encoding)
    if len(rows) < 7:
        raise ValueError(f"{csv_path} does not look like a vZEV export")

    header_rows = rows[:6]
    data_rows = rows[6:]
    header_width = len(header_rows[0])

    raw_meters: list[dict[str, str | int]] = []
    display_names: list[str] = []
    for value_index in range(1, header_width, 2):
        display_name = header_rows[0][value_index].strip()
        if not display_name:
            continue
        display_names.append(display_name)
        raw_meters.append(
            {
                "value_index": value_index,
                "display_name": display_name,
                "metering_code": header_rows[1][value_index].strip(),
                "line_name": header_rows[2][value_index].strip(),
                "obis_code": header_rows[3][value_index].strip(),
                "unit": header_rows[4][value_index].strip(),
                "initial_reading": header_rows[5][value_index].strip(),
            }
        )

    duplicate_display_names = {
        display_name
        for display_name, count in Counter(display_names).items()
        if count > 1
    }
    assigned_names: set[str] = set()
    meters: list[MeterColumn] = []
    for raw_meter in raw_meters:
        display_name = str(raw_meter["display_name"])
        line_name = str(raw_meter["line_name"])
        obis_code = str(raw_meter["obis_code"])
        unit = str(raw_meter["unit"])
        meters.append(
            MeterColumn(
                value_index=int(raw_meter["value_index"]),
                display_name=display_name,
                metering_code=str(raw_meter["metering_code"]),
                line_name=line_name,
                obis_code=obis_code,
                unit=unit,
                initial_reading=str(raw_meter["initial_reading"]),
                role=_classify_meter(
                    display_name=display_name,
                    line_name=line_name,
                    obis_code=obis_code,
                    unit=unit,
                ),
                dataframe_column=_dataframe_column_name(
                    display_name=display_name,
                    line_name=line_name,
                    unit=unit,
                    duplicate_display_names=duplicate_display_names,
                    assigned_names=assigned_names,
                ),
            )
        )

    if not meters:
        raise ValueError(f"{csv_path} does not contain any meter columns")

    community_import = next((meter for meter in meters if meter.role == "community_import"), None)
    if community_import is None:
        raise ValueError("Could not find the community import column ('vZEV Summe Bezug')")

    community_excess = next((meter for meter in meters if meter.role == "community_excess"), None)
    producers = [meter for meter in meters if meter.role == "producer"]
    consumers = [meter for meter in meters if meter.role == "consumer"]
    community_reactive = next((meter for meter in meters if meter.role == "community_reactive"), None)
    consumer_reactive = [meter for meter in meters if meter.role == "consumer_reactive"]

    if not consumers:
        raise ValueError("Could not find any consumer columns in the vZEV export")

    records: list[dict[str, Any]] = []
    for row in data_rows:
        if not row or not row[0].strip():
            continue
        record: dict[str, Any] = {
            "timestamp": pd.to_datetime(row[0].strip(), dayfirst=True, errors="raise"),
        }
        for meter in meters:
            raw_value = row[meter.value_index] if meter.value_index < len(row) else ""
            record[meter.column_name] = float(_parse_decimal(raw_value))
        records.append(record)

    if not records:
        raise ValueError(f"{csv_path} does not contain time-series rows")

    df = pd.DataFrame.from_records(records).sort_values("timestamp").reset_index(drop=True)
    return ParsedVzevCsv(
        path=csv_path,
        dataframe=df,
        meters=meters,
        community_import=community_import,
        community_excess=community_excess,
        producers=producers,
        consumers=consumers,
        community_reactive=community_reactive,
        consumer_reactive=consumer_reactive,
        source_encoding=used_encoding,
    )


def _infer_interval_hours(df: pd.DataFrame) -> Decimal:
    timestamps = pd.to_datetime(df["timestamp"], errors="raise")
    diffs = timestamps.diff().dropna().dt.total_seconds() / 3600.0
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return Decimal("0.25")
    return Decimal(str(float(diffs.median())))


def _select_tariff(
    *,
    year: int,
    community_import_total_kwh: Decimal,
    covered_hours: Decimal,
    tariff_tier: str,
    annual_kwh_for_tariff: Decimal | None,
):
    if tariff_tier == "lt_50000":
        basis_kwh = Decimal("0")
        basis_source = "explicit_lt_50000"
    elif tariff_tier == "gt_50000":
        basis_kwh = TARIFF_THRESHOLD_KWH + Decimal("1")
        basis_source = "explicit_gt_50000"
    elif annual_kwh_for_tariff is not None:
        basis_kwh = annual_kwh_for_tariff
        basis_source = "annual_kwh_for_tariff"
    elif covered_hours < MIN_LONG_PERIOD_HOURS and covered_hours > 0:
        basis_kwh = community_import_total_kwh * (Decimal("8760") / covered_hours)
        basis_source = "annualized_from_file_period"
    else:
        basis_kwh = community_import_total_kwh
        basis_source = "measured_from_file"

    tariff = select_import_tariff(year=year, kwh=basis_kwh, threshold_kwh=TARIFF_THRESHOLD_KWH)
    selected_tier = "gt_50000" if basis_kwh > TARIFF_THRESHOLD_KWH else "lt_50000"
    return tariff, basis_kwh, basis_source, selected_tier


def _sum_meter_decimal(df: pd.DataFrame, meter: MeterColumn) -> Decimal:
    values = pd.to_numeric(df[meter.column_name], errors="coerce").fillna(0.0)
    return Decimal(str(float(values.sum())))


def _matching_consumer_for_reactive_meter(
    reactive_meter: MeterColumn,
    consumers: list[MeterColumn],
) -> MeterColumn | None:
    same_metering_code = [
        consumer
        for consumer in consumers
        if consumer.metering_code and consumer.metering_code == reactive_meter.metering_code
    ]
    if len(same_metering_code) == 1:
        return same_metering_code[0]

    reactive_name = _fold(reactive_meter.display_name)
    same_display_name = [
        consumer
        for consumer in consumers
        if _fold(consumer.display_name) == reactive_name
    ]
    if len(same_display_name) == 1:
        return same_display_name[0]

    return None


def _consumer_reactive_kvarh_by_column(parsed: ParsedVzevCsv, df: pd.DataFrame) -> dict[str, Decimal]:
    reactive_by_column = {consumer.column_name: Decimal("0") for consumer in parsed.consumers}
    for reactive_meter in parsed.consumer_reactive:
        consumer = _matching_consumer_for_reactive_meter(reactive_meter, parsed.consumers)
        if consumer is None:
            continue
        reactive_by_column[consumer.column_name] += _sum_meter_decimal(df, reactive_meter)
    return reactive_by_column


def _infer_indirect_meter_count(parsed: ParsedVzevCsv) -> int:
    """Treat consumers with a matching reactive meter as transformer metering."""

    indirect_columns: set[str] = set()
    for reactive_meter in parsed.consumer_reactive:
        consumer = _matching_consumer_for_reactive_meter(reactive_meter, parsed.consumers)
        if consumer is not None:
            indirect_columns.add(consumer.column_name)
    return len(indirect_columns)


def _indirect_consumer_columns(parsed: ParsedVzevCsv) -> set[str]:
    columns: set[str] = set()
    for reactive_meter in parsed.consumer_reactive:
        consumer = _matching_consumer_for_reactive_meter(reactive_meter, parsed.consumers)
        if consumer is not None:
            columns.add(consumer.column_name)
    return columns


def calculate_vzev_invoices(
    parsed: ParsedVzevCsv,
    *,
    year: int | None = None,
    tariff_tier: str = "auto",
    annual_kwh_for_tariff: Decimal | None = None,
    annual_kwh_by_consumer: dict[str, Decimal] | None = None,
    local_energy_factor: Decimal = Decimal("0.80"),
    local_energy_rp_per_kwh: Decimal | None = None,
    local_energy_pricing_method: str = "80%-pauschale",
    peak_allocation_mode: str = "community_peak_share",
    blindenergie_kvarh: Decimal | None = None,
    direct_meter_count: int | None = None,
    indirect_meter_count: int | None = None,
    virtual_meter_count: int | None = None,
    vat_rate: Decimal | None = DEFAULT_VAT_RATE,
    ckw_invoice_rounding: bool = True,
) -> dict[str, Any]:
    if peak_allocation_mode not in PEAK_ALLOCATION_MODES:
        raise ValueError(
            f"Unsupported peak_allocation_mode {peak_allocation_mode!r}. "
            f"Expected one of {PEAK_ALLOCATION_MODES}."
        )
    if local_energy_factor < 0:
        raise ValueError("local_energy_factor must be non-negative")
    if local_energy_rp_per_kwh is not None and local_energy_rp_per_kwh < 0:
        raise ValueError("local_energy_rp_per_kwh must be non-negative")
    if local_energy_pricing_method not in {"80%-pauschale", "effektive-kosten", "manuell"}:
        raise ValueError("Unsupported local_energy_pricing_method")

    df = parsed.dataframe.copy()
    consumer_cols = [meter.column_name for meter in parsed.consumers]
    import_col = parsed.community_import.column_name
    excess_col = parsed.community_excess.column_name if parsed.community_excess is not None else None

    for col in consumer_cols + [import_col]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    if excess_col is not None:
        df[excess_col] = pd.to_numeric(df[excess_col], errors="coerce").fillna(0.0)

    df["consumer_total_kwh"] = df[consumer_cols].sum(axis=1)
    df["community_import_kwh"] = df[import_col].clip(lower=0.0)

    invalid_import_mask = (df["community_import_kwh"] - df["consumer_total_kwh"]) > FLOAT_TOLERANCE
    if invalid_import_mask.any():
        bad_row = df.loc[invalid_import_mask].iloc[0]
        raise ValueError(
            "Community import exceeds summed consumer load at "
            f"{bad_row['timestamp']}: import={bad_row['community_import_kwh']}, "
            f"consumers={bad_row['consumer_total_kwh']}"
        )

    zero_consumer_mask = (df["community_import_kwh"] > FLOAT_TOLERANCE) & (df["consumer_total_kwh"] <= FLOAT_TOLERANCE)
    if zero_consumer_mask.any():
        bad_row = df.loc[zero_consumer_mask].iloc[0]
        raise ValueError(
            "Community import is positive while all consumer columns are zero at "
            f"{bad_row['timestamp']}"
        )

    denominator = df["consumer_total_kwh"].where(df["consumer_total_kwh"] > FLOAT_TOLERANCE)
    shares = df[consumer_cols].div(denominator, axis=0).fillna(0.0)
    allocated_import = shares.mul(df["community_import_kwh"], axis=0)
    local_pv = df[consumer_cols] - allocated_import

    # Community peak is the externally billed CKW peak for the whole vZEV.
    peak_kw_series = df["community_import_kwh"] * 4.0
    peak_kw = float(peak_kw_series.max()) if not peak_kw_series.empty else 0.0
    if peak_kw > FLOAT_TOLERANCE:
        peak_mask = (peak_kw_series - peak_kw).abs() <= FLOAT_TOLERANCE
        community_peak_shares = shares.loc[peak_mask].mean(axis=0).fillna(0.0)
        community_peak_timestamps = df.loc[peak_mask, "timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
    else:
        community_peak_shares = pd.Series(0.0, index=consumer_cols)
        community_peak_timestamps = []

    individual_peak_kw_series = df[consumer_cols].mul(4.0)
    individual_peak_kw = individual_peak_kw_series.max(axis=0).fillna(0.0)
    sum_individual_peak_kw = float(individual_peak_kw.sum())
    if sum_individual_peak_kw > FLOAT_TOLERANCE:
        individual_peak_shares = individual_peak_kw / sum_individual_peak_kw
    else:
        individual_peak_shares = pd.Series(0.0, index=consumer_cols)

    period_year = year if year is not None else int(pd.to_datetime(df["timestamp"]).dt.year.mode().iloc[0])
    interval_hours = _infer_interval_hours(df)
    covered_hours = interval_hours * Decimal(str(len(df)))
    community_import_total_kwh = Decimal(str(float(df["community_import_kwh"].sum())))
    community_excess_total_kwh = (
        Decimal(str(float(df[excess_col].sum()))) if excess_col is not None else Decimal("0")
    )
    total_local_pv_kwh = Decimal(str(float(local_pv.sum(axis=1).sum())))

    tariff, tariff_basis_kwh, tariff_basis_source, selected_tier = _select_tariff(
        year=period_year,
        community_import_total_kwh=community_import_total_kwh,
        covered_hours=covered_hours,
        tariff_tier=tariff_tier,
        annual_kwh_for_tariff=annual_kwh_for_tariff,
    )

    energy_rp_per_kwh = tariff.energielieferung_rp_per_kwh
    grid_rp_per_kwh = (
        tariff.netznutzung_einheitstarif_rp_per_kwh
        + tariff.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh
        + tariff.netznutzung_stromreserve_rp_per_kwh
        + tariff.netznutzung_solidarisierte_kosten_rp_per_kwh
    )
    public_charges_rp_per_kwh = (
        tariff.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh
        + tariff.oeffentliche_abgaben_gemeinde_rp_per_kwh
    )
    peak_chf_per_kw_month = tariff.netz_leistungstarif_chf_per_kw_month
    peak_kw_decimal = Decimal(str(peak_kw))

    billing_import_kwh = (
        _quantize(community_import_total_kwh, BILLING_KWH_QUANT)
        if ckw_invoice_rounding
        else community_import_total_kwh
    )
    billing_peak_kw = _quantize(peak_kw_decimal, BILLING_KW_QUANT) if ckw_invoice_rounding else peak_kw_decimal
    if blindenergie_kvarh is None:
        if parsed.community_reactive is not None:
            blindenergie_kvarh_d = _sum_meter_decimal(df, parsed.community_reactive)
            blindenergie_source = "csv_community_reactive"
        else:
            blindenergie_kvarh_d = Decimal("0")
            blindenergie_source = "none"
    else:
        blindenergie_kvarh_d = (
            blindenergie_kvarh
            if isinstance(blindenergie_kvarh, Decimal)
            else Decimal(str(blindenergie_kvarh))
        )
        blindenergie_source = "manual"
    billing_blindenergie_kvarh = (
        _quantize(blindenergie_kvarh_d, BILLING_KVARH_QUANT) if ckw_invoice_rounding else blindenergie_kvarh_d
    )
    consumer_reactive_kvarh = _consumer_reactive_kvarh_by_column(parsed, df)
    total_consumer_reactive_kvarh = sum(consumer_reactive_kvarh.values(), Decimal("0"))
    if total_consumer_reactive_kvarh > 0:
        blindenergie_allocation_mode = "reactive_meter"
    elif blindenergie_kvarh_d != 0:
        blindenergie_allocation_mode = "grid_import_pro_rata"
    else:
        blindenergie_allocation_mode = "none"

    meter_counts_inferred = indirect_meter_count is None
    if indirect_meter_count is None:
        indirect_meter_count = _infer_indirect_meter_count(parsed)
    if indirect_meter_count < 0:
        raise ValueError("indirect_meter_count must be non-negative")
    if direct_meter_count is None:
        direct_meter_count = len(parsed.consumers) - indirect_meter_count
    if direct_meter_count < 0:
        raise ValueError("direct_meter_count must be non-negative")
    if virtual_meter_count is None:
        virtual_meter_count = len(parsed.producers) + 1
    if virtual_meter_count < 0:
        raise ValueError("virtual_meter_count must be non-negative")

    direct_meter_count_d = Decimal(str(direct_meter_count))
    indirect_meter_count_d = Decimal(str(indirect_meter_count))
    virtual_meter_count_d = Decimal(str(virtual_meter_count))

    aggregated_energy_chf = _quantize(rp_to_chf(energy_rp_per_kwh * billing_import_kwh), MONEY_QUANT)
    aggregated_grid_unit_chf = _quantize(
        rp_to_chf(tariff.netznutzung_einheitstarif_rp_per_kwh * billing_import_kwh), MONEY_QUANT
    )
    aggregated_grid_swissgrid_chf = _quantize(
        rp_to_chf(tariff.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh * billing_import_kwh), MONEY_QUANT
    )
    aggregated_grid_stromreserve_chf = _quantize(
        rp_to_chf(tariff.netznutzung_stromreserve_rp_per_kwh * billing_import_kwh), MONEY_QUANT
    )
    aggregated_grid_solidarisierte_kosten_chf = _quantize(
        rp_to_chf(tariff.netznutzung_solidarisierte_kosten_rp_per_kwh * billing_import_kwh), MONEY_QUANT
    )
    aggregated_grid_chf = (
        aggregated_grid_unit_chf
        + aggregated_grid_swissgrid_chf
        + aggregated_grid_stromreserve_chf
        + aggregated_grid_solidarisierte_kosten_chf
    )
    aggregated_peak_price_chf = _quantize(peak_chf_per_kw_month * billing_peak_kw, MONEY_QUANT)
    aggregated_blindenergie_chf = _quantize(
        rp_to_chf(tariff.blindenergie_rp_per_kvarh * billing_blindenergie_kvarh), MONEY_QUANT
    )
    aggregated_public_netzzuschlag_chf = _quantize(
        rp_to_chf(tariff.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh * billing_import_kwh),
        MONEY_QUANT,
    )
    aggregated_public_gemeinde_chf = _quantize(
        rp_to_chf(tariff.oeffentliche_abgaben_gemeinde_rp_per_kwh * billing_import_kwh), MONEY_QUANT
    )
    aggregated_public_charges_chf = aggregated_public_netzzuschlag_chf + aggregated_public_gemeinde_chf
    aggregated_metering_direct_chf = _quantize(
        tariff.messentgelt_direkt_chf_per_month * direct_meter_count_d, MONEY_QUANT
    )
    aggregated_metering_indirect_chf = _quantize(
        tariff.messentgelt_indirekt_chf_per_month * indirect_meter_count_d, MONEY_QUANT
    )
    aggregated_metering_virtual_chf = _quantize(
        tariff.messentgelt_virtuell_chf_per_month * virtual_meter_count_d, MONEY_QUANT
    )
    aggregated_metering_chf = (
        aggregated_metering_direct_chf + aggregated_metering_indirect_chf + aggregated_metering_virtual_chf
    )
    aggregated_netznutzung_total_chf = (
        aggregated_grid_chf
        + aggregated_peak_price_chf
        + aggregated_blindenergie_chf
    )
    vat_rate_d = vat_rate if isinstance(vat_rate, Decimal) or vat_rate is None else Decimal(str(vat_rate))
    aggregated_subtotal_excl_vat_chf = (
        aggregated_energy_chf
        + aggregated_netznutzung_total_chf
        + aggregated_metering_chf
        + aggregated_public_charges_chf
    )
    aggregated_vat_chf = (
        _quantize(aggregated_subtotal_excl_vat_chf * vat_rate_d, MONEY_QUANT)
        if vat_rate_d is not None
        else Decimal("0")
    )
    aggregated_total_chf = aggregated_subtotal_excl_vat_chf + aggregated_vat_chf
    # Used only for the internal "peak_in_grid_kwh" option.
    embedded_peak_rp_per_kwh = (
        (aggregated_peak_price_chf / community_import_total_kwh) * Decimal("100")
        if community_import_total_kwh > 0
        else Decimal("0")
    )

    consumers: list[dict[str, Any]] = []
    indirect_consumer_columns = _indirect_consumer_columns(parsed)
    timestamp_series = pd.to_datetime(df["timestamp"], errors="raise")
    # CKW timestamps describe interval ends, so 00:00 on the first day still
    # belongs to the preceding billing month.
    interval_delta = pd.to_timedelta(float(interval_hours), unit="h")
    period_series = (timestamp_series - interval_delta).dt.to_period("M")
    billing_months = Decimal("0")
    for period in period_series.unique():
        rows_in_period = int((period_series == period).sum())
        measured_hours = interval_hours * Decimal(str(rows_in_period))
        calendar_hours = Decimal(str(period.days_in_month * 24))
        coverage_fraction = measured_hours / calendar_hours if calendar_hours > 0 else Decimal("0")
        billing_months += Decimal("1") if coverage_fraction >= Decimal("0.90") else coverage_fraction
    for meter in parsed.consumers:
        name = meter.display_name
        column_name = meter.column_name
        consumption_kwh = Decimal(str(float(df[column_name].sum())))
        allocated_import_kwh = Decimal(str(float(allocated_import[column_name].sum())))
        local_pv_kwh = Decimal(str(float(local_pv[column_name].sum())))
        individual_peak_kw_d = Decimal(str(float(individual_peak_kw[column_name])))
        inferred_annualized_consumption_kwh = (
            consumption_kwh * Decimal("8760") / covered_hours if covered_hours > 0 else consumption_kwh
        )
        annualized_consumption_kwh = (
            annual_kwh_by_consumer.get(name, inferred_annualized_consumption_kwh)
            if annual_kwh_by_consumer is not None
            else inferred_annualized_consumption_kwh
        )
        if annualized_consumption_kwh < 0:
            raise ValueError(f"annual_kwh_by_consumer must be non-negative for {name}")
        standard_reference = calculate_standard_product_reference(
            year=period_year,
            consumption_kwh=consumption_kwh,
            annualized_consumption_kwh=annualized_consumption_kwh,
            peak_kw=individual_peak_kw_d,
            months=billing_months,
            meter_type="indirect" if column_name in indirect_consumer_columns else "direct",
            vat_rate=DEFAULT_VAT_RATE,
        )
        reference_rate = standard_reference.equivalent_rp_per_kwh_incl_vat
        legal_factor = Decimal("0.80") if local_energy_pricing_method == "80%-pauschale" else Decimal("1")
        legal_cap_rate = reference_rate * legal_factor
        if local_energy_rp_per_kwh is None:
            requested_local_rate = reference_rate * local_energy_factor
        else:
            requested_local_rate = (
                local_energy_rp_per_kwh
                if isinstance(local_energy_rp_per_kwh, Decimal)
                else Decimal(str(local_energy_rp_per_kwh))
            )
        participant_local_energy_rp_per_kwh = min(requested_local_rate, legal_cap_rate)
        local_tariff_was_capped = requested_local_rate > legal_cap_rate
        monthly_consumption_share = (
            allocated_import_kwh / community_import_total_kwh if community_import_total_kwh > 0 else Decimal("0")
        )
        if peak_allocation_mode == "community_peak_share":
            peak_share = Decimal(str(float(community_peak_shares[column_name])))
            allocated_peak_kw = peak_kw_decimal * peak_share
            peak_in_grid_chf = Decimal("0")
            peak_price_chf = peak_chf_per_kw_month * allocated_peak_kw
        elif peak_allocation_mode == "individual_peak_pro_rata":
            peak_share = Decimal(str(float(individual_peak_shares[column_name])))
            allocated_peak_kw = peak_kw_decimal * peak_share
            peak_in_grid_chf = Decimal("0")
            peak_price_chf = peak_chf_per_kw_month * allocated_peak_kw
        else:
            peak_share = Decimal("0")
            allocated_peak_kw = Decimal("0")
            peak_in_grid_chf = rp_to_chf(embedded_peak_rp_per_kwh * allocated_import_kwh)
            peak_price_chf = Decimal("0")

        grid_energy_chf = rp_to_chf(energy_rp_per_kwh * allocated_import_kwh)
        local_energy_chf = rp_to_chf(participant_local_energy_rp_per_kwh * local_pv_kwh)
        energy_chf = grid_energy_chf + local_energy_chf
        grid_unit_chf = rp_to_chf(tariff.netznutzung_einheitstarif_rp_per_kwh * allocated_import_kwh)
        grid_swissgrid_chf = rp_to_chf(
            tariff.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh * allocated_import_kwh
        )
        grid_stromreserve_chf = rp_to_chf(
            tariff.netznutzung_stromreserve_rp_per_kwh * allocated_import_kwh
        )
        grid_solidarity_chf = rp_to_chf(
            tariff.netznutzung_solidarisierte_kosten_rp_per_kwh * allocated_import_kwh
        )
        variable_grid_chf = grid_unit_chf + grid_swissgrid_chf + grid_stromreserve_chf + grid_solidarity_chf
        public_netzzuschlag_chf = rp_to_chf(
            tariff.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh * allocated_import_kwh
        )
        public_gemeinde_chf = rp_to_chf(
            tariff.oeffentliche_abgaben_gemeinde_rp_per_kwh * allocated_import_kwh
        )
        public_charges_chf = public_netzzuschlag_chf + public_gemeinde_chf
        if total_consumer_reactive_kvarh > 0:
            blindenergie_kvarh_for_consumer = consumer_reactive_kvarh.get(column_name, Decimal("0"))
            blindenergie_share = blindenergie_kvarh_for_consumer / total_consumer_reactive_kvarh
        else:
            blindenergie_kvarh_for_consumer = blindenergie_kvarh_d * monthly_consumption_share
            blindenergie_share = monthly_consumption_share
        billing_blindenergie_kvarh_for_consumer = billing_blindenergie_kvarh * blindenergie_share
        blindenergie_chf = aggregated_blindenergie_chf * blindenergie_share
        metering_chf = aggregated_metering_chf * monthly_consumption_share
        grid_chf = variable_grid_chf + peak_in_grid_chf + blindenergie_chf
        external_subtotal_chf = (
            grid_energy_chf
            + grid_chf
            + peak_price_chf
            + public_charges_chf
            + metering_chf
        )
        vat_chf = (
            external_subtotal_chf * vat_rate_d
            if vat_rate_d is not None
            else Decimal("0")
        )
        invoice_subtotal_excl_vat_chf = energy_chf + grid_chf + peak_price_chf + public_charges_chf + metering_chf
        invoice_total_chf = invoice_subtotal_excl_vat_chf + vat_chf

        consumers.append(
            {
                "display_name": name,
                "metering_code": meter.metering_code,
                "consumption_kwh": _decimal_to_float(consumption_kwh, ENERGY_QUANT),
                "allocated_grid_import_kwh": _decimal_to_float(allocated_import_kwh, ENERGY_QUANT),
                "allocated_local_pv_kwh": _decimal_to_float(local_pv_kwh, ENERGY_QUANT),
                "standard_product_reference": standard_reference.as_dict(),
                "standard_product_reference_rp_per_kwh": _decimal_to_float(reference_rate, Decimal("0.01")),
                "local_energy_factor": _decimal_to_float(
                    participant_local_energy_rp_per_kwh / reference_rate if reference_rate > 0 else Decimal("0"),
                    Decimal("0.0001"),
                ),
                "local_energy_requested_rp_per_kwh": _decimal_to_float(requested_local_rate, Decimal("0.01")),
                "local_energy_cap_rp_per_kwh": _decimal_to_float(legal_cap_rate, Decimal("0.01")),
                "local_energy_rp_per_kwh": _decimal_to_float(
                    participant_local_energy_rp_per_kwh,
                    Decimal("0.01"),
                ),
                "local_energy_pricing_method": local_energy_pricing_method,
                "local_energy_tariff_was_capped": local_tariff_was_capped,
                "monthly_import_share": _decimal_to_float(monthly_consumption_share, PERCENT_QUANT),
                "individual_peak_kw": _decimal_to_float(
                    individual_peak_kw_d,
                    PEAK_QUANT,
                ),
                "peak_share": _decimal_to_float(peak_share, PERCENT_QUANT),
                "allocated_peak_kw": _decimal_to_float(allocated_peak_kw, PEAK_QUANT),
                "grid_energy_chf": _decimal_to_float(grid_energy_chf, MONEY_QUANT),
                "local_energy_chf": _decimal_to_float(local_energy_chf, MONEY_QUANT),
                "energy_chf": _decimal_to_float(energy_chf, MONEY_QUANT),
                "variable_grid_utilization_chf": _decimal_to_float(variable_grid_chf, MONEY_QUANT),
                "grid_unit_chf": _decimal_to_float(grid_unit_chf, MONEY_QUANT),
                "grid_swissgrid_chf": _decimal_to_float(grid_swissgrid_chf, MONEY_QUANT),
                "grid_stromreserve_chf": _decimal_to_float(grid_stromreserve_chf, MONEY_QUANT),
                "grid_solidarity_chf": _decimal_to_float(grid_solidarity_chf, MONEY_QUANT),
                "embedded_peak_in_grid_chf": _decimal_to_float(peak_in_grid_chf, MONEY_QUANT),
                "blindenergie_kvarh": _decimal_to_float(blindenergie_kvarh_for_consumer, ENERGY_QUANT),
                "billing_blindenergie_kvarh": _decimal_to_float(
                    billing_blindenergie_kvarh_for_consumer,
                    ENERGY_QUANT,
                ),
                "blindenergie_allocation_mode": blindenergie_allocation_mode,
                "blindenergie_chf": _decimal_to_float(blindenergie_chf, MONEY_QUANT),
                "grid_utilization_chf": _decimal_to_float(grid_chf, MONEY_QUANT),
                "peak_price_chf": _decimal_to_float(peak_price_chf, MONEY_QUANT),
                "public_charges_chf": _decimal_to_float(public_charges_chf, MONEY_QUANT),
                "public_netzzuschlag_chf": _decimal_to_float(public_netzzuschlag_chf, MONEY_QUANT),
                "public_gemeinde_chf": _decimal_to_float(public_gemeinde_chf, MONEY_QUANT),
                "metering_chf": _decimal_to_float(metering_chf, MONEY_QUANT),
                "external_subtotal_excl_vat_chf": _decimal_to_float(external_subtotal_chf, MONEY_QUANT),
                "invoice_subtotal_excl_vat_chf": _decimal_to_float(invoice_subtotal_excl_vat_chf, MONEY_QUANT),
                "vat_chf": _decimal_to_float(vat_chf, MONEY_QUANT),
                "invoice_total_chf": _decimal_to_float(invoice_total_chf, MONEY_QUANT),
            }
        )

    consumers.sort(key=lambda item: item["display_name"])
    community_local_energy_chf = sum(
        (Decimal(str(consumer["local_energy_chf"])) for consumer in consumers),
        Decimal("0"),
    )
    weighted_local_energy_rp_per_kwh = (
        sum(
            (
                Decimal(str(consumer["local_energy_rp_per_kwh"]))
                * Decimal(str(consumer["allocated_local_pv_kwh"]))
                for consumer in consumers
            ),
            Decimal("0"),
        )
        / total_local_pv_kwh
        if total_local_pv_kwh > 0
        else Decimal("0")
    )
    weighted_reference_rp_per_kwh = (
        sum(
            (
                Decimal(str(consumer["standard_product_reference_rp_per_kwh"]))
                * Decimal(str(consumer["allocated_local_pv_kwh"]))
                for consumer in consumers
            ),
            Decimal("0"),
        )
        / total_local_pv_kwh
        if total_local_pv_kwh > 0
        else Decimal("0")
    )
    effective_weighted_factor = (
        weighted_local_energy_rp_per_kwh / weighted_reference_rp_per_kwh
        if weighted_reference_rp_per_kwh > 0
        else Decimal("0")
    )

    return {
        "source_file": str(parsed.path),
        "source_encoding": parsed.source_encoding,
        "period_start": pd.Timestamp(df["timestamp"].min()).isoformat(sep=" "),
        "period_end": pd.Timestamp(df["timestamp"].max()).isoformat(sep=" "),
        "interval_hours": _decimal_to_float(interval_hours, Decimal("0.000001")),
        "covered_hours": _decimal_to_float(covered_hours, Decimal("0.001")),
        "tariff_year": period_year,
        "tariff_threshold_kwh": _decimal_to_float(TARIFF_THRESHOLD_KWH, ENERGY_QUANT),
        "tariff_basis_kwh": _decimal_to_float(tariff_basis_kwh, ENERGY_QUANT),
        "tariff_basis_source": tariff_basis_source,
        "selected_tariff_tier": selected_tier,
        "peak_allocation_mode": peak_allocation_mode,
        "blindenergie_source": blindenergie_source,
        "blindenergie_allocation_mode": blindenergie_allocation_mode,
        "rates": {
            "energy_rp_per_kwh": _decimal_to_float(energy_rp_per_kwh, Decimal("0.01")),
            "local_energy_factor": _decimal_to_float(effective_weighted_factor, Decimal("0.0001")),
            "local_energy_requested_factor": _decimal_to_float(local_energy_factor, Decimal("0.0001")),
            "standard_product_reference_rp_per_kwh": _decimal_to_float(
                weighted_reference_rp_per_kwh,
                Decimal("0.01"),
            ),
            "local_energy_rp_per_kwh": _decimal_to_float(
                weighted_local_energy_rp_per_kwh,
                Decimal("0.01"),
            ),
            "local_energy_rp_per_kwh_is_weighted_average": True,
            "local_energy_pricing_method": local_energy_pricing_method,
            "local_energy_reference_basis": "full_standard_product_including_vat",
            "grid_utilization_rp_per_kwh": _decimal_to_float(grid_rp_per_kwh, Decimal("0.01")),
            "grid_unit_rp_per_kwh": _decimal_to_float(
                tariff.netznutzung_einheitstarif_rp_per_kwh,
                Decimal("0.01"),
            ),
            "grid_swissgrid_rp_per_kwh": _decimal_to_float(
                tariff.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh,
                Decimal("0.01"),
            ),
            "grid_stromreserve_rp_per_kwh": _decimal_to_float(
                tariff.netznutzung_stromreserve_rp_per_kwh,
                Decimal("0.01"),
            ),
            "grid_solidarity_rp_per_kwh": _decimal_to_float(
                tariff.netznutzung_solidarisierte_kosten_rp_per_kwh,
                Decimal("0.01"),
            ),
            "public_charges_rp_per_kwh": _decimal_to_float(public_charges_rp_per_kwh, Decimal("0.01")),
            "netzzuschlag_rp_per_kwh": _decimal_to_float(
                tariff.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh, Decimal("0.01")
            ),
            "gemeinde_abgabe_rp_per_kwh": _decimal_to_float(
                tariff.oeffentliche_abgaben_gemeinde_rp_per_kwh, Decimal("0.01")
            ),
            "peak_price_chf_per_kw_month": _decimal_to_float(peak_chf_per_kw_month, MONEY_QUANT),
            "blindenergie_rp_per_kvarh": _decimal_to_float(tariff.blindenergie_rp_per_kvarh, Decimal("0.01")),
            "direct_meter_chf_per_month": _decimal_to_float(tariff.messentgelt_direkt_chf_per_month, MONEY_QUANT),
            "indirect_meter_chf_per_month": _decimal_to_float(
                tariff.messentgelt_indirekt_chf_per_month, MONEY_QUANT
            ),
            "virtual_meter_chf_per_month": _decimal_to_float(tariff.messentgelt_virtuell_chf_per_month, MONEY_QUANT),
            "vat_rate": _decimal_to_float(vat_rate_d, Decimal("0.0001")) if vat_rate_d is not None else None,
            "embedded_peak_rp_per_kwh": _decimal_to_float(embedded_peak_rp_per_kwh, Decimal("0.0001")),
        },
        "aggregated_invoice": {
            "grid_import_kwh": _decimal_to_float(community_import_total_kwh, ENERGY_QUANT),
            "billing_grid_import_kwh": _decimal_to_float(billing_import_kwh, ENERGY_QUANT),
            "peak_kw": _decimal_to_float(peak_kw_decimal, PEAK_QUANT),
            "billing_peak_kw": _decimal_to_float(billing_peak_kw, PEAK_QUANT),
            "blindenergie_kvarh": _decimal_to_float(blindenergie_kvarh_d, ENERGY_QUANT),
            "billing_blindenergie_kvarh": _decimal_to_float(billing_blindenergie_kvarh, ENERGY_QUANT),
            "blindenergie_source": blindenergie_source,
            "blindenergie_metering_code": (
                parsed.community_reactive.metering_code if parsed.community_reactive is not None else None
            ),
            "blindenergie_display_name": (
                parsed.community_reactive.display_name if parsed.community_reactive is not None else None
            ),
            "blindenergie_allocation_mode": blindenergie_allocation_mode,
            "energy_chf": _decimal_to_float(aggregated_energy_chf, MONEY_QUANT),
            "grid_unit_chf": _decimal_to_float(aggregated_grid_unit_chf, MONEY_QUANT),
            "grid_swissgrid_chf": _decimal_to_float(aggregated_grid_swissgrid_chf, MONEY_QUANT),
            "grid_stromreserve_chf": _decimal_to_float(aggregated_grid_stromreserve_chf, MONEY_QUANT),
            "grid_solidarisierte_kosten_chf": _decimal_to_float(
                aggregated_grid_solidarisierte_kosten_chf, MONEY_QUANT
            ),
            "grid_utilization_chf": _decimal_to_float(aggregated_grid_chf, MONEY_QUANT),
            "peak_price_chf": _decimal_to_float(aggregated_peak_price_chf, MONEY_QUANT),
            "blindenergie_chf": _decimal_to_float(aggregated_blindenergie_chf, MONEY_QUANT),
            "netznutzung_total_chf": _decimal_to_float(aggregated_netznutzung_total_chf, MONEY_QUANT),
            "metering_direct_count": direct_meter_count,
            "metering_indirect_count": indirect_meter_count,
            "metering_virtual_count": virtual_meter_count,
            "metering_count_source": "inferred_from_reactive_meters" if meter_counts_inferred else "configured",
            "metering_direct_chf": _decimal_to_float(aggregated_metering_direct_chf, MONEY_QUANT),
            "metering_indirect_chf": _decimal_to_float(aggregated_metering_indirect_chf, MONEY_QUANT),
            "metering_virtual_chf": _decimal_to_float(aggregated_metering_virtual_chf, MONEY_QUANT),
            "metering_chf": _decimal_to_float(aggregated_metering_chf, MONEY_QUANT),
            "public_netzzuschlag_chf": _decimal_to_float(aggregated_public_netzzuschlag_chf, MONEY_QUANT),
            "public_gemeinde_chf": _decimal_to_float(aggregated_public_gemeinde_chf, MONEY_QUANT),
            "public_charges_chf": _decimal_to_float(aggregated_public_charges_chf, MONEY_QUANT),
            "subtotal_excl_vat_chf": _decimal_to_float(aggregated_subtotal_excl_vat_chf, MONEY_QUANT),
            "vat_chf": _decimal_to_float(aggregated_vat_chf, MONEY_QUANT),
            "invoice_total_chf": _decimal_to_float(aggregated_total_chf, MONEY_QUANT),
        },
        "community": {
            "consumer_count": len(parsed.consumers),
            "producer_count": len(parsed.producers),
            "community_import_total_kwh": _decimal_to_float(community_import_total_kwh, ENERGY_QUANT),
            "community_excess_total_kwh": _decimal_to_float(community_excess_total_kwh, ENERGY_QUANT),
            "community_local_pv_kwh": _decimal_to_float(total_local_pv_kwh, ENERGY_QUANT),
            "community_local_energy_chf": _decimal_to_float(community_local_energy_chf, MONEY_QUANT),
            "peak_kw": _decimal_to_float(peak_kw_decimal, PEAK_QUANT),
            "community_peak_timestamps": community_peak_timestamps,
        },
        "consumers": consumers,
    }


def allocate_ckw_invoice_totals(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Allocate the gross CKW invoice exactly across consumers.

    CKW rounds invoice lines before calculating the final total. Consumer-level
    interval arithmetic therefore differs by a few rappen. This helper uses the
    configured allocation keys and assigns the final rounding remainder to the
    largest grid importer so the participant shares reconcile to the community
    invoice exactly.
    """

    aggregate = result["aggregated_invoice"]
    grid_import = Decimal(str(aggregate["grid_import_kwh"]))
    import_shared_subtotal = (
        Decimal(str(aggregate["energy_chf"]))
        + Decimal(str(aggregate["grid_utilization_chf"]))
        + Decimal(str(aggregate["metering_chf"]))
        + Decimal(str(aggregate["public_charges_chf"]))
    )
    peak_total = Decimal(str(aggregate["peak_price_chf"]))
    community_peak_kw = Decimal(str(aggregate["peak_kw"]))
    vat_rate = result["rates"].get("vat_rate")
    vat_rate_d = Decimal(str(vat_rate)) if vat_rate is not None else Decimal("0")

    allocations: dict[str, dict[str, float]] = {}
    rounded_total = Decimal("0")
    largest_key: str | None = None
    largest_import = Decimal("-1")

    for consumer in result["consumers"]:
        key = str(consumer["metering_code"] or consumer["display_name"])
        allocated_import = Decimal(str(consumer["allocated_grid_import_kwh"]))
        import_share = allocated_import / grid_import if grid_import > 0 else Decimal("0")
        if result["peak_allocation_mode"] == "peak_in_grid_kwh":
            peak_share = import_share
        else:
            allocated_peak_kw = Decimal(str(consumer["allocated_peak_kw"]))
            peak_share = allocated_peak_kw / community_peak_kw if community_peak_kw > 0 else Decimal("0")

        subtotal = (
            import_shared_subtotal * import_share
            + Decimal(str(consumer.get("blindenergie_chf", 0)))
            + peak_total * peak_share
        )
        total = (subtotal * (Decimal("1") + vat_rate_d)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        local_energy = Decimal(str(consumer["local_energy_chf"]))
        calculated_external = Decimal(str(consumer["invoice_total_chf"])) - local_energy
        allocations[key] = {
            "ckw_invoice_share_chf": float(total),
            "local_energy_chf": float(local_energy),
            "participant_invoice_total_chf": float(total + local_energy),
            "ckw_rounding_adjustment_chf": float(total - calculated_external),
        }
        rounded_total += total
        if allocated_import > largest_import:
            largest_import = allocated_import
            largest_key = key

    payment_rounding = Decimal(str(result.get("feed_in_compensation", {}).get("payment_rounding_chf", 0)))
    target = Decimal(str(aggregate["invoice_total_chf"])) + payment_rounding
    remainder = target - rounded_total
    if largest_key is not None and remainder:
        row = allocations[largest_key]
        row["ckw_invoice_share_chf"] = float(Decimal(str(row["ckw_invoice_share_chf"])) + remainder)
        row["participant_invoice_total_chf"] = float(
            Decimal(str(row["participant_invoice_total_chf"])) + remainder
        )
        row["ckw_rounding_adjustment_chf"] = float(
            Decimal(str(row["ckw_rounding_adjustment_chf"])) + remainder
        )

    return allocations


def _render_table(result: dict[str, Any]) -> str:
    lines = [
        f"File: {result['source_file']}",
        f"Period: {result['period_start']} -> {result['period_end']}",
        f"Tariff year: {result['tariff_year']} ({result['selected_tariff_tier']}, basis={result['tariff_basis_kwh']:.3f} kWh via {result['tariff_basis_source']})",
        f"Peak allocation: {result['peak_allocation_mode']}",
        (
            "Rates: "
            f"Energy {result['rates']['energy_rp_per_kwh']:.2f} rp/kWh, "
            f"Local energy weighted average {result['rates']['local_energy_rp_per_kwh']:.2f} rp/kWh "
            f"({result['rates']['local_energy_factor']:.0%} of each participant's full standard-product price), "
            f"Grid {result['rates']['grid_utilization_rp_per_kwh']:.2f} rp/kWh, "
            f"Public charges {result['rates']['public_charges_rp_per_kwh']:.2f} rp/kWh, "
            f"Peak {result['rates']['peak_price_chf_per_kw_month']:.2f} CHF/kW-month, "
            f"Blind energy {result['rates']['blindenergie_rp_per_kvarh']:.2f} rp/kvarh, "
            f"Embedded peak {result['rates']['embedded_peak_rp_per_kwh']:.4f} rp/kWh"
        ),
        (
            "Blind energy: "
            f"{result['aggregated_invoice']['blindenergie_kvarh']:.3f} kvarh "
            f"via {result['blindenergie_source']}, "
            f"allocated by {result['blindenergie_allocation_mode']}"
        ),
        (
            "Community: "
            f"import {result['community']['community_import_total_kwh']:.3f} kWh, "
            f"local PV {result['community']['community_local_pv_kwh']:.3f} kWh, "
            f"local energy CHF {result['community']['community_local_energy_chf']:.2f}, "
            f"excess {result['community']['community_excess_total_kwh']:.3f} kWh, "
            f"peak {result['community']['peak_kw']:.3f} kW"
        ),
        (
            "Aggregated vZEV invoice: "
            f"Energy CHF {result['aggregated_invoice']['energy_chf']:.2f}, "
            f"Grid CHF {result['aggregated_invoice']['grid_utilization_chf']:.2f}, "
            f"Peak CHF {result['aggregated_invoice']['peak_price_chf']:.2f}, "
            f"Blind energy CHF {result['aggregated_invoice']['blindenergie_chf']:.2f}, "
            f"Metering CHF {result['aggregated_invoice']['metering_chf']:.2f}, "
            f"Public charges CHF {result['aggregated_invoice']['public_charges_chf']:.2f}, "
            f"VAT CHF {result['aggregated_invoice']['vat_chf']:.2f}, "
            f"Total CHF {result['aggregated_invoice']['invoice_total_chf']:.2f}"
        ),
    ]

    invoice_df = pd.DataFrame(result["consumers"])[
        [
            "display_name",
            "consumption_kwh",
            "allocated_grid_import_kwh",
            "allocated_local_pv_kwh",
            "standard_product_reference_rp_per_kwh",
            "local_energy_rp_per_kwh",
            "individual_peak_kw",
            "allocated_peak_kw",
            "grid_energy_chf",
            "local_energy_chf",
            "energy_chf",
            "embedded_peak_in_grid_chf",
            "billing_blindenergie_kvarh",
            "blindenergie_chf",
            "grid_utilization_chf",
            "peak_price_chf",
            "public_charges_chf",
            "metering_chf",
            "vat_chf",
            "invoice_total_chf",
        ]
    ]
    lines.append("")
    lines.append(invoice_df.to_string(index=False))

    if result["community"]["community_peak_timestamps"]:
        lines.append("")
        lines.append("Community peak timestamps:")
        lines.extend(result["community"]["community_peak_timestamps"])

    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate consumer invoice lines for a monthly CKW vZEV CSV export. "
            "Energy and variable grid usage are allocated from the community import column, "
            "and the monthly peak price is allocated by consumer share in the peak interval."
        ),
        epilog=(
            "Examples:\n"
            "  python3 src/ckw_script/vzev_invoice.py data/example.csv --tariff-tier gt_50000\n"
            "  python3 src/ckw_script/vzev_invoice.py data/example.csv --tariff-tier gt_50000 --format json\n"
            "  python3 src/ckw_script/vzev_invoice.py data/example.csv --tariff-tier gt_50000 --local-energy-factor 0.8\n"
            "  python3 src/ckw_script/vzev_invoice.py data/example.csv --tariff-tier gt_50000 "
            "--peak-allocation-mode individual_peak_pro_rata"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("csv_path", help="Path to the monthly vZEV CSV export")
    parser.add_argument(
        "--encoding",
        help="Force a specific text encoding. Default: try cp1252, iso-8859-1, utf-8-sig.",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Tariff year override. Default: infer from timestamps in the CSV.",
    )
    parser.add_argument(
        "--tariff-tier",
        choices=INVOICE_TARIFF_TIERS,
        required=True,
        help="Confirmed CKW contract tariff for this invoice; monthly consumption does not select the tier.",
    )
    parser.add_argument(
        "--annual-kwh-for-tariff",
        type=Decimal,
        help="Explicit annual community import kWh used only for tariff-tier selection.",
    )
    parser.add_argument(
        "--local-energy-factor",
        type=Decimal,
        default=Decimal("0.80"),
        help="Factor applied to the energy tariff for locally shared PV energy. Default: 0.80.",
    )
    parser.add_argument(
        "--peak-allocation-mode",
        choices=PEAK_ALLOCATION_MODES,
        default="community_peak_share",
        help=(
            "How to allocate the actual vZEV peak charge to consumers. "
            "community_peak_share: default, use shares at the community peak interval. "
            "individual_peak_pro_rata: allocate by each consumer's own monthly peak. "
            "peak_in_grid_kwh: embed the actual peak cost into the grid-utilization kWh price."
        ),
    )
    parser.add_argument(
        "--blindenergie-kvarh",
        type=Decimal,
        default=None,
        help=(
            "Reactive energy in kvarh from the CKW invoice. "
            "Default: use the aggregate CSV reactive-energy column when present, otherwise 0."
        ),
    )
    parser.add_argument(
        "--direct-meter-count",
        type=int,
        help="Number of direct physical consumption meters. Default: consumer count minus indirect-meter-count.",
    )
    parser.add_argument(
        "--indirect-meter-count",
        type=int,
        default=None,
        help="Number of indirect physical consumption meters. Default: infer from reactive-energy meters.",
    )
    parser.add_argument(
        "--virtual-meter-count",
        type=int,
        help="Number of virtual meters. Default: producer count plus one vZEV aggregate meter.",
    )
    parser.add_argument(
        "--vat-rate",
        type=Decimal,
        default=DEFAULT_VAT_RATE,
        help="VAT rate as a decimal fraction. Default: 0.081.",
    )
    parser.add_argument(
        "--no-vat",
        action="store_true",
        help="Do not add VAT to the aggregate or participant invoice totals.",
    )
    parser.add_argument(
        "--exact-billing-basis",
        action="store_true",
        help="Use exact CSV kWh/kW bases instead of CKW-style rounded invoice bases for aggregate charges.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    parsed = parse_vzev_csv(args.csv_path, encoding=args.encoding)
    result = calculate_vzev_invoices(
        parsed,
        year=args.year,
        tariff_tier=args.tariff_tier,
        annual_kwh_for_tariff=args.annual_kwh_for_tariff,
        local_energy_factor=args.local_energy_factor,
        peak_allocation_mode=args.peak_allocation_mode,
        blindenergie_kvarh=args.blindenergie_kvarh,
        direct_meter_count=args.direct_meter_count,
        indirect_meter_count=args.indirect_meter_count,
        virtual_meter_count=args.virtual_meter_count,
        vat_rate=None if args.no_vat else args.vat_rate,
        ckw_invoice_rounding=not args.exact_billing_basis,
    )

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_table(result))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
