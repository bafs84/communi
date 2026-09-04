from __future__ import annotations

"""Build and value a synthetic CKW vZEV aggregate from uploaded time series."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .vzev_invoice import (
    DEFAULT_VAT_RATE,
    MeterColumn,
    ParsedVzevCsv,
    allocate_ckw_invoice_totals,
    calculate_vzev_invoices,
)
from .vzev_operator_overview import calculate_allocated_ckw_bill_total, calculate_without_vzev_cost


SYNTHETIC_CSV_COLUMNS = (
    "Zeitstempel",
    "Energiebedarf_total_kWh",
    "Rueckspeissung_total_kWh",
    "Bezug_total_kWh",
    "Produktion_kWh",
    "Eigenverbrauch_%",
    "Produktion_total_kWh",
    "Energiebedarf_Hochtarif_kWh",
    "Energiebedarf_Niedertarif_kWh",
    "Produktion_Hochtarif_kWh",
    "Produktion_Niedertarif_kWh",
)
DEFAULT_VZEV_SIMULATION_TARIFF_YEAR = 2026
SIMULATION_PEAK_ALLOCATION_MODE = "peak_in_grid_kwh"


@dataclass(frozen=True)
class VzevSimulation:
    dataframe: pd.DataFrame
    summary: dict[str, Any]

    def csv_bytes(self) -> bytes:
        return self.dataframe.to_csv(
            sep=";",
            index=False,
            columns=list(SYNTHETIC_CSV_COLUMNS),
            float_format="%.3f",
            lineterminator="\n",
        ).encode("utf-8")

    def tariff_comparison_csv_bytes(self) -> bytes:
        columns = {
            "participant": "Teilnehmer",
            "metering_code": "Messpunkt",
            "consumption_kwh": "Verbrauch_kWh",
            "standalone_annualized_kwh": "Jahresverbrauch_einzeln_kWh",
            "standalone_tariff_tier": "Tarifklasse_einzeln",
            "community_annualized_grid_import_kwh": "Jahresnetzbezug_vZEV_kWh",
            "community_tariff_tier": "Tarifklasse_vZEV",
            "standalone_demand_rate_chf_per_kw_month": "Leistungspreis_einzeln_CHF_pro_kW_Monat",
            "community_demand_rate_chf_per_kw_month": "Leistungspreis_vZEV_CHF_pro_kW_Monat",
            "pv_tariff_mode": "PV_Tarifmodus",
            "fixed_pv_tariff_rp_per_kwh": "Fixer_PV_Tarif_Rp_pro_kWh",
            "local_energy_cap_rp_per_kwh": "PV_Tarif_80pct_Obergrenze_Rp_pro_kWh",
            "local_energy_rp_per_kwh": "PV_Tarif_verwendet_Rp_pro_kWh",
            "local_energy_tariff_exceeds_cap": "PV_Tarif_ueber_80pct_Obergrenze",
            "standalone_demand_cost_chf": "Leistungskosten_einzeln_CHF",
            "allocated_community_demand_cost_chf": "Leistungskosten_vZEV_zugeteilt_CHF",
            "demand_cost_change_chf": "Leistungskosten_Aenderung_CHF",
            "other_ckw_cost_change_chf": "Uebrige_CKW_Kosten_Aenderung_CHF",
            "ckw_cost_change_chf": "CKW_Kosten_Aenderung_CHF",
            "local_pv_energy_cost_chf": "Lokale_PV_Kosten_CHF",
            "total_cost_change_chf": "Gesamtkosten_Aenderung_CHF",
        }
        frame = pd.DataFrame(self.summary.get("participants", []))
        pv_tariff = self.summary.get("pv_tariff", {})
        frame["pv_tariff_mode"] = pv_tariff.get("mode")
        frame["fixed_pv_tariff_rp_per_kwh"] = pv_tariff.get("fixed_rp_per_kwh")
        if "local_energy_tariff_exceeds_cap" in frame:
            frame["local_energy_tariff_exceeds_cap"] = frame["local_energy_tariff_exceeds_cap"].map(
                {True: "Ja", False: "Nein"}
            )
        frame = frame.reindex(columns=list(columns)).rename(columns=columns)
        return frame.to_csv(sep=";", index=False, float_format="%.2f", lineterminator="\n").encode("utf-8")


def _meter(*, display_name: str, role: str, index: int, metering_code: str | None = None) -> MeterColumn:
    return MeterColumn(
        value_index=index,
        display_name=display_name,
        metering_code=metering_code or f"SIM-{index:04d}",
        line_name=display_name,
        obis_code="",
        unit="kWh",
        initial_reading="",
        role=role,
    )


def _parsed_month(
    *,
    dataframe: pd.DataFrame,
    participant_names: list[str],
    participant_metering_codes: dict[str, str] | None = None,
) -> ParsedVzevCsv:
    community_import = _meter(display_name="vZEV Summe Bezug", role="community_import", index=1)
    community_excess = _meter(display_name="vZEV Summe Überschuss", role="community_excess", index=3)
    producer = _meter(display_name="Überschuss PVA simuliert", role="producer", index=5)
    consumers = [
        _meter(
            display_name=participant_name,
            role="consumer",
            index=7 + index * 2,
            metering_code=(participant_metering_codes or {}).get(participant_name),
        )
        for index, participant_name in enumerate(participant_names)
    ]
    meters = [community_import, community_excess, producer, *consumers]
    return ParsedVzevCsv(
        path=Path("synthetic-vzev-simulation.csv"),
        dataframe=dataframe,
        meters=meters,
        community_import=community_import,
        community_excess=community_excess,
        producers=[producer],
        consumers=consumers,
        community_reactive=None,
        consumer_reactive=[],
        source_encoding="utf-8",
    )


def _localize_for_export(timestamps: pd.Series) -> pd.Series:
    timestamps = pd.to_datetime(timestamps, errors="raise")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize("Europe/Zurich", ambiguous=False, nonexistent="shift_forward")
    else:
        timestamps = timestamps.dt.tz_convert("Europe/Zurich")
    return timestamps.map(lambda value: value.isoformat(timespec="minutes"))


def _annualized_kwh(*, timestamps: pd.Series, kwh: pd.Series) -> Decimal:
    timestamps = pd.to_datetime(timestamps, errors="raise").sort_values()
    differences = timestamps.diff().dropna().dt.total_seconds() / 3600.0
    positive_differences = differences[differences > 0]
    interval_hours = float(positive_differences.median()) if not positive_differences.empty else 0.25
    covered_hours = interval_hours * len(timestamps)
    total_kwh = Decimal(str(float(kwh.sum())))
    if 0 < covered_hours < 7200:
        return total_kwh * (Decimal("8760") / Decimal(str(covered_hours)))
    return total_kwh


def _financial_summary(
    *,
    billing_df: pd.DataFrame,
    participant_names: list[str],
    tariff_year: int,
    local_energy_factor: Decimal,
    vat_rate: Decimal | None,
    participant_metering_codes: dict[str, str] | None = None,
    fixed_local_energy_rp_per_kwh: Decimal | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], float]:
    annualized_grid_import = _annualized_kwh(
        timestamps=billing_df["timestamp"],
        kwh=billing_df["vZEV Summe Bezug"],
    )
    annualized_consumption_by_participant = {
        name: _annualized_kwh(timestamps=billing_df["timestamp"], kwh=billing_df[name])
        for name in participant_names
    }
    totals = {
        "without_vzev_total_chf": 0.0,
        "with_vzev_ckw_invoice_chf": 0.0,
        "local_pv_energy_cost_chf": 0.0,
        "with_vzev_total_chf": 0.0,
        "savings_chf": 0.0,
        "standalone_demand_cost_chf": 0.0,
        "allocated_community_demand_cost_chf": 0.0,
        "demand_cost_change_chf": 0.0,
        "other_ckw_cost_change_chf": 0.0,
        "ckw_cost_change_chf": 0.0,
        "total_cost_change_chf": 0.0,
    }
    participant_totals: dict[str, dict[str, Any]] = {
        name: {
            "participant": name,
            "metering_code": (participant_metering_codes or {}).get(name, ""),
            "consumption_kwh": 0.0,
            "allocated_grid_import_kwh": 0.0,
            "allocated_local_pv_kwh": 0.0,
            "without_vzev_total_chf": 0.0,
            "with_vzev_ckw_invoice_chf": 0.0,
            "local_pv_energy_cost_chf": 0.0,
            "with_vzev_total_chf": 0.0,
            "savings_chf": 0.0,
            "standalone_annualized_kwh": float(annualized_consumption_by_participant[name]),
            "community_annualized_grid_import_kwh": float(annualized_grid_import),
            "standalone_tariff_tier": "",
            "community_tariff_tier": "",
            "tariff_changed": False,
            "standalone_demand_rate_chf_per_kw_month": 0.0,
            "community_demand_rate_chf_per_kw_month": 0.0,
            "standalone_demand_cost_chf": 0.0,
            "allocated_community_demand_cost_chf": 0.0,
            "demand_cost_change_chf": 0.0,
            "other_ckw_cost_change_chf": 0.0,
            "ckw_cost_change_chf": 0.0,
            "total_cost_change_chf": 0.0,
            "local_energy_cap_rp_per_kwh": 0.0,
            "local_energy_tariff_was_capped": False,
            "local_energy_tariff_exceeds_cap": False,
            "_standard_reference_rate_kwh": 0.0,
            "_local_energy_rate_kwh": 0.0,
            "_local_energy_cap_rate_kwh": 0.0,
        }
        for name in participant_names
    }
    monthly_rows: list[dict[str, Any]] = []
    minimum_local_energy_cap_rp_per_kwh: float | None = None

    month_keys = pd.to_datetime(billing_df["timestamp"], errors="raise").dt.strftime("%Y-%m")
    for month, month_df in billing_df.groupby(month_keys, sort=True):
        parsed = _parsed_month(
            dataframe=month_df.reset_index(drop=True),
            participant_names=participant_names,
            participant_metering_codes=participant_metering_codes,
        )
        result = calculate_vzev_invoices(
            parsed,
            year=tariff_year,
            annual_kwh_for_tariff=annualized_grid_import,
            annual_kwh_by_consumer=annualized_consumption_by_participant,
            local_energy_factor=local_energy_factor,
            peak_allocation_mode=SIMULATION_PEAK_ALLOCATION_MODE,
            vat_rate=vat_rate,
        )
        monthly_minimum_cap = min(
            (float(consumer["local_energy_cap_rp_per_kwh"]) for consumer in result["consumers"]),
            default=None,
        )
        if monthly_minimum_cap is not None:
            minimum_local_energy_cap_rp_per_kwh = (
                monthly_minimum_cap
                if minimum_local_energy_cap_rp_per_kwh is None
                else min(minimum_local_energy_cap_rp_per_kwh, monthly_minimum_cap)
            )
        aggregate = result["aggregated_invoice"]
        ckw_invoice_chf = float(aggregate["invoice_total_chf"])
        local_pv_cost_chf = 0.0
        ckw_allocations = allocate_ckw_invoice_totals(result)
        without_vzev_chf = 0.0
        monthly_demand_change_chf = 0.0
        monthly_other_ckw_change_chf = 0.0

        for consumer in result["consumers"]:
            participant = participant_totals[str(consumer["display_name"])]
            without = calculate_without_vzev_cost(result, consumer)
            without_total = float(without["without_vzev_total_chf"])
            allocation_key = str(consumer["metering_code"] or consumer["display_name"])
            ckw_bill_share = float(ckw_allocations[allocation_key]["ckw_invoice_share_chf"])
            applied_local_energy_rp_per_kwh = (
                float(fixed_local_energy_rp_per_kwh)
                if fixed_local_energy_rp_per_kwh is not None
                else float(consumer["local_energy_rp_per_kwh"])
            )
            participant_local_pv_cost = (
                float(
                    (
                        fixed_local_energy_rp_per_kwh
                        * Decimal(str(consumer["allocated_local_pv_kwh"]))
                        / Decimal("100")
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                )
                if fixed_local_energy_rp_per_kwh is not None
                else float(consumer["local_energy_chf"])
            )
            local_tariff_exceeds_cap = (
                fixed_local_energy_rp_per_kwh is not None
                and fixed_local_energy_rp_per_kwh > Decimal(str(consumer["local_energy_cap_rp_per_kwh"]))
            )
            local_pv_cost_chf += participant_local_pv_cost
            with_total = ckw_bill_share + participant_local_pv_cost
            reference = consumer["standard_product_reference"]
            vat_multiplier = 1.0 + float(reference.get("vat_rate", 0) or 0)
            grid_import_total = float(aggregate["grid_import_kwh"])
            import_share = (
                float(consumer["allocated_grid_import_kwh"]) / grid_import_total
                if grid_import_total > 0
                else 0.0
            )
            standalone_demand_cost_chf = float(reference["demand_chf"]) * vat_multiplier
            allocated_community_demand_cost_chf = (
                float(aggregate["peak_price_chf"]) * import_share * vat_multiplier
            )
            demand_cost_change_chf = allocated_community_demand_cost_chf - standalone_demand_cost_chf
            ckw_cost_change_chf = ckw_bill_share - without_total
            other_ckw_cost_change_chf = ckw_cost_change_chf - demand_cost_change_chf
            total_cost_change_chf = with_total - without_total
            without_vzev_chf += without_total
            participant["consumption_kwh"] += float(consumer["consumption_kwh"])
            participant["allocated_grid_import_kwh"] += float(consumer["allocated_grid_import_kwh"])
            participant["allocated_local_pv_kwh"] += float(consumer["allocated_local_pv_kwh"])
            participant["without_vzev_total_chf"] += without_total
            participant["with_vzev_ckw_invoice_chf"] += ckw_bill_share
            participant["local_pv_energy_cost_chf"] += participant_local_pv_cost
            participant["with_vzev_total_chf"] += with_total
            participant["local_energy_tariff_exceeds_cap"] = (
                participant["local_energy_tariff_exceeds_cap"] or local_tariff_exceeds_cap
            )
            participant["standalone_tariff_tier"] = str(reference["tariff_tier"])
            participant["community_tariff_tier"] = str(result["selected_tariff_tier"])
            participant["tariff_changed"] = (
                participant["standalone_tariff_tier"] != participant["community_tariff_tier"]
            )
            participant["standalone_demand_rate_chf_per_kw_month"] = float(
                reference["demand_chf_per_kw_month"]
            )
            participant["community_demand_rate_chf_per_kw_month"] = float(
                result["rates"]["peak_price_chf_per_kw_month"]
            )
            participant["standalone_demand_cost_chf"] += standalone_demand_cost_chf
            participant["allocated_community_demand_cost_chf"] += allocated_community_demand_cost_chf
            participant["demand_cost_change_chf"] += demand_cost_change_chf
            participant["other_ckw_cost_change_chf"] += other_ckw_cost_change_chf
            participant["ckw_cost_change_chf"] += ckw_cost_change_chf
            participant["total_cost_change_chf"] += total_cost_change_chf
            participant["_standard_reference_rate_kwh"] += (
                float(consumer["standard_product_reference_rp_per_kwh"])
                * float(consumer["consumption_kwh"])
            )
            participant["_local_energy_rate_kwh"] += (
                applied_local_energy_rp_per_kwh * float(consumer["allocated_local_pv_kwh"])
            )
            participant["_local_energy_cap_rate_kwh"] += (
                float(consumer["local_energy_cap_rp_per_kwh"])
                * float(consumer["consumption_kwh"])
            )
            monthly_demand_change_chf += demand_cost_change_chf
            monthly_other_ckw_change_chf += other_ckw_cost_change_chf

        with_vzev_chf = ckw_invoice_chf + local_pv_cost_chf
        savings_chf = without_vzev_chf - with_vzev_chf
        totals["without_vzev_total_chf"] += without_vzev_chf
        totals["with_vzev_ckw_invoice_chf"] += ckw_invoice_chf
        totals["local_pv_energy_cost_chf"] += local_pv_cost_chf
        totals["with_vzev_total_chf"] += with_vzev_chf
        totals["savings_chf"] += savings_chf
        totals["standalone_demand_cost_chf"] += sum(
            float(consumer["standard_product_reference"]["demand_chf"])
            * (1.0 + float(consumer["standard_product_reference"].get("vat_rate", 0) or 0))
            for consumer in result["consumers"]
        )
        totals["allocated_community_demand_cost_chf"] += float(aggregate["peak_price_chf"]) * (
            1.0 + float(result["rates"].get("vat_rate", 0) or 0)
        )
        totals["demand_cost_change_chf"] += monthly_demand_change_chf
        totals["other_ckw_cost_change_chf"] += monthly_other_ckw_change_chf
        totals["ckw_cost_change_chf"] += ckw_invoice_chf - without_vzev_chf
        totals["total_cost_change_chf"] += with_vzev_chf - without_vzev_chf
        monthly_rows.append(
            {
                "month": month,
                "without_vzev_total_chf": round(without_vzev_chf, 2),
                "with_vzev_ckw_invoice_chf": round(ckw_invoice_chf, 2),
                "local_pv_energy_cost_chf": round(local_pv_cost_chf, 2),
                "with_vzev_total_chf": round(with_vzev_chf, 2),
                "savings_chf": round(savings_chf, 2),
                "demand_cost_change_chf": round(monthly_demand_change_chf, 2),
                "other_ckw_cost_change_chf": round(monthly_other_ckw_change_chf, 2),
                "ckw_cost_change_chf": round(ckw_invoice_chf - without_vzev_chf, 2),
                "total_cost_change_chf": round(with_vzev_chf - without_vzev_chf, 2),
            }
        )

    for participant in participant_totals.values():
        participant["savings_chf"] = participant["without_vzev_total_chf"] - participant["with_vzev_total_chf"]
        participant["standard_product_reference_rp_per_kwh"] = (
            participant["_standard_reference_rate_kwh"] / participant["consumption_kwh"]
            if participant["consumption_kwh"]
            else 0.0
        )
        participant["local_energy_rp_per_kwh"] = (
            participant["_local_energy_rate_kwh"] / participant["allocated_local_pv_kwh"]
            if participant["allocated_local_pv_kwh"]
            else float(fixed_local_energy_rp_per_kwh or 0)
        )
        participant["local_energy_cap_rp_per_kwh"] = (
            participant["_local_energy_cap_rate_kwh"] / participant["consumption_kwh"]
            if participant["consumption_kwh"]
            else 0.0
        )
        del participant["_standard_reference_rate_kwh"]
        del participant["_local_energy_rate_kwh"]
        del participant["_local_energy_cap_rate_kwh"]
        for key, value in list(participant.items()):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                participant[key] = round(float(value), 2)
    rounded_totals = {key: round(value, 2) for key, value in totals.items()}
    rounded_totals["savings_pct"] = (
        rounded_totals["savings_chf"] / rounded_totals["without_vzev_total_chf"]
        if rounded_totals["without_vzev_total_chf"]
        else 0.0
    )
    return (
        rounded_totals,
        list(participant_totals.values()),
        monthly_rows,
        round(minimum_local_energy_cap_rp_per_kwh or 0.0, 2),
    )


def simulate_vzev(
    *,
    timestamps: Iterable[object],
    pv_kwh: Iterable[float],
    consumption_by_participant: pd.DataFrame,
    tariff_year: int | None = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
    local_energy_factor: Decimal = Decimal("0.80"),
    vat_rate: Decimal | None = DEFAULT_VAT_RATE,
    warnings: list[str] | None = None,
    participant_metering_codes: dict[str, str] | None = None,
    fixed_local_energy_rp_per_kwh: Decimal | None = None,
) -> VzevSimulation:
    resolved_tariff_year = tariff_year if tariff_year is not None else DEFAULT_VZEV_SIMULATION_TARIFF_YEAR
    participant_names = [str(column) for column in consumption_by_participant.columns]
    if not participant_names:
        raise ValueError("Für die vZEV-Simulation werden Verbrauchsdaten benötigt.")
    if len(set(participant_names)) != len(participant_names):
        raise ValueError("Die Namen der Verbrauchszeitreihen müssen eindeutig sein.")
    if fixed_local_energy_rp_per_kwh is not None and fixed_local_energy_rp_per_kwh < 0:
        raise ValueError("Der fixe PV-Tarif muss mindestens 0 Rp./kWh betragen.")

    df = pd.DataFrame({"timestamp": pd.to_datetime(list(timestamps), errors="raise")})
    df["Produktion_kWh"] = pd.to_numeric(pd.Series(list(pv_kwh)), errors="raise").fillna(0.0).astype(float)
    if len(consumption_by_participant) != len(df):
        raise ValueError("Solar- und Verbrauchszeitreihen müssen dieselbe Anzahl Intervalle enthalten.")
    if (df["Produktion_kWh"] < 0).any():
        raise ValueError("Solarzeitreihen dürfen keine negativen Werte enthalten.")

    participants = consumption_by_participant.reset_index(drop=True).copy()
    participants.columns = participant_names
    for column in participant_names:
        participants[column] = pd.to_numeric(participants[column], errors="raise").fillna(0.0).astype(float)
        if (participants[column] < 0).any():
            raise ValueError("Verbrauchszeitreihen dürfen keine negativen Werte enthalten.")
        df[column] = participants[column]

    df = df.sort_values("timestamp", ignore_index=True)
    consumption = df[participant_names].sum(axis=1)
    production = df["Produktion_kWh"]
    local_pv = production.combine(consumption, min)
    grid_import = (consumption - local_pv).clip(lower=0.0)
    pv_excess = (production - local_pv).clip(lower=0.0)
    high_tariff = df["timestamp"].dt.hour.between(7, 21)

    billing_df = df[["timestamp", *participant_names]].copy()
    billing_df["vZEV Summe Bezug"] = grid_import
    billing_df["vZEV Summe Überschuss"] = pv_excess
    billing_df["Überschuss PVA simuliert"] = pv_excess
    financial_totals, participant_rows, monthly_rows, minimum_local_energy_cap_rp_per_kwh = _financial_summary(
        billing_df=billing_df,
        participant_names=participant_names,
        tariff_year=resolved_tariff_year,
        local_energy_factor=local_energy_factor,
        vat_rate=vat_rate,
        participant_metering_codes=participant_metering_codes,
        fixed_local_energy_rp_per_kwh=fixed_local_energy_rp_per_kwh,
    )

    synthetic_df = pd.DataFrame(
        {
            "Zeitstempel": _localize_for_export(df["timestamp"]),
            "Energiebedarf_total_kWh": consumption,
            "Rueckspeissung_total_kWh": pv_excess,
            "Bezug_total_kWh": grid_import,
            "Produktion_kWh": production,
            "Eigenverbrauch_%": local_pv.div(consumption.where(consumption > 0)).fillna(0.0),
            "Produktion_total_kWh": local_pv,
            "Energiebedarf_Hochtarif_kWh": grid_import.where(high_tariff, 0.0),
            "Energiebedarf_Niedertarif_kWh": grid_import.where(~high_tariff, 0.0),
            "Produktion_Hochtarif_kWh": local_pv.where(high_tariff, 0.0),
            "Produktion_Niedertarif_kWh": local_pv.where(~high_tariff, 0.0),
        }
    )
    local_pv_total = float(local_pv.sum())
    consumption_total = float(consumption.sum())
    production_total = float(production.sum())
    simulation_warnings = list(warnings or [])
    fixed_tariff_exceeds_cap = fixed_local_energy_rp_per_kwh is not None and any(
        bool(participant["local_energy_tariff_exceeds_cap"]) for participant in participant_rows
    )
    if fixed_tariff_exceeds_cap:
        simulation_warnings.append(
            f"Warnung: Der fixe PV-Tarif von {float(fixed_local_energy_rp_per_kwh):.2f} Rp./kWh "
            f"überschreitet mindestens eine teilnehmerspezifische 80%-Obergrenze. Die strengste "
            f"Obergrenze über alle Teilnehmer und Monate beträgt {minimum_local_energy_cap_rp_per_kwh:.2f} "
            "Rp./kWh. Die Simulation verwendet den eingegebenen Tarif trotzdem unverändert für alle Teilnehmer."
        )

    summary = {
        "tariff_year": resolved_tariff_year,
        "peak_allocation_mode": SIMULATION_PEAK_ALLOCATION_MODE,
        "pv_tariff": {
            "mode": "fixed" if fixed_local_energy_rp_per_kwh is not None else "participant_80_pct",
            "fixed_rp_per_kwh": (
                float(fixed_local_energy_rp_per_kwh) if fixed_local_energy_rp_per_kwh is not None else None
            ),
            "minimum_participant_cap_rp_per_kwh": minimum_local_energy_cap_rp_per_kwh,
            "exceeds_80_percent_cap": fixed_tariff_exceeds_cap,
        },
        "warnings": simulation_warnings,
        "assumptions": [
            "Die hochgeladenen klassischen PV- und Verbrauchszeitreihen werden je 15-Minuten-Intervall zu einem synthetischen vZEV aggregiert.",
            "Die Hoch- und Niedertarifspalten folgen dem Muster der Referenzdatei: Hochtarif täglich von 07:00 bis 21:59 Uhr.",
            f"Die finanzielle Bewertung ruft die bestehende vZEV-Abrechnung für jeden Kalendermonat einzeln mit den CKW-Tarifen {resolved_tariff_year} auf.",
            "Der Netz-Leistungstarif wird einmal auf dem gesamten vZEV-Bezug berechnet. Für den Teilnehmervergleich wird die gemeinsame CKW-Rechnung inklusive Leistungstarif proportional zum verbleibenden Netzbezug verteilt.",
            "Kostenänderungen sind als Kosten mit vZEV minus Kosten ohne vZEV definiert: Positive Werte sind Mehrkosten, negative Werte Kostenreduktionen.",
            "Lokal genutzte PV-Energie ist von Netznutzung und öffentlichen Abgaben befreit.",
            (
                f"Lokal genutzte PV-Energie wird für alle Teilnehmer mit dem fixen Tarif von "
                f"{float(fixed_local_energy_rp_per_kwh):.2f} Rp./kWh bewertet. Der Wert wird für jeden "
                "Teilnehmer und Monat gegen die jeweilige 80%-Obergrenze geprüft; eine Überschreitung wird "
                "ausgewiesen, der eingegebene Tarif aber trotzdem verwendet."
                if fixed_local_energy_rp_per_kwh is not None
                else "Lokal genutzte PV-Energie wird je Teilnehmer mit 80% des vollständigen CKW-Standardproduktpreises inklusive Netznutzung, Leistung, Messung, Abgaben und MWST bewertet."
            ),
        ],
        "totals": {
            "consumption_kwh": consumption_total,
            "pv_production_kwh": production_total,
            "local_pv_consumption_kwh": local_pv_total,
            "grid_import_kwh": float(grid_import.sum()),
            "pv_excess_kwh": float(pv_excess.sum()),
            "self_consumption_pct": local_pv_total / consumption_total if consumption_total else 0.0,
            "pv_local_use_pct": local_pv_total / production_total if production_total else 0.0,
            **financial_totals,
        },
        "participants": participant_rows,
        "monthly": monthly_rows,
    }
    return VzevSimulation(dataframe=synthetic_df, summary=summary)
