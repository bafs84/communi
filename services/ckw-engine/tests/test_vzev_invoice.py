from __future__ import annotations

from decimal import Decimal

import pytest

from ckw_script.vzev_invoice import calculate_vzev_invoices, parse_vzev_csv


def _write_vzev_csv(tmp_path, rows: list[str]):
    path = tmp_path / "vzev.csv"
    path.write_bytes("\n".join(rows).encode("cp1252"))
    return path


def _new_reactive_csv_rows() -> list[str]:
    return [
        "Zählpunkt Webbezeichnung;PV: Überschuss PVA;;virtuelles AO vZEV: vZEV Summe Bezug;;virtuelles AO vZEV: vZEV Summe Bezug;;virtuelles AO vZEV: vZEV Summe Überschuss;;Haus A: Allgemein;;Haus B: WP+Allgemein;;Haus B: WP+Allgemein;",
        "Metering-Code;CH1003601234500000000000000000001;;CH1003601234500000000000000000002;;CH1003601234500000000000000000002;;CH1003601234500000000000000000003;;CH1003601234500000000000000000004;;CH1003601234500000000000000000005;;CH1003601234500000000000000000005;",
        "Linie Webbezeichnung;Wirkenergie Total - (Bezug);;Fakturierbar Anteil Blind (Total);;Wirkenergie Total + (Abgabe);;Wirkenergie Total - (Bezug);;Wirkenergie Total + (Abgabe);;Fakturierbar Anteil Blind (Total);;Wirkenergie Total + (Abgabe);",
        "Obis-Code;1-5:2.29.0*255;;1-5:132.29.0*255;;1-5:1.29.0*255;;1-5:2.29.0*255;;1-5:1.29.0*255;;1-5:132.29.0*255;;1-5:1.29.0*255;",
        "Einheit;kWh;;kvarh;;kWh;;kWh;;kWh;;kvarh;;kWh;",
        ";0;;0;;0;;0;;0;;0;;0;",
        "01.03.2026 00:15;0;W;4.4;W;2.0;W;0;W;1.0;W;4.4;W;3.0;W",
        "01.03.2026 00:30;0;W;5.6;W;4.0;W;0;W;2.0;W;5.6;W;4.0;W",
    ]


def _old_manual_blindenergie_csv_rows() -> list[str]:
    return [
        "Zählpunkt Webbezeichnung;PV: Überschuss PVA;;virtuelles AO vZEV: vZEV Summe Bezug;;virtuelles AO vZEV: vZEV Summe Überschuss;;Haus A: Allgemein;;Haus B: WP+Allgemein;",
        "Metering-Code;CH1003601234500000000000000000001;;CH1003601234500000000000000000002;;CH1003601234500000000000000000003;;CH1003601234500000000000000000004;;CH1003601234500000000000000000005;",
        "Linie Webbezeichnung;Wirkenergie Total - (Bezug);;Wirkenergie Total + (Abgabe);;Wirkenergie Total - (Bezug);;Wirkenergie Total + (Abgabe);;Wirkenergie Total + (Abgabe);",
        "Obis-Code;1-5:2.29.0*255;;1-5:1.29.0*255;;1-5:2.29.0*255;;1-5:1.29.0*255;;1-5:1.29.0*255;",
        "Einheit;kWh;;kWh;;kWh;;kWh;;kWh;",
        ";0;;0;;0;;0;;0;",
        "01.03.2026 00:15;0;W;2.0;W;0;W;1.0;W;3.0;W",
        "01.03.2026 00:30;0;W;4.0;W;0;W;2.0;W;4.0;W",
    ]


def test_parse_new_ckw_export_keeps_active_and_reactive_duplicate_labels_separate(tmp_path) -> None:
    parsed = parse_vzev_csv(_write_vzev_csv(tmp_path, _new_reactive_csv_rows()))

    assert parsed.community_import.unit == "kWh"
    assert parsed.community_reactive is not None
    assert parsed.community_reactive.unit == "kvarh"
    assert len(parsed.consumers) == 2
    assert len(parsed.consumer_reactive) == 1
    assert [meter.display_name for meter in parsed.consumers] == [
        "Haus A: Allgemein",
        "Haus B: WP+Allgemein",
    ]
    assert parsed.community_import.column_name != parsed.community_reactive.column_name
    assert parsed.consumers[1].column_name != parsed.consumer_reactive[0].column_name


def test_calculate_vzev_invoices_uses_csv_reactive_energy_for_source_meter(tmp_path) -> None:
    parsed = parse_vzev_csv(_write_vzev_csv(tmp_path, _new_reactive_csv_rows()))

    result = calculate_vzev_invoices(parsed, tariff_tier="gt_50000", vat_rate=None)
    by_meter = {consumer["metering_code"][-1]: consumer for consumer in result["consumers"]}

    assert result["blindenergie_source"] == "csv_community_reactive"
    assert result["blindenergie_allocation_mode"] == "reactive_meter"
    assert result["aggregated_invoice"]["blindenergie_kvarh"] == pytest.approx(10.0)
    assert result["aggregated_invoice"]["blindenergie_chf"] == pytest.approx(0.45)
    assert by_meter["4"]["blindenergie_chf"] == pytest.approx(0.0)
    assert by_meter["5"]["billing_blindenergie_kvarh"] == pytest.approx(10.0)
    assert by_meter["5"]["blindenergie_chf"] == pytest.approx(0.45)
    assert by_meter["4"]["standard_product_reference"]["product_name"] == "CKW ClassicStrom"
    assert by_meter["4"]["standard_product_reference"]["energy_rp_per_kwh"] == pytest.approx(12.0)
    assert by_meter["4"]["standard_product_reference"]["meter_type"] == "direct"
    assert by_meter["5"]["standard_product_reference"]["meter_type"] == "indirect"
    assert by_meter["4"]["local_energy_rp_per_kwh"] == pytest.approx(
        by_meter["4"]["standard_product_reference_rp_per_kwh"] * 0.8,
        abs=0.02,
    )


def test_calculate_vzev_invoices_keeps_manual_blindenergie_pro_rata_fallback(tmp_path) -> None:
    parsed = parse_vzev_csv(_write_vzev_csv(tmp_path, _old_manual_blindenergie_csv_rows()))

    result = calculate_vzev_invoices(
        parsed,
        tariff_tier="gt_50000",
        blindenergie_kvarh=Decimal("10"),
        vat_rate=None,
    )

    assert result["blindenergie_source"] == "manual"
    assert result["blindenergie_allocation_mode"] == "grid_import_pro_rata"
    assert result["aggregated_invoice"]["blindenergie_chf"] == pytest.approx(0.45)
    assert sum(consumer["blindenergie_chf"] for consumer in result["consumers"]) == pytest.approx(0.45)
    assert all(consumer["blindenergie_chf"] > 0 for consumer in result["consumers"])
