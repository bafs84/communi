from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

import duckdb
import pandas as pd


RP_PER_CHF = Decimal("100")


def rp_to_chf(rp: Decimal) -> Decimal:
    return rp / RP_PER_CHF


@dataclass(frozen=True)
class ImportTariff2025:
    energielieferung_rp_per_kwh: Decimal = Decimal("8.50")
    # The standard product is the legal comparison product for the vZEV
    # internal electricity price.  It is deliberately separate from the
    # product used on an actual community invoice.
    standardprodukt_energielieferung_rp_per_kwh: Decimal = Decimal("8.50")

    netznutzung_einheitstarif_rp_per_kwh: Decimal = Decimal("5.40")
    netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh: Decimal = Decimal("0.55")
    netznutzung_stromreserve_rp_per_kwh: Decimal = Decimal("0.23")
    netznutzung_solidarisierte_kosten_rp_per_kwh: Decimal = Decimal("0")

    netz_grundtarif_chf_per_month: Decimal = Decimal("5.10")
    netz_leistungstarif_chf_per_kw_month: Decimal = Decimal("1.50")
    messentgelt_chf_per_month: Decimal = Decimal("0")
    messentgelt_direkt_chf_per_month: Decimal = Decimal("0")
    messentgelt_indirekt_chf_per_month: Decimal = Decimal("0")
    messentgelt_virtuell_chf_per_month: Decimal = Decimal("0")

    oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh: Decimal = Decimal("2.30")
    oeffentliche_abgaben_gemeinde_rp_per_kwh: Decimal = Decimal("0.94")

    blindenergie_rp_per_kvarh: Decimal = Decimal("0")

    total_rp_per_kwh: Decimal = Decimal("21.12")

    @classmethod
    def lt_50000_kwh_per_year(cls) -> "ImportTariff2025":
        return cls(
            energielieferung_rp_per_kwh=Decimal("8.50"),
            netznutzung_einheitstarif_rp_per_kwh=Decimal("5.40"),
            netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh=Decimal("0.55"),
            netznutzung_stromreserve_rp_per_kwh=Decimal("0.23"),
            netznutzung_solidarisierte_kosten_rp_per_kwh=Decimal("0"),
            netz_grundtarif_chf_per_month=Decimal("5.10"),
            netz_leistungstarif_chf_per_kw_month=Decimal("1.50"),
            messentgelt_chf_per_month=Decimal("0"),
            messentgelt_direkt_chf_per_month=Decimal("0"),
            messentgelt_indirekt_chf_per_month=Decimal("0"),
            messentgelt_virtuell_chf_per_month=Decimal("0"),
            oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh=Decimal("2.30"),
            oeffentliche_abgaben_gemeinde_rp_per_kwh=Decimal("0.94"),
            blindenergie_rp_per_kvarh=Decimal("0"),
            total_rp_per_kwh=Decimal("21.12"),
        )

    @classmethod
    def gt_50000_kwh_per_year(cls) -> "ImportTariff2025":
        return cls(
            energielieferung_rp_per_kwh=Decimal("8.50"),
            netznutzung_einheitstarif_rp_per_kwh=Decimal("4.40"),
            netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh=Decimal("0.55"),
            netznutzung_stromreserve_rp_per_kwh=Decimal("0.23"),
            netznutzung_solidarisierte_kosten_rp_per_kwh=Decimal("0"),
            netz_grundtarif_chf_per_month=Decimal("49.00"),
            netz_leistungstarif_chf_per_kw_month=Decimal("9.00"),
            messentgelt_chf_per_month=Decimal("0"),
            messentgelt_direkt_chf_per_month=Decimal("0"),
            messentgelt_indirekt_chf_per_month=Decimal("0"),
            messentgelt_virtuell_chf_per_month=Decimal("0"),
            oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh=Decimal("2.30"),
            oeffentliche_abgaben_gemeinde_rp_per_kwh=Decimal("0.94"),
            blindenergie_rp_per_kvarh=Decimal("4.50"),
            total_rp_per_kwh=Decimal("21.15"),
        )

    def variable_rp_per_kwh_components(self) -> dict[str, Decimal]:
        return {
            "energielieferung": self.energielieferung_rp_per_kwh,
            "netznutzung_einheitstarif": self.netznutzung_einheitstarif_rp_per_kwh,
            "netznutzung_systemdienstleistungen_swissgrid": self.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh,
            "netznutzung_stromreserve": self.netznutzung_stromreserve_rp_per_kwh,
            "netznutzung_solidarisierte_kosten": self.netznutzung_solidarisierte_kosten_rp_per_kwh,
            "oeffentliche_abgaben_foerderung_erneuerbare": self.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh,
            "oeffentliche_abgaben_gemeinde": self.oeffentliche_abgaben_gemeinde_rp_per_kwh,
        }

    def variable_rp_per_kwh_sum(self) -> Decimal:
        return sum(self.variable_rp_per_kwh_components().values(), start=Decimal("0"))


def select_import_tariff_2025(
    *,
    kwh: float | Decimal,
    threshold_kwh: float | Decimal = Decimal("50000"),
) -> ImportTariff2025:
    kwh_d = kwh if isinstance(kwh, Decimal) else Decimal(str(kwh))
    thr_d = threshold_kwh if isinstance(threshold_kwh, Decimal) else Decimal(str(threshold_kwh))
    if kwh_d > thr_d:
        return ImportTariff2025.gt_50000_kwh_per_year()
    return ImportTariff2025.lt_50000_kwh_per_year()


@dataclass(frozen=True)
class ImportTariff2026:
    energielieferung_rp_per_kwh: Decimal = Decimal("11.30")
    standardprodukt_energielieferung_rp_per_kwh: Decimal = Decimal("12.00")

    netznutzung_einheitstarif_rp_per_kwh: Decimal = Decimal("6.00")
    netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh: Decimal = Decimal("0.27")
    netznutzung_stromreserve_rp_per_kwh: Decimal = Decimal("0.41")
    netznutzung_solidarisierte_kosten_rp_per_kwh: Decimal = Decimal("0.05")

    netz_grundtarif_chf_per_month: Decimal = Decimal("0")
    netz_leistungstarif_chf_per_kw_month: Decimal = Decimal("1.50")
    messentgelt_chf_per_month: Decimal = Decimal("5.15")
    messentgelt_direkt_chf_per_month: Decimal = Decimal("5.15")
    messentgelt_indirekt_chf_per_month: Decimal = Decimal("18.60")
    messentgelt_virtuell_chf_per_month: Decimal = Decimal("1.05")

    oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh: Decimal = Decimal("2.30")
    oeffentliche_abgaben_gemeinde_rp_per_kwh: Decimal = Decimal("0.85")

    blindenergie_rp_per_kvarh: Decimal = Decimal("0")

    total_rp_per_kwh: Decimal = Decimal("21.18")

    @classmethod
    def lt_50000_kwh_per_year(cls) -> "ImportTariff2026":
        return cls(
            energielieferung_rp_per_kwh=Decimal("11.30"),
            netznutzung_einheitstarif_rp_per_kwh=Decimal("6.00"),
            netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh=Decimal("0.27"),
            netznutzung_stromreserve_rp_per_kwh=Decimal("0.41"),
            netznutzung_solidarisierte_kosten_rp_per_kwh=Decimal("0.05"),
            netz_grundtarif_chf_per_month=Decimal("0"),
            netz_leistungstarif_chf_per_kw_month=Decimal("1.50"),
            messentgelt_chf_per_month=Decimal("5.15"),
            messentgelt_direkt_chf_per_month=Decimal("5.15"),
            messentgelt_indirekt_chf_per_month=Decimal("18.60"),
            messentgelt_virtuell_chf_per_month=Decimal("1.05"),
            oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh=Decimal("2.30"),
            oeffentliche_abgaben_gemeinde_rp_per_kwh=Decimal("0.85"),
            blindenergie_rp_per_kvarh=Decimal("0"),
            total_rp_per_kwh=Decimal("21.18"),
        )

    @classmethod
    def gt_50000_kwh_per_year(cls) -> "ImportTariff2026":
        return cls(
            energielieferung_rp_per_kwh=Decimal("11.30"),
            netznutzung_einheitstarif_rp_per_kwh=Decimal("4.40"),
            netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh=Decimal("0.27"),
            netznutzung_stromreserve_rp_per_kwh=Decimal("0.41"),
            netznutzung_solidarisierte_kosten_rp_per_kwh=Decimal("0.05"),
            netz_grundtarif_chf_per_month=Decimal("0"),
            netz_leistungstarif_chf_per_kw_month=Decimal("8.00"),
            messentgelt_chf_per_month=Decimal("18.60"),
            messentgelt_direkt_chf_per_month=Decimal("5.15"),
            messentgelt_indirekt_chf_per_month=Decimal("18.60"),
            messentgelt_virtuell_chf_per_month=Decimal("1.05"),
            oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh=Decimal("2.30"),
            oeffentliche_abgaben_gemeinde_rp_per_kwh=Decimal("0.85"),
            blindenergie_rp_per_kvarh=Decimal("4.50"),
            total_rp_per_kwh=Decimal("19.58"),
        )

    def variable_rp_per_kwh_components(self) -> dict[str, Decimal]:
        return {
            "energielieferung": self.energielieferung_rp_per_kwh,
            "netznutzung_einheitstarif": self.netznutzung_einheitstarif_rp_per_kwh,
            "netznutzung_systemdienstleistungen_swissgrid": self.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh,
            "netznutzung_stromreserve": self.netznutzung_stromreserve_rp_per_kwh,
            "netznutzung_solidarisierte_kosten": self.netznutzung_solidarisierte_kosten_rp_per_kwh,
            "oeffentliche_abgaben_foerderung_erneuerbare": self.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh,
            "oeffentliche_abgaben_gemeinde": self.oeffentliche_abgaben_gemeinde_rp_per_kwh,
        }

    def variable_rp_per_kwh_sum(self) -> Decimal:
        return sum(self.variable_rp_per_kwh_components().values(), start=Decimal("0"))


def select_import_tariff_2026(
    *,
    kwh: float | Decimal,
    threshold_kwh: float | Decimal = Decimal("50000"),
) -> ImportTariff2026:
    kwh_d = kwh if isinstance(kwh, Decimal) else Decimal(str(kwh))
    thr_d = threshold_kwh if isinstance(threshold_kwh, Decimal) else Decimal(str(threshold_kwh))
    if kwh_d > thr_d:
        return ImportTariff2026.gt_50000_kwh_per_year()
    return ImportTariff2026.lt_50000_kwh_per_year()


def select_import_tariff(
    *,
    year: int,
    kwh: float | Decimal,
    threshold_kwh: float | Decimal = Decimal("50000"),
) -> ImportTariff2025 | ImportTariff2026:
    if year == 2025:
        return select_import_tariff_2025(kwh=kwh, threshold_kwh=threshold_kwh)
    if year == 2026:
        return select_import_tariff_2026(kwh=kwh, threshold_kwh=threshold_kwh)
    raise ValueError(f"Unsupported tariff year: {year}")


STANDARD_PRODUCT_NAME = "CKW ClassicStrom"
STANDARD_PRODUCT_SOURCE_URLS = {
    (2025, "lt_50000"): (
        "https://www.ckw.ch/_Resources/Persistent/0/0/b/f/"
        "00bfe83d77d39cfefe608129d4d0ee9b71988369/CKW_MM_Tarife%202025.pdf"
    ),
    (2025, "gt_50000"): (
        "https://www.ckw.ch/_Resources/Persistent/0/0/b/f/"
        "00bfe83d77d39cfefe608129d4d0ee9b71988369/CKW_MM_Tarife%202025.pdf"
    ),
    (2026, "lt_50000"): (
        "https://www.ckw.ch/_Resources/Persistent/b/2/b/1/"
        "b2b135be0472fe589ad8a75c21c770c99c39b79f/"
        "Preisinformation%20Stromprodukte%202026_Privat-und%20Gewerbekunden_weniger%20als%2050%27000.pdf"
    ),
    (2026, "gt_50000"): (
        "https://www.ckw.ch/_Resources/Persistent/b/f/c/7/"
        "bfc7e9065ceee458a3b537bdd12dc1970e04e549/"
        "Preisinformation%20Stromprodukte%202026_Gewerbekunden_%C3%BCber%2050%27000.pdf"
    ),
}
VZEV_PRICE_RULE_SOURCE_URL = "https://www.fedlex.admin.ch/eli/cc/2017/763/de#art_16"
VZEV_PRICE_GUIDANCE_SOURCE_URL = "https://pubdb.bfe.admin.ch/de/publication/download/9329"


@dataclass(frozen=True)
class StandardProductReference:
    """Participant-specific vZEV comparison price under EnV article 16.

    Fixed, demand and metering charges are converted to an equivalent Rp/kWh
    rate over the participant's measured consumption.  The result includes VAT
    because the published 80% comparison in the BFE guidance includes VAT.
    """

    year: int
    product_name: str
    tariff_tier: str
    meter_type: str
    consumption_kwh: Decimal
    annualized_consumption_kwh: Decimal
    peak_kw: Decimal
    months: Decimal
    energy_rp_per_kwh: Decimal
    network_variable_rp_per_kwh: Decimal
    public_charges_rp_per_kwh: Decimal
    demand_chf_per_kw_month: Decimal
    fixed_and_metering_chf_per_month: Decimal
    energy_chf: Decimal
    network_variable_chf: Decimal
    demand_chf: Decimal
    fixed_and_metering_chf: Decimal
    public_charges_chf: Decimal
    subtotal_excl_vat_chf: Decimal
    vat_rate: Decimal
    vat_chf: Decimal
    total_incl_vat_chf: Decimal
    equivalent_rp_per_kwh_incl_vat: Decimal
    source_url: str
    legal_source_url: str = VZEV_PRICE_RULE_SOURCE_URL
    guidance_source_url: str = VZEV_PRICE_GUIDANCE_SOURCE_URL

    def as_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "product_name": self.product_name,
            "tariff_tier": self.tariff_tier,
            "meter_type": self.meter_type,
            "consumption_kwh": float(self.consumption_kwh),
            "annualized_consumption_kwh": float(self.annualized_consumption_kwh),
            "peak_kw": float(self.peak_kw),
            "months": float(self.months),
            "energy_rp_per_kwh": float(self.energy_rp_per_kwh),
            "network_variable_rp_per_kwh": float(self.network_variable_rp_per_kwh),
            "public_charges_rp_per_kwh": float(self.public_charges_rp_per_kwh),
            "demand_chf_per_kw_month": float(self.demand_chf_per_kw_month),
            "fixed_and_metering_chf_per_month": float(self.fixed_and_metering_chf_per_month),
            "energy_chf": float(self.energy_chf),
            "network_variable_chf": float(self.network_variable_chf),
            "demand_chf": float(self.demand_chf),
            "fixed_and_metering_chf": float(self.fixed_and_metering_chf),
            "public_charges_chf": float(self.public_charges_chf),
            "subtotal_excl_vat_chf": float(self.subtotal_excl_vat_chf),
            "vat_rate": float(self.vat_rate),
            "vat_chf": float(self.vat_chf),
            "total_incl_vat_chf": float(self.total_incl_vat_chf),
            "equivalent_rp_per_kwh_incl_vat": float(self.equivalent_rp_per_kwh_incl_vat),
            "source_url": self.source_url,
            "legal_source_url": self.legal_source_url,
            "guidance_source_url": self.guidance_source_url,
        }


def calculate_standard_product_reference(
    *,
    year: int,
    consumption_kwh: float | Decimal,
    annualized_consumption_kwh: float | Decimal | None = None,
    peak_kw: float | Decimal = Decimal("0"),
    months: float | Decimal = Decimal("1"),
    meter_type: str = "direct",
    vat_rate: float | Decimal = Decimal("0.081"),
) -> StandardProductReference:
    """Calculate the full standard-product price used by the vZEV 80% rule."""

    consumption = consumption_kwh if isinstance(consumption_kwh, Decimal) else Decimal(str(consumption_kwh))
    annualized = (
        annualized_consumption_kwh
        if isinstance(annualized_consumption_kwh, Decimal)
        else Decimal(str(annualized_consumption_kwh))
        if annualized_consumption_kwh is not None
        else consumption
    )
    peak = peak_kw if isinstance(peak_kw, Decimal) else Decimal(str(peak_kw))
    months_d = months if isinstance(months, Decimal) else Decimal(str(months))
    vat = vat_rate if isinstance(vat_rate, Decimal) else Decimal(str(vat_rate))
    if consumption < 0 or annualized < 0 or peak < 0 or months_d < 0:
        raise ValueError("Standard-product reference inputs must be non-negative")
    if meter_type not in {"direct", "indirect"}:
        raise ValueError("meter_type must be 'direct' or 'indirect'")

    tariff = select_import_tariff(year=year, kwh=annualized)
    tier = "gt_50000" if annualized > Decimal("50000") else "lt_50000"
    energy_rp = tariff.standardprodukt_energielieferung_rp_per_kwh
    network_rp = (
        tariff.netznutzung_einheitstarif_rp_per_kwh
        + tariff.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh
        + tariff.netznutzung_stromreserve_rp_per_kwh
        + tariff.netznutzung_solidarisierte_kosten_rp_per_kwh
    )
    public_rp = (
        tariff.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh
        + tariff.oeffentliche_abgaben_gemeinde_rp_per_kwh
    )
    meter_chf = (
        tariff.messentgelt_indirekt_chf_per_month
        if meter_type == "indirect"
        else tariff.messentgelt_direkt_chf_per_month
    )
    fixed_and_metering_monthly = tariff.netz_grundtarif_chf_per_month + meter_chf
    energy_chf = rp_to_chf(consumption * energy_rp)
    network_chf = rp_to_chf(consumption * network_rp)
    public_chf = rp_to_chf(consumption * public_rp)
    demand_chf = peak * tariff.netz_leistungstarif_chf_per_kw_month * months_d
    fixed_chf = fixed_and_metering_monthly * months_d
    subtotal = energy_chf + network_chf + public_chf + demand_chf + fixed_chf
    vat_chf = subtotal * vat
    total = subtotal + vat_chf
    equivalent_rate = total * Decimal("100") / consumption if consumption > 0 else Decimal("0")

    return StandardProductReference(
        year=year,
        product_name=STANDARD_PRODUCT_NAME,
        tariff_tier=tier,
        meter_type=meter_type,
        consumption_kwh=consumption,
        annualized_consumption_kwh=annualized,
        peak_kw=peak,
        months=months_d,
        energy_rp_per_kwh=energy_rp,
        network_variable_rp_per_kwh=network_rp,
        public_charges_rp_per_kwh=public_rp,
        demand_chf_per_kw_month=tariff.netz_leistungstarif_chf_per_kw_month,
        fixed_and_metering_chf_per_month=fixed_and_metering_monthly,
        energy_chf=energy_chf,
        network_variable_chf=network_chf,
        demand_chf=demand_chf,
        fixed_and_metering_chf=fixed_chf,
        public_charges_chf=public_chf,
        subtotal_excl_vat_chf=subtotal,
        vat_rate=vat,
        vat_chf=vat_chf,
        total_incl_vat_chf=total,
        equivalent_rp_per_kwh_incl_vat=equivalent_rate,
        source_url=STANDARD_PRODUCT_SOURCE_URLS.get(
            (year, tier),
            "https://www.ckw.ch/energie/strom/preise",
        ),
    )


@dataclass(frozen=True)
class ImportCostBreakdown:
    kwh: Decimal
    variable_rp_per_kwh: Decimal
    variable_cost_chf: Decimal
    fixed_cost_chf: Decimal
    demand_cost_chf: Decimal
    blindenergie_kvarh: Decimal
    blindenergie_cost_chf: Decimal
    subtotal_chf: Decimal
    vat_rate: Decimal | None
    vat_amount_chf: Decimal
    total_chf: Decimal
    details: dict[str, Any]


@dataclass(frozen=True)
class ImportCostRun:
    breakdown: ImportCostBreakdown
    kwh_import: Decimal
    months: int
    monthly_peak_kw: dict[str, Decimal]
    blindenergie_kvarh: Decimal


def monthly_peak_kw_from_15min_timeseries(
    *,
    df: pd.DataFrame,
    timestamp_col: str = "timestamp",
    energy_kwh_col: str = "energy_kwh_signed",
) -> dict[str, Decimal]:
    if timestamp_col not in df.columns:
        raise ValueError(f"Missing timestamp column: {timestamp_col}")
    if energy_kwh_col not in df.columns:
        raise ValueError(f"Missing energy column: {energy_kwh_col}")

    ts = pd.to_datetime(df[timestamp_col], errors="raise")
    kwh = pd.to_numeric(df[energy_kwh_col], errors="coerce").fillna(0)
    kwh_import = kwh.clip(lower=0)

    kw_15min = kwh_import * 4
    month_key = ts.dt.strftime("%Y-%m")
    peaks = kw_15min.groupby(month_key).max()

    out: dict[str, Decimal] = {}
    for k, v in peaks.items():
        if pd.isna(v):
            continue
        out[str(k)] = Decimal(str(float(v)))
    return out


def blindenergie_kvarh_from_timeseries(
    *,
    df: pd.DataFrame,
    blindenergie_col: str | None = None,
) -> Decimal:
    col = blindenergie_col
    if col is None:
        for c in df.columns:
            if str(c).startswith("Blindenergie"):
                col = str(c)
                break
        if col is None and "blindenergie_kvar" in df.columns:
            col = "blindenergie_kvar"

    if col is None or col not in df.columns:
        return Decimal("0")

    s = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return Decimal(str(float(s.abs().sum())))


_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def load_timeseries_from_duckdb(
    *,
    db_path: str | Path,
    table_name: str,
) -> pd.DataFrame:
    if not _SAFE_IDENTIFIER_RE.match(table_name):
        raise ValueError(f"Unsafe table name: {table_name!r}")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table_name}')").fetchall()]
        select_cols = ["timestamp", "energy_kwh_signed"]
        if "blindenergie_kvar" in cols:
            select_cols.append("blindenergie_kvar")
        cols_sql = ", ".join(select_cols)
        return con.execute(f"SELECT {cols_sql} FROM {table_name}").fetch_df()
    finally:
        con.close()


def calculate_import_cost_from_timeseries_df(
    *,
    year: int,
    df: pd.DataFrame,
    tariff_threshold_kwh: float | Decimal = Decimal("50000"),
    use_total_rp_per_kwh: bool = False,
    include_fixed_charges: bool = True,
    include_demand_charges: bool = True,
    include_blindenergie: bool = True,
    vat_rate: float | Decimal | None = None,
) -> ImportCostRun:
    kwh_import = pd.to_numeric(df["energy_kwh_signed"], errors="coerce").fillna(0).clip(lower=0).sum()
    kwh_import_d = Decimal(str(float(kwh_import)))

    monthly_peak_kw = monthly_peak_kw_from_15min_timeseries(df=df)
    blindenergie_kvarh = blindenergie_kvarh_from_timeseries(df=df)

    breakdown = calculate_import_cost(
        year=year,
        kwh=kwh_import_d,
        monthly_peak_kw=monthly_peak_kw,
        blindenergie_kvarh=blindenergie_kvarh,
        include_blindenergie=include_blindenergie,
        tariff_threshold_kwh=tariff_threshold_kwh,
        use_total_rp_per_kwh=use_total_rp_per_kwh,
        include_fixed_charges=include_fixed_charges,
        include_demand_charges=include_demand_charges,
        vat_rate=vat_rate,
    )

    return ImportCostRun(
        breakdown=breakdown,
        kwh_import=kwh_import_d,
        months=len(monthly_peak_kw),
        monthly_peak_kw=monthly_peak_kw,
        blindenergie_kvarh=blindenergie_kvarh,
    )


def calculate_import_cost_from_duckdb(
    *,
    year: int,
    db_path: str | Path,
    table_name: str,
    tariff_threshold_kwh: float | Decimal = Decimal("50000"),
    use_total_rp_per_kwh: bool = False,
    include_fixed_charges: bool = True,
    include_demand_charges: bool = True,
    include_blindenergie: bool = True,
    vat_rate: float | Decimal | None = None,
) -> ImportCostRun:
    df = load_timeseries_from_duckdb(db_path=db_path, table_name=table_name)
    return calculate_import_cost_from_timeseries_df(
        year=year,
        df=df,
        tariff_threshold_kwh=tariff_threshold_kwh,
        use_total_rp_per_kwh=use_total_rp_per_kwh,
        include_fixed_charges=include_fixed_charges,
        include_demand_charges=include_demand_charges,
        include_blindenergie=include_blindenergie,
        vat_rate=vat_rate,
    )


def calculate_import_cost_2025(
    *,
    kwh: float | Decimal,
    months: int | Decimal | None = None,
    peak_kw: float | Decimal | None = None,
    monthly_peak_kw: dict[str, float | Decimal] | None = None,
    blindenergie_kvarh: float | Decimal | None = None,
    include_blindenergie: bool = True,
    tariff_threshold_kwh: float | Decimal = Decimal("50000"),
    use_total_rp_per_kwh: bool = False,
    include_fixed_charges: bool = True,
    include_demand_charges: bool = True,
    vat_rate: float | Decimal | None = None,
    tariff: ImportTariff2025 | None = None,
) -> ImportCostBreakdown:
    return calculate_import_cost(
        year=2025,
        kwh=kwh,
        months=months,
        peak_kw=peak_kw,
        monthly_peak_kw=monthly_peak_kw,
        blindenergie_kvarh=blindenergie_kvarh,
        include_blindenergie=include_blindenergie,
        tariff_threshold_kwh=tariff_threshold_kwh,
        use_total_rp_per_kwh=use_total_rp_per_kwh,
        include_fixed_charges=include_fixed_charges,
        include_demand_charges=include_demand_charges,
        vat_rate=vat_rate,
        tariff=tariff,
    )


def calculate_import_cost_2026(
    *,
    kwh: float | Decimal,
    months: int | Decimal | None = None,
    peak_kw: float | Decimal | None = None,
    monthly_peak_kw: dict[str, float | Decimal] | None = None,
    blindenergie_kvarh: float | Decimal | None = None,
    include_blindenergie: bool = True,
    tariff_threshold_kwh: float | Decimal = Decimal("50000"),
    use_total_rp_per_kwh: bool = False,
    include_fixed_charges: bool = True,
    include_demand_charges: bool = True,
    vat_rate: float | Decimal | None = None,
    tariff: ImportTariff2026 | None = None,
) -> ImportCostBreakdown:
    return calculate_import_cost(
        year=2026,
        kwh=kwh,
        months=months,
        peak_kw=peak_kw,
        monthly_peak_kw=monthly_peak_kw,
        blindenergie_kvarh=blindenergie_kvarh,
        include_blindenergie=include_blindenergie,
        tariff_threshold_kwh=tariff_threshold_kwh,
        use_total_rp_per_kwh=use_total_rp_per_kwh,
        include_fixed_charges=include_fixed_charges,
        include_demand_charges=include_demand_charges,
        vat_rate=vat_rate,
        tariff=tariff,
    )


def calculate_import_cost(
    *,
    year: int,
    kwh: float | Decimal,
    months: int | Decimal | None = None,
    peak_kw: float | Decimal | None = None,
    monthly_peak_kw: dict[str, float | Decimal] | None = None,
    blindenergie_kvarh: float | Decimal | None = None,
    include_blindenergie: bool = True,
    tariff_threshold_kwh: float | Decimal = Decimal("50000"),
    use_total_rp_per_kwh: bool = False,
    include_fixed_charges: bool = True,
    include_demand_charges: bool = True,
    vat_rate: float | Decimal | None = None,
    tariff: ImportTariff2025 | ImportTariff2026 | None = None,
) -> ImportCostBreakdown:
    t = tariff or select_import_tariff(year=year, kwh=kwh, threshold_kwh=tariff_threshold_kwh)

    kwh_d = kwh if isinstance(kwh, Decimal) else Decimal(str(kwh))
    if kwh_d < 0:
        raise ValueError("kwh must be non-negative for import cost calculation")

    if use_total_rp_per_kwh:
        variable_rp_per_kwh = t.total_rp_per_kwh
        variable_components: dict[str, Decimal] = {"total_rp_per_kwh": t.total_rp_per_kwh}
    else:
        variable_components = t.variable_rp_per_kwh_components()
        variable_rp_per_kwh = t.variable_rp_per_kwh_sum()

    energielieferung_rp = t.energielieferung_rp_per_kwh
    netznutzung_variable_rp = (
        t.netznutzung_einheitstarif_rp_per_kwh
        + t.netznutzung_systemdienstleistungen_swissgrid_rp_per_kwh
        + t.netznutzung_stromreserve_rp_per_kwh
        + t.netznutzung_solidarisierte_kosten_rp_per_kwh
    )
    oeffentliche_abgaben_rp = (
        t.oeffentliche_abgaben_foerderung_erneuerbare_rp_per_kwh + t.oeffentliche_abgaben_gemeinde_rp_per_kwh
    )

    variable_cost_rp = variable_rp_per_kwh * kwh_d
    variable_cost_chf = rp_to_chf(variable_cost_rp)

    blindenergie_kvarh_d = (
        blindenergie_kvarh
        if isinstance(blindenergie_kvarh, Decimal)
        else (Decimal(str(blindenergie_kvarh)) if blindenergie_kvarh is not None else Decimal("0"))
    )
    blindenergie_cost_rp = Decimal("0")
    if include_blindenergie and t.blindenergie_rp_per_kvarh > 0 and blindenergie_kvarh_d != 0:
        blindenergie_cost_rp = t.blindenergie_rp_per_kvarh * blindenergie_kvarh_d
    blindenergie_cost_chf = rp_to_chf(blindenergie_cost_rp)

    monthly_peak_kw_d: dict[str, Decimal] | None = None
    if monthly_peak_kw is not None:
        monthly_peak_kw_d = {
            str(k): (v if isinstance(v, Decimal) else Decimal(str(v))) for k, v in monthly_peak_kw.items()
        }

    if months is None and monthly_peak_kw_d is not None:
        months = len(monthly_peak_kw_d)

    fixed_cost_chf = Decimal("0")
    if include_fixed_charges:
        if months is None:
            raise ValueError("months is required when include_fixed_charges=True")
        months_d = months if isinstance(months, Decimal) else Decimal(str(months))
        fixed_cost_chf = (t.netz_grundtarif_chf_per_month + t.messentgelt_chf_per_month) * months_d

    demand_cost_chf = Decimal("0")
    if include_demand_charges:
        if monthly_peak_kw_d is not None:
            sum_peaks = sum(monthly_peak_kw_d.values(), start=Decimal("0"))
            demand_cost_chf = t.netz_leistungstarif_chf_per_kw_month * sum_peaks
        else:
            if months is None:
                raise ValueError("months is required when include_demand_charges=True")
            if peak_kw is None:
                raise ValueError(
                    "peak_kw (single value) or monthly_peak_kw (per-month peaks) is required when include_demand_charges=True"
                )
            months_d = months if isinstance(months, Decimal) else Decimal(str(months))
            peak_kw_d = peak_kw if isinstance(peak_kw, Decimal) else Decimal(str(peak_kw))
            demand_cost_chf = t.netz_leistungstarif_chf_per_kw_month * peak_kw_d * months_d

    subtotal_chf = variable_cost_chf + fixed_cost_chf + demand_cost_chf + blindenergie_cost_chf

    vat_rate_d: Decimal | None
    if vat_rate is None:
        vat_rate_d = None
    else:
        vat_rate_d = vat_rate if isinstance(vat_rate, Decimal) else Decimal(str(vat_rate))

    vat_amount_chf = Decimal("0")
    if vat_rate_d is not None:
        vat_amount_chf = subtotal_chf * vat_rate_d

    total_chf = subtotal_chf + vat_amount_chf

    details: dict[str, Any] = {
        "variable_components_rp_per_kwh": {k: str(v) for k, v in variable_components.items()},
        "variable_cost_rp": str(variable_cost_rp),
        "fixed_monthly_chf_per_month": str(t.netz_grundtarif_chf_per_month),
        "messentgelt_chf_per_month": str(t.messentgelt_chf_per_month),
        "demand_chf_per_kw_month": str(t.netz_leistungstarif_chf_per_kw_month),
    }

    details["blindenergie"] = {
        "blindenergie_kvarh": str(blindenergie_kvarh_d),
        "blindenergie_rp_per_kvarh": str(t.blindenergie_rp_per_kvarh),
        "blindenergie_cost_rp": str(blindenergie_cost_rp),
        "blindenergie_cost_chf": str(blindenergie_cost_chf),
    }

    details["category_rp_per_kwh"] = {
        "energielieferung": str(energielieferung_rp),
        "netznutzung_variable": str(netznutzung_variable_rp),
        "oeffentliche_abgaben": str(oeffentliche_abgaben_rp),
    }

    details["category_costs_chf"] = {
        "energielieferung": str(rp_to_chf(energielieferung_rp * kwh_d)),
        "netznutzung_variable": str(rp_to_chf(netznutzung_variable_rp * kwh_d)),
        "netznutzung_fixed": str(fixed_cost_chf),
        "netznutzung_demand": str(demand_cost_chf),
        "netznutzung_blindenergie": str(blindenergie_cost_chf),
        "netznutzung_total": str(
            rp_to_chf(netznutzung_variable_rp * kwh_d) + fixed_cost_chf + demand_cost_chf + blindenergie_cost_chf
        ),
        "oeffentliche_abgaben": str(rp_to_chf(oeffentliche_abgaben_rp * kwh_d)),
    }

    if monthly_peak_kw_d is not None:
        details["monthly_peak_kw"] = {k: str(v) for k, v in monthly_peak_kw_d.items()}

    return ImportCostBreakdown(
        kwh=kwh_d,
        variable_rp_per_kwh=variable_rp_per_kwh,
        variable_cost_chf=variable_cost_chf,
        fixed_cost_chf=fixed_cost_chf,
        demand_cost_chf=demand_cost_chf,
        blindenergie_kvarh=blindenergie_kvarh_d,
        blindenergie_cost_chf=blindenergie_cost_chf,
        subtotal_chf=subtotal_chf,
        vat_rate=vat_rate_d,
        vat_amount_chf=vat_amount_chf,
        total_chf=total_chf,
        details=details,
    )
