from __future__ import annotations

from decimal import Decimal

import pytest

from ckw_script.portal_analysis import (
    FLOW_GRID_IMPORT,
    FLOW_LOCAL_PV_CONSUMPTION,
    FLOW_PV_EXCESS,
    FLOW_PV_PRODUCTION,
    FLOW_ZEV_AGGREGATE,
    add_cached_project_plant,
    add_manual_project_plant,
    adopt_automatic_project_plants,
    analyze_battery_placements,
    analyze_load_shifting,
    analyze_residual_solar,
    calculate_leg_potential,
    clear_project_plant_selection,
    connect_portal_db,
    create_project,
    delete_project,
    delete_portal_file,
    delete_selected_project_plant,
    get_project,
    ingest_portal_file,
    kwp_to_ampere,
    list_projects,
    match_project_plants,
    optimize_leg_combinations,
    project_timeseries,
    set_pv_battery_consumers,
    summarize_project,
)
from ckw_script.portal_municipalities import validate_ckw_municipality


def _ckw_csv(
    *,
    contract: str,
    column: str,
    values: list[float],
    address: str = "Bahnhofstrasse 1, 6210 Sursee",
    start_hour: int = 0,
) -> bytes:
    rows = [
        "Kunde: Test AG",
        "Kundennummer: 1",
        f"Adresse: {address}",
        f"Vertrag: {contract}",
        "Zaehlpunkt: CH1003601234500000000000000000001",
        ";;",
        "Datum der Auswertung:01.01.2026",
        "Auswertungszeitraum:01.01.2025 - 01.01.2025",
        "Einheit:Viertelstunde",
        "",
        f"Zeitraum;{column}",
    ]
    for index, value in enumerate(values):
        minute = index * 15
        rows.append(f"01.01.2025 {start_hour:02d}:{minute:02d};{value}")
    return "\n".join(rows).encode("utf-8")


def _ckw_zev_csv() -> bytes:
    return "\n".join(
        [
            "Zeitstempel;Energiebedarf_total_kWh;Rueckspeissung_total_kWh;Bezug_total_kWh;Produktion_kWh;"
            "Eigenverbrauch_%;Produktion_total_kWh;Energiebedarf_Hochtarif_kWh;"
            "Energiebedarf_Niedertarif_kWh;Produktion_Hochtarif_kWh;Produktion_Niedertarif_kWh",
            "2025-01-01T00:00+01:00;2.0;0.5;1.0;1.5;0.5;1.0;1.0;0;1.0;0",
            "2025-01-01T00:15+01:00;2.0;0;0;2.0;1.0;2.0;0;0;2.0;0",
        ]
    ).encode("utf-8")


def test_validate_ckw_municipality() -> None:
    assert validate_ckw_municipality("Sursee").name == "Sursee"
    with pytest.raises(ValueError):
        validate_ckw_municipality("Zuerich")


def test_ingest_excess_and_calculate_leg_potential(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)

    solar = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[1.0, 2.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    assert solar.flow_kind == FLOW_PV_EXCESS
    assert solar.rows_inserted == 2
    assert solar.warnings

    summary = summarize_project(project_id=project["project_id"], db_path=db_path)
    assert summary["totals"]["pv_excess_kwh"] == pytest.approx(3.0)
    assert summary["totals"]["first_estimated_local_solar_kwh"] == pytest.approx(2.5)

    result = calculate_leg_potential(
        project_id=project["project_id"],
        tariff_year=2025,
        network_discount=Decimal("0.20"),
        db_path=db_path,
    )
    assert result["totals"]["local_solar_consumed_kwh"] == pytest.approx(2.5)
    assert result["totals"]["remaining_grid_import_kwh"] == pytest.approx(1.0)
    assert result["rates"]["leg_energy_price_rp_per_kwh"] == pytest.approx(8.5)
    assert result["rates"]["discountable_netznutzung_rp_per_kwh"] == pytest.approx(4.4)
    assert result["totals"]["energy_savings_chf"] == pytest.approx(0.0)
    assert result["totals"]["network_discount_chf"] == pytest.approx(0.022)
    assert result["totals"]["local_solar_energy_value_chf"] == pytest.approx(0.2125)
    assert result["totals"]["producer_revenue_chf"] == pytest.approx(0.2125)
    assert result["totals"]["total_monetary_benefit_chf"] == pytest.approx(0.022)

    cheaper_local_energy = calculate_leg_potential(
        project_id=project["project_id"],
        tariff_year=2025,
        network_discount=Decimal("0.20"),
        leg_energy_price_rp_per_kwh=Decimal("6.00"),
        db_path=db_path,
    )
    assert cheaper_local_energy["totals"]["producer_revenue_chf"] == pytest.approx(0.15)
    assert cheaper_local_energy["totals"]["energy_price_savings_chf"] == pytest.approx(0.0625)
    assert cheaper_local_energy["totals"]["consumer_total_savings_chf"] == pytest.approx(0.0845)


def test_ingest_simulated_production_with_ckw_export_column_as_full_pv(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Simulated PV", municipality="Sursee", db_path=db_path)

    solar = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="2025_badstrasse 19c Produktion PVA simuliert 130kW.csv",
        content=_ckw_csv(
            contract="Produktion Simuliert 130kW",
            column="Rücklieferung (kWh)",
            values=[-1.25, -2.75],
            address="Badstrasse 19 C, 6210 Sursee",
        ),
        upload_kind="solar",
        db_path=db_path,
    )

    assert solar.flow_kind == FLOW_PV_PRODUCTION
    assert solar.total_kwh == pytest.approx(4.0)
    assert solar.warnings == [
        "Die simulierte Solarzeitreihe wird trotz Spaltenname Rücklieferung als gesamte PV-Produktion behandelt."
    ]

    summary = summarize_project(project_id=project["project_id"], db_path=db_path)
    assert summary["totals"]["pv_production_kwh"] == pytest.approx(4.0)
    assert summary["totals"]["pv_excess_kwh"] == pytest.approx(0.0)


def test_empty_project_returns_a_complete_zero_calculation_contract(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Empty project", municipality="Sursee", db_path=db_path)

    result = calculate_leg_potential(
        project_id=project["project_id"],
        tariff_year=2026,
        network_discount=Decimal("0.40"),
        leg_energy_price_rp_per_kwh=Decimal("6.50"),
        db_path=db_path,
    )

    assert result["pricing_scope"] == "empty_project"
    assert result["rates"]["leg_energy_price_rp_per_kwh"] == pytest.approx(6.5)
    assert result["rates"]["network_discount"] == pytest.approx(0.4)
    assert result["totals"]["local_solar_consumed_kwh"] == 0.0
    assert result["totals"]["consumer_total_savings_chf"] == 0.0
    assert result["monthly"] == []
    assert result["participants"] == []
    assert "Es sind noch keine Messwerte hochgeladen." in result["warnings"]


def test_analyze_residual_solar_describes_season_time_of_day_and_peak_intervals(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Residual solar", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(
            contract="Ueberschuss PVA",
            column="Rücklieferung (kWh)",
            values=[1.0, 2.0],
            start_hour=6,
        ),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3.0], start_hour=6),
        upload_kind="consumption",
        db_path=db_path,
    )

    result = analyze_residual_solar(project_id=project["project_id"], db_path=db_path)

    assert result["totals"]["available_solar_kwh"] == pytest.approx(3.0)
    assert result["totals"]["local_solar_kwh"] == pytest.approx(2.5)
    assert result["totals"]["residual_solar_kwh"] == pytest.approx(0.5)
    assert result["totals"]["residual_solar_pct"] == pytest.approx(1 / 6)
    assert result["totals"]["residual_interval_count"] == 1
    assert result["totals"]["residual_interval_pct"] == pytest.approx(0.5)
    assert result["peak_interval"]["timestamp"] == "2025-01-01T06:00:00"
    assert result["peak_interval"]["residual_solar_kwh"] == pytest.approx(0.5)
    assert result["monthly"][0]["residual_solar_kwh"] == pytest.approx(0.5)
    assert result["hourly"][0]["label"] == "06:00"
    assert result["hourly"][0]["average_residual_when_present_kwh"] == pytest.approx(0.5)
    assert result["heatmap"]["year"] == 2025
    assert len(result["heatmap"]["days"]) == 365
    assert result["heatmap"]["hours"] == [f"{hour:02d}:00" for hour in range(6, 23)]
    assert result["heatmap"]["scale_max_residual_solar_kwh"] == pytest.approx(0.5)
    assert result["heatmap"]["selected_peak"] == {
        "date": "2025-01-01",
        "hour": 6,
        "residual_solar_kwh": pytest.approx(0.5),
    }
    first_heatmap_cell = result["heatmap"]["values"][0]
    assert first_heatmap_cell == {
        "date": "2025-01-01",
        "hour": 6,
        "residual_solar_kwh": pytest.approx(0.5),
        "interval_count": 2,
    }
    assert result["peak_intervals"][0] == result["peak_interval"]


def test_analyze_residual_solar_can_filter_any_combination_of_pv_sources(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Filtered residual solar", municipality="Sursee", db_path=db_path)
    first_solar = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar-one.csv",
        content=_ckw_csv(
            contract="PVA Dach Ost",
            column="Rücklieferung (kWh)",
            values=[1.0, 2.0],
            start_hour=6,
        ),
        upload_kind="solar",
        db_path=db_path,
    )
    second_solar = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar-two.csv",
        content=_ckw_csv(
            contract="PVA Dach West",
            column="Rücklieferung (kWh)",
            values=[4.0, 0.0],
            address="Werkstrasse 2, 6210 Sursee",
            start_hour=6,
        ),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3.0], start_hour=6),
        upload_kind="consumption",
        db_path=db_path,
    )

    all_sources = analyze_residual_solar(project_id=project["project_id"], db_path=db_path)
    east_only = analyze_residual_solar(
        project_id=project["project_id"],
        solar_file_ids=[first_solar.file_id],
        db_path=db_path,
    )
    west_only = analyze_residual_solar(
        project_id=project["project_id"],
        solar_file_ids=[second_solar.file_id],
        db_path=db_path,
    )
    both_sources = analyze_residual_solar(
        project_id=project["project_id"],
        solar_file_ids=[first_solar.file_id, second_solar.file_id],
        db_path=db_path,
    )

    assert {source["label"] for source in all_sources["sources"]} == {"PVA Dach Ost", "PVA Dach West"}
    assert all_sources["selected_solar_file_ids"] is None
    assert all_sources["totals"]["residual_solar_kwh"] == pytest.approx(4.5)
    assert east_only["totals"]["available_solar_kwh"] == pytest.approx(3.0)
    assert east_only["totals"]["residual_solar_kwh"] == pytest.approx(0.9)
    assert west_only["selected_solar_file_ids"] == [second_solar.file_id]
    assert west_only["totals"]["available_solar_kwh"] == pytest.approx(4.0)
    assert west_only["totals"]["residual_solar_kwh"] == pytest.approx(3.6)
    assert all_sources["heatmap"]["scale_max_residual_solar_kwh"] == pytest.approx(4.5)
    assert east_only["heatmap"]["scale_max_residual_solar_kwh"] == pytest.approx(4.5)
    assert west_only["heatmap"]["scale_max_residual_solar_kwh"] == pytest.approx(4.5)
    assert east_only["heatmap"]["selected_peak"] == {
        "date": "2025-01-01",
        "hour": 6,
        "residual_solar_kwh": pytest.approx(0.9),
    }
    assert west_only["heatmap"]["selected_peak"] == {
        "date": "2025-01-01",
        "hour": 6,
        "residual_solar_kwh": pytest.approx(3.6),
    }
    assert (
        east_only["totals"]["residual_solar_kwh"] + west_only["totals"]["residual_solar_kwh"]
        == pytest.approx(all_sources["totals"]["residual_solar_kwh"])
    )
    assert both_sources["totals"]["residual_solar_kwh"] == pytest.approx(all_sources["totals"]["residual_solar_kwh"])


def test_analyze_battery_placements_applies_both_cases_to_uploaded_streams(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Battery placement", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="PVA Dach", column="Produktion (kWh)", values=[20.0, 0.0, 0.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    consumer = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Werkstatt", column="Energieverbrauch (kWh)", values=[0.0, 2.5, 10.0]),
        upload_kind="consumption",
        db_path=db_path,
    )
    unrelated_consumer = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="unrelated-load.csv",
        content=_ckw_csv(contract="Nicht verbunden", column="Energieverbrauch (kWh)", values=[20.0, 0.0, 0.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    result = analyze_battery_placements(project_id=project["project_id"], tariff_year=2025, db_path=db_path)

    assert result["battery"] == {
        "capacity_kwh": 100.0,
        "max_charge_kw": 30.0,
        "max_discharge_kw": 30.0,
    }
    assert result["best_pv_site"] is None
    assert result["pv_sites"][0]["configured"] is False
    assert result["pv_sites"][0]["additional_local_solar_kwh"] == pytest.approx(0.0)
    assert {consumer["file_id"] for consumer in result["consumer_options"]} == {
        consumer.file_id,
        unrelated_consumer.file_id,
    }
    assert result["best_consumption_site"]["label"] == "Werkstatt"
    assert result["best_consumption_site"]["baseline_peak_kw_month_sum"] == pytest.approx(40.0)
    assert result["best_consumption_site"]["battery_peak_kw_month_sum"] == pytest.approx(50 / 3)
    assert result["best_consumption_site"]["demand_rate_chf_per_kw_month"] == pytest.approx(9.0)
    assert result["best_consumption_site"]["demand_charge_savings_chf"] == pytest.approx(210.0)

    configured = set_pv_battery_consumers(
        project_id=project["project_id"],
        solar_file_id=result["pv_sites"][0]["file_id"],
        consumer_file_ids=[consumer.file_id],
        tariff_year=2025,
        db_path=db_path,
    )

    assert configured["best_pv_site"]["label"] == "PVA Dach"
    assert configured["best_pv_site"]["selected_consumer_file_ids"] == [consumer.file_id]
    assert configured["best_pv_site"]["assigned_consumers"][0]["label"] == "Werkstatt"
    assert configured["best_pv_site"]["additional_local_solar_kwh"] == pytest.approx(7.5)
    assert configured["best_pv_site"]["remaining_export_kwh"] == pytest.approx(12.5)

    delete_portal_file(project_id=project["project_id"], file_id=consumer.file_id, db_path=db_path)
    after_delete = analyze_battery_placements(project_id=project["project_id"], tariff_year=2025, db_path=db_path)
    assert after_delete["best_pv_site"] is None
    assert after_delete["pv_sites"][0]["selected_consumer_file_ids"] == []


def test_analyze_load_shifting_ranks_night_demand_and_filters_heatmap(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Load shifting", municipality="Sursee", db_path=db_path)
    boiler = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="boiler.csv",
        content=_ckw_csv(
            contract="Boiler",
            column="Energieverbrauch (kWh)",
            values=[1.0, 2.0],
            start_hour=22,
        ),
        upload_kind="consumption",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="office.csv",
        content=_ckw_csv(
            contract="Büro",
            column="Energieverbrauch (kWh)",
            values=[4.0],
            start_hour=8,
        ),
        upload_kind="consumption",
        db_path=db_path,
    )

    all_profiles = analyze_load_shifting(project_id=project["project_id"], db_path=db_path)
    boiler_only = analyze_load_shifting(
        project_id=project["project_id"],
        consumption_file_id=boiler.file_id,
        db_path=db_path,
    )

    assert all_profiles["years"] == [2025]
    assert all_profiles["selected_year"] == 2025
    assert all_profiles["selected_consumption_file_id"] is None
    assert all_profiles["totals"] == {
        "annual_demand_kwh": pytest.approx(7.0),
        "nighttime_demand_kwh": pytest.approx(3.0),
        "nighttime_share": pytest.approx(3 / 7),
        "daytime_demand_kwh": pytest.approx(4.0),
        "profile_count": 2,
    }
    assert [profile["label"] for profile in all_profiles["profiles"]] == ["Boiler", "Büro"]
    assert all_profiles["profiles"][0]["average_nightly_demand_kwh"] == pytest.approx(3.0)
    assert len(all_profiles["heatmap"]["days"]) == 365
    assert all_profiles["heatmap"]["hours"] == [f"{hour:02d}:00" for hour in range(24)]
    assert boiler_only["selected_consumption_file_id"] == boiler.file_id
    assert boiler_only["totals"]["annual_demand_kwh"] == pytest.approx(3.0)
    assert boiler_only["heatmap"]["selected_peak"] == {
        "date": "2025-01-01",
        "hour": 22,
        "demand_kwh": pytest.approx(3.0),
    }
    assert boiler_only["heatmap"]["values"][22] == {
        "date": "2025-01-01",
        "hour": 22,
        "demand_kwh": pytest.approx(3.0),
        "interval_count": 2,
    }


def test_analyze_load_shifting_includes_zev_consumption_profile(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="ZEV load shifting", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="zev.csv",
        content=_ckw_zev_csv(),
        upload_kind="zev",
        db_path=db_path,
    )

    result = analyze_load_shifting(project_id=project["project_id"], db_path=db_path)

    assert result["totals"]["annual_demand_kwh"] == pytest.approx(4.0)
    assert result["totals"]["nighttime_demand_kwh"] == pytest.approx(4.0)
    assert result["profiles"][0]["label"] == "CKW ZEV Verbrauch: zev.csv"
    assert result["profiles"][0]["is_zev"] is True


def test_duplicate_upload_is_ignored_in_summary_and_project_count(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    content = _ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[1.0, 1.0])

    first = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=content,
        upload_kind="consumption",
        db_path=db_path,
    )
    second = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load-copy.csv",
        content=content,
        upload_kind="consumption",
        db_path=db_path,
    )

    summary = summarize_project(project_id=project["project_id"], db_path=db_path)
    projects = list_projects(db_path=db_path)

    assert first.rows_inserted == 2
    assert second.duplicate is True
    assert second.rows_inserted == 0
    assert summary["totals"]["consumption_kwh"] == pytest.approx(2.0)
    assert len(summary["files"]) == 1
    assert projects[0]["file_count"] == 1


def test_ingest_zev_aggregate_preserves_measured_flows(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="ZEV", municipality="Sursee", db_path=db_path)

    result = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="zev.csv",
        content=_ckw_zev_csv(),
        upload_kind="zev",
        db_path=db_path,
    )
    summary = summarize_project(project_id=project["project_id"], db_path=db_path)
    zev_rows = project_timeseries(
        project_id=project["project_id"],
        kind="zev",
        resolution="15min",
        db_path=db_path,
    )
    calculation = calculate_leg_potential(project_id=project["project_id"], tariff_year=2025, db_path=db_path)
    battery_placements = analyze_battery_placements(project_id=project["project_id"], tariff_year=2025, db_path=db_path)

    assert result.flow_kind == FLOW_ZEV_AGGREGATE
    assert result.upload_kind == "zev"
    assert result.rows_inserted == 10
    assert result.total_kwh == pytest.approx(4.0)
    assert summary["totals"]["consumption_kwh"] == pytest.approx(4.0)
    assert summary["totals"]["pv_production_kwh"] == pytest.approx(3.5)
    assert summary["totals"]["pv_excess_kwh"] == pytest.approx(0.5)
    assert summary["totals"][FLOW_GRID_IMPORT + "_kwh"] == pytest.approx(1.0)
    assert summary["totals"][FLOW_LOCAL_PV_CONSUMPTION + "_kwh"] == pytest.approx(3.0)
    assert zev_rows[0][FLOW_GRID_IMPORT] == pytest.approx(1.0)
    assert zev_rows[0][FLOW_LOCAL_PV_CONSUMPTION] == pytest.approx(1.0)
    assert calculation["totals"]["available_solar_kwh"] == pytest.approx(0.5)
    assert calculation["totals"]["consumption_kwh"] == pytest.approx(1.0)
    assert calculation["totals"]["local_solar_consumed_kwh"] == pytest.approx(0.5)
    zev_flow = summary["zev_flow"]
    node_ids = {node["id"] for node in zev_flow["nodes"]}
    assert node_ids == {"zev-production", "zev-grid-import", "zev-consumption", "zev-grid-export"}
    edges = {edge["id"]: edge for edge in zev_flow["edges"]}
    assert edges["zev-production-consumption"]["label"] == "3 kWh"
    assert edges["zev-grid-import-consumption"]["label"] == "1 kWh"
    assert edges["zev-production-grid-export"]["label"] == "0 kWh"
    assert battery_placements["pv_sites"][0]["label"] == "CKW ZEV Überschuss: zev.csv"
    assert battery_placements["pv_sites"][0]["configured"] is True
    assert battery_placements["pv_sites"][0]["assignment_mode"] == "implicit_zev"
    assert battery_placements["consumption_sites"][0]["label"] == "CKW ZEV Restbezug: zev.csv"


def test_optimize_leg_combinations_includes_zev_surplus_and_residual_demand(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="ZEV optimization", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="zev.csv",
        content=_ckw_zev_csv(),
        upload_kind="zev",
        db_path=db_path,
    )

    result = optimize_leg_combinations(project_id=project["project_id"], tariff_year=2025, db_path=db_path)

    assert result["combination_count"] == 1
    assert result["best"]["consumption_kwh"] == pytest.approx(1.0)
    assert result["best"]["local_solar_kwh"] == pytest.approx(0.5)
    assert result["best"]["residual_solar_kwh"] == pytest.approx(0.0)
    assert result["best"]["members"][0]["label"] == "CKW ZEV Restbezug: zev.csv"
    assert result["best"]["solar_sources"][0]["label"] == "CKW ZEV Überschuss: zev.csv"
    node_ids = {node["id"] for node in result["best"]["flow"]["nodes"]}
    assert "solar-source-0" in node_ids
    assert "participant-0" in node_ids
    assert "zev-production" in node_ids
    assert "zev-consumption" in node_ids
    assert "zev-grid-import" not in node_ids
    assert "zev-grid-export" not in node_ids
    edges = {edge["id"]: edge for edge in result["best"]["flow"]["edges"]}
    assert edges["zev-production-consumption"]["label"] == "3 kWh"
    assert edges["zev-production-solar-source-0"]["label"] == "0 kWh"
    assert edges["participant-0-zev-consumption"]["label"] == "1 kWh"


def test_optimize_leg_combinations_adds_zev_flows_to_classic_sources_and_consumers(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Mixed optimization", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="zev.csv",
        content=_ckw_zev_csv(),
        upload_kind="zev",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="production.csv",
        content=_ckw_csv(contract="PVA", column="Produktion (kWh)", values=[1.0, 1.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Load", column="Energieverbrauch (kWh)", values=[1.0, 0.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    result = optimize_leg_combinations(project_id=project["project_id"], tariff_year=2025, db_path=db_path)

    assert result["best"]["participant_count"] == 2
    assert result["best"]["consumption_kwh"] == pytest.approx(2.0)
    assert result["best"]["local_solar_kwh"] == pytest.approx(1.5)
    assert sum(source["available_solar_kwh"] for source in result["best"]["solar_sources"]) == pytest.approx(2.5)
    assert {member["label"] for member in result["best"]["members"]} == {"Load", "CKW ZEV Restbezug: zev.csv"}


def test_summary_warns_when_mixed_pv_sources_make_raw_totals_incomparable(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Mixed PV", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="zev.csv",
        content=_ckw_zev_csv(),
        upload_kind="zev",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="additional-excess.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[2.0, 2.0]),
        upload_kind="solar",
        db_path=db_path,
    )

    summary = summarize_project(project_id=project["project_id"], db_path=db_path)

    assert summary["totals"]["pv_production_kwh"] == pytest.approx(3.5)
    assert summary["totals"]["pv_excess_kwh"] == pytest.approx(4.5)
    assert (
        "Der ausgewiesene PV-Überschuss ist höher als die ausgewiesene PV-Produktion, weil das Projekt "
        "Brutto-PV-Zeitreihen und PV-Überschuss-Zeitreihen aus unterschiedlichen Messquellen enthält. "
        "Die Rohwerte sind nicht direkt vergleichbar."
    ) in summary["warnings"]


def test_delete_portal_file_removes_measurements_and_recalculates_savings(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    other_project = create_project(name="Other", municipality="Sursee", db_path=db_path)
    solar = ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[1.0, 2.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    before = calculate_leg_potential(project_id=project["project_id"], tariff_year=2025, db_path=db_path)
    assert before["totals"]["local_solar_consumed_kwh"] == pytest.approx(2.5)
    assert before["totals"]["total_monetary_benefit_chf"] > 0

    with pytest.raises(KeyError):
        delete_portal_file(
            project_id=other_project["project_id"],
            file_id=solar.file_id,
            db_path=db_path,
        )

    deleted = delete_portal_file(project_id=project["project_id"], file_id=solar.file_id, db_path=db_path)
    summary = summarize_project(project_id=project["project_id"], db_path=db_path)
    after = calculate_leg_potential(project_id=project["project_id"], tariff_year=2025, db_path=db_path)
    projects = {item["project_id"]: item for item in list_projects(db_path=db_path)}

    assert deleted["measurements_deleted"] == 2
    assert [file["original_filename"] for file in summary["files"]] == ["load.csv"]
    assert summary["totals"]["pv_excess_kwh"] == pytest.approx(0.0)
    assert after["totals"]["local_solar_consumed_kwh"] == pytest.approx(0.0)
    assert after["totals"]["total_monetary_benefit_chf"] == pytest.approx(0.0)
    assert projects[project["project_id"]]["file_count"] == 1


def test_delete_project_removes_files_and_measurements(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Delete me", municipality="Sursee", db_path=db_path)
    other_project = create_project(name="Keep me", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[1.0, 2.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    deleted = delete_project(project_id=project["project_id"], db_path=db_path)
    projects = list_projects(db_path=db_path)
    con = connect_portal_db(db_path)
    try:
        remaining_files = con.execute("SELECT COUNT(*) FROM files WHERE project_id = ?", [project["project_id"]]).fetchone()[0]
        remaining_measurements = con.execute(
            "SELECT COUNT(*) FROM measurements WHERE project_id = ?",
            [project["project_id"]],
        ).fetchone()[0]
    finally:
        con.close()

    assert deleted["files_deleted"] == 2
    assert deleted["measurements_deleted"] == 4
    assert remaining_files == 0
    assert remaining_measurements == 0
    assert [item["project_id"] for item in projects] == [other_project["project_id"]]
    with pytest.raises(KeyError):
        get_project(project_id=project["project_id"], db_path=db_path)
    with pytest.raises(KeyError):
        delete_project(project_id=project["project_id"], db_path=db_path)


def test_stored_english_warnings_are_translated_in_summary(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[1.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    con = connect_portal_db(db_path)
    try:
        con.execute(
            """
            UPDATE files
            SET warnings_json = ?
            WHERE project_id = ?
            """,
            [
                '["PV file contains measured excess after local consumption, not full PV production.",'
                ' "Solar upload looks like consumption data."]',
                project["project_id"],
            ],
        )
    finally:
        con.close()

    summary = summarize_project(project_id=project["project_id"], db_path=db_path)

    assert "PV file contains measured excess after local consumption, not full PV production." not in summary["warnings"]
    assert "Solar upload looks like consumption data." not in summary["warnings"]
    assert "PV-Datei enthält gemessenen Überschuss nach lokalem Eigenverbrauch, nicht die gesamte PV-Produktion." in summary["warnings"]
    assert "Solar-Upload sieht wie Verbrauchsdaten aus." in summary["warnings"]


def test_match_project_plants_by_street_and_house_number(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    cache_path = tmp_path / "plants.csv"
    cache_path.write_text(
        "\n".join(
            [
                "Gemeinde;Strasse;Hausnummer;PLZ;Energietraeger;Installierte Leistung",
                "Sursee;Allmendstrasse;4 A;6210;Photovoltaik;30",
                "Sursee;Andere Strasse;9;6210;Photovoltaik;99",
            ]
        ),
        encoding="utf-8",
    )
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(
            contract="Ueberschuss PVA",
            column="Rücklieferung (kWh)",
            values=[1.0],
            address="Allmendstrasse 4 A, 6210 Sursee",
        ),
        upload_kind="solar",
        db_path=db_path,
    )

    result = match_project_plants(
        project_id=project["project_id"],
        municipality="Sursee",
        db_path=db_path,
        cache_path=cache_path,
    )

    assert result["total_installed_kwp"] == pytest.approx(30.0)
    assert result["matches"][0]["confidence"] == "Adresse"


def test_curated_project_plants_can_use_cache_and_manual_entries(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    cache_path = tmp_path / "plants.csv"
    cache_path.write_text(
        "\n".join(
            [
                "xtf_id;Municipality;Address;PostCode;TotalPower;SubCategory",
                "pv-1;Sursee;Allmendstrasse 4a;6210;30;subcat_2",
                "pv-2;Sursee;Andere Strasse 9;6210;15;subcat_2",
                "hydro-1;Sursee;Allmendstrasse 4a;6210;999;subcat_8",
            ]
        ),
        encoding="utf-8",
    )
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(
            contract="Ueberschuss PVA",
            column="Rücklieferung (kWh)",
            values=[1.0],
            address="Allmendstrasse 4 A, 6210 Sursee",
        ),
        upload_kind="solar",
        db_path=db_path,
    )

    automatic = match_project_plants(
        project_id=project["project_id"],
        municipality="Sursee",
        db_path=db_path,
        cache_path=cache_path,
    )
    assert automatic["selection_mode"] == "automatic"
    assert [item["cache_id"] for item in automatic["available_plants"]] == ["pv-1", "pv-2"]
    assert automatic["total_installed_kwp"] == pytest.approx(30.0)

    curated = adopt_automatic_project_plants(
        project_id=project["project_id"],
        municipality="Sursee",
        db_path=db_path,
        cache_path=cache_path,
    )
    assert curated["selection_mode"] == "curated"
    assert [item["cache_id"] for item in curated["matches"]] == ["pv-1"]

    curated = delete_selected_project_plant(
        project_id=project["project_id"],
        project_plant_id=curated["matches"][0]["project_plant_id"],
        municipality="Sursee",
        db_path=db_path,
        cache_path=cache_path,
    )
    assert curated["matches"] == []
    assert curated["total_installed_kwp"] == pytest.approx(0.0)

    curated = add_cached_project_plant(
        project_id=project["project_id"],
        municipality="Sursee",
        cache_id="pv-2",
        db_path=db_path,
        cache_path=cache_path,
    )
    curated = add_manual_project_plant(
        project_id=project["project_id"],
        municipality="Sursee",
        address="Manuelle Anlage 1",
        installed_kwp=10.0,
        db_path=db_path,
        cache_path=cache_path,
    )
    assert [item["source"] for item in curated["matches"]] == ["cache", "manual"]
    assert curated["total_installed_kwp"] == pytest.approx(25.0)

    reset = clear_project_plant_selection(
        project_id=project["project_id"],
        municipality="Sursee",
        db_path=db_path,
        cache_path=cache_path,
    )
    assert reset["selection_mode"] == "automatic"
    assert reset["total_installed_kwp"] == pytest.approx(30.0)


def test_optimize_leg_combinations_evaluates_all_subsets(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Test", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[0.001, 0.001]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load-a.csv",
        content=_ckw_csv(contract="A", column="Energieverbrauch (kWh)", values=[0.001, 0.0]),
        upload_kind="consumption",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load-b.csv",
        content=_ckw_csv(contract="B", column="Energieverbrauch (kWh)", values=[0.0, 0.002]),
        upload_kind="consumption",
        db_path=db_path,
    )

    result = optimize_leg_combinations(project_id=project["project_id"], tariff_year=2026, db_path=db_path)

    assert result["combination_count"] == 3
    assert result["evaluated_all"] is True
    assert result["best"]["participant_count"] >= 1
    assert result["best"]["flow"]["nodes"]
    node_ids = {node["id"] for node in result["best"]["flow"]["nodes"]}
    assert "solar-source-0" in node_ids
    assert "ckw" in node_ids
    assert "residual_solar_kwh" in result["best"]
    assert result["best"]["solar_sources"]
    assert result["best"]["cost_savings_chf"] == pytest.approx(
        result["best"]["tariff_switch_effect_chf"] + result["best"]["leg_network_discount_chf"]
    )


def test_optimize_leg_combinations_does_not_create_tariff_switch_savings(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="No tariff switch", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[2.0, 0.0]),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="small-load.csv",
        content=_ckw_csv(contract="Klein", column="Energieverbrauch (kWh)", values=[0.0, 1.0]),
        upload_kind="consumption",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="large-load.csv",
        content=_ckw_csv(contract="Gross", column="Energieverbrauch (kWh)", values=[3.0, 0.0]),
        upload_kind="consumption",
        db_path=db_path,
    )

    result = optimize_leg_combinations(
        project_id=project["project_id"],
        tariff_year=2026,
        max_exhaustive_participants=1,
        db_path=db_path,
    )

    members = {member["label"]: member for member in result["best"]["members"]}
    assert result["best"]["combined_tariff_tier"] == "> 50 MWh/Jahr"
    assert result["best"]["tariff_switch_effect_chf"] == pytest.approx(0.0)
    assert result["best"]["cost_savings_chf"] == pytest.approx(result["best"]["leg_network_discount_chf"])
    assert members["Klein"]["tariff_tier"] == "≤ 50 MWh/Jahr"
    assert members["Klein"]["allocated_local_solar_kwh"] == pytest.approx(0.0)
    assert members["Klein"]["tariff_switch_effect_chf"] == pytest.approx(0.0)
    assert members["Klein"]["cost_savings_chf"] == pytest.approx(0.0)
    assert members["Gross"]["allocated_local_solar_kwh"] == pytest.approx(2.0)
    assert members["Gross"]["cost_savings_chf"] == pytest.approx(
        members["Gross"]["leg_network_discount_chf"]
    )


def test_optimize_leg_combinations_uses_all_participants_for_large_community_assumption(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Large LEG", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[0.01, 0.01]),
        upload_kind="solar",
        db_path=db_path,
    )
    for label, values in [("A", [0.001, 0.0]), ("B", [0.0, 0.002]), ("C", [0.003, 0.0])]:
        ingest_portal_file(
            project_id=project["project_id"],
            original_filename=f"load-{label.casefold()}.csv",
            content=_ckw_csv(contract=label, column="Energieverbrauch (kWh)", values=values),
            upload_kind="consumption",
            db_path=db_path,
        )

    result = optimize_leg_combinations(
        project_id=project["project_id"],
        tariff_year=2026,
        max_exhaustive_participants=2,
        db_path=db_path,
    )

    assert result["optimization_mode"] == "all_participants_assumed_optimal"
    assert result["evaluated_all"] is False
    assert result["participant_count"] == 3
    assert result["evaluated_participant_count"] == 3
    assert result["combination_count"] == 1
    assert result["best"]["participant_count"] == 3
    assert result["best"]["consumption_kwh"] == pytest.approx(0.006)
    assert {member["label"] for member in result["best"]["members"]} == {"A", "B", "C"}
    assert "Die Einsparung wird mit allen Teilnehmern" in result["warnings"][0]


def test_kwp_to_ampere_three_phase_conversion() -> None:
    assert kwp_to_ampere(30.0) == pytest.approx(43.3, abs=0.1)
