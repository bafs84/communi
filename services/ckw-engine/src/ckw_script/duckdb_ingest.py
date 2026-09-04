from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from .ckw_csv import ParsedCkwCsv, parse_ckw_csv, parsed_metadata_normalized


@dataclass(frozen=True)
class IngestResult:
    original_filename: str
    table_name: str
    kind: str
    rows_inserted: int


_IDENTIFIER_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _sanitize_identifier(value: str) -> str:
    value = value.strip().replace("-", "_")
    value = _IDENTIFIER_RE.sub("_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = "ts"
    if value[0].isdigit():
        value = f"t_{value}"
    return value.lower()


def _stable_table_name(filename: str, parsed: ParsedCkwCsv, content: bytes) -> str:
    base = Path(filename).stem
    digest = hashlib.sha1(content).hexdigest()[:10]

    z = parsed.metadata.get("Zählpunkt") or ""
    kind = parsed.kind

    parts = [base]
    if z:
        parts.append(z[-8:])
    parts.append(kind)
    parts.append(digest)

    return _sanitize_identifier("_".join(parts))


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            table_name TEXT PRIMARY KEY,
            original_filename TEXT,
            kind TEXT,
            energy_column TEXT,
            total_energy REAL,
            total_blindenergie REAL,
            ingested_at TIMESTAMP,
            kunde TEXT,
            kundennummer TEXT,
            adresse TEXT,
            vertrag TEXT,
            zaehlpunkt TEXT,
            datum_auswertung TEXT,
            auswertungszeitraum_start TEXT,
            auswertungszeitraum_end TEXT,
            einheit TEXT
        )
        """
    )

    con.execute("ALTER TABLE metadata ADD COLUMN IF NOT EXISTS total_energy REAL")
    con.execute("ALTER TABLE metadata ADD COLUMN IF NOT EXISTS total_blindenergie REAL")


def ingest_csv_bytes_to_duckdb(
    *,
    content: bytes,
    original_filename: str,
    db_path: str | Path,
    encoding: str = "utf-8",
) -> IngestResult:
    parsed = parse_ckw_csv(content, encoding=encoding)
    table_name = _stable_table_name(original_filename, parsed, content)

    con = duckdb.connect(str(db_path))
    try:
        _ensure_schema(con)

        df = parsed.dataframe.copy()

        for c in df.columns:
            if c.startswith("Blindenergie"):
                df = df.rename(columns={c: "blindenergie_kvar"})
                break

        expected_cols = {"timestamp", "energy_kwh_signed"}
        if not expected_cols.issubset(set(df.columns)):
            raise ValueError(f"Missing expected columns in parsed dataframe: {expected_cols}")

        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {table_name} AS SELECT * FROM _df")

        meta_norm: dict[str, Any] = parsed_metadata_normalized(parsed.metadata)

        totals = parsed.totals or {}
        total_energy = totals.get(parsed.energy_column)
        total_blind = None
        for k, v in totals.items():
            if str(k).startswith("Blindenergie"):
                total_blind = v
                break

        meta_norm.update(
            {
                "table_name": table_name,
                "original_filename": original_filename,
                "kind": parsed.kind,
                "energy_column": parsed.energy_column,
                "total_energy": total_energy,
                "total_blindenergie": total_blind,
                "ingested_at": datetime.now(),
            }
        )

        meta_df = pd.DataFrame([meta_norm])
        meta_columns = [
            "table_name",
            "original_filename",
            "kind",
            "energy_column",
            "total_energy",
            "total_blindenergie",
            "ingested_at",
            "kunde",
            "kundennummer",
            "adresse",
            "vertrag",
            "zaehlpunkt",
            "datum_auswertung",
            "auswertungszeitraum_start",
            "auswertungszeitraum_end",
            "einheit",
        ]
        meta_df = meta_df.reindex(columns=meta_columns)
        con.register("_meta", meta_df)
        con.execute(
            """
            INSERT OR REPLACE INTO metadata (table_name, original_filename, kind, energy_column, total_energy, total_blindenergie, ingested_at, kunde, kundennummer, adresse, vertrag, zaehlpunkt, datum_auswertung, auswertungszeitraum_start, auswertungszeitraum_end, einheit)
            SELECT table_name, original_filename, kind, energy_column, total_energy, total_blindenergie, ingested_at, kunde, kundennummer, adresse, vertrag, zaehlpunkt, datum_auswertung, auswertungszeitraum_start, auswertungszeitraum_end, einheit FROM _meta
            """
        )

        return IngestResult(
            original_filename=original_filename,
            table_name=table_name,
            kind=parsed.kind,
            rows_inserted=len(df),
        )
    finally:
        con.close()


def ingest_many(
    *,
    files: Iterable[tuple[str, bytes]],
    db_path: str | Path,
    encoding: str = "utf-8",
) -> list[IngestResult]:
    results: list[IngestResult] = []
    for filename, content in files:
        results.append(
            ingest_csv_bytes_to_duckdb(
                content=content,
                original_filename=filename,
                db_path=db_path,
                encoding=encoding,
            )
        )
    return results
