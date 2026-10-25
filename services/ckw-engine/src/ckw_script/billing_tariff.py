"""Confirmed contract tariffs for invoices, separate from simulation estimates."""
from __future__ import annotations

from typing import Any


INVOICE_TARIFF_TIERS = ("lt_50000", "gt_50000")
INVOICE_TARIFF_REQUIRED = (
    "Bitte die Tarifklasse laut CKW-Vertrag auswählen. "
    "Sie gilt für alle Monate des Rechnungs-Batches; automatische Tarifwahl ist für Rechnungen nicht zulässig."
)
TARIFF_LABELS = {
    "lt_50000": "bis 50'000 kWh/Jahr",
    "gt_50000": "über 50'000 kWh/Jahr (LG7)",
}


def require_invoice_tariff_tier(value: object) -> str:
    if not isinstance(value, str) or value not in INVOICE_TARIFF_TIERS:
        raise ValueError(INVOICE_TARIFF_REQUIRED)
    return value


def invoice_tariff_metadata(result: dict[str, Any]) -> dict[str, Any]:
    tier = require_invoice_tariff_tier(result.get("selected_tariff_tier"))
    if result.get("tariff_basis_source") != f"explicit_{tier}":
        raise ValueError(INVOICE_TARIFF_REQUIRED)
    return {
        "selected_tariff_tier": tier,
        "tariff_year": result["tariff_year"],
        "tariff_basis_source": result["tariff_basis_source"],
        "tariff_selection_source": "contract_confirmed",
    }


def invoice_tariff_label(result: dict[str, Any]) -> str:
    metadata = invoice_tariff_metadata(result)
    return f"{TARIFF_LABELS[metadata['selected_tariff_tier']]} (vertraglich gewählt)"
