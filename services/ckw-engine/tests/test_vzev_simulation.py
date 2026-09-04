from __future__ import annotations

from decimal import Decimal
from io import BytesIO

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from ckw_script.ckw_csv import parse_ckw_zev_csv
from ckw_script.portal_analysis import create_project, ingest_portal_file, simulate_project_vzev
from ckw_script.portal_api import PortalSettings, create_app
from ckw_script.vzev_simulation import SYNTHETIC_CSV_COLUMNS, simulate_vzev


def _ckw_csv(*, contract: str, column: str, values: list[float], start_hour: int = 0) -> bytes:
    rows = [
        "Kunde: Test AG",
        "Kundennummer: 1",
        "Adresse: Bahnhofstrasse 1, 6210 Sursee",
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
        rows.append(f"01.01.2025 {start_hour:02d}:{index * 15:02d};{value}")
    return "\n".join(rows).encode("utf-8")


def test_simulate_vzev_builds_ckw_style_csv_and_monthly_billing_summary() -> None:
    simulation = simulate_vzev(
        timestamps=["2025-01-01T06:45", "2025-01-01T07:00"],
        pv_kwh=[1.0, 4.0],
        consumption_by_participant=pd.DataFrame({"Haus A": [2.0, 1.0], "Haus B": [1.0, 1.0]}),
        tariff_year=2025,
    )

    df = simulation.dataframe
    assert list(df.columns) == list(SYNTHETIC_CSV_COLUMNS)
    assert df["Energiebedarf_total_kWh"].tolist() == pytest.approx([3.0, 2.0])
    assert df["Produktion_total_kWh"].tolist() == pytest.approx([1.0, 2.0])
    assert df["Bezug_total_kWh"].tolist() == pytest.approx([2.0, 0.0])
    assert df["Rueckspeissung_total_kWh"].tolist() == pytest.approx([0.0, 2.0])
    assert df["Energiebedarf_Niedertarif_kWh"].tolist() == pytest.approx([2.0, 0.0])
    assert df["Produktion_Hochtarif_kWh"].tolist() == pytest.approx([0.0, 2.0])
    assert simulation.summary["totals"]["savings_chf"] == pytest.approx(
        simulation.summary["totals"]["without_vzev_total_chf"]
        - simulation.summary["totals"]["with_vzev_ckw_invoice_chf"]
        - simulation.summary["totals"]["local_pv_energy_cost_chf"]
    )
    participant = simulation.summary["participants"][0]
    assert participant["with_vzev_total_chf"] == pytest.approx(
        participant["with_vzev_ckw_invoice_chf"] + participant["local_pv_energy_cost_chf"]
    )
    assert simulation.summary["monthly"][0]["month"] == "2025-01"
    parsed = parse_ckw_zev_csv(simulation.csv_bytes())
    assert parsed.totals["Produktion_total_kWh"] == pytest.approx(3.0)


def test_simulate_vzev_defaults_to_ckw_2026_and_allocates_shared_peak_by_residual_import() -> None:
    simulation = simulate_vzev(
        timestamps=["2025-01-01T10:00", "2025-01-01T10:15"],
        pv_kwh=[1.0, 0.0],
        consumption_by_participant=pd.DataFrame({"Haus A": [2.0, 1.0], "Haus B": [1.0, 1.0]}),
    )

    assert simulation.summary["tariff_year"] == 2026
    assert simulation.summary["peak_allocation_mode"] == "peak_in_grid_kwh"
    assert simulation.summary["totals"]["local_pv_energy_cost_chf"] == pytest.approx(0.19)
    assert all(
        participant["local_energy_rp_per_kwh"]
        == pytest.approx(participant["standard_product_reference_rp_per_kwh"] * 0.8, abs=0.02)
        for participant in simulation.summary["participants"]
    )

    simulation_with_missing_year = simulate_vzev(
        timestamps=["2025-01-01T10:00", "2025-01-01T10:15"],
        pv_kwh=[1.0, 0.0],
        consumption_by_participant=pd.DataFrame({"Haus A": [2.0, 1.0], "Haus B": [1.0, 1.0]}),
        tariff_year=None,
    )
    assert simulation_with_missing_year.summary["tariff_year"] == 2026


def test_simulate_vzev_accepts_fixed_pv_tariff_and_warns_without_capping_above_80_percent() -> None:
    kwargs = {
        "timestamps": ["2025-01-01T10:00", "2025-01-01T10:15"],
        "pv_kwh": [1.0, 1.0],
        "consumption_by_participant": pd.DataFrame({"Haus A": [2.0, 1.0], "Haus B": [1.0, 1.0]}),
    }

    automatic = simulate_vzev(**kwargs)
    assert automatic.summary["pv_tariff"]["mode"] == "participant_80_pct"
    assert all(participant["local_energy_cap_rp_per_kwh"] > 5 for participant in automatic.summary["participants"])

    fixed = simulate_vzev(**kwargs, fixed_local_energy_rp_per_kwh=Decimal("5.00"))
    assert fixed.summary["pv_tariff"] == {
        "mode": "fixed",
        "fixed_rp_per_kwh": 5.0,
        "minimum_participant_cap_rp_per_kwh": pytest.approx(
            min(participant["local_energy_cap_rp_per_kwh"] for participant in fixed.summary["participants"])
        ),
        "exceeds_80_percent_cap": False,
    }
    assert all(
        participant["local_energy_rp_per_kwh"] == pytest.approx(5.0)
        for participant in fixed.summary["participants"]
    )

    excessive = simulate_vzev(**kwargs, fixed_local_energy_rp_per_kwh=Decimal("1000"))
    assert excessive.summary["pv_tariff"]["exceeds_80_percent_cap"] is True
    assert all(
        participant["local_energy_rp_per_kwh"] == pytest.approx(1000.0)
        and participant["local_energy_tariff_exceeds_cap"] is True
        for participant in excessive.summary["participants"]
    )
    assert excessive.summary["totals"]["local_pv_energy_cost_chf"] == pytest.approx(20.0)
    assert any("verwendet den eingegebenen Tarif trotzdem" in warning for warning in excessive.summary["warnings"])


def test_simulate_vzev_reports_tariff_and_demand_cost_change_per_meter() -> None:
    timestamps = pd.date_range("2025-01-01", periods=31 * 24 * 4, freq="15min")
    simulation = simulate_vzev(
        timestamps=timestamps,
        pv_kwh=[0.0] * len(timestamps),
        consumption_by_participant=pd.DataFrame(
            {
                "Klein A": [1.25] * len(timestamps),
                "Klein B": [1.25] * len(timestamps),
            }
        ),
        participant_metering_codes={"Klein A": "CH-A", "Klein B": "CH-B"},
    )

    for participant in simulation.summary["participants"]:
        assert participant["metering_code"] in {"CH-A", "CH-B"}
        assert participant["standalone_annualized_kwh"] == pytest.approx(43_800)
        assert participant["community_annualized_grid_import_kwh"] == pytest.approx(87_600)
        assert participant["standalone_tariff_tier"] == "lt_50000"
        assert participant["community_tariff_tier"] == "gt_50000"
        assert participant["tariff_changed"] is True
        assert participant["standalone_demand_rate_chf_per_kw_month"] == pytest.approx(1.5)
        assert participant["community_demand_rate_chf_per_kw_month"] == pytest.approx(8.0)
        assert participant["demand_cost_change_chf"] > 0
        assert participant["ckw_cost_change_chf"] == pytest.approx(
            participant["demand_cost_change_chf"] + participant["other_ckw_cost_change_chf"],
            abs=0.02,
        )
        assert participant["total_cost_change_chf"] == pytest.approx(-participant["savings_chf"], abs=0.01)

    assert simulation.summary["totals"]["ckw_cost_change_chf"] == pytest.approx(
        sum(participant["ckw_cost_change_chf"] for participant in simulation.summary["participants"]),
        abs=0.02,
    )
    tariff_csv = pd.read_csv(BytesIO(simulation.tariff_comparison_csv_bytes()), sep=";")
    assert tariff_csv["Messpunkt"].tolist() == ["CH-A", "CH-B"]
    assert tariff_csv["Tarifklasse_einzeln"].tolist() == ["lt_50000", "lt_50000"]
    assert tariff_csv["Tarifklasse_vZEV"].tolist() == ["gt_50000", "gt_50000"]


def test_project_vzev_simulation_downloads_synthetic_csv(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Sursee Test", municipality="Sursee", db_path=db_path)
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="solar.csv",
        content=_ckw_csv(contract="PVA", column="Produktion (kWh)", values=[1.0, 4.0], start_hour=7),
        upload_kind="solar",
        db_path=db_path,
    )
    ingest_portal_file(
        project_id=project["project_id"],
        original_filename="load.csv",
        content=_ckw_csv(contract="Haus", column="Energieverbrauch (kWh)", values=[3.0, 2.0], start_hour=7),
        upload_kind="consumption",
        db_path=db_path,
    )

    simulation = simulate_project_vzev(project_id=project["project_id"], tariff_year=2025, db_path=db_path)
    assert simulation.summary["totals"]["grid_import_kwh"] == pytest.approx(2.0)
    assert simulation.summary["totals"]["pv_excess_kwh"] == pytest.approx(2.0)
    assert simulate_project_vzev(project_id=project["project_id"], db_path=db_path).summary["tariff_year"] == 2026

    app = create_app(
        PortalSettings(
            portal_db_path=str(db_path),
            pv_plant_cache_path=str(tmp_path / "missing-plants.csv"),
            frontend_dist_path=str(tmp_path / "missing-frontend"),
        )
    )
    response = TestClient(app).get(f"/api/projects/{project['project_id']}/vzev-simulation.csv?tariff_year=2025")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="vzev-simulation-sursee-test.csv"'
    downloaded = pd.read_csv(BytesIO(response.content), sep=";")
    assert list(downloaded.columns) == list(SYNTHETIC_CSV_COLUMNS)

    tariff_response = TestClient(app).get(
        f"/api/projects/{project['project_id']}/vzev-tariff-comparison.csv?tariff_year=2025"
    )
    assert tariff_response.status_code == 200
    assert tariff_response.headers["content-disposition"] == (
        'attachment; filename="vzev-tarifvergleich-sursee-test.csv"'
    )
    tariff_download = pd.read_csv(BytesIO(tariff_response.content), sep=";")
    assert tariff_download.loc[0, "Messpunkt"] == "CH1003601234500000000000000000001"
    assert "Leistungskosten_Aenderung_CHF" in tariff_download.columns

    fixed_response = TestClient(app).get(
        f"/api/projects/{project['project_id']}/vzev-simulation?tariff_year=2025&fixed_pv_tariff_rp_per_kwh=5"
    )
    assert fixed_response.status_code == 200
    assert fixed_response.json()["pv_tariff"]["mode"] == "fixed"
    assert fixed_response.json()["participants"][0]["local_energy_rp_per_kwh"] == pytest.approx(5.0)

    excessive_response = TestClient(app).get(
        f"/api/projects/{project['project_id']}/vzev-simulation?tariff_year=2025&fixed_pv_tariff_rp_per_kwh=1000"
    )
    assert excessive_response.status_code == 200
    assert excessive_response.json()["pv_tariff"]["exceeds_80_percent_cap"] is True
    assert excessive_response.json()["participants"][0]["local_energy_rp_per_kwh"] == pytest.approx(1000.0)
    assert "80%-Obergrenze" in excessive_response.json()["warnings"][0]


def test_project_vzev_simulation_requires_classic_uploads(tmp_path) -> None:
    db_path = tmp_path / "portal.duckdb"
    project = create_project(name="Missing data", municipality="Sursee", db_path=db_path)

    with pytest.raises(ValueError, match="Verbrauchsdaten"):
        simulate_project_vzev(project_id=project["project_id"], db_path=db_path)
