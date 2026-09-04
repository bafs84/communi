from __future__ import annotations

import pandas as pd
import pytest

from ckw_script.ckw_csv import parse_ckw_csv, parsed_metadata_normalized


def test_parse_ckw_csv_resaved_by_excel() -> None:
    content = (
        "Kunde: Stadt Sursee;\r\n"
        "Kundennummer: 1063304;\r\n"
        "Adresse: Badstrasse 19 C, 6210 Sursee;\r\n"
        "Vertrag: Produktion Simuliert 130kW;\r\n"
        "Zählpunkt: CH1003601234500000000000000007280;\r\n"
        ";\r\n"
        "Datum der Auswertung:22.05.2026;\r\n"
        "Auswertungszeitraum:01.01.2025 - 31.12.2025;\r\n"
        "Einheit:Viertelstunde;\r\n"
        ";\r\n"
        "Zeitraum;Rücklieferung (kWh)\r\n"
        "01.01.25 00:00;0\r\n"
        "01.01.25 00:15;-0.058157898\r\n"
        "Total;-0.058157898"
    ).encode("utf-8-sig")

    parsed = parse_ckw_csv(content)
    metadata = parsed_metadata_normalized(parsed.metadata)

    assert parsed.kind == "export"
    assert parsed.energy_column == "Rücklieferung (kWh)"
    assert metadata["kunde"] == "Stadt Sursee"
    assert metadata["adresse"] == "Badstrasse 19 C, 6210 Sursee"
    assert metadata["zaehlpunkt"] == "CH1003601234500000000000000007280"
    assert parsed.totals["Rücklieferung (kWh)"] == pytest.approx(-0.058157898)
    assert parsed.dataframe["timestamp"].tolist() == [
        pd.Timestamp("2025-01-01 00:00:00"),
        pd.Timestamp("2025-01-01 00:15:00"),
    ]
    assert parsed.dataframe["energy_kwh_signed"].tolist() == pytest.approx([0.0, -0.058157898])
