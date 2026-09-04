from __future__ import annotations

"""CKW export-energy and HKN compensation used in community reconciliation."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


MONEY_QUANT = Decimal("0.01")
KWH_BILLING_QUANT = Decimal("1")
PAYMENT_QUANT = Decimal("0.05")


# BFE reference market prices published by CKW, in Rp./kWh. The reference
# price is quarterly; statutory minimum compensation can supersede it for
# individual plants depending on installed capacity.
REFERENCE_MARKET_PRICE_RP_PER_KWH: dict[tuple[int, int], Decimal] = {
    (2025, 1): Decimal("10.380"),
    (2025, 2): Decimal("2.759"),
    (2025, 3): Decimal("5.731"),
    (2025, 4): Decimal("9.508"),
    (2026, 1): Decimal("10.266"),
    (2026, 2): Decimal("3.896"),
}
DEFAULT_HKN_RP_PER_KWH = Decimal("2.00")


@dataclass(frozen=True)
class FeedInTariff:
    year: int
    quarter: int
    energy_rp_per_kwh: Decimal | None
    hkn_rp_per_kwh: Decimal
    energy_rate_source: str
    hkn_rate_source: str
    warning: str | None


def _quarter(period_start: str) -> tuple[int, int]:
    timestamp = datetime.fromisoformat(period_start)
    return timestamp.year, ((timestamp.month - 1) // 3) + 1


def select_feed_in_tariff(
    *,
    period_start: str,
    energy_rp_per_kwh: Decimal | None = None,
    hkn_rp_per_kwh: Decimal | None = None,
) -> FeedInTariff:
    year, quarter = _quarter(period_start)
    published_reference = REFERENCE_MARKET_PRICE_RP_PER_KWH.get((year, quarter))

    if energy_rp_per_kwh is not None:
        energy_rate = energy_rp_per_kwh
        energy_source = "Manuelle Eingabe"
        warning = None
    elif published_reference is not None:
        energy_rate = published_reference
        energy_source = f"BFE Referenz-Marktpreis {year} Q{quarter}, publiziert durch CKW"
        warning = (
            "Die gesetzliche Mindestvergütung kann den Referenz-Marktpreis je PV-Anlage ersetzen. "
            "Falls CKW einen anderen Satz ausweist, muss er im Abrechnungsformular überschrieben werden."
        )
    else:
        energy_rate = None
        energy_source = f"Für {year} Q{quarter} noch kein hinterlegter Referenz-Marktpreis"
        warning = "Die Energie-Rückliefervergütung ist nicht enthalten; bitte den CKW-Satz manuell eingeben."

    if hkn_rp_per_kwh is None:
        hkn_rate = DEFAULT_HKN_RP_PER_KWH
        hkn_source = "CKW R HKN, Standardannahme PV bis 100 kVA"
    else:
        hkn_rate = hkn_rp_per_kwh
        hkn_source = "Manuelle Eingabe"

    return FeedInTariff(
        year=year,
        quarter=quarter,
        energy_rp_per_kwh=energy_rate,
        hkn_rp_per_kwh=hkn_rate,
        energy_rate_source=energy_source,
        hkn_rate_source=hkn_source,
        warning=warning,
    )


def calculate_feed_in_compensation(
    *,
    excess_kwh: Decimal,
    gross_ckw_total_chf: Decimal,
    vat_rate: Decimal | None,
    tariff: FeedInTariff,
    ckw_invoice_rounding: bool,
) -> dict[str, object]:
    billing_excess_kwh = (
        excess_kwh.quantize(KWH_BILLING_QUANT, rounding=ROUND_HALF_UP)
        if ckw_invoice_rounding
        else excess_kwh
    )
    energy_rate = tariff.energy_rp_per_kwh or Decimal("0")
    energy_chf = (billing_excess_kwh * energy_rate / Decimal("100")).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    hkn_chf = (billing_excess_kwh * tariff.hkn_rp_per_kwh / Decimal("100")).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_UP
    )
    subtotal_chf = energy_chf + hkn_chf
    vat_chf = (
        (subtotal_chf * vat_rate).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if vat_rate is not None
        else Decimal("0")
    )
    total_chf = subtotal_chf + vat_chf
    net_before_rounding_chf = gross_ckw_total_chf - total_chf
    net_payable_chf = (
        (net_before_rounding_chf / PAYMENT_QUANT).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        * PAYMENT_QUANT
    )
    payment_rounding_chf = net_payable_chf - net_before_rounding_chf

    return {
        "excess_pv_kwh": float(excess_kwh),
        "billing_excess_pv_kwh": float(billing_excess_kwh),
        "energy_rp_per_kwh": float(energy_rate),
        "hkn_rp_per_kwh": float(tariff.hkn_rp_per_kwh),
        "energy_revenue_chf": float(energy_chf),
        "hkn_revenue_chf": float(hkn_chf),
        "subtotal_chf": float(subtotal_chf),
        "vat_chf": float(vat_chf),
        "total_chf": float(total_chf),
        "gross_ckw_total_chf": float(gross_ckw_total_chf),
        "net_ckw_payable_before_rounding_chf": float(net_before_rounding_chf),
        "payment_rounding_chf": float(payment_rounding_chf),
        "net_ckw_payable_chf": float(net_payable_chf),
        "energy_rate_source": tariff.energy_rate_source,
        "hkn_rate_source": tariff.hkn_rate_source,
        "energy_rate_available": tariff.energy_rp_per_kwh is not None,
        "warning": tariff.warning,
    }
