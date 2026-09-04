from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ParsedCkwCsv:
    metadata: dict[str, str]
    dataframe: pd.DataFrame
    kind: str
    energy_column: str
    totals: dict[str, float]


@dataclass(frozen=True)
class ParsedCkwZevCsv:
    dataframe: pd.DataFrame
    totals: dict[str, float]


ZEV_COLUMN_MAP = {
    "Energiebedarf_total_kWh": "consumption_kwh",
    "Bezug_total_kWh": "grid_import_kwh",
    "Produktion_kWh": "pv_production_kwh",
    "Produktion_total_kWh": "local_pv_consumption_kwh",
    "Rueckspeissung_total_kWh": "pv_excess_kwh",
}
ZEV_REQUIRED_COLUMNS = {"Zeitstempel", *ZEV_COLUMN_MAP}


def _decode_csv(content: bytes, *, encoding: str) -> str:
    return content.decode(encoding, errors="replace").lstrip("\ufeff")


def is_ckw_zev_csv(content: bytes, *, encoding: str = "utf-8") -> bool:
    text = _decode_csv(content, encoding=encoding)
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    return ZEV_REQUIRED_COLUMNS.issubset({column.strip() for column in first_line.split(";")})


def parse_ckw_zev_csv(content: bytes, *, encoding: str = "utf-8") -> ParsedCkwZevCsv:
    text = _decode_csv(content, encoding=encoding)
    df = pd.read_csv(StringIO(text), sep=";", dtype_backend="numpy_nullable")
    missing = sorted(ZEV_REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"Expected CKW ZEV columns but missing: {missing}")

    out_df = pd.DataFrame()
    timestamp = pd.to_datetime(df["Zeitstempel"], errors="raise", utc=True)
    out_df["timestamp"] = timestamp.dt.tz_convert("Europe/Zurich").dt.tz_localize(None)

    totals: dict[str, float] = {}
    for source_column, normalized_column in ZEV_COLUMN_MAP.items():
        values = pd.to_numeric(df[source_column], errors="raise")
        out_df[normalized_column] = values
        totals[source_column] = float(values.sum())

    return ParsedCkwZevCsv(dataframe=out_df, totals=totals)


def _parse_metadata_lines(lines: list[str]) -> tuple[dict[str, str], int]:
    metadata: dict[str, str] = {}

    for index, line in enumerate(lines):
        cells = [cell.strip() for cell in next(csv.reader([line], delimiter=";"))]
        first_value = next((cell for cell in cells if cell), "")

        # Excel preserves the maximum column count when re-saving a CKW export.
        # Metadata rows then gain a trailing semicolon and visually blank rows
        # become ";" or ";;". Locate the real table header instead of relying
        # on a physically empty line as the metadata terminator.
        if first_value.casefold() == "zeitraum":
            return metadata, index
        if not first_value:
            continue

        if ":" in first_value:
            k, v = first_value.split(":", 1)
            metadata[k.strip()] = v.strip()

    return metadata, len(lines)


def _detect_kind_and_energy_column(columns: list[str]) -> tuple[str, str]:
    cols = [c.strip() for c in columns]

    for c in cols:
        folded = c.casefold()
        if (
            folded.startswith("produktion")
            or folded.startswith("pv-produktion")
            or folded.startswith("solarproduktion")
            or folded.startswith("energieproduktion")
        ):
            return "production", c
    for c in cols:
        if c.startswith("Energieverbrauch"):
            return "import", c
    for c in cols:
        if c.startswith("Rücklieferung"):
            return "export", c

    raise ValueError(f"Unrecognized timeseries columns: {cols}")


def parse_ckw_csv(content: bytes, *, encoding: str = "utf-8") -> ParsedCkwCsv:
    if is_ckw_zev_csv(content, encoding=encoding):
        raise ValueError("CKW ZEV aggregate exports require the ZEV portal upload")

    text = _decode_csv(content, encoding=encoding)

    lines = text.splitlines()
    metadata, header_row_index = _parse_metadata_lines(lines)

    if header_row_index >= len(lines):
        raise ValueError("CSV does not contain a timeseries header row")

    data_text = "\n".join(lines[header_row_index:])
    df = pd.read_csv(StringIO(data_text), sep=";", dtype_backend="numpy_nullable")

    if "Zeitraum" not in df.columns:
        raise ValueError(f"Expected column 'Zeitraum' but got: {list(df.columns)}")

    kind, energy_col = _detect_kind_and_energy_column(list(df.columns))

    df = df.rename(columns={"Zeitraum": "timestamp"})

    totals: dict[str, float] = {}
    ts = df["timestamp"].astype(str).str.strip()
    total_mask = ts.str.casefold().eq("total")
    if total_mask.any():
        total_row = df.loc[total_mask].iloc[-1]
        for c in df.columns:
            if c == "timestamp":
                continue
            v = pd.to_numeric(total_row[c], errors="coerce")
            if pd.notna(v):
                totals[str(c)] = float(v)
        df = df.loc[~total_mask].copy()

    df["timestamp"] = pd.to_datetime(df["timestamp"], format="mixed", dayfirst=True, errors="raise")

    df[energy_col] = pd.to_numeric(df[energy_col], errors="coerce")
    if kind == "export":
        df["energy_kwh_signed"] = -df[energy_col].abs()
    else:
        df["energy_kwh_signed"] = df[energy_col].abs()

    blind_cols = [c for c in df.columns if str(c).startswith("Blindenergie")]
    if blind_cols:
        blind_col = blind_cols[0]
        df[blind_col] = pd.to_numeric(df[blind_col], errors="coerce")

    keep_cols: list[str] = ["timestamp", "energy_kwh_signed"]
    keep_cols.extend([c for c in df.columns if str(c).startswith("Blindenergie")])
    out_df = df[keep_cols].copy()

    return ParsedCkwCsv(metadata=metadata, dataframe=out_df, kind=kind, energy_column=energy_col, totals=totals)


def parsed_metadata_normalized(metadata: dict[str, str]) -> dict[str, Any]:
    start: str | None = None
    end: str | None = None
    if "Auswertungszeitraum" in metadata and "-" in metadata["Auswertungszeitraum"]:
        parts = [p.strip() for p in metadata["Auswertungszeitraum"].split("-", 1)]
        if len(parts) == 2:
            start, end = parts

    return {
        "kunde": metadata.get("Kunde"),
        "kundennummer": metadata.get("Kundennummer"),
        "adresse": metadata.get("Adresse"),
        "vertrag": metadata.get("Vertrag"),
        "zaehlpunkt": metadata.get("Zählpunkt") or metadata.get("Zaehlpunkt"),
        "datum_auswertung": metadata.get("Datum der Auswertung"),
        "auswertungszeitraum_start": start,
        "auswertungszeitraum_end": end,
        "einheit": metadata.get("Einheit"),
    }
