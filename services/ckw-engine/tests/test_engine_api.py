from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ckw_script.engine_api import EngineSettings, create_engine_app


def _ckw_csv(*, contract: str, column: str, values: list[float]) -> bytes:
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
        f"01.01.2025 00:00;{values[0]}",
        f"01.01.2025 00:15;{values[1]}",
    ]
    return "\n".join(rows).encode()


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_engine_app(
            EngineSettings(
                internal_token="test-token",
                tmp_dir=str(tmp_path),
                pv_plant_cache_path=str(tmp_path / "plants.csv"),
            )
        )
    )


def test_internal_endpoints_require_service_token(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.get("/internal/health").status_code == 401
    assert client.get(
        "/internal/health", headers={"Authorization": "Bearer test-token"}
    ).json() == {"status": "ok"}


def test_stateless_summary_preserves_external_project_and_file_ids(tmp_path) -> None:
    client = _client(tmp_path)
    manifest = {
        "operation": "summary",
        "project": {"id": "project-public-id", "name": "Test", "municipality": "Sursee"},
        "files": [
            {
                "id": "solar-public-id",
                "original_filename": "solar.csv",
                "upload_kind": "solar",
            },
            {
                "id": "load-public-id",
                "original_filename": "load.csv",
                "upload_kind": "consumption",
            },
        ],
    }
    response = client.post(
        "/internal/v1/execute",
        headers={"Authorization": "Bearer test-token"},
        data={"manifest": json.dumps(manifest)},
        files=[
            (
                "files",
                (
                    "solar.csv",
                    _ckw_csv(contract="Ueberschuss PVA", column="Rücklieferung (kWh)", values=[1, 2]),
                    "text/csv",
                ),
            ),
            (
                "files",
                (
                    "load.csv",
                    _ckw_csv(contract="Allgemein", column="Energieverbrauch (kWh)", values=[0.5, 3]),
                    "text/csv",
                ),
            ),
        ],
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] == "project-public-id"
    assert {item["file_id"] for item in payload["files"]} == {
        "solar-public-id",
        "load-public-id",
    }
    assert payload["totals"]["pv_excess_kwh"] == 3.0


def test_file_manifest_mismatch_is_rejected_without_partial_response(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/internal/v1/execute",
        headers={"Authorization": "Bearer test-token"},
        data={
            "manifest": json.dumps(
                {
                    "operation": "summary",
                    "project": {"id": "project", "name": "Test", "municipality": "Sursee"},
                    "files": [{"id": "missing", "original_filename": "missing.csv", "upload_kind": "solar"}],
                }
            )
        },
    )

    assert response.status_code == 400
    assert "file manifest" in response.json()["detail"]
