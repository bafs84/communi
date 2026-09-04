from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any
import unicodedata
from urllib.request import urlopen
import zipfile

import duckdb
import numpy as np
import pandas as pd

from .battery_simulation import DEFAULT_BATTERY, simulate_peak_shaving, simulate_pv_battery
from .ckw_csv import (
    ZEV_COLUMN_MAP,
    is_ckw_zev_csv,
    parse_ckw_csv,
    parse_ckw_zev_csv,
    parsed_metadata_normalized,
)
from .pricing_import_2025 import select_import_tariff
from .vzev_simulation import DEFAULT_VZEV_SIMULATION_TARIFF_YEAR, VzevSimulation, simulate_vzev


FLOW_CONSUMPTION = "consumption"
FLOW_PV_PRODUCTION = "pv_production"
FLOW_PV_EXCESS = "pv_excess"
FLOW_GRID_IMPORT = "grid_import"
FLOW_LOCAL_PV_CONSUMPTION = "local_pv_consumption"
FLOW_ZEV_AGGREGATE = "zev_aggregate"

DEFAULT_PORTAL_DB = Path("data") / "portal.duckdb"
DEFAULT_PLANT_CACHE = Path("data") / "electricity_production_plants.csv"
PV_PLANT_CKAN_URL = "https://ckan.opendata.swiss/api/3/action/package_show?id=elektrizitatsproduktionsanlagen"
PV_PLANT_CKAN_FALLBACK_URL = "https://opendata.swiss/api/3/action/package_show?id=elektrizitatsproduktionsanlagen"
WARNING_TRANSLATIONS = {
    "PV file contains measured excess after local consumption, not full PV production.": (
        "PV-Datei enthält gemessenen Überschuss nach lokalem Eigenverbrauch, nicht die gesamte PV-Produktion."
    ),
    "Solar upload looks like consumption data.": "Solar-Upload sieht wie Verbrauchsdaten aus.",
    "Consumption upload looks like solar data.": "Verbrauchs-Upload sieht wie Solardaten aus.",
    "Could not fully classify solar file; treating it as PV excess.": (
        "Solardatei konnte nicht eindeutig klassifiziert werden; sie wird als PV-Überschuss behandelt."
    ),
    "Could not fully classify consumption file; treating it as consumption.": (
        "Verbrauchsdatei konnte nicht eindeutig klassifiziert werden; sie wird als Verbrauch behandelt."
    ),
    "Only PV excess files are available; total PV production is unknown.": (
        "Es sind nur PV-Überschussdateien vorhanden; die gesamte PV-Produktion ist unbekannt."
    ),
    "Calculation uses PV excess as shareable solar because total production is unknown.": (
        "Die Rechnung nutzt PV-Überschuss als teilbaren Solarstrom, weil die Gesamtproduktion unbekannt ist."
    ),
    "No consumption upload available; LEG consumption potential is zero.": (
        "Es sind keine Verbrauchsdaten hochgeladen; das LEG-Verbrauchspotenzial ist null."
    ),
    "No uploaded measurements available.": "Es sind noch keine Messwerte hochgeladen.",
}


@dataclass(frozen=True)
class UploadedPortalFile:
    file_id: str
    original_filename: str
    upload_kind: str
    flow_kind: str
    rows_inserted: int
    total_kwh: float
    warnings: list[str]
    duplicate: bool = False


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def _translate_warning(value: object) -> str:
    text = str(value)
    return WARNING_TRANSLATIONS.get(text, text)


def classify_portal_flow(*, parser_kind: str, metadata: dict[str, str], upload_kind: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    contract = normalize_text(metadata.get("Vertrag", ""))
    energy_col = normalize_text(metadata.get("_energy_column", ""))

    if parser_kind == "production":
        return FLOW_PV_PRODUCTION, warnings
    if parser_kind == "import":
        return FLOW_CONSUMPTION, warnings
    if parser_kind == "export":
        if "produktion" in contract and ("simuliert" in contract or "simulation" in contract):
            warnings.append(
                "Die simulierte Solarzeitreihe wird trotz Spaltenname Rücklieferung als gesamte PV-Produktion behandelt."
            )
            return FLOW_PV_PRODUCTION, warnings
        if "uberschuss" in contract or "ruecklieferung" in energy_col or "rucklieferung" in energy_col:
            warnings.append(
                "PV-Datei enthält gemessenen Überschuss nach lokalem Eigenverbrauch, nicht die gesamte PV-Produktion."
            )
        return FLOW_PV_EXCESS, warnings

    if upload_kind == "solar":
        warnings.append("Solardatei konnte nicht eindeutig klassifiziert werden; sie wird als PV-Überschuss behandelt.")
        return FLOW_PV_EXCESS, warnings
    warnings.append("Verbrauchsdatei konnte nicht eindeutig klassifiziert werden; sie wird als Verbrauch behandelt.")
    return FLOW_CONSUMPTION, warnings


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            municipality TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            plant_selection_mode TEXT NOT NULL DEFAULT 'automatic'
        )
        """
    )
    con.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS plant_selection_mode TEXT DEFAULT 'automatic'")
    con.execute("UPDATE projects SET plant_selection_mode = 'automatic' WHERE plant_selection_mode IS NULL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            upload_kind TEXT NOT NULL,
            flow_kind TEXT NOT NULL,
            energy_column TEXT,
            content_hash TEXT,
            total_kwh DOUBLE,
            metadata_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            ingested_at TIMESTAMP NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS project_plants (
            project_plant_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            source TEXT NOT NULL,
            cache_id TEXT,
            address TEXT NOT NULL,
            municipality TEXT NOT NULL,
            installed_kwp DOUBLE NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS pv_battery_consumers (
            project_id TEXT NOT NULL,
            solar_file_id TEXT NOT NULL,
            consumer_file_id TEXT NOT NULL,
            PRIMARY KEY (project_id, solar_file_id, consumer_file_id)
        )
        """
    )
    con.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS content_hash TEXT")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS measurements (
            project_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            flow_kind TEXT NOT NULL,
            kwh DOUBLE NOT NULL,
            address TEXT,
            zaehlpunkt TEXT,
            meter_label TEXT
        )
        """
    )


def connect_portal_db(db_path: str | Path = DEFAULT_PORTAL_DB) -> duckdb.DuckDBPyConnection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    ensure_schema(con)
    return con


def create_project(*, name: str, municipality: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    con = connect_portal_db(db_path)
    try:
        con.execute(
            """
            INSERT INTO projects (project_id, name, municipality, created_at)
            VALUES (?, ?, ?, now())
            """,
            [project_id, name.strip() or municipality, municipality],
        )
        return get_project(project_id=project_id, db_path=db_path)
    finally:
        con.close()


def list_projects(*, db_path: str | Path = DEFAULT_PORTAL_DB) -> list[dict[str, Any]]:
    con = connect_portal_db(db_path)
    try:
        rows = con.execute(
            """
            SELECT
                p.project_id,
                p.name,
                p.municipality,
                p.created_at,
                COUNT(DISTINCT COALESCE(
                    f.content_hash,
                    f.original_filename || '|' || f.upload_kind || '|' || f.flow_kind || '|' || COALESCE(CAST(f.total_kwh AS TEXT), '')
                )) AS file_count
            FROM projects p
            LEFT JOIN files f USING (project_id)
            GROUP BY 1, 2, 3, 4
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        return [
            {
                "project_id": row[0],
                "name": row[1],
                "municipality": row[2],
                "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
                "file_count": row[4],
            }
            for row in rows
        ]
    finally:
        con.close()


def get_project(*, project_id: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        row = con.execute(
            "SELECT project_id, name, municipality, created_at FROM projects WHERE project_id = ?",
            [project_id],
        ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return {
            "project_id": row[0],
            "name": row[1],
            "municipality": row[2],
            "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
        }
    finally:
        con.close()


def delete_project(*, project_id: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    transaction_started = False
    try:
        con.execute("BEGIN TRANSACTION")
        transaction_started = True
        project_row = con.execute(
            "SELECT name, municipality FROM projects WHERE project_id = ?",
            [project_id],
        ).fetchone()
        if project_row is None:
            raise KeyError(project_id)
        file_count = con.execute(
            "SELECT COUNT(*) FROM files WHERE project_id = ?",
            [project_id],
        ).fetchone()[0]
        measurement_count = con.execute(
            "SELECT COUNT(*) FROM measurements WHERE project_id = ?",
            [project_id],
        ).fetchone()[0]
        plant_count = con.execute(
            "SELECT COUNT(*) FROM project_plants WHERE project_id = ?",
            [project_id],
        ).fetchone()[0]
        battery_assignment_count = con.execute(
            "SELECT COUNT(*) FROM pv_battery_consumers WHERE project_id = ?",
            [project_id],
        ).fetchone()[0]
        con.execute("DELETE FROM pv_battery_consumers WHERE project_id = ?", [project_id])
        con.execute("DELETE FROM measurements WHERE project_id = ?", [project_id])
        con.execute("DELETE FROM files WHERE project_id = ?", [project_id])
        con.execute("DELETE FROM project_plants WHERE project_id = ?", [project_id])
        con.execute("DELETE FROM projects WHERE project_id = ?", [project_id])
        con.execute("COMMIT")
        transaction_started = False
        return {
            "project_id": project_id,
            "name": project_row[0],
            "municipality": project_row[1],
            "files_deleted": file_count,
            "measurements_deleted": measurement_count,
            "plants_deleted": plant_count,
            "battery_assignments_deleted": battery_assignment_count,
        }
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def ingest_portal_file(
    *,
    project_id: str,
    original_filename: str,
    content: bytes,
    upload_kind: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> UploadedPortalFile:
    content_hash = hashlib.sha256(content).hexdigest()
    con = connect_portal_db(db_path)
    try:
        duplicate_row = con.execute(
            """
            SELECT file_id, original_filename, upload_kind, flow_kind, total_kwh, warnings_json
            FROM files
            WHERE project_id = ? AND content_hash = ?
            """,
            [project_id, content_hash],
        ).fetchone()
        if duplicate_row is not None:
            warnings = [_translate_warning(warning) for warning in json.loads(duplicate_row[5] or "[]")]
            warnings.append("Datei wurde bereits importiert; Duplikat ignoriert.")
            return UploadedPortalFile(
                file_id=duplicate_row[0],
                original_filename=duplicate_row[1],
                upload_kind=duplicate_row[2],
                flow_kind=duplicate_row[3],
                rows_inserted=0,
                total_kwh=float(duplicate_row[4] or 0.0),
                warnings=warnings,
                duplicate=True,
            )
    finally:
        con.close()

    if is_ckw_zev_csv(content):
        return _ingest_portal_zev_file(
            project_id=project_id,
            original_filename=original_filename,
            content=content,
            content_hash=content_hash,
            db_path=db_path,
        )

    parsed = parse_ckw_csv(content)
    metadata = dict(parsed.metadata)
    metadata["_energy_column"] = parsed.energy_column
    flow_kind, warnings = classify_portal_flow(
        parser_kind=parsed.kind,
        metadata=metadata,
        upload_kind=upload_kind,
    )
    if upload_kind == "solar" and flow_kind == FLOW_CONSUMPTION:
        warnings.append("Solar-Upload sieht wie Verbrauchsdaten aus.")
    if upload_kind == "consumption" and flow_kind != FLOW_CONSUMPTION:
        warnings.append("Verbrauchs-Upload sieht wie Solardaten aus.")

    con = connect_portal_db(db_path)
    try:
        fallback_duplicate = con.execute(
            """
            SELECT file_id, original_filename, upload_kind, flow_kind, total_kwh, warnings_json
            FROM files
            WHERE project_id = ?
              AND original_filename = ?
              AND upload_kind = ?
              AND flow_kind = ?
              AND ABS(COALESCE(total_kwh, 0) - ?) < 0.000001
            """,
            [project_id, original_filename, upload_kind, flow_kind, float(parsed.dataframe["energy_kwh_signed"].abs().sum())],
        ).fetchone()
        if fallback_duplicate is not None:
            con.execute(
                "UPDATE files SET content_hash = COALESCE(content_hash, ?) WHERE file_id = ?",
                [content_hash, fallback_duplicate[0]],
            )
            duplicate_warnings = [_translate_warning(warning) for warning in json.loads(fallback_duplicate[5] or "[]")]
            duplicate_warnings.append("Datei wurde bereits importiert; Duplikat ignoriert.")
            return UploadedPortalFile(
                file_id=fallback_duplicate[0],
                original_filename=fallback_duplicate[1],
                upload_kind=fallback_duplicate[2],
                flow_kind=fallback_duplicate[3],
                rows_inserted=0,
                total_kwh=float(fallback_duplicate[4] or 0.0),
                warnings=duplicate_warnings,
                duplicate=True,
            )
    finally:
        con.close()

    file_id = str(uuid.uuid4())
    meta_norm = parsed_metadata_normalized(parsed.metadata)
    df = parsed.dataframe.copy()
    signed = pd.to_numeric(df["energy_kwh_signed"], errors="coerce").fillna(0.0)
    if flow_kind == FLOW_CONSUMPTION:
        df["kwh"] = signed.clip(lower=0.0)
    else:
        df["kwh"] = signed.abs()

    measurement_df = pd.DataFrame(
        {
            "project_id": project_id,
            "file_id": file_id,
            "timestamp": pd.to_datetime(df["timestamp"], errors="raise"),
            "flow_kind": flow_kind,
            "kwh": df["kwh"].astype(float),
            "address": meta_norm.get("adresse"),
            "zaehlpunkt": meta_norm.get("zaehlpunkt"),
            "meter_label": meta_norm.get("vertrag") or original_filename,
        }
    )
    total_kwh = float(measurement_df["kwh"].sum())

    con = connect_portal_db(db_path)
    try:
        if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [project_id]).fetchone() is None:
            raise KeyError(project_id)
        con.register("_measurements", measurement_df)
        con.execute("INSERT INTO measurements SELECT * FROM _measurements")
        con.execute(
            """
            INSERT INTO files (
                file_id,
                project_id,
                original_filename,
                upload_kind,
                flow_kind,
                energy_column,
                content_hash,
                total_kwh,
                metadata_json,
                warnings_json,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            """,
            [
                file_id,
                project_id,
                original_filename,
                upload_kind,
                flow_kind,
                parsed.energy_column,
                content_hash,
                total_kwh,
                json.dumps(meta_norm, ensure_ascii=False),
                json.dumps(warnings, ensure_ascii=False),
            ],
        )
    finally:
        con.close()

    return UploadedPortalFile(
        file_id=file_id,
        original_filename=original_filename,
        upload_kind=upload_kind,
        flow_kind=flow_kind,
        rows_inserted=len(measurement_df),
        total_kwh=total_kwh,
        warnings=warnings,
        duplicate=False,
    )


def _ingest_portal_zev_file(
    *,
    project_id: str,
    original_filename: str,
    content: bytes,
    content_hash: str,
    db_path: str | Path,
) -> UploadedPortalFile:
    parsed = parse_ckw_zev_csv(content)
    file_id = str(uuid.uuid4())
    flow_columns = {
        FLOW_CONSUMPTION: "consumption_kwh",
        FLOW_GRID_IMPORT: "grid_import_kwh",
        FLOW_PV_PRODUCTION: "pv_production_kwh",
        FLOW_LOCAL_PV_CONSUMPTION: "local_pv_consumption_kwh",
        FLOW_PV_EXCESS: "pv_excess_kwh",
    }
    measurement_frames: list[pd.DataFrame] = []
    for flow_kind, column in flow_columns.items():
        measurement_frames.append(
            pd.DataFrame(
                {
                    "project_id": project_id,
                    "file_id": file_id,
                    "timestamp": parsed.dataframe["timestamp"],
                    "flow_kind": flow_kind,
                    "kwh": pd.to_numeric(parsed.dataframe[column], errors="raise").astype(float),
                    "address": None,
                    "zaehlpunkt": None,
                    "meter_label": original_filename,
                }
            )
        )
    measurement_df = pd.concat(measurement_frames, ignore_index=True)
    total_kwh = float(parsed.dataframe["consumption_kwh"].sum())
    warnings = [
        "ZEV-Aggregat enthält bereits Verbrauch, PV-Produktion und PV-Überschuss. "
        "Nicht mit überlappenden Einzelmesswerten kombinieren."
    ]
    metadata = {
        "format": "ckw_zev",
        "source_columns": ZEV_COLUMN_MAP,
        "totals": parsed.totals,
    }

    con = connect_portal_db(db_path)
    transaction_started = False
    try:
        if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [project_id]).fetchone() is None:
            raise KeyError(project_id)
        con.execute("BEGIN TRANSACTION")
        transaction_started = True
        con.register("_measurements", measurement_df)
        con.execute("INSERT INTO measurements SELECT * FROM _measurements")
        con.execute(
            """
            INSERT INTO files (
                file_id,
                project_id,
                original_filename,
                upload_kind,
                flow_kind,
                energy_column,
                content_hash,
                total_kwh,
                metadata_json,
                warnings_json,
                ingested_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
            """,
            [
                file_id,
                project_id,
                original_filename,
                "zev",
                FLOW_ZEV_AGGREGATE,
                "Energiebedarf_total_kWh",
                content_hash,
                total_kwh,
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(warnings, ensure_ascii=False),
            ],
        )
        con.execute("COMMIT")
        transaction_started = False
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    return UploadedPortalFile(
        file_id=file_id,
        original_filename=original_filename,
        upload_kind="zev",
        flow_kind=FLOW_ZEV_AGGREGATE,
        rows_inserted=len(measurement_df),
        total_kwh=total_kwh,
        warnings=warnings,
        duplicate=False,
    )


def delete_portal_file(
    *,
    project_id: str,
    file_id: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    transaction_started = False
    try:
        con.execute("BEGIN TRANSACTION")
        transaction_started = True
        file_row = con.execute(
            """
            SELECT original_filename, upload_kind, flow_kind
            FROM files
            WHERE project_id = ? AND file_id = ?
            """,
            [project_id, file_id],
        ).fetchone()
        if file_row is None:
            raise KeyError(file_id)
        measurement_count = con.execute(
            """
            SELECT COUNT(*)
            FROM measurements
            WHERE project_id = ? AND file_id = ?
            """,
            [project_id, file_id],
        ).fetchone()[0]
        con.execute(
            """
            DELETE FROM pv_battery_consumers
            WHERE project_id = ? AND (solar_file_id = ? OR consumer_file_id = ?)
            """,
            [project_id, file_id, file_id],
        )
        con.execute(
            "DELETE FROM measurements WHERE project_id = ? AND file_id = ?",
            [project_id, file_id],
        )
        con.execute(
            "DELETE FROM files WHERE project_id = ? AND file_id = ?",
            [project_id, file_id],
        )
        con.execute("COMMIT")
        transaction_started = False
        return {
            "file_id": file_id,
            "original_filename": file_row[0],
            "upload_kind": file_row[1],
            "flow_kind": file_row[2],
            "measurements_deleted": measurement_count,
        }
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _timeseries_for_project(
    con: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
    flow_kind: str | None = None,
) -> pd.DataFrame:
    params: list[object] = [project_id]
    where = "project_id = ?"
    if flow_kind is not None:
        where += " AND flow_kind = ?"
        params.append(flow_kind)
    return con.execute(
        f"""
        WITH selected_files AS (
            SELECT file_id
            FROM (
                SELECT
                    file_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            content_hash,
                            original_filename || '|' || upload_kind || '|' || flow_kind || '|' || COALESCE(CAST(total_kwh AS TEXT), '')
                        )
                        ORDER BY ingested_at, file_id
                    ) AS rn
                FROM files
                WHERE project_id = ?
            )
            WHERE rn = 1
        )
        SELECT m.timestamp, m.flow_kind, SUM(m.kwh) AS kwh
        FROM measurements m
        JOIN selected_files sf USING (file_id)
        WHERE {where.replace("project_id", "m.project_id")}
        GROUP BY 1, 2
        ORDER BY 1
        """,
        [project_id, *params],
    ).df()


def _pivot_flows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(
            columns=[
                "timestamp",
                FLOW_CONSUMPTION,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
                FLOW_GRID_IMPORT,
                FLOW_LOCAL_PV_CONSUMPTION,
            ]
        )
    pivot = (
        df.pivot_table(index="timestamp", columns="flow_kind", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .reset_index()
    )
    for col in (FLOW_CONSUMPTION, FLOW_PV_PRODUCTION, FLOW_PV_EXCESS, FLOW_GRID_IMPORT, FLOW_LOCAL_PV_CONSUMPTION):
        if col not in pivot.columns:
            pivot[col] = 0.0
    return pivot.sort_values("timestamp")


def _available_solar_kwh(df: pd.DataFrame) -> pd.Series:
    production = pd.to_numeric(df[FLOW_PV_PRODUCTION], errors="coerce").fillna(0.0)
    excess = pd.to_numeric(df[FLOW_PV_EXCESS], errors="coerce").fillna(0.0)
    return production.where(production > 0, excess)


def _solar_sources_for_project(con: duckdb.DuckDBPyConnection, *, project_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        f"""
        WITH selected_files AS ({_deduped_file_cte()})
        SELECT f.file_id, f.original_filename, f.flow_kind, f.metadata_json
        FROM files f
        JOIN selected_files sf USING (file_id)
        WHERE f.flow_kind IN (?, ?, ?)
        ORDER BY f.ingested_at, f.file_id
        """,
        [project_id, FLOW_PV_PRODUCTION, FLOW_PV_EXCESS, FLOW_ZEV_AGGREGATE],
    ).fetchall()
    sources: list[dict[str, Any]] = []
    for file_id, original_filename, flow_kind, metadata_json in rows:
        metadata = json.loads(metadata_json or "{}")
        label = metadata.get("vertrag") or original_filename
        if flow_kind == FLOW_ZEV_AGGREGATE:
            label = f"CKW ZEV Überschuss: {original_filename}"
        sources.append(
            {
                "file_id": str(file_id),
                "label": str(label),
                "address": metadata.get("adresse"),
                "flow_kind": str(flow_kind),
            }
        )
    return sources


def _available_solar_by_source_for_project(con: duckdb.DuckDBPyConnection, *, project_id: str) -> pd.DataFrame:
    df = con.execute(
        f"""
        WITH selected_files AS ({_deduped_file_cte()})
        SELECT m.timestamp, f.file_id, f.flow_kind, SUM(m.kwh) AS available_solar_kwh
        FROM measurements m
        JOIN selected_files sf USING (file_id)
        JOIN files f USING (file_id)
        WHERE m.project_id = ?
          AND (
            (f.flow_kind = ? AND m.flow_kind = ?)
            OR (f.flow_kind = ? AND m.flow_kind = ?)
            OR (f.flow_kind = ? AND m.flow_kind = ?)
          )
        GROUP BY 1, 2, 3
        ORDER BY 1, 2
        """,
        [
            project_id,
            project_id,
            FLOW_PV_PRODUCTION,
            FLOW_PV_PRODUCTION,
            FLOW_PV_EXCESS,
            FLOW_PV_EXCESS,
            FLOW_ZEV_AGGREGATE,
            FLOW_PV_EXCESS,
        ],
    ).df()
    if df.empty:
        return pd.DataFrame()

    source_flows = df[["file_id", "flow_kind"]].drop_duplicates()
    available = (
        df.pivot_table(index="timestamp", columns="file_id", values="available_solar_kwh", aggfunc="sum")
        .fillna(0.0)
        .sort_index()
    )
    production_ids = source_flows.loc[source_flows["flow_kind"] == FLOW_PV_PRODUCTION, "file_id"].tolist()
    excess_ids = source_flows.loc[source_flows["flow_kind"] == FLOW_PV_EXCESS, "file_id"].tolist()
    if production_ids and excess_ids:
        has_classic_production = available[production_ids].sum(axis=1) > 0
        available.loc[has_classic_production, excess_ids] = 0.0
    return available


def _leg_input_timeseries_for_project(
    con: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
) -> pd.DataFrame:
    return con.execute(
        f"""
        WITH selected_files AS ({_deduped_file_cte()}),
        interval_flows AS (
            SELECT
                m.timestamp,
                SUM(
                    CASE
                        WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh
                        WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh
                        ELSE 0
                    END
                ) AS consumption_kwh,
                SUM(CASE WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh ELSE 0 END) AS classic_pv_production_kwh,
                SUM(CASE WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh ELSE 0 END) AS classic_pv_excess_kwh,
                SUM(CASE WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh ELSE 0 END) AS zev_pv_excess_kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
            GROUP BY 1
        )
        SELECT
            timestamp,
            consumption_kwh,
            CASE
                WHEN classic_pv_production_kwh > 0 THEN classic_pv_production_kwh
                ELSE classic_pv_excess_kwh
            END + zev_pv_excess_kwh AS available_solar_kwh
        FROM interval_flows
        ORDER BY timestamp
        """,
        [
            project_id,
            FLOW_CONSUMPTION,
            FLOW_CONSUMPTION,
            FLOW_ZEV_AGGREGATE,
            FLOW_GRID_IMPORT,
            FLOW_PV_PRODUCTION,
            FLOW_PV_PRODUCTION,
            FLOW_PV_EXCESS,
            FLOW_PV_EXCESS,
            FLOW_ZEV_AGGREGATE,
            FLOW_PV_EXCESS,
            project_id,
        ],
    ).df()


def _variable_net_rp_per_kwh(tariff: Any) -> Decimal:
    """Netztarif component eligible for the LEG discount.

    Swissgrid system services, electricity reserve and solidarity charges are
    separate tariff lines and are not discounted as local grid usage.
    """

    return tariff.netznutzung_einheitstarif_rp_per_kwh


def _tariff_label(annualized_kwh: float) -> str:
    return "> 50 MWh/Jahr" if annualized_kwh > 50_000 else "≤ 50 MWh/Jahr"


def _infer_annualization_factor(timestamps: pd.Series) -> float:
    ts = pd.to_datetime(timestamps, errors="coerce").dropna().sort_values()
    if ts.empty:
        return 1.0
    diffs = ts.diff().dropna().dt.total_seconds() / 3600.0
    diffs = diffs[diffs > 0]
    interval_hours = float(diffs.median()) if not diffs.empty else 0.25
    covered_hours = interval_hours * len(ts)
    if covered_hours <= 0:
        return 1.0
    return 8760.0 / covered_hours


def _deduped_file_cte() -> str:
    return """
        SELECT file_id
        FROM (
            SELECT
                file_id,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(
                        content_hash,
                        original_filename || '|' || upload_kind || '|' || flow_kind || '|' || COALESCE(CAST(total_kwh AS TEXT), '')
                    )
                    ORDER BY ingested_at, file_id
                ) AS rn
            FROM files
            WHERE project_id = ?
        )
        WHERE rn = 1
    """


def _format_graph_kwh(value: float) -> str:
    return f"{value:,.0f} kWh".replace(",", "'")


def _zev_flow_totals(con: duckdb.DuckDBPyConnection, *, project_id: str) -> dict[str, float]:
    rows = con.execute(
        f"""
        WITH selected_files AS ({_deduped_file_cte()})
        SELECT m.flow_kind, SUM(m.kwh) AS kwh
        FROM measurements m
        JOIN selected_files sf USING (file_id)
        JOIN files f USING (file_id)
        WHERE m.project_id = ?
          AND f.flow_kind = ?
          AND m.flow_kind IN (?, ?, ?, ?, ?)
        GROUP BY 1
        """,
        [
            project_id,
            project_id,
            FLOW_ZEV_AGGREGATE,
            FLOW_CONSUMPTION,
            FLOW_PV_PRODUCTION,
            FLOW_PV_EXCESS,
            FLOW_GRID_IMPORT,
            FLOW_LOCAL_PV_CONSUMPTION,
        ],
    ).fetchall()
    return {str(flow_kind): float(kwh or 0.0) for flow_kind, kwh in rows}


def _zev_flow_graph(con: duckdb.DuckDBPyConnection, *, project_id: str) -> dict[str, list[dict[str, Any]]] | None:
    totals = _zev_flow_totals(con, project_id=project_id)
    if not totals:
        return None

    consumption_kwh = totals.get(FLOW_CONSUMPTION, 0.0)
    pv_production_kwh = totals.get(FLOW_PV_PRODUCTION, 0.0)
    pv_excess_kwh = totals.get(FLOW_PV_EXCESS, 0.0)
    grid_import_kwh = totals.get(FLOW_GRID_IMPORT, 0.0)
    local_pv_consumption_kwh = totals.get(FLOW_LOCAL_PV_CONSUMPTION, 0.0)

    return {
        "nodes": [
            {
                "id": "zev-production",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": "CKW ZEV PV-Produktion",
                    "value": _format_graph_kwh(pv_production_kwh),
                    "subtitle": "Brutto-PV gemessen",
                    "kind": "zev-source",
                },
            },
            {
                "id": "zev-grid-import",
                "position": {"x": 0, "y": 180},
                "data": {
                    "label": "Netzbezug CKW",
                    "value": _format_graph_kwh(grid_import_kwh),
                    "subtitle": "gemessen",
                    "kind": "zev-grid-import",
                },
            },
            {
                "id": "zev-consumption",
                "position": {"x": 720, "y": 0},
                "data": {
                    "label": "CKW ZEV Verbrauch",
                    "value": _format_graph_kwh(consumption_kwh),
                    "subtitle": f"Eigenverbrauch: {_format_graph_kwh(local_pv_consumption_kwh)}",
                    "kind": "zev-consumption",
                },
            },
            {
                "id": "zev-grid-export",
                "position": {"x": 720, "y": 180},
                "data": {
                    "label": "Rückspeisung CKW",
                    "value": _format_graph_kwh(pv_excess_kwh),
                    "subtitle": "PV-Überschuss gemessen",
                    "kind": "zev-grid-export",
                },
            },
        ],
        "edges": [
            {
                "id": "zev-production-consumption",
                "source": "zev-production",
                "target": "zev-consumption",
                "label": _format_graph_kwh(local_pv_consumption_kwh),
                "animated": True,
            },
            {
                "id": "zev-grid-import-consumption",
                "source": "zev-grid-import",
                "target": "zev-consumption",
                "label": _format_graph_kwh(grid_import_kwh),
            },
            {
                "id": "zev-production-grid-export",
                "source": "zev-production",
                "target": "zev-grid-export",
                "label": _format_graph_kwh(pv_excess_kwh),
            },
        ],
    }


def _optimization_assumptions(network_discount: Decimal) -> list[str]:
    discount_percent = int(network_discount * Decimal("100"))
    return [
        "Alle nichtleeren Kombinationen der Verbrauchsteilnehmer werden bewertet, sofern die Teilnehmerzahl innerhalb der Grenze für vollständige Suche liegt.",
        "Bei grösseren Gemeinschaften wird die Einsparung mit allen Verbrauchsteilnehmern unter der Annahme berechnet, dass diese vollständige LEG optimal ist.",
        "Die Tarifklasse ≤ oder > 50 MWh/Jahr wird je Einzelteilnehmer aus annualisierten kWh bestimmt; die LEG löst keinen Tarifwechsel auf eine gemeinsame Tarifklasse aus.",
        "Der Leistungspreis wird nicht optimiert, weil der LEG-Tarif den Leistungsanteil nicht reduziert.",
        f"LEG-Netzkostenrabatt = {discount_percent}%-Rabatt auf den CKW-Netznutzungseinheitstarif des jedem Teilnehmer zugeordneten lokal verbrauchten Solarstroms.",
        "Systemdienstleistungen, Stromreserve, solidarisierte Kosten, Leistungspreis, Messung, Abgaben und MWST werden nicht rabattiert.",
        "Der lokale Energiepreis wird zwischen Produzenten und Verbrauchern vereinbart und deshalb separat als Produzentenerlös ausgewiesen.",
        "Kostenersparnis bezeichnet Netztarifrabatt plus Differenz zwischen externem Energiepreis und vereinbartem lokalen Energiepreis; ein Tarifwechsel-Effekt wird nicht angesetzt.",
    ]


def summarize_project(*, project_id: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        project = get_project(project_id=project_id, db_path=db_path)
        file_rows = con.execute(
            """
            SELECT file_id, original_filename, upload_kind, flow_kind, total_kwh, warnings_json
            FROM (
                SELECT
                    file_id,
                    original_filename,
                    upload_kind,
                    flow_kind,
                    total_kwh,
                    warnings_json,
                    ingested_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(
                            content_hash,
                            original_filename || '|' || upload_kind || '|' || flow_kind || '|' || COALESCE(CAST(total_kwh AS TEXT), '')
                        )
                        ORDER BY ingested_at, file_id
                    ) AS rn
                FROM files
                WHERE project_id = ?
            )
            WHERE rn = 1
            ORDER BY ingested_at
            """,
            [project_id],
        ).fetchall()
        flow_df = _timeseries_for_project(con, project_id=project_id)
        zev_flow = _zev_flow_graph(con, project_id=project_id)
    finally:
        con.close()

    pivot = _pivot_flows(flow_df)
    warnings: list[str] = []
    files: list[dict[str, Any]] = []
    for row in file_rows:
        row_warnings = [_translate_warning(warning) for warning in json.loads(row[5] or "[]")]
        warnings.extend(row_warnings)
        files.append(
            {
                "file_id": row[0],
                "original_filename": row[1],
                "upload_kind": row[2],
                "flow_kind": row[3],
                "total_kwh": row[4],
                "warnings": row_warnings,
            }
        )

    if not pivot.empty and float(pivot[FLOW_PV_EXCESS].sum()) > 0 and float(pivot[FLOW_PV_PRODUCTION].sum()) == 0:
        warnings.append("Es sind nur PV-Überschussdateien vorhanden; die gesamte PV-Produktion ist unbekannt.")
    if not pivot.empty and float(pivot[FLOW_PV_EXCESS].sum()) > float(pivot[FLOW_PV_PRODUCTION].sum()) > 0:
        warnings.append(
            "Der ausgewiesene PV-Überschuss ist höher als die ausgewiesene PV-Produktion, weil das Projekt "
            "Brutto-PV-Zeitreihen und PV-Überschuss-Zeitreihen aus unterschiedlichen Messquellen enthält. "
            "Die Rohwerte sind nicht direkt vergleichbar."
        )

    available_solar = _available_solar_kwh(pivot) if not pivot.empty else pd.Series(dtype=float)
    consumption = (
        pd.to_numeric(pivot[FLOW_CONSUMPTION], errors="coerce").fillna(0.0)
        if not pivot.empty
        else pd.Series(dtype=float)
    )
    interval_local = available_solar.combine(consumption, min) if not pivot.empty else pd.Series(dtype=float)

    return {
        **project,
        "files": files,
        "zev_flow": zev_flow,
        "warnings": sorted(set(warnings)),
        "totals": {
            "consumption_kwh": float(consumption.sum()) if not consumption.empty else 0.0,
            "pv_production_kwh": float(pivot[FLOW_PV_PRODUCTION].sum()) if not pivot.empty else 0.0,
            "pv_excess_kwh": float(pivot[FLOW_PV_EXCESS].sum()) if not pivot.empty else 0.0,
            "grid_import_kwh": float(pivot[FLOW_GRID_IMPORT].sum()) if not pivot.empty else 0.0,
            "local_pv_consumption_kwh": float(pivot[FLOW_LOCAL_PV_CONSUMPTION].sum()) if not pivot.empty else 0.0,
            "available_solar_kwh": float(available_solar.sum()) if not available_solar.empty else 0.0,
            "first_estimated_local_solar_kwh": float(interval_local.sum()) if not interval_local.empty else 0.0,
        },
    }


def project_timeseries(
    *,
    project_id: str,
    kind: str,
    resolution: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> list[dict[str, Any]]:
    con = connect_portal_db(db_path)
    try:
        df = _pivot_flows(_timeseries_for_project(con, project_id=project_id))
        leg_inputs = _leg_input_timeseries_for_project(con, project_id=project_id)
    finally:
        con.close()
    if df.empty:
        return []

    leg_inputs["timestamp"] = pd.to_datetime(leg_inputs["timestamp"], errors="raise")
    leg_inputs = leg_inputs.set_index("timestamp").reindex(df["timestamp"], fill_value=0.0)
    df["available_solar_kwh"] = leg_inputs["available_solar_kwh"].to_numpy(dtype=float)
    leg_consumption = leg_inputs["consumption_kwh"].to_numpy(dtype=float)
    df["local_solar_kwh"] = np.minimum(df["available_solar_kwh"], leg_consumption)
    df["residual_solar_kwh"] = (df["available_solar_kwh"] - df["local_solar_kwh"]).clip(lower=0.0)
    df["grid_import_kwh"] = (leg_consumption - df["local_solar_kwh"]).clip(lower=0.0)

    cols_by_kind = {
        "solar": ["timestamp", FLOW_PV_PRODUCTION, FLOW_PV_EXCESS, "available_solar_kwh"],
        "consumption": ["timestamp", FLOW_CONSUMPTION],
        "zev": ["timestamp", FLOW_GRID_IMPORT, FLOW_LOCAL_PV_CONSUMPTION],
        "leg": ["timestamp", "local_solar_kwh", "residual_solar_kwh", "grid_import_kwh"],
    }
    cols = cols_by_kind.get(kind, cols_by_kind["leg"])
    out = df[cols].copy()

    if resolution in {"day", "month"}:
        rule = "D" if resolution == "day" else "MS"
        out = out.set_index("timestamp").resample(rule).sum(numeric_only=True).reset_index()
    elif resolution != "15min":
        raise ValueError("Auflösung muss einer dieser Werte sein: 15min, day, month")

    return [
        {
            **{k: (v.isoformat() if k == "timestamp" and hasattr(v, "isoformat") else v) for k, v in row.items()}
        }
        for row in out.to_dict(orient="records")
    ]


def analyze_residual_solar(
    *,
    project_id: str,
    solar_file_ids: list[str] | None = None,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        sources = _solar_sources_for_project(con, project_id=project_id)
        available_file_ids = {source["file_id"] for source in sources}
        selected_file_ids = list(dict.fromkeys(solar_file_ids)) if solar_file_ids is not None else None
        unknown_file_ids = sorted(set(selected_file_ids or []) - available_file_ids)
        if unknown_file_ids:
            raise ValueError(f"Unbekannte PV-Zeitreihe: {', '.join(unknown_file_ids)}")
        df = _leg_input_timeseries_for_project(con, project_id=project_id)
        available_by_source = _available_solar_by_source_for_project(con, project_id=project_id)
    finally:
        con.close()

    empty_totals = {
        "available_solar_kwh": 0.0,
        "local_solar_kwh": 0.0,
        "residual_solar_kwh": 0.0,
        "residual_solar_pct": 0.0,
        "interval_count": 0,
        "residual_interval_count": 0,
        "residual_interval_pct": 0.0,
        "peak_residual_solar_kwh": 0.0,
    }
    if df.empty:
        return {
            "totals": empty_totals,
            "peak_interval": None,
            "sources": sources,
            "selected_solar_file_ids": selected_file_ids,
            "monthly": [],
            "hourly": [],
            "heatmap": {
                "year": None,
                "days": [],
                "hours": [],
                "scale_max_residual_solar_kwh": 0.0,
                "selected_peak": None,
                "values": [],
            },
            "peak_intervals": [],
        }

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="raise")
    df["consumption_kwh"] = pd.to_numeric(df["consumption_kwh"], errors="coerce").fillna(0.0)
    all_available_solar = pd.to_numeric(df["available_solar_kwh"], errors="coerce").fillna(0.0)
    all_residual_solar = (all_available_solar - df["consumption_kwh"]).clip(lower=0.0)
    selected_attribution_ids = selected_file_ids if selected_file_ids is not None else list(available_file_ids)
    if available_by_source.empty or not selected_attribution_ids:
        selected_available_solar = pd.Series(0.0, index=df.index)
    else:
        available_by_source.index = pd.to_datetime(available_by_source.index, errors="raise")
        selected_available_solar = (
            available_by_source.reindex(index=df["timestamp"], columns=selected_attribution_ids, fill_value=0.0)
            .fillna(0.0)
            .sum(axis=1)
            .reset_index(drop=True)
        )
    residual_share = selected_available_solar.divide(all_available_solar.replace(0.0, np.nan)).fillna(0.0)
    df["available_solar_kwh"] = selected_available_solar
    df["residual_solar_kwh"] = all_residual_solar * residual_share
    df["local_solar_kwh"] = (df["available_solar_kwh"] - df["residual_solar_kwh"]).clip(lower=0.0)
    df["has_residual_solar"] = (df["residual_solar_kwh"] > 0).astype(int)

    available_solar_kwh = float(df["available_solar_kwh"].sum())
    residual_solar_kwh = float(df["residual_solar_kwh"].sum())
    interval_count = int(len(df))
    residual_interval_count = int(df["has_residual_solar"].sum())

    def _with_interval_share(frame: pd.DataFrame) -> pd.DataFrame:
        frame["residual_interval_pct"] = frame["residual_interval_count"] / frame["interval_count"].replace(0, np.nan)
        frame["residual_interval_pct"] = frame["residual_interval_pct"].fillna(0.0)
        return frame

    monthly = _with_interval_share(
        df.set_index("timestamp")
        .resample("MS")
        .agg(
            available_solar_kwh=("available_solar_kwh", "sum"),
            local_solar_kwh=("local_solar_kwh", "sum"),
            residual_solar_kwh=("residual_solar_kwh", "sum"),
            residual_interval_count=("has_residual_solar", "sum"),
            interval_count=("has_residual_solar", "size"),
            peak_residual_solar_kwh=("residual_solar_kwh", "max"),
        )
        .reset_index()
    )
    monthly["residual_solar_pct"] = monthly["residual_solar_kwh"] / monthly["available_solar_kwh"].replace(0, np.nan)
    monthly["residual_solar_pct"] = monthly["residual_solar_pct"].fillna(0.0)

    hourly = _with_interval_share(
        df.assign(hour=df["timestamp"].dt.hour)
        .groupby("hour", as_index=False)
        .agg(
            residual_solar_kwh=("residual_solar_kwh", "sum"),
            residual_interval_count=("has_residual_solar", "sum"),
            interval_count=("has_residual_solar", "size"),
            peak_residual_solar_kwh=("residual_solar_kwh", "max"),
        )
    )
    positive_hourly_average = (
        df[df["has_residual_solar"] > 0]
        .assign(hour=lambda item: item["timestamp"].dt.hour)
        .groupby("hour")["residual_solar_kwh"]
        .mean()
    )
    hourly["average_residual_when_present_kwh"] = hourly["hour"].map(positive_hourly_average).fillna(0.0)
    hourly["label"] = hourly["hour"].map(lambda value: f"{int(value):02d}:00")

    heatmap_year = int(df["timestamp"].dt.year.mode().iloc[0])
    heatmap_days = pd.date_range(f"{heatmap_year}-01-01", f"{heatmap_year}-12-31", freq="D")
    heatmap_hours = list(range(6, 23))
    heatmap_source = df.assign(date=df["timestamp"].dt.normalize(), hour=df["timestamp"].dt.hour)
    heatmap = (
        heatmap_source
        .query("6 <= hour <= 22")
        .groupby(["date", "hour"])
        .agg(
            residual_solar_kwh=("residual_solar_kwh", "sum"),
            interval_count=("residual_solar_kwh", "size"),
        )
        .reindex(pd.MultiIndex.from_product([heatmap_days, heatmap_hours], names=["date", "hour"]))
        .reset_index()
    )
    all_heatmap = (
        heatmap_source.assign(all_residual_solar_kwh=all_residual_solar)
        .query("6 <= hour <= 22")
        .groupby(["date", "hour"], as_index=False)
        .agg(residual_solar_kwh=("all_residual_solar_kwh", "sum"))
    )
    scale_max_residual_solar_kwh = (
        float(all_heatmap["residual_solar_kwh"].max()) if not all_heatmap.empty else 0.0
    )
    selected_heatmap_values = heatmap[heatmap["residual_solar_kwh"].fillna(0.0) > 0]
    if selected_heatmap_values.empty:
        selected_heatmap_peak = None
    else:
        selected_peak_row = selected_heatmap_values.loc[selected_heatmap_values["residual_solar_kwh"].idxmax()]
        selected_heatmap_peak = {
            "date": selected_peak_row["date"].strftime("%Y-%m-%d"),
            "hour": int(selected_peak_row["hour"]),
            "residual_solar_kwh": float(selected_peak_row["residual_solar_kwh"]),
        }

    peak_rows = df[df["has_residual_solar"] > 0].nlargest(10, "residual_solar_kwh")

    def _interval_record(row: pd.Series) -> dict[str, Any]:
        return {
            "timestamp": row["timestamp"].isoformat(),
            "available_solar_kwh": float(row["available_solar_kwh"]),
            "consumption_kwh": float(row["consumption_kwh"]),
            "local_solar_kwh": float(row["local_solar_kwh"]),
            "residual_solar_kwh": float(row["residual_solar_kwh"]),
        }

    peak_interval = _interval_record(peak_rows.iloc[0]) if not peak_rows.empty else None
    return {
        "totals": {
            "available_solar_kwh": available_solar_kwh,
            "local_solar_kwh": float(df["local_solar_kwh"].sum()),
            "residual_solar_kwh": residual_solar_kwh,
            "residual_solar_pct": residual_solar_kwh / available_solar_kwh if available_solar_kwh else 0.0,
            "interval_count": interval_count,
            "residual_interval_count": residual_interval_count,
            "residual_interval_pct": residual_interval_count / interval_count if interval_count else 0.0,
            "peak_residual_solar_kwh": float(df["residual_solar_kwh"].max()),
        },
        "peak_interval": peak_interval,
        "sources": sources,
        "selected_solar_file_ids": selected_file_ids,
        "monthly": [
            {
                **row,
                "timestamp": row["timestamp"].isoformat(),
                "residual_interval_count": int(row["residual_interval_count"]),
                "interval_count": int(row["interval_count"]),
            }
            for row in monthly.to_dict(orient="records")
        ],
        "hourly": [
            {
                **row,
                "hour": int(row["hour"]),
                "residual_interval_count": int(row["residual_interval_count"]),
                "interval_count": int(row["interval_count"]),
            }
            for row in hourly.to_dict(orient="records")
        ],
        "heatmap": {
            "year": heatmap_year,
            "days": [day.strftime("%Y-%m-%d") for day in heatmap_days],
            "hours": [f"{hour:02d}:00" for hour in heatmap_hours],
            "scale_max_residual_solar_kwh": scale_max_residual_solar_kwh,
            "selected_peak": selected_heatmap_peak,
            "values": [
                {
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "hour": int(row["hour"]),
                    "residual_solar_kwh": (
                        float(row["residual_solar_kwh"]) if pd.notna(row["residual_solar_kwh"]) else None
                    ),
                    "interval_count": int(row["interval_count"]) if pd.notna(row["interval_count"]) else 0,
                }
                for row in heatmap.to_dict(orient="records")
            ],
        },
        "peak_intervals": [_interval_record(row) for _, row in peak_rows.iterrows()],
    }


def analyze_load_shifting(
    *,
    project_id: str,
    consumption_file_id: str | None = None,
    year: int | None = None,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT
                m.timestamp,
                f.file_id,
                f.original_filename,
                f.metadata_json,
                f.flow_kind = ? AS is_zev,
                SUM(m.kwh) AS kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind = ? AND m.flow_kind = ?)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY 1, 2
            """,
            [
                project_id,
                FLOW_ZEV_AGGREGATE,
                project_id,
                FLOW_CONSUMPTION,
                FLOW_CONSUMPTION,
                FLOW_ZEV_AGGREGATE,
                FLOW_CONSUMPTION,
            ],
        ).df()
    finally:
        con.close()

    assumptions = [
        "Als Nachtfenster gilt 22:00 bis 06:00 Uhr; als Vergleichsfenster tagsüber 08:00 bis 18:00 Uhr.",
        "Der Nachtverbrauch ist eine Screening-Obergrenze. Aus den Messwerten allein ist nicht ableitbar, "
        "welche Verbraucher technisch verschiebbar sind.",
        "Die Heatmap summiert die 15-Minuten-Messwerte stündlich für ein Kalenderjahr.",
    ]
    empty_totals = {
        "annual_demand_kwh": 0.0,
        "nighttime_demand_kwh": 0.0,
        "nighttime_share": 0.0,
        "daytime_demand_kwh": 0.0,
        "profile_count": 0,
    }
    if measurements.empty:
        return {
            "assumptions": assumptions,
            "years": [],
            "selected_year": None,
            "selected_consumption_file_id": consumption_file_id,
            "totals": empty_totals,
            "profiles": [],
            "heatmap": {
                "year": None,
                "days": [],
                "hours": [],
                "scale_max_demand_kwh": 0.0,
                "selected_peak": None,
                "values": [],
            },
        }

    measurements["timestamp"] = pd.to_datetime(measurements["timestamp"], errors="raise")
    measurements["kwh"] = pd.to_numeric(measurements["kwh"], errors="coerce").fillna(0.0)
    years = sorted(int(value) for value in measurements["timestamp"].dt.year.unique())
    selected_year = int(year) if year is not None else max(years)
    if selected_year not in years:
        raise ValueError(f"Für das Jahr {selected_year} sind keine Verbrauchsmesswerte vorhanden.")

    measurements = measurements.loc[measurements["timestamp"].dt.year == selected_year].copy()
    source_rows = measurements[["file_id", "original_filename", "metadata_json", "is_zev"]].drop_duplicates()
    sources: list[dict[str, Any]] = []
    for _, row in source_rows.iterrows():
        metadata = json.loads(row["metadata_json"] or "{}")
        label = str(metadata.get("vertrag") or row["original_filename"] or row["file_id"])
        if bool(row["is_zev"]):
            label = f"CKW ZEV Verbrauch: {label}"
        sources.append(
            {
                "file_id": str(row["file_id"]),
                "label": label,
                "address": metadata.get("adresse"),
                "is_zev": bool(row["is_zev"]),
            }
        )
    sources_by_id = {source["file_id"]: source for source in sources}
    if consumption_file_id is not None and consumption_file_id not in sources_by_id:
        raise ValueError(f"Unbekannte Verbrauchszeitreihe: {consumption_file_id}")

    hourly = (
        measurements.assign(
            date=measurements["timestamp"].dt.normalize(),
            hour=measurements["timestamp"].dt.hour,
        )
        .groupby(["file_id", "date", "hour"], as_index=False)
        .agg(demand_kwh=("kwh", "sum"), interval_count=("kwh", "size"))
    )
    hourly["is_night"] = (hourly["hour"] >= 22) | (hourly["hour"] < 6)
    hourly["is_day"] = (hourly["hour"] >= 8) & (hourly["hour"] < 18)
    hourly["nighttime_demand_kwh"] = hourly["demand_kwh"].where(hourly["is_night"], 0.0)
    hourly["daytime_demand_kwh"] = hourly["demand_kwh"].where(hourly["is_day"], 0.0)

    profile_summary = (
        hourly.groupby("file_id", as_index=False)
        .agg(
            annual_demand_kwh=("demand_kwh", "sum"),
            nighttime_demand_kwh=("nighttime_demand_kwh", "sum"),
            daytime_demand_kwh=("daytime_demand_kwh", "sum"),
            peak_hour_demand_kwh=("demand_kwh", "max"),
            coverage_days=("date", "nunique"),
        )
    )
    profile_summary["nighttime_share"] = (
        profile_summary["nighttime_demand_kwh"]
        / profile_summary["annual_demand_kwh"].replace(0.0, np.nan)
    ).fillna(0.0)
    profile_summary["average_nightly_demand_kwh"] = (
        profile_summary["nighttime_demand_kwh"]
        / profile_summary["coverage_days"].replace(0, np.nan)
    ).fillna(0.0)
    profiles = []
    for row in profile_summary.to_dict(orient="records"):
        source = sources_by_id[str(row["file_id"])]
        profiles.append(
            {
                **source,
                "annual_demand_kwh": float(row["annual_demand_kwh"]),
                "nighttime_demand_kwh": float(row["nighttime_demand_kwh"]),
                "nighttime_share": float(row["nighttime_share"]),
                "daytime_demand_kwh": float(row["daytime_demand_kwh"]),
                "average_nightly_demand_kwh": float(row["average_nightly_demand_kwh"]),
                "peak_hour_demand_kwh": float(row["peak_hour_demand_kwh"]),
                "coverage_days": int(row["coverage_days"]),
            }
        )
    profiles.sort(key=lambda row: (row["nighttime_demand_kwh"], row["nighttime_share"]), reverse=True)

    selected_hourly = hourly
    selected_profiles = profiles
    if consumption_file_id is not None:
        selected_hourly = hourly.loc[hourly["file_id"] == consumption_file_id]
        selected_profiles = [profile for profile in profiles if profile["file_id"] == consumption_file_id]

    annual_demand_kwh = sum(float(profile["annual_demand_kwh"]) for profile in selected_profiles)
    nighttime_demand_kwh = sum(float(profile["nighttime_demand_kwh"]) for profile in selected_profiles)
    daytime_demand_kwh = sum(float(profile["daytime_demand_kwh"]) for profile in selected_profiles)

    heatmap_days = pd.date_range(f"{selected_year}-01-01", f"{selected_year}-12-31", freq="D")
    heatmap_hours = list(range(24))
    heatmap = (
        selected_hourly.groupby(["date", "hour"])
        .agg(demand_kwh=("demand_kwh", "sum"), interval_count=("interval_count", "sum"))
        .reindex(pd.MultiIndex.from_product([heatmap_days, heatmap_hours], names=["date", "hour"]))
        .reset_index()
    )
    populated_heatmap = heatmap.loc[heatmap["interval_count"].fillna(0) > 0]
    if populated_heatmap.empty:
        selected_peak = None
        scale_max_demand_kwh = 0.0
    else:
        selected_peak_row = populated_heatmap.loc[populated_heatmap["demand_kwh"].idxmax()]
        selected_peak = {
            "date": selected_peak_row["date"].strftime("%Y-%m-%d"),
            "hour": int(selected_peak_row["hour"]),
            "demand_kwh": float(selected_peak_row["demand_kwh"]),
        }
        scale_max_demand_kwh = float(selected_peak_row["demand_kwh"])

    return {
        "assumptions": assumptions,
        "years": years,
        "selected_year": selected_year,
        "selected_consumption_file_id": consumption_file_id,
        "totals": {
            "annual_demand_kwh": annual_demand_kwh,
            "nighttime_demand_kwh": nighttime_demand_kwh,
            "nighttime_share": nighttime_demand_kwh / annual_demand_kwh if annual_demand_kwh else 0.0,
            "daytime_demand_kwh": daytime_demand_kwh,
            "profile_count": len(selected_profiles),
        },
        "profiles": profiles,
        "heatmap": {
            "year": selected_year,
            "days": [day.strftime("%Y-%m-%d") for day in heatmap_days],
            "hours": [f"{hour:02d}:00" for hour in heatmap_hours],
            "scale_max_demand_kwh": scale_max_demand_kwh,
            "selected_peak": selected_peak,
            "values": [
                {
                    "date": row["date"].strftime("%Y-%m-%d"),
                    "hour": int(row["hour"]),
                    "demand_kwh": float(row["demand_kwh"]) if pd.notna(row["demand_kwh"]) else None,
                    "interval_count": int(row["interval_count"]) if pd.notna(row["interval_count"]) else 0,
                }
                for row in heatmap.to_dict(orient="records")
            ],
        },
    }


def analyze_battery_placements(
    *,
    project_id: str,
    tariff_year: int = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        solar_sources = _solar_sources_for_project(con, project_id=project_id)
        consumer_files = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT f.file_id, f.original_filename, f.metadata_json, f.flow_kind = ? AS is_zev
            FROM files f
            JOIN selected_files sf USING (file_id)
            WHERE f.project_id = ? AND f.flow_kind IN (?, ?)
            ORDER BY f.ingested_at, f.file_id
            """,
            [project_id, FLOW_ZEV_AGGREGATE, project_id, FLOW_CONSUMPTION, FLOW_ZEV_AGGREGATE],
        ).df()
        solar_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT m.timestamp, m.file_id, SUM(m.kwh) AS kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind IN (?, ?) AND m.flow_kind = f.flow_kind)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2
            ORDER BY 1
            """,
            [
                project_id,
                project_id,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
                FLOW_ZEV_AGGREGATE,
                FLOW_PV_EXCESS,
            ],
        ).df()
        consumer_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT m.timestamp, m.file_id, SUM(m.kwh) AS kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind = ? AND m.flow_kind = ?)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2
            ORDER BY 1
            """,
            [
                project_id,
                project_id,
                FLOW_CONSUMPTION,
                FLOW_CONSUMPTION,
                FLOW_ZEV_AGGREGATE,
                FLOW_GRID_IMPORT,
            ],
        ).df()
        pv_battery_consumer_rows = con.execute(
            """
            SELECT solar_file_id, consumer_file_id
            FROM pv_battery_consumers
            WHERE project_id = ?
            ORDER BY solar_file_id, consumer_file_id
            """,
            [project_id],
        ).fetchall()
    finally:
        con.close()

    timestamps = sorted(
        set(pd.to_datetime(solar_measurements.get("timestamp", pd.Series(dtype="datetime64[ns]")), errors="raise"))
        | set(pd.to_datetime(consumer_measurements.get("timestamp", pd.Series(dtype="datetime64[ns]")), errors="raise"))
    )
    timestamp_index = pd.Index(timestamps, name="timestamp")
    solar_wide = (
        solar_measurements.assign(timestamp=pd.to_datetime(solar_measurements["timestamp"], errors="raise"))
        .pivot_table(index="timestamp", columns="file_id", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .reindex(timestamp_index, fill_value=0.0)
        if not solar_measurements.empty
        else pd.DataFrame(index=timestamp_index)
    )
    consumer_wide = (
        consumer_measurements.assign(timestamp=pd.to_datetime(consumer_measurements["timestamp"], errors="raise"))
        .pivot_table(index="timestamp", columns="file_id", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .reindex(timestamp_index, fill_value=0.0)
        if not consumer_measurements.empty
        else pd.DataFrame(index=timestamp_index)
    )
    consumer_options: list[dict[str, Any]] = []
    for _, row in consumer_files.iterrows():
        metadata = json.loads(row.get("metadata_json") or "{}")
        is_zev = bool(row["is_zev"])
        label = metadata.get("vertrag") or row.get("original_filename") or str(row["file_id"])
        if is_zev:
            label = f"CKW ZEV Restbezug: {label}"
        consumer_options.append(
            {
                "file_id": str(row["file_id"]),
                "label": label,
                "address": metadata.get("adresse"),
                "is_zev": is_zev,
            }
        )
    consumer_options_by_id = {item["file_id"]: item for item in consumer_options}
    selected_consumer_ids_by_solar_id: dict[str, list[str]] = {}
    for solar_file_id, consumer_file_id in pv_battery_consumer_rows:
        selected_consumer_ids_by_solar_id.setdefault(str(solar_file_id), []).append(str(consumer_file_id))

    pv_sites: list[dict[str, Any]] = []
    for source in solar_sources:
        file_id = source["file_id"]
        is_zev = source["flow_kind"] == FLOW_ZEV_AGGREGATE
        selected_consumer_file_ids = [file_id] if is_zev else selected_consumer_ids_by_solar_id.get(file_id, [])
        configured = bool(selected_consumer_file_ids)
        corresponding_consumption = (
            consumer_wide.reindex(columns=selected_consumer_file_ids, fill_value=0.0).sum(axis=1).to_numpy(dtype=float)
            if configured
            else np.zeros(len(timestamp_index), dtype=float)
        )
        pv_values = (
            solar_wide[file_id].to_numpy(dtype=float)
            if file_id in solar_wide.columns
            else np.zeros(len(timestamp_index), dtype=float)
        )
        simulation = simulate_pv_battery(
            timestamps=timestamp_index,
            pv_kwh=pv_values,
            consumption_kwh=corresponding_consumption,
        )
        totals = simulation.totals
        pv_sites.append(
            {
                **source,
                "configured": configured,
                "assignment_mode": "implicit_zev" if is_zev else "manual",
                "selected_consumer_file_ids": selected_consumer_file_ids,
                "assigned_consumers": [
                    consumer_options_by_id[consumer_id]
                    for consumer_id in selected_consumer_file_ids
                    if consumer_id in consumer_options_by_id
                ],
                **totals,
                "additional_local_solar_pct": (
                    totals["additional_local_solar_kwh"] / totals["pv_kwh"] if totals["pv_kwh"] else 0.0
                ),
            }
        )
    pv_sites.sort(
        key=lambda item: (item["configured"], item["additional_local_solar_kwh"], item["pv_kwh"]),
        reverse=True,
    )

    consumption_sites: list[dict[str, Any]] = []
    for _, row in consumer_files.iterrows():
        file_id = str(row["file_id"])
        if file_id not in consumer_wide.columns:
            continue
        site_measurements = consumer_measurements[consumer_measurements["file_id"].astype(str) == file_id].copy()
        site_measurements["timestamp"] = pd.to_datetime(site_measurements["timestamp"], errors="raise")
        site_measurements = site_measurements.sort_values("timestamp")
        measured_kwh = float(site_measurements["kwh"].sum())
        annualized_kwh = measured_kwh * _infer_annualization_factor(site_measurements["timestamp"])
        year = tariff_year or int(site_measurements["timestamp"].dt.year.mode().iloc[0])
        tariff = select_import_tariff(year=year, kwh=annualized_kwh)
        simulation = simulate_peak_shaving(
            timestamps=site_measurements["timestamp"],
            consumption_kwh=site_measurements["kwh"],
        )
        rate = float(tariff.netz_leistungstarif_chf_per_kw_month)
        baseline_demand_charge_chf = simulation.totals["baseline_peak_kw_month_sum"] * rate
        battery_demand_charge_chf = simulation.totals["battery_peak_kw_month_sum"] * rate
        consumer = consumer_options_by_id[file_id]
        consumption_sites.append(
            {
                "file_id": file_id,
                "label": consumer["label"],
                "address": consumer["address"],
                "is_zev": consumer["is_zev"],
                "tariff_year": year,
                "tariff_tier": _tariff_label(annualized_kwh),
                "measured_kwh": measured_kwh,
                "annualized_kwh": annualized_kwh,
                "analyzed_months": len(simulation.monthly),
                "demand_rate_chf_per_kw_month": rate,
                **simulation.totals,
                "baseline_demand_charge_chf": baseline_demand_charge_chf,
                "battery_demand_charge_chf": battery_demand_charge_chf,
                "demand_charge_savings_chf": baseline_demand_charge_chf - battery_demand_charge_chf,
                "monthly": simulation.monthly,
            }
        )
    consumption_sites.sort(key=lambda item: item["demand_charge_savings_chf"], reverse=True)

    return {
        "battery": {
            "capacity_kwh": DEFAULT_BATTERY.capacity_kwh,
            "max_charge_kw": DEFAULT_BATTERY.max_charge_kw,
            "max_discharge_kw": DEFAULT_BATTERY.max_discharge_kw,
        },
        "assumptions": [
            "Die Batterie wird mit 100 kWh Kapazität und 30 kW Lade- sowie Entladeleistung modelliert.",
            "Die Simulation verwendet einen idealisierten Speicher ohne Umwandlungsverluste.",
            "PV-Standorte werden einzeln gegen ihre zugeordneten Verbraucher bewertet. Bei klassischen PV-Zeitreihen wird diese Zuordnung manuell erfasst; bei CKW-ZEV-Aggregaten ist der Restbezug desselben ZEV automatisch zugeordnet.",
            "Die Zusatznutzung mehrerer PV-Standorte darf nicht addiert werden.",
            "Beim Peak Shaving wird jeder Verbraucher einzeln bewertet. Die Batterie startet und endet in jedem Kalendermonat leer und wird unterhalb der optimierten Leistungsspitze aus dem Netz geladen.",
        ],
        "pv_sites": pv_sites,
        "consumer_options": consumer_options,
        "consumption_sites": consumption_sites,
        "best_pv_site": next((site for site in pv_sites if site["configured"]), None),
        "best_consumption_site": consumption_sites[0] if consumption_sites else None,
    }


def set_pv_battery_consumers(
    *,
    project_id: str,
    solar_file_id: str,
    consumer_file_ids: list[str],
    tariff_year: int | None = None,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    selected_consumer_file_ids = list(dict.fromkeys(consumer_file_ids))
    con = connect_portal_db(db_path)
    transaction_started = False
    try:
        solar_row = con.execute(
            "SELECT flow_kind FROM files WHERE project_id = ? AND file_id = ?",
            [project_id, solar_file_id],
        ).fetchone()
        if solar_row is None:
            raise ValueError("Unbekannte PV-Zeitreihe.")
        if solar_row[0] == FLOW_ZEV_AGGREGATE:
            raise ValueError("Bei CKW-ZEV-Aggregaten ist der zugehörige Restbezug bereits automatisch festgelegt.")
        if solar_row[0] not in {FLOW_PV_PRODUCTION, FLOW_PV_EXCESS}:
            raise ValueError("Die ausgewählte Datei ist keine PV-Zeitreihe.")

        available_consumer_ids = {
            str(row[0])
            for row in con.execute(
                "SELECT file_id FROM files WHERE project_id = ? AND flow_kind IN (?, ?)",
                [project_id, FLOW_CONSUMPTION, FLOW_ZEV_AGGREGATE],
            ).fetchall()
        }
        unknown_consumer_ids = sorted(set(selected_consumer_file_ids) - available_consumer_ids)
        if unknown_consumer_ids:
            raise ValueError(f"Unbekannte Verbrauchszeitreihe: {', '.join(unknown_consumer_ids)}")

        con.execute("BEGIN TRANSACTION")
        transaction_started = True
        con.execute(
            "DELETE FROM pv_battery_consumers WHERE project_id = ? AND solar_file_id = ?",
            [project_id, solar_file_id],
        )
        for consumer_file_id in selected_consumer_file_ids:
            con.execute(
                """
                INSERT INTO pv_battery_consumers (project_id, solar_file_id, consumer_file_id)
                VALUES (?, ?, ?)
                """,
                [project_id, solar_file_id, consumer_file_id],
            )
        con.execute("COMMIT")
        transaction_started = False
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()

    return analyze_battery_placements(project_id=project_id, tariff_year=tariff_year, db_path=db_path)


def calculate_leg_potential(
    *,
    project_id: str,
    tariff_year: int | None = None,
    network_discount: Decimal = Decimal("0.40"),
    leg_energy_price_rp_per_kwh: Decimal | None = None,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    if network_discount not in {Decimal("0.20"), Decimal("0.40")}:
        raise ValueError("network_discount must be 0.40 (one network level) or 0.20 (multiple network levels)")
    configured_energy_rp = (
        leg_energy_price_rp_per_kwh
        if isinstance(leg_energy_price_rp_per_kwh, Decimal)
        else Decimal(str(leg_energy_price_rp_per_kwh))
        if leg_energy_price_rp_per_kwh is not None
        else None
    )
    if configured_energy_rp is not None and configured_energy_rp < 0:
        raise ValueError("leg_energy_price_rp_per_kwh must be non-negative")

    con = connect_portal_db(db_path)
    try:
        df = _leg_input_timeseries_for_project(con, project_id=project_id)
        participant_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT
                m.timestamp,
                m.file_id,
                SUM(m.kwh) AS kwh,
                ANY_VALUE(m.meter_label) AS meter_label,
                ANY_VALUE(f.original_filename) AS original_filename,
                ANY_VALUE(f.metadata_json) AS metadata_json
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind = ? AND m.flow_kind = ?)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2
            ORDER BY 1
            """,
            [
                project_id,
                project_id,
                FLOW_CONSUMPTION,
                FLOW_CONSUMPTION,
                FLOW_ZEV_AGGREGATE,
                FLOW_GRID_IMPORT,
            ],
        ).df()
    finally:
        con.close()
    if df.empty:
        return {
            "assumptions": [
                f"LEG {int(network_discount * 100)}% wird nur auf den CKW-Netznutzungseinheitstarif angewendet.",
                "Der lokale Energiepreis wird zwischen Produzenten und Verbrauchern vereinbart.",
                "Ohne Messwerte werden noch keine Tarif- oder Potenzialwerte berechnet.",
            ],
            "warnings": ["Es sind noch keine Messwerte hochgeladen."],
            "tariff_year": tariff_year or DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
            "annualization_factor": 0.0,
            "annualized_consumption_kwh": 0.0,
            "tariff_tier": _tariff_label(0.0),
            "pricing_scope": "empty_project",
            "rates": {
                "external_energy_rp_per_kwh": 0.0,
                "energy_rp_per_kwh": 0.0,
                "leg_energy_price_rp_per_kwh": float(configured_energy_rp or Decimal("0")),
                "discountable_netznutzung_rp_per_kwh": 0.0,
                "variable_netznutzung_rp_per_kwh": 0.0,
                "network_discount": float(network_discount),
                "network_discount_rp_per_kwh": 0.0,
            },
            "totals": {
                "consumption_kwh": 0.0,
                "available_solar_kwh": 0.0,
                "local_solar_consumed_kwh": 0.0,
                "residual_solar_kwh": 0.0,
                "remaining_grid_import_kwh": 0.0,
                "avoided_external_energy_purchase_chf": 0.0,
                "local_solar_energy_value_chf": 0.0,
                "producer_revenue_chf": 0.0,
                "energy_price_savings_chf": 0.0,
                "energy_savings_chf": 0.0,
                "network_discount_chf": 0.0,
                "consumer_total_savings_chf": 0.0,
                "total_monetary_benefit_chf": 0.0,
                "total_gross_potential_chf": 0.0,
            },
            "monthly": [],
            "participants": [],
        }

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="raise")
    year = tariff_year or int(df["timestamp"].dt.year.mode().iloc[0])
    consumption = pd.to_numeric(df["consumption_kwh"], errors="coerce").fillna(0.0)
    available_solar = pd.to_numeric(df["available_solar_kwh"], errors="coerce").fillna(0.0)
    local_solar = available_solar.combine(consumption, min)
    residual_solar = (available_solar - local_solar).clip(lower=0.0)
    grid_import = (consumption - local_solar).clip(lower=0.0)

    annualization_factor = _infer_annualization_factor(df["timestamp"])
    annual_basis_kwh = float(consumption.sum()) * annualization_factor
    interval_avoided_external_energy = np.zeros(len(df), dtype=float)
    interval_producer_revenue = np.zeros(len(df), dtype=float)
    interval_energy_price_savings = np.zeros(len(df), dtype=float)
    interval_network_discount = np.zeros(len(df), dtype=float)
    participant_prices: list[dict[str, Any]] = []

    if not participant_measurements.empty:
        participant_measurements["timestamp"] = pd.to_datetime(
            participant_measurements["timestamp"], errors="raise"
        )
        load_wide = (
            participant_measurements.pivot_table(
                index="timestamp", columns="file_id", values="kwh", aggfunc="sum"
            )
            .fillna(0.0)
            .reindex(pd.DatetimeIndex(df["timestamp"]), fill_value=0.0)
        )
        total_interval_load = load_wide.sum(axis=1)
        load_shares = load_wide.div(total_interval_load.where(total_interval_load > 0), axis=0).fillna(0.0)
        allocated_local = load_shares.mul(local_solar.to_numpy(dtype=float), axis=0)
        file_metadata = participant_measurements.drop_duplicates("file_id").set_index("file_id")

        for file_id in load_wide.columns:
            measured_kwh = float(load_wide[file_id].sum())
            annualized_kwh = measured_kwh * annualization_factor
            tariff = select_import_tariff(year=year, kwh=annualized_kwh)
            external_energy_rp = tariff.standardprodukt_energielieferung_rp_per_kwh
            discountable_net_rp = _variable_net_rp_per_kwh(tariff)
            agreed_energy_rp = configured_energy_rp if configured_energy_rp is not None else external_energy_rp
            local_values = allocated_local[file_id].to_numpy(dtype=float)
            local_kwh = float(local_values.sum())
            interval_avoided_external_energy += local_values * float(external_energy_rp) / 100.0
            interval_producer_revenue += local_values * float(agreed_energy_rp) / 100.0
            interval_energy_price_savings += (
                local_values * float(external_energy_rp - agreed_energy_rp) / 100.0
            )
            interval_network_discount += (
                local_values * float(discountable_net_rp * network_discount) / 100.0
            )
            meta_row = file_metadata.loc[file_id]
            metadata = json.loads(meta_row.get("metadata_json") or "{}")
            participant_prices.append(
                {
                    "file_id": str(file_id),
                    "label": metadata.get("vertrag") or meta_row.get("meter_label") or meta_row.get("original_filename"),
                    "measured_consumption_kwh": measured_kwh,
                    "annualized_consumption_kwh": annualized_kwh,
                    "tariff_tier": _tariff_label(annualized_kwh),
                    "allocated_local_solar_kwh": local_kwh,
                    "external_energy_rp_per_kwh": float(external_energy_rp),
                    "leg_energy_price_rp_per_kwh": float(agreed_energy_rp),
                    "discountable_netznutzung_rp_per_kwh": float(discountable_net_rp),
                    "network_discount_rp_per_kwh": float(discountable_net_rp * network_discount),
                    "producer_revenue_chf": local_kwh * float(agreed_energy_rp) / 100.0,
                    "energy_price_savings_chf": local_kwh * float(external_energy_rp - agreed_energy_rp) / 100.0,
                    "network_discount_chf": local_kwh * float(discountable_net_rp * network_discount) / 100.0,
                    "consumer_total_savings_chf": local_kwh
                    * float((external_energy_rp - agreed_energy_rp) + discountable_net_rp * network_discount)
                    / 100.0,
                }
            )
    else:
        tariff = select_import_tariff(year=year, kwh=annual_basis_kwh)
        external_energy_rp = tariff.standardprodukt_energielieferung_rp_per_kwh
        discountable_net_rp = _variable_net_rp_per_kwh(tariff)
        agreed_energy_rp = configured_energy_rp if configured_energy_rp is not None else external_energy_rp
        local_values = local_solar.to_numpy(dtype=float)
        interval_avoided_external_energy = local_values * float(external_energy_rp) / 100.0
        interval_producer_revenue = local_values * float(agreed_energy_rp) / 100.0
        interval_energy_price_savings = local_values * float(external_energy_rp - agreed_energy_rp) / 100.0
        interval_network_discount = local_values * float(discountable_net_rp * network_discount) / 100.0

    participant_prices.sort(key=lambda participant: str(participant["label"]))
    local_weights = np.array(
        [participant["allocated_local_solar_kwh"] for participant in participant_prices], dtype=float
    )
    rate_weights = local_weights if float(local_weights.sum()) > 0 else np.array(
        [participant["measured_consumption_kwh"] for participant in participant_prices], dtype=float
    )

    def _weighted_participant_rate(key: str, fallback: Decimal) -> Decimal:
        if participant_prices and float(rate_weights.sum()) > 0:
            values = np.array([participant[key] for participant in participant_prices], dtype=float)
            return Decimal(str(float(np.dot(values, rate_weights) / rate_weights.sum())))
        return fallback

    fallback_tariff = select_import_tariff(year=year, kwh=annual_basis_kwh)
    fallback_external_rp = fallback_tariff.standardprodukt_energielieferung_rp_per_kwh
    fallback_net_rp = _variable_net_rp_per_kwh(fallback_tariff)
    fallback_agreed_rp = configured_energy_rp if configured_energy_rp is not None else fallback_external_rp
    energy_rp = _weighted_participant_rate("external_energy_rp_per_kwh", fallback_external_rp)
    discountable_net_rp = _weighted_participant_rate("discountable_netznutzung_rp_per_kwh", fallback_net_rp)
    agreed_energy_rp = _weighted_participant_rate("leg_energy_price_rp_per_kwh", fallback_agreed_rp)

    avoided_external_energy_chf = Decimal(str(float(interval_avoided_external_energy.sum())))
    producer_revenue_chf = Decimal(str(float(interval_producer_revenue.sum())))
    energy_price_savings_chf = Decimal(str(float(interval_energy_price_savings.sum())))
    network_discount_chf = Decimal(str(float(interval_network_discount.sum())))
    total_monetary_benefit_chf = energy_price_savings_chf + network_discount_chf

    monthly_df = pd.DataFrame(
        {
            "timestamp": df["timestamp"],
            "consumption_kwh": consumption,
            "available_solar_kwh": available_solar,
            "local_solar_kwh": local_solar,
            "residual_solar_kwh": residual_solar,
            "grid_import_kwh": grid_import,
            "avoided_external_energy_purchase_chf": interval_avoided_external_energy,
            "producer_revenue_chf": interval_producer_revenue,
            "energy_price_savings_chf": interval_energy_price_savings,
            "network_discount_chf": interval_network_discount,
        }
    ).set_index("timestamp").resample("MS").sum(numeric_only=True).reset_index()
    monthly_df["energy_savings_chf"] = monthly_df["energy_price_savings_chf"]
    monthly_df["total_monetary_benefit_chf"] = monthly_df["energy_price_savings_chf"] + monthly_df["network_discount_chf"]
    monthly_df["total_potential_chf"] = monthly_df["total_monetary_benefit_chf"]

    warnings: list[str] = []
    if float(consumption.sum()) == 0:
        warnings.append("Es sind keine Verbrauchsdaten hochgeladen; das LEG-Verbrauchspotenzial ist null.")

    return {
        "assumptions": [
            f"LEG {int(network_discount * 100)}% wird nur auf den CKW-Netznutzungseinheitstarif angewendet.",
            "Der lokale Energiepreis wird zwischen Produzenten und Verbrauchern vereinbart. Ohne Eingabe setzt die Simulation ihn transparent gleich dem externen CKW-Energiepreis; daraus entsteht kein zusätzlicher Energiepreisvorteil.",
            "Systemdienstleistungen, Stromreserve, solidarisierte Kosten, Leistungspreise, Messung, öffentliche Abgaben und MWST sind nicht Teil des LEG-Rabatts.",
        ],
        "warnings": warnings,
        "tariff_year": year,
        "annualization_factor": annualization_factor,
        "annualized_consumption_kwh": annual_basis_kwh,
        "tariff_tier": _tariff_label(annual_basis_kwh),
        "pricing_scope": "participant_specific" if participant_prices else "aggregate_fallback",
        "rates": {
            "external_energy_rp_per_kwh": float(energy_rp),
            "energy_rp_per_kwh": float(energy_rp),
            "leg_energy_price_rp_per_kwh": float(agreed_energy_rp),
            "discountable_netznutzung_rp_per_kwh": float(discountable_net_rp),
            "variable_netznutzung_rp_per_kwh": float(discountable_net_rp),
            "network_discount": float(network_discount),
            "network_discount_rp_per_kwh": float(discountable_net_rp * network_discount),
        },
        "totals": {
            "consumption_kwh": float(consumption.sum()),
            "available_solar_kwh": float(available_solar.sum()),
            "local_solar_consumed_kwh": float(local_solar.sum()),
            "residual_solar_kwh": float(residual_solar.sum()),
            "remaining_grid_import_kwh": float(grid_import.sum()),
            "avoided_external_energy_purchase_chf": float(avoided_external_energy_chf),
            "local_solar_energy_value_chf": float(producer_revenue_chf),
            "producer_revenue_chf": float(producer_revenue_chf),
            "energy_price_savings_chf": float(energy_price_savings_chf),
            "energy_savings_chf": float(energy_price_savings_chf),
            "network_discount_chf": float(network_discount_chf),
            "consumer_total_savings_chf": float(total_monetary_benefit_chf),
            "total_monetary_benefit_chf": float(total_monetary_benefit_chf),
            "total_gross_potential_chf": float(total_monetary_benefit_chf),
        },
        "monthly": [
            {k: (v.isoformat() if k == "timestamp" and hasattr(v, "isoformat") else v) for k, v in row.items()}
            for row in monthly_df.to_dict(orient="records")
        ],
        "participants": participant_prices,
    }


def simulate_project_vzev(
    *,
    project_id: str,
    tariff_year: int | None = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
    fixed_local_energy_rp_per_kwh: Decimal | None = None,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> VzevSimulation:
    con = connect_portal_db(db_path)
    try:
        consumer_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT m.timestamp, f.file_id, f.original_filename, f.metadata_json, SUM(m.kwh) AS kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND f.flow_kind = ?
              AND m.flow_kind = ?
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 2
            """,
            [project_id, project_id, FLOW_CONSUMPTION, FLOW_CONSUMPTION],
        ).df()
        solar_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT
                m.timestamp,
                SUM(CASE WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh ELSE 0 END) AS pv_production_kwh,
                SUM(CASE WHEN f.flow_kind = ? AND m.flow_kind = ? THEN m.kwh ELSE 0 END) AS pv_excess_kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND f.flow_kind IN (?, ?)
            GROUP BY 1
            ORDER BY 1
            """,
            [
                project_id,
                FLOW_PV_PRODUCTION,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
                FLOW_PV_EXCESS,
                project_id,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
            ],
        ).df()
    finally:
        con.close()

    if consumer_measurements.empty:
        raise ValueError("Für die vZEV-Simulation werden klassische Verbrauchsdaten benötigt.")
    if solar_measurements.empty:
        raise ValueError("Für die vZEV-Simulation wird mindestens eine klassische Solarzeitreihe benötigt.")

    consumer_measurements["timestamp"] = pd.to_datetime(consumer_measurements["timestamp"], errors="raise")
    solar_measurements["timestamp"] = pd.to_datetime(solar_measurements["timestamp"], errors="raise")
    consumer_labels: dict[str, str] = {}
    participant_metering_codes: dict[str, str] = {}
    used_labels: set[str] = set()
    for _, row in consumer_measurements[["file_id", "original_filename", "metadata_json"]].drop_duplicates().iterrows():
        metadata = json.loads(row["metadata_json"] or "{}")
        base_label = str(metadata.get("vertrag") or row["original_filename"] or row["file_id"])
        label = base_label
        suffix = 2
        while label in used_labels:
            label = f"{base_label} ({suffix})"
            suffix += 1
        consumer_labels[str(row["file_id"])] = label
        participant_metering_codes[label] = str(metadata.get("zaehlpunkt") or "")
        used_labels.add(label)

    consumer_measurements["participant"] = consumer_measurements["file_id"].astype(str).map(consumer_labels)
    consumer_wide = (
        consumer_measurements.pivot_table(index="timestamp", columns="participant", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .sort_index()
    )
    solar = solar_measurements.set_index("timestamp").sort_index()
    production = pd.to_numeric(solar["pv_production_kwh"], errors="coerce").fillna(0.0)
    excess = pd.to_numeric(solar["pv_excess_kwh"], errors="coerce").fillna(0.0)
    available_solar = production.where(production > 0, excess)
    timestamps = consumer_wide.index.union(available_solar.index).sort_values()
    participant_df = consumer_wide.reindex(timestamps, fill_value=0.0)
    warnings: list[str] = []
    if float(production.sum()) == 0 and float(excess.sum()) > 0:
        warnings.append(
            "Es wurden nur PV-Überschusszeitreihen hochgeladen. Die synthetische Datei verwendet diesen "
            "verfügbaren Solarstrom als simulierte Produktion; eine Brutto-PV-Produktion ist daraus nicht ableitbar."
        )
    elif float(production.sum()) > 0 and float(excess.sum()) > 0:
        warnings.append(
            "Das Projekt enthält Brutto-PV- und Überschusszeitreihen. Die Simulation bevorzugt je Intervall "
            "die Brutto-PV-Produktion und verwendet Überschusswerte nur als Fallback."
        )
    return simulate_vzev(
        timestamps=timestamps,
        pv_kwh=available_solar.reindex(timestamps, fill_value=0.0),
        consumption_by_participant=participant_df,
        tariff_year=tariff_year,
        warnings=warnings,
        participant_metering_codes=participant_metering_codes,
        fixed_local_energy_rp_per_kwh=fixed_local_energy_rp_per_kwh,
    )


def vzev_simulation_filename(*, project_id: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> str:
    project = get_project(project_id=project_id, db_path=db_path)
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(project["name"])).strip("-")
    return f"vzev-simulation-{slug or 'projekt'}.csv"


def vzev_tariff_comparison_filename(*, project_id: str, db_path: str | Path = DEFAULT_PORTAL_DB) -> str:
    project = get_project(project_id=project_id, db_path=db_path)
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(project["name"])).strip("-")
    return f"vzev-tarifvergleich-{slug or 'projekt'}.csv"


def optimize_leg_combinations(
    *,
    project_id: str,
    tariff_year: int | None = None,
    network_discount: Decimal = Decimal("0.40"),
    leg_energy_price_rp_per_kwh: Decimal | None = None,
    max_exhaustive_participants: int = 18,
    top_n: int = 25,
    db_path: str | Path = DEFAULT_PORTAL_DB,
) -> dict[str, Any]:
    if network_discount not in {Decimal("0.20"), Decimal("0.40")}:
        raise ValueError("network_discount must be 0.40 (one network level) or 0.20 (multiple network levels)")
    con = connect_portal_db(db_path)
    try:
        flow_df = _pivot_flows(_timeseries_for_project(con, project_id=project_id))
        consumer_files = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT
                f.file_id,
                f.original_filename,
                f.total_kwh,
                f.metadata_json,
                f.flow_kind = ? AS is_zev
            FROM files f
            JOIN selected_files sf USING (file_id)
            WHERE f.project_id = ? AND f.flow_kind IN (?, ?)
            ORDER BY f.original_filename
            """,
            [project_id, FLOW_ZEV_AGGREGATE, project_id, FLOW_CONSUMPTION, FLOW_ZEV_AGGREGATE],
        ).df()
        solar_files = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT
                f.file_id,
                f.original_filename,
                CASE WHEN f.flow_kind = ? THEN ? ELSE f.flow_kind END AS flow_kind,
                f.metadata_json,
                f.flow_kind = ? AS is_zev
            FROM files f
            JOIN selected_files sf USING (file_id)
            WHERE f.project_id = ? AND f.flow_kind IN (?, ?, ?)
            ORDER BY f.original_filename
            """,
            [
                project_id,
                FLOW_ZEV_AGGREGATE,
                FLOW_PV_EXCESS,
                FLOW_ZEV_AGGREGATE,
                project_id,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
                FLOW_ZEV_AGGREGATE,
            ],
        ).df()
        measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT m.timestamp, m.file_id, SUM(m.kwh) AS kwh, ANY_VALUE(m.meter_label) AS meter_label
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind = ? AND m.flow_kind = ?)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2
            ORDER BY 1
            """,
            [
                project_id,
                project_id,
                FLOW_CONSUMPTION,
                FLOW_CONSUMPTION,
                FLOW_ZEV_AGGREGATE,
                FLOW_GRID_IMPORT,
            ],
        ).df()
        solar_measurements = con.execute(
            f"""
            WITH selected_files AS ({_deduped_file_cte()})
            SELECT m.timestamp, m.file_id, SUM(m.kwh) AS kwh
            FROM measurements m
            JOIN selected_files sf USING (file_id)
            JOIN files f USING (file_id)
            WHERE m.project_id = ?
              AND (
                  (f.flow_kind IN (?, ?) AND m.flow_kind = f.flow_kind)
                  OR (f.flow_kind = ? AND m.flow_kind = ?)
              )
            GROUP BY 1, 2
            ORDER BY 1
            """,
            [
                project_id,
                project_id,
                FLOW_PV_PRODUCTION,
                FLOW_PV_EXCESS,
                FLOW_ZEV_AGGREGATE,
                FLOW_PV_EXCESS,
            ],
        ).df()
        zev_totals = _zev_flow_totals(con, project_id=project_id)
    finally:
        con.close()

    if flow_df.empty or consumer_files.empty or measurements.empty or solar_files.empty or solar_measurements.empty:
        return {
            "assumptions": _optimization_assumptions(network_discount),
            "warnings": ["Für die Optimierung braucht es Solarzeitreihen und mindestens eine Verbrauchsdatei."],
            "combination_count": 0,
            "evaluated_all": True,
            "optimization_mode": "exhaustive",
            "best": None,
            "combinations": [],
        }

    pivot = flow_df.copy()
    pivot["timestamp"] = pd.to_datetime(pivot["timestamp"], errors="raise")
    year = tariff_year or int(pivot["timestamp"].dt.year.mode().iloc[0])
    measurements["timestamp"] = pd.to_datetime(measurements["timestamp"], errors="raise")
    load_df = (
        measurements.pivot_table(index="timestamp", columns="file_id", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .sort_index()
    )
    all_timestamps = pd.Index(sorted(set(load_df.index).union(set(pivot["timestamp"]))))
    load_df = load_df.reindex(all_timestamps, fill_value=0.0)
    annualization_factor = _infer_annualization_factor(pd.Series(all_timestamps))

    solar_measurements["timestamp"] = pd.to_datetime(solar_measurements["timestamp"], errors="raise")
    solar_wide = (
        solar_measurements.pivot_table(index="timestamp", columns="file_id", values="kwh", aggfunc="sum")
        .fillna(0.0)
        .sort_index()
        .reindex(all_timestamps, fill_value=0.0)
    )
    solar_file_rows: list[dict[str, Any]] = []
    for _, row in solar_files.iterrows():
        metadata = json.loads(row.get("metadata_json") or "{}")
        is_zev = bool(row["is_zev"])
        label = metadata.get("vertrag") or row.get("original_filename") or str(row["file_id"])
        if is_zev:
            label = f"CKW ZEV Überschuss: {label}"
        solar_file_rows.append(
            {
                "file_id": str(row["file_id"]),
                "label": label,
                "address": metadata.get("adresse"),
                "flow_kind": row["flow_kind"],
                "is_zev": is_zev,
            }
        )
    production_ids = [item["file_id"] for item in solar_file_rows if item["flow_kind"] == FLOW_PV_PRODUCTION]
    excess_ids = [item["file_id"] for item in solar_file_rows if item["flow_kind"] == FLOW_PV_EXCESS]
    production_total = (
        solar_wide.reindex(columns=production_ids, fill_value=0.0).sum(axis=1).to_numpy(dtype=float)
        if production_ids
        else np.zeros(len(all_timestamps), dtype=float)
    )
    use_excess = production_total <= 0
    solar_sources: list[dict[str, Any]] = []
    source_vectors: dict[str, np.ndarray] = {}
    for source in solar_file_rows:
        file_id = source["file_id"]
        values = (
            solar_wide[file_id].to_numpy(dtype=float)
            if file_id in solar_wide.columns
            else np.zeros(len(all_timestamps), dtype=float)
        )
        if source["flow_kind"] == FLOW_PV_EXCESS and production_ids and not source["is_zev"]:
            values = np.where(use_excess, values, 0.0)
        elif source["flow_kind"] == FLOW_PV_EXCESS and not excess_ids:
            values = np.zeros(len(all_timestamps), dtype=float)
        source_vectors[file_id] = values

    solar_vector = sum(source_vectors.values(), np.zeros(len(all_timestamps), dtype=float))

    participants: list[dict[str, Any]] = []
    for _, row in consumer_files.iterrows():
        file_id = str(row["file_id"])
        if file_id not in load_df.columns:
            continue
        metadata = json.loads(row.get("metadata_json") or "{}")
        measured_kwh = float(load_df[file_id].sum())
        annualized_kwh = measured_kwh * annualization_factor
        tariff = select_import_tariff(year=year, kwh=annualized_kwh)
        energy_rp = tariff.standardprodukt_energielieferung_rp_per_kwh
        net_rp = _variable_net_rp_per_kwh(tariff)
        agreed_energy_rp = (
            leg_energy_price_rp_per_kwh
            if isinstance(leg_energy_price_rp_per_kwh, Decimal)
            else Decimal(str(leg_energy_price_rp_per_kwh))
            if leg_energy_price_rp_per_kwh is not None
            else energy_rp
        )
        if agreed_energy_rp < 0:
            raise ValueError("leg_energy_price_rp_per_kwh must be non-negative")
        is_zev = bool(row["is_zev"])
        label = metadata.get("vertrag") or row.get("original_filename") or file_id
        if is_zev:
            label = f"CKW ZEV Restbezug: {label}"
        participants.append(
            {
                "file_id": file_id,
                "label": label,
                "address": metadata.get("adresse"),
                "is_zev": is_zev,
                "measured_kwh": measured_kwh,
                "annualized_kwh": annualized_kwh,
                "tariff_tier": _tariff_label(annualized_kwh),
                "energy_rp_per_kwh": float(energy_rp),
                "leg_energy_price_rp_per_kwh": float(agreed_energy_rp),
                "discountable_netznutzung_rp_per_kwh": float(net_rp),
                "variable_netznutzung_rp_per_kwh": float(net_rp),
                "baseline_variable_cost_chf": float(Decimal(str(measured_kwh)) * (energy_rp + net_rp) / Decimal("100")),
            }
        )

    if not participants:
        return {
            "assumptions": _optimization_assumptions(network_discount),
            "warnings": ["Keine auswertbaren Verbrauchsteilnehmer gefunden."],
            "combination_count": 0,
            "evaluated_all": True,
            "optimization_mode": "exhaustive",
            "best": None,
            "combinations": [],
        }

    warnings: list[str] = []
    ordered = sorted(participants, key=lambda item: item["annualized_kwh"], reverse=True)
    evaluated_participants = ordered
    evaluated_all = True
    optimization_mode = "exhaustive"
    if len(ordered) > max_exhaustive_participants:
        evaluated_all = False
        optimization_mode = "all_participants_assumed_optimal"
        warnings.append(
            f"Die Gemeinschaft mit {len(ordered)} Teilnehmern ist zu gross für eine vollständige Kombinationssuche. "
            "Die Einsparung wird mit allen Teilnehmern unter der Annahme berechnet, dass diese vollständige LEG optimal ist."
        )

    file_ids = [participant["file_id"] for participant in evaluated_participants]
    load_matrix = load_df[file_ids].to_numpy(dtype=float)
    n = len(evaluated_participants)
    all_participants_mask = (1 << n) - 1
    masks = range(1, all_participants_mask + 1) if optimization_mode == "exhaustive" else [all_participants_mask]
    combinations: list[dict[str, Any]] = []

    def _weighted_average_rate(rates: np.ndarray, weights: np.ndarray) -> float:
        total_weight = float(weights.sum())
        if total_weight <= 0:
            return float(rates.mean()) if len(rates) else 0.0
        return float(np.dot(rates, weights) / total_weight)

    for mask in masks:
        indices = [idx for idx in range(n) if mask & (1 << idx)]
        subset_load_matrix = load_matrix[:, indices]
        subset_load = subset_load_matrix.sum(axis=1)
        consumption_kwh = float(subset_load.sum())
        annualized_kwh = consumption_kwh * annualization_factor
        local_solar_series = np.minimum(subset_load, solar_vector)
        local_solar_kwh = float(local_solar_series.sum())
        residual_solar_kwh = float(np.maximum(solar_vector - subset_load, 0.0).sum())
        grid_import_kwh = consumption_kwh - local_solar_kwh
        baseline_cost_chf = sum(evaluated_participants[idx]["baseline_variable_cost_chf"] for idx in indices)
        shares = np.divide(
            subset_load_matrix,
            subset_load[:, None],
            out=np.zeros_like(subset_load_matrix),
            where=subset_load[:, None] > 0,
        )
        allocated_local_solar = (local_solar_series[:, None] * shares).sum(axis=0)
        energy_rates = np.array(
            [evaluated_participants[idx]["energy_rp_per_kwh"] for idx in indices],
            dtype=float,
        )
        network_rates = np.array(
            [evaluated_participants[idx]["variable_netznutzung_rp_per_kwh"] for idx in indices],
            dtype=float,
        )
        rate_weights = (
            allocated_local_solar if float(allocated_local_solar.sum()) > 0 else subset_load_matrix.sum(axis=0)
        )
        average_energy_rp = _weighted_average_rate(energy_rates, rate_weights)
        average_net_rp = _weighted_average_rate(network_rates, rate_weights)
        leg_network_discount = Decimal("0")
        producer_revenue = Decimal("0")
        energy_price_savings = Decimal("0")
        for local_index, (allocated_kwh, energy_rp, network_rp) in enumerate(
            zip(allocated_local_solar, energy_rates, network_rates)
        ):
            allocated = Decimal(str(float(allocated_kwh)))
            leg_network_discount += allocated * Decimal(str(float(network_rp))) * network_discount / Decimal("100")
            agreed_rate = Decimal(str(evaluated_participants[indices[local_index]]["leg_energy_price_rp_per_kwh"]))
            producer_revenue += allocated * agreed_rate / Decimal("100")
            energy_price_savings += allocated * (Decimal(str(float(energy_rp))) - agreed_rate) / Decimal("100")
        tariff_switch_effect_chf = 0.0
        leg_network_discount_chf = float(leg_network_discount)
        energy_price_savings_chf = float(energy_price_savings)
        cost_savings_chf = leg_network_discount_chf + energy_price_savings_chf
        producer_revenue_chf = float(producer_revenue)
        local_solar_energy_value_chf = producer_revenue_chf
        total_value_chf = cost_savings_chf
        combinations.append(
            {
                "mask": mask,
                "participant_count": len(indices),
                "participant_ids": [evaluated_participants[idx]["file_id"] for idx in indices],
                "participant_labels": [evaluated_participants[idx]["label"] for idx in indices],
                "consumption_kwh": consumption_kwh,
                "annualized_kwh": annualized_kwh,
                "local_solar_kwh": local_solar_kwh,
                "residual_solar_kwh": residual_solar_kwh,
                "grid_import_kwh": grid_import_kwh,
                "combined_tariff_tier": _tariff_label(annualized_kwh),
                "combined_energy_rp_per_kwh": average_energy_rp,
                "combined_variable_netznutzung_rp_per_kwh": average_net_rp,
                "combined_discountable_netznutzung_rp_per_kwh": average_net_rp,
                "leg_energy_price_rp_per_kwh": _weighted_average_rate(
                    np.array([evaluated_participants[idx]["leg_energy_price_rp_per_kwh"] for idx in indices]),
                    rate_weights,
                ),
                "baseline_variable_cost_chf": baseline_cost_chf,
                "combined_full_cost_chf": baseline_cost_chf,
                "tariff_switch_effect_chf": tariff_switch_effect_chf,
                "leg_network_discount_chf": leg_network_discount_chf,
                "energy_price_savings_chf": energy_price_savings_chf,
                "cost_savings_chf": cost_savings_chf,
                "producer_revenue_chf": producer_revenue_chf,
                "local_solar_energy_value_chf": local_solar_energy_value_chf,
                "total_value_chf": total_value_chf,
            }
        )

    combinations.sort(key=lambda item: item["total_value_chf"], reverse=True)
    best = combinations[0]
    best_indices = [idx for idx in range(n) if best["mask"] & (1 << idx)]
    best_load = load_matrix[:, best_indices].sum(axis=1)
    best_local_series = np.minimum(best_load, solar_vector)
    best_residual_series = np.maximum(solar_vector - best_load, 0.0)
    selected_members: list[dict[str, Any]] = []
    for idx in best_indices:
        participant = evaluated_participants[idx]
        load = load_matrix[:, idx]
        shares = np.divide(load, best_load, out=np.zeros_like(load), where=best_load > 0)
        allocated_local_solar_kwh = float((best_local_series * shares).sum())
        tariff_effect_chf = 0.0
        network_discount_chf = float(
            Decimal(str(allocated_local_solar_kwh))
            * Decimal(str(participant["variable_netznutzung_rp_per_kwh"]))
            * network_discount
            / Decimal("100")
        )
        energy_value_chf = float(
            Decimal(str(allocated_local_solar_kwh))
            * Decimal(str(participant["leg_energy_price_rp_per_kwh"]))
            / Decimal("100")
        )
        energy_price_savings_chf = float(
            Decimal(str(allocated_local_solar_kwh))
            * (
                Decimal(str(participant["energy_rp_per_kwh"]))
                - Decimal(str(participant["leg_energy_price_rp_per_kwh"]))
            )
            / Decimal("100")
        )
        consumer_savings_chf = network_discount_chf + energy_price_savings_chf
        selected_members.append(
            {
                **participant,
                "allocated_local_solar_kwh": allocated_local_solar_kwh,
                "tariff_switch_effect_chf": tariff_effect_chf,
                "leg_network_discount_chf": network_discount_chf,
                "energy_price_savings_chf": energy_price_savings_chf,
                "cost_savings_chf": consumer_savings_chf,
                "producer_revenue_chf": energy_value_chf,
                "local_solar_energy_value_chf": energy_value_chf,
                "total_value_chf": consumer_savings_chf,
            }
        )
    selected_members.sort(key=lambda item: item["total_value_chf"], reverse=True)

    for source in solar_file_rows:
        values = source_vectors[source["file_id"]]
        shares = np.divide(values, solar_vector, out=np.zeros_like(values), where=solar_vector > 0)
        allocated_local_solar_kwh = float((best_local_series * shares).sum())
        residual_solar_kwh = float((best_residual_series * shares).sum())
        available_solar_kwh = allocated_local_solar_kwh + residual_solar_kwh
        if available_solar_kwh <= 0:
            continue
        solar_sources.append(
            {
                **source,
                "available_solar_kwh": available_solar_kwh,
                "allocated_local_solar_kwh": allocated_local_solar_kwh,
                "residual_solar_kwh": residual_solar_kwh,
                "leg_energy_price_rp_per_kwh": float(
                    best.get("leg_energy_price_rp_per_kwh", 0.0)
                ),
                "producer_revenue_chf": (
                    allocated_local_solar_kwh * float(best.get("leg_energy_price_rp_per_kwh", 0.0)) / 100.0
                ),
            }
        )
    solar_sources.sort(key=lambda item: item["available_solar_kwh"], reverse=True)

    source_count = max(len(solar_sources), 1)
    participant_count = max(len(selected_members), 1)
    source_center_y = ((source_count - 1) * 120) / 2
    participant_center_y = ((participant_count - 1) * 120) / 2
    center_y = max(source_center_y, participant_center_y, 80)
    residual_solar_kwh = float(best_residual_series.sum())

    nodes = [
        {
            "id": "solar",
            "position": {"x": 360, "y": center_y},
            "data": {
                "label": "Verfügbarer Solarstrom",
                "value": f"{float(solar_vector.sum()):,.0f} kWh".replace(",", "'"),
                "subtitle": f"LEG: {best['local_solar_kwh']:,.0f} kWh".replace(",", "'"),
                "kind": "solar",
            },
        },
        {
            "id": "leg",
            "position": {"x": 720, "y": center_y - 80},
            "data": {
                "label": f"Optimale LEG ({best['combined_tariff_tier']})",
                "value": f"CHF {best['total_value_chf']:,.0f}".replace(",", "'"),
                "subtitle": f"{best['local_solar_kwh']:,.0f} kWh lokal".replace(",", "'"),
                "kind": "leg",
            },
        },
        {
            "id": "ckw",
            "position": {"x": 720, "y": center_y + 120},
            "data": {
                "label": "Rückspeisung CKW",
                "value": f"{residual_solar_kwh:,.0f} kWh".replace(",", "'"),
                "kind": "grid",
            },
        },
    ]
    edges = [
        {
            "id": "solar-leg",
            "source": "solar",
            "target": "leg",
            "label": f"{best['local_solar_kwh']:,.0f} kWh".replace(",", "'"),
            "animated": True,
        },
        {
            "id": "solar-ckw",
            "source": "solar",
            "target": "ckw",
            "label": f"{residual_solar_kwh:,.0f} kWh".replace(",", "'"),
        }
    ]
    zev_source_nodes: list[tuple[str, dict[str, Any]]] = []
    for idx, source in enumerate(solar_sources):
        node_id = f"solar-source-{idx}"
        nodes.append(
            {
                "id": node_id,
                "position": {"x": 0, "y": idx * 120},
                "data": {
                    "label": source["label"],
                    "value": f"{source['available_solar_kwh']:,.0f} kWh".replace(",", "'"),
                    "subtitle": f"LEG: {source['allocated_local_solar_kwh']:,.0f} kWh".replace(",", "'"),
                    "kind": "source",
                },
            }
        )
        edges.append(
            {
                "id": f"{node_id}-solar",
                "source": node_id,
                "target": "solar",
                "label": f"{source['available_solar_kwh']:,.0f} kWh".replace(",", "'"),
            }
        )
        if source["is_zev"]:
            zev_source_nodes.append((node_id, source))
    zev_participant_nodes: list[tuple[str, dict[str, Any]]] = []
    for idx, member in enumerate(selected_members):
        node_id = f"participant-{idx}"
        node_value = f"CHF {member['total_value_chf']:,.0f}".replace(",", "'")
        if member["is_zev"]:
            node_value = _format_graph_kwh(member["measured_kwh"])
        nodes.append(
            {
                "id": node_id,
                "position": {"x": 1080, "y": idx * 120},
                "data": {
                    "label": member["label"],
                    "value": node_value,
                    "subtitle": f"LEG: {member['allocated_local_solar_kwh']:,.0f} kWh".replace(",", "'"),
                    "kind": "participant",
                    "tariff": member["tariff_tier"],
                },
            }
        )
        edges.append(
            {
                "id": f"leg-{node_id}",
                "source": "leg",
                "target": node_id,
                "label": f"CHF {member['total_value_chf']:,.0f}".replace(",", "'"),
            }
        )
        if member["is_zev"]:
            zev_participant_nodes.append((node_id, member))

    if zev_totals and zev_source_nodes and zev_participant_nodes:
        zev_production_kwh = zev_totals.get(FLOW_PV_PRODUCTION, 0.0)
        zev_consumption_kwh = zev_totals.get(FLOW_CONSUMPTION, 0.0)
        zev_local_pv_kwh = zev_totals.get(FLOW_LOCAL_PV_CONSUMPTION, 0.0)
        nodes.extend(
            [
                {
                    "id": "zev-production",
                    "position": {"x": -360, "y": 0},
                    "data": {
                        "label": "CKW ZEV PV-Produktion",
                        "value": _format_graph_kwh(zev_production_kwh),
                        "subtitle": "Brutto-PV gemessen",
                        "kind": "zev-source",
                    },
                },
                {
                    "id": "zev-consumption",
                    "position": {"x": 1440, "y": 0},
                    "data": {
                        "label": "CKW ZEV Verbrauch",
                        "value": _format_graph_kwh(zev_consumption_kwh),
                        "subtitle": f"Eigenverbrauch: {_format_graph_kwh(zev_local_pv_kwh)}",
                        "kind": "zev-consumption",
                    },
                },
            ]
        )
        edges.append(
            {
                "id": "zev-production-consumption",
                "source": "zev-production",
                "target": "zev-consumption",
                "label": _format_graph_kwh(zev_local_pv_kwh),
            }
        )
        for node_id, source in zev_source_nodes:
            edges.append(
                {
                    "id": f"zev-production-{node_id}",
                    "source": "zev-production",
                    "target": node_id,
                    "label": _format_graph_kwh(source["available_solar_kwh"]),
                }
            )
        for node_id, member in zev_participant_nodes:
            edges.append(
                {
                    "id": f"{node_id}-zev-consumption",
                    "source": node_id,
                    "target": "zev-consumption",
                    "label": _format_graph_kwh(member["measured_kwh"]),
                }
            )

    best = {
        **best,
        "members": selected_members,
        "solar_sources": solar_sources,
        "flow": {"nodes": nodes, "edges": edges},
    }

    return {
        "assumptions": _optimization_assumptions(network_discount),
        "warnings": warnings,
        "tariff_year": year,
        "network_discount": float(network_discount),
        "network_discount_rp_per_kwh": best["combined_discountable_netznutzung_rp_per_kwh"] * float(network_discount),
        "leg_energy_price_rp_per_kwh": best["leg_energy_price_rp_per_kwh"],
        "annualization_factor": annualization_factor,
        "participant_count": len(ordered),
        "evaluated_participant_count": len(evaluated_participants),
        "combination_count": len(combinations),
        "evaluated_all": evaluated_all,
        "optimization_mode": optimization_mode,
        "best": best,
        "combinations": combinations[:top_n],
    }


def kwp_to_ampere(kwp: float) -> float:
    return kwp * 1000.0 / (math.sqrt(3.0) * 400.0)


def _first_existing_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {normalize_text(col): col for col in columns}
    for candidate in candidates:
        candidate_key = normalize_text(candidate)
        found = normalized.get(candidate_key)
        if found:
            return found
        for normalized_col, original_col in normalized.items():
            if candidate_key and (candidate_key in normalized_col or normalized_col in candidate_key):
                return original_col
    return None


def _exact_existing_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    normalized = {normalize_text(col): col for col in columns}
    for candidate in candidates:
        found = normalized.get(normalize_text(candidate))
        if found:
            return found
    return None


def load_plant_cache(cache_path: str | Path = DEFAULT_PLANT_CACHE) -> pd.DataFrame:
    path = Path(cache_path)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, sep=None, engine="python", dtype=str)


def _urlopen_json(url: str) -> dict[str, Any]:
    from urllib.request import Request

    request = Request(url, headers={"User-Agent": "CKW-LEG-Portal/0.1"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_plant_cache(cache_path: str | Path = DEFAULT_PLANT_CACHE) -> Path:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    package: dict[str, Any] | None = None
    for url in (PV_PLANT_CKAN_URL, PV_PLANT_CKAN_FALLBACK_URL):
        try:
            package = _urlopen_json(url)
            break
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if package is None:
        raise RuntimeError(f"Open-Data-Metadaten konnten nicht geladen werden: {errors}")
    resources = package.get("result", {}).get("resources", [])
    def _resource_name(resource: dict[str, Any]) -> str:
        name = resource.get("name", "")
        if isinstance(name, dict):
            return " ".join(str(part) for part in name.values())
        return str(name)

    csv_resources = [
        r
        for r in resources
        if "csv" in str(r.get("format", "")).casefold()
        or "csv" in _resource_name(r).casefold()
        or str(r.get("url", "")).casefold().endswith(".csv")
        or str(r.get("download_url", "")).casefold().endswith(".csv")
        or (
            str(r.get("download_url") or r.get("url") or "").casefold().endswith(".zip")
            and "csv" in _resource_name(r).casefold()
        )
    ]
    if not csv_resources:
        raise RuntimeError("Keine CSV-Ressource für Elektrizitätsproduktionsanlagen gefunden")
    url = csv_resources[0].get("download_url") or csv_resources[0].get("url")
    if not url:
        raise RuntimeError("CSV-Ressource hat keine Download-URL")
    from urllib.request import Request

    request = Request(str(url), headers={"User-Agent": "CKW-LEG-Portal/0.1"})
    with urlopen(request, timeout=120) as response:
        payload = response.read()
    if str(url).casefold().endswith(".zip"):
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            csv_names = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
            if not csv_names:
                raise RuntimeError("ZIP-Ressource enthält keine CSV-Datei")
            path.write_bytes(archive.read(csv_names[0]))
    else:
        path.write_bytes(payload)
    return path


def _parse_uploaded_address(value: object) -> dict[str, str]:
    normalized = normalize_text(value)
    postal_match = re.search(r"\b(\d{4})\b", normalized)
    postal_code = postal_match.group(1) if postal_match else ""
    text = re.sub(r"\b\d{4}\b.*$", "", normalized).strip()
    match = re.search(r"(.+?)\s+(\d+\s*[a-z]?)\b", text)
    if match:
        street = normalize_text(match.group(1))
        house = normalize_text(match.group(2)).replace(" ", "")
    else:
        street = text
        house = ""
    return {"street": street, "house": house, "postal_code": postal_code, "raw": normalized}


def _plant_power_kw(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("'", "").replace(",", ".")
    number = pd.to_numeric(pd.Series([text]), errors="coerce").iloc[0]
    if pd.isna(number):
        return None
    power = float(number)
    # Some sources expose W while others expose kW/kWp.
    if power > 2000:
        power = power / 1000.0
    return power


def _plant_address(row: pd.Series, *, address_col: str | None, street_col: str | None, house_col: str | None) -> str:
    if address_col and str(row.get(address_col, "")).strip():
        return str(row.get(address_col, "")).strip()
    street = str(row.get(street_col, "")).strip() if street_col else ""
    house = str(row.get(house_col, "")).strip() if house_col else ""
    return " ".join(part for part in (street, house) if part)


def _plant_match_confidence(plant_address: str, uploaded_addresses: list[dict[str, str]]) -> str | None:
    plant_parts = _parse_uploaded_address(plant_address)
    if not uploaded_addresses:
        return "Gemeinde"
    for uploaded in uploaded_addresses:
        if plant_parts["raw"] and (plant_parts["raw"] in uploaded["raw"] or uploaded["raw"] in plant_parts["raw"]):
            return "Adresse"
        if plant_parts["street"] and plant_parts["street"] == uploaded["street"]:
            if plant_parts["house"] and uploaded["house"] and plant_parts["house"] == uploaded["house"]:
                return "Adresse"
            plant_house_number = re.match(r"\d+", plant_parts["house"] or "")
            uploaded_house_number = re.match(r"\d+", uploaded["house"] or "")
            if plant_house_number and uploaded_house_number and plant_house_number.group(0) == uploaded_house_number.group(0):
                return "Adresse"
            if not plant_parts["house"] or not uploaded["house"]:
                return "Strasse"
    return None


def _plant_cache_context(plants: pd.DataFrame, municipality: str) -> tuple[pd.DataFrame, dict[str, str | None], str | None]:
    cols = list(plants.columns)
    municipality_col = _first_existing_column(cols, ("Gemeinde", "MunicipalityName", "municipality", "ort", "city"))
    canton_col = _first_existing_column(cols, ("Kanton", "canton"))
    address_col = _first_existing_column(cols, ("Adresse", "Address", "Standortadresse"))
    street_col = _first_existing_column(cols, ("Strasse", "Street", "street_name"))
    house_col = _first_existing_column(cols, ("Hausnummer", "HouseNumber", "house_number", "number"))
    postal_col = _first_existing_column(cols, ("PLZ", "Postleitzahl", "postal_code", "zip"))
    power_col = _first_existing_column(
        cols,
        (
            "Leistung",
            "Elektrische Leistung",
            "InstallierteLeistung",
            "Installierte Leistung",
            "TotalPower",
            "kwp",
            "kWp",
            "power",
        ),
    )
    carrier_col = _exact_existing_column(
        cols,
        ("Energietraeger", "Energieträger", "energy_source", "subkategorie", "SubCategory"),
    )
    cache_id_col = _exact_existing_column(cols, ("xtf_id", "id", "plant_id", "anlage_id"))
    if municipality_col is None or power_col is None:
        return (
            pd.DataFrame(),
            {},
            "Spalten der PV-Anlagen-Datei konnten nicht erkannt werden. "
            f"Vorhandene Spalten: {', '.join(cols[:20])}",
        )

    filtered = plants[plants[municipality_col].map(normalize_text).eq(normalize_text(municipality))].copy()
    if filtered.empty and canton_col is not None:
        filtered = plants[plants[canton_col].map(normalize_text).eq("lu")].copy()
    if carrier_col is not None:
        carrier = filtered[carrier_col].map(normalize_text)
        pv_mask = carrier.str.contains("solar|photovoltaik|pv", regex=True, na=False)
        if not pv_mask.any() and normalize_text(carrier_col) == "subcategory":
            # The current BFE CSV exports photovoltaic plants as the encoded value subcat_2.
            pv_mask = carrier.eq("subcat 2")
        if pv_mask.any():
            filtered = filtered[pv_mask].copy()

    return (
        filtered,
        {
            "municipality_col": municipality_col,
            "address_col": address_col,
            "street_col": street_col,
            "house_col": house_col,
            "postal_col": postal_col,
            "power_col": power_col,
            "cache_id_col": cache_id_col,
        },
        None,
    )


def _cache_plant_rows(plants: pd.DataFrame, municipality: str) -> tuple[list[dict[str, Any]], str | None]:
    filtered, cols, warning = _plant_cache_context(plants, municipality)
    if warning:
        return [], warning

    rows: list[dict[str, Any]] = []
    for index, row in filtered.iterrows():
        address = _plant_address(
            row,
            address_col=cols["address_col"],
            street_col=cols["street_col"],
            house_col=cols["house_col"],
        )
        power = _plant_power_kw(row.get(cols["power_col"]))
        if power is None:
            continue
        cache_id = str(row.get(cols["cache_id_col"], "")).strip() if cols["cache_id_col"] else ""
        if not cache_id:
            cache_id = hashlib.sha256(f"{municipality}|{address}|{power}|{index}".encode()).hexdigest()[:24]
        postal_code = str(row.get(cols["postal_col"], "")).strip() if cols["postal_col"] else ""
        rows.append(
            {
                "project_plant_id": None,
                "cache_id": cache_id,
                "address": address,
                "municipality": str(row.get(cols["municipality_col"], municipality)),
                "installed_kwp": power,
                "max_connection_ampere": kwp_to_ampere(power),
                "confidence": "Open Data",
                "source": "cache",
                "_postal_code": postal_code,
            }
        )
    rows.sort(key=lambda item: (normalize_text(item["address"]), item["installed_kwp"], item["cache_id"]))
    return rows, None


def _public_plant_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _automatic_plant_matches(
    available_plants: list[dict[str, Any]],
    uploaded_addresses: list[dict[str, str]],
) -> list[dict[str, Any]]:
    candidates = available_plants
    upload_postal_codes = {item["postal_code"] for item in uploaded_addresses if item["postal_code"]}
    if upload_postal_codes:
        postal_matches = [
            plant
            for plant in candidates
            if (postal_match := re.search(r"\b(\d{4})\b", plant["_postal_code"]))
            and postal_match.group(1) in upload_postal_codes
        ]
        if postal_matches:
            candidates = postal_matches

    matches: list[dict[str, Any]] = []
    for plant in candidates:
        confidence = _plant_match_confidence(plant["address"], uploaded_addresses)
        if confidence is None and not uploaded_addresses:
            confidence = "Gemeinde"
        if confidence is None:
            continue
        matches.append({**plant, "confidence": confidence, "source": "automatic"})

    return (
        [plant for plant in matches if plant["confidence"] == "Adresse"]
        or [plant for plant in matches if plant["confidence"] == "Strasse"]
        or matches
    )


def _stored_project_plants(con: duckdb.DuckDBPyConnection, project_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT project_plant_id, cache_id, address, municipality, installed_kwp, source
        FROM project_plants
        WHERE project_id = ?
        ORDER BY created_at, project_plant_id
        """,
        [project_id],
    ).fetchall()
    return [
        {
            "project_plant_id": row[0],
            "cache_id": row[1],
            "address": row[2],
            "municipality": row[3],
            "installed_kwp": float(row[4]),
            "max_connection_ampere": kwp_to_ampere(float(row[4])),
            "confidence": "Manuell" if row[5] == "manual" else "Ausgewählt",
            "source": row[5],
        }
        for row in rows
    ]


def match_project_plants(
    *,
    project_id: str,
    municipality: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        project_row = con.execute(
            "SELECT plant_selection_mode FROM projects WHERE project_id = ?",
            [project_id],
        ).fetchone()
        if project_row is None:
            raise KeyError(project_id)
        selection_mode = str(project_row[0] or "automatic")
        addresses = con.execute(
            """
            SELECT DISTINCT address, zaehlpunkt
            FROM measurements
            WHERE project_id = ? AND flow_kind IN (?, ?)
            """,
            [project_id, FLOW_PV_PRODUCTION, FLOW_PV_EXCESS],
        ).df()
        selected_plants = _stored_project_plants(con, project_id)
    finally:
        con.close()

    plants = load_plant_cache(cache_path)
    if plants.empty:
        try:
            refresh_plant_cache(cache_path)
            plants = load_plant_cache(cache_path)
        except Exception as exc:
            matches = selected_plants if selection_mode == "curated" else []
            return {
                "cache_available": False,
                "selection_mode": selection_mode,
                "matches": matches,
                "automatic_matches": [],
                "available_plants": [],
                "total_installed_kwp": float(sum(item["installed_kwp"] for item in matches)),
                "max_connection_ampere": kwp_to_ampere(sum(item["installed_kwp"] for item in matches)),
                "warnings": [
                    "PV-Anlagen-Cache fehlt und konnte nicht automatisch geladen werden. "
                    f"Bitte CSV in data/electricity_production_plants.csv ablegen. Fehler: {exc}"
                ],
            }

    available_plants, warning = _cache_plant_rows(plants, municipality)
    if warning:
        matches = selected_plants if selection_mode == "curated" else []
        total_kwp = float(sum(item["installed_kwp"] for item in matches))
        return {
            "cache_available": True,
            "selection_mode": selection_mode,
            "matches": matches,
            "automatic_matches": [],
            "available_plants": [],
            "total_installed_kwp": total_kwp,
            "max_connection_ampere": kwp_to_ampere(total_kwp),
            "warnings": [warning],
        }

    upload_addresses = [
        _parse_uploaded_address(row["address"]) for _, row in addresses.iterrows() if normalize_text(row.get("address"))
    ]
    automatic_matches = _automatic_plant_matches(available_plants, upload_addresses)
    matches = selected_plants if selection_mode == "curated" else automatic_matches
    total_kwp = float(sum(item["installed_kwp"] for item in matches))
    warnings = []
    if not matches:
        warnings.append(
            "Manuelle PV-Auswahl ist aktiv, enthält aber keine Anlage."
            if selection_mode == "curated"
            else "Keine passende PV-Anlage im Open-Data-Cache gefunden."
        )
    return {
        "cache_available": True,
        "selection_mode": selection_mode,
        "matches": [_public_plant_row(item) for item in matches],
        "automatic_matches": [_public_plant_row(item) for item in automatic_matches],
        "available_plants": [_public_plant_row(item) for item in available_plants],
        "total_installed_kwp": total_kwp,
        "max_connection_ampere": kwp_to_ampere(total_kwp),
        "warnings": warnings,
    }


def _insert_project_plant(
    con: duckdb.DuckDBPyConnection,
    *,
    project_id: str,
    source: str,
    cache_id: str | None,
    address: str,
    municipality: str,
    installed_kwp: float,
) -> str:
    project_plant_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO project_plants (
            project_plant_id, project_id, source, cache_id, address, municipality, installed_kwp, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, now())
        """,
        [project_plant_id, project_id, source, cache_id, address, municipality, installed_kwp],
    )
    return project_plant_id


def adopt_automatic_project_plants(
    *,
    project_id: str,
    municipality: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    result = match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )
    con = connect_portal_db(db_path)
    transaction_started = False
    try:
        con.execute("BEGIN TRANSACTION")
        transaction_started = True
        con.execute("DELETE FROM project_plants WHERE project_id = ?", [project_id])
        for plant in result["automatic_matches"]:
            _insert_project_plant(
                con,
                project_id=project_id,
                source="cache",
                cache_id=plant["cache_id"],
                address=plant["address"],
                municipality=plant["municipality"],
                installed_kwp=float(plant["installed_kwp"]),
            )
        con.execute("UPDATE projects SET plant_selection_mode = 'curated' WHERE project_id = ?", [project_id])
        con.execute("COMMIT")
        transaction_started = False
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )


def add_cached_project_plant(
    *,
    project_id: str,
    municipality: str,
    cache_id: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    result = match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )
    plant = next((item for item in result["available_plants"] if item["cache_id"] == cache_id), None)
    if plant is None:
        raise ValueError("PV-Anlage wurde im Open-Data-Cache der Gemeinde nicht gefunden.")
    con = connect_portal_db(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM project_plants WHERE project_id = ? AND cache_id = ?",
            [project_id, cache_id],
        ).fetchone()
        if exists:
            raise ValueError("PV-Anlage ist bereits ausgewählt.")
        _insert_project_plant(
            con,
            project_id=project_id,
            source="cache",
            cache_id=cache_id,
            address=plant["address"],
            municipality=plant["municipality"],
            installed_kwp=float(plant["installed_kwp"]),
        )
        con.execute("UPDATE projects SET plant_selection_mode = 'curated' WHERE project_id = ?", [project_id])
    finally:
        con.close()
    return match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )


def add_manual_project_plant(
    *,
    project_id: str,
    municipality: str,
    address: str,
    installed_kwp: float,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    address = address.strip()
    if not address:
        raise ValueError("Adresse der PV-Anlage fehlt.")
    if not math.isfinite(installed_kwp) or installed_kwp <= 0:
        raise ValueError("Installierte Leistung muss grösser als 0 kWp sein.")
    con = connect_portal_db(db_path)
    try:
        if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [project_id]).fetchone() is None:
            raise KeyError(project_id)
        _insert_project_plant(
            con,
            project_id=project_id,
            source="manual",
            cache_id=None,
            address=address,
            municipality=municipality,
            installed_kwp=float(installed_kwp),
        )
        con.execute("UPDATE projects SET plant_selection_mode = 'curated' WHERE project_id = ?", [project_id])
    finally:
        con.close()
    return match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )


def delete_selected_project_plant(
    *,
    project_id: str,
    project_plant_id: str,
    municipality: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        result = con.execute(
            "DELETE FROM project_plants WHERE project_id = ? AND project_plant_id = ? RETURNING project_plant_id",
            [project_id, project_plant_id],
        ).fetchone()
        if result is None:
            raise KeyError(project_plant_id)
    finally:
        con.close()
    return match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )


def clear_project_plant_selection(
    *,
    project_id: str,
    municipality: str,
    db_path: str | Path = DEFAULT_PORTAL_DB,
    cache_path: str | Path = DEFAULT_PLANT_CACHE,
) -> dict[str, Any]:
    con = connect_portal_db(db_path)
    try:
        if con.execute("SELECT 1 FROM projects WHERE project_id = ?", [project_id]).fetchone() is None:
            raise KeyError(project_id)
        con.execute("DELETE FROM project_plants WHERE project_id = ?", [project_id])
        con.execute("UPDATE projects SET plant_selection_mode = 'automatic' WHERE project_id = ?", [project_id])
    finally:
        con.close()
    return match_project_plants(
        project_id=project_id,
        municipality=municipality,
        db_path=db_path,
        cache_path=cache_path,
    )
