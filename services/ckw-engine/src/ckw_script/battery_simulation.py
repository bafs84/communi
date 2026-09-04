from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BatterySpec:
    capacity_kwh: float = 100.0
    max_charge_kw: float = 30.0
    max_discharge_kw: float = 30.0


DEFAULT_BATTERY = BatterySpec()


@dataclass(frozen=True)
class BatterySimulation:
    timeseries: pd.DataFrame
    totals: dict[str, float]
    monthly: list[dict[str, float | str]]


def _timeseries_frame(
    *,
    timestamps: Iterable[object],
    values: Iterable[float],
    value_column: str,
) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(list(timestamps), errors="raise"),
            value_column: pd.to_numeric(pd.Series(list(values)), errors="raise").astype(float),
        }
    )
    if (df[value_column] < 0).any():
        raise ValueError(f"{value_column} must not contain negative values")
    return df


def _infer_interval_hours(timestamps: pd.Series) -> float:
    differences = timestamps.sort_values().diff().dropna().dt.total_seconds() / 3600.0
    positive_differences = differences[differences > 0]
    return float(positive_differences.median()) if not positive_differences.empty else 0.25


def simulate_pv_battery(
    *,
    timestamps: Iterable[object],
    pv_kwh: Iterable[float],
    consumption_kwh: Iterable[float],
    battery: BatterySpec = DEFAULT_BATTERY,
) -> BatterySimulation:
    df = _timeseries_frame(timestamps=timestamps, values=pv_kwh, value_column="pv_kwh")
    consumption = pd.to_numeric(pd.Series(list(consumption_kwh)), errors="raise").astype(float)
    if len(consumption) != len(df):
        raise ValueError("pv_kwh and consumption_kwh must contain the same number of intervals")
    if (consumption < 0).any():
        raise ValueError("consumption_kwh must not contain negative values")
    df["consumption_kwh"] = consumption.to_numpy(dtype=float)
    df = df.sort_values("timestamp", ignore_index=True)

    interval_hours = _infer_interval_hours(df["timestamp"])
    max_charge_kwh = battery.max_charge_kw * interval_hours
    max_discharge_kwh = battery.max_discharge_kw * interval_hours
    state_of_charge_kwh = 0.0
    rows: list[dict[str, float]] = []

    for pv_value, consumption_value in zip(df["pv_kwh"], df["consumption_kwh"], strict=True):
        direct_local_kwh = min(pv_value, consumption_value)
        discharge_kwh = min(consumption_value - direct_local_kwh, max_discharge_kwh, state_of_charge_kwh)
        charge_kwh = min(
            pv_value - direct_local_kwh,
            max_charge_kwh,
            battery.capacity_kwh - state_of_charge_kwh + discharge_kwh,
        )
        state_of_charge_kwh = state_of_charge_kwh - discharge_kwh + charge_kwh
        rows.append(
            {
                "direct_local_solar_kwh": direct_local_kwh,
                "battery_charge_kwh": charge_kwh,
                "battery_discharge_kwh": discharge_kwh,
                "state_of_charge_kwh": state_of_charge_kwh,
                "remaining_export_kwh": pv_value - direct_local_kwh - charge_kwh,
                "remaining_grid_import_kwh": consumption_value - direct_local_kwh - discharge_kwh,
            }
        )

    result = pd.concat([df, pd.DataFrame(rows)], axis=1)
    direct_local_solar_kwh = float(result["direct_local_solar_kwh"].sum())
    battery_discharge_kwh = float(result["battery_discharge_kwh"].sum())
    totals = {
        "pv_kwh": float(result["pv_kwh"].sum()),
        "consumption_kwh": float(result["consumption_kwh"].sum()),
        "direct_local_solar_kwh": direct_local_solar_kwh,
        "battery_charge_kwh": float(result["battery_charge_kwh"].sum()),
        "battery_discharge_kwh": battery_discharge_kwh,
        "additional_local_solar_kwh": battery_discharge_kwh,
        "local_solar_with_battery_kwh": direct_local_solar_kwh + battery_discharge_kwh,
        "remaining_export_kwh": float(result["remaining_export_kwh"].sum()),
        "remaining_grid_import_kwh": float(result["remaining_grid_import_kwh"].sum()),
        "ending_state_of_charge_kwh": state_of_charge_kwh,
    }
    return BatterySimulation(timeseries=result, totals=totals, monthly=[])


def _dispatch_peak_shaving_month(
    *,
    load_kwh: np.ndarray,
    interval_hours: float,
    battery: BatterySpec,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    load_kw = load_kwh / interval_hours
    max_charge_kwh = battery.max_charge_kw * interval_hours
    max_discharge_kwh = battery.max_discharge_kw * interval_hours

    def dispatch(
        target_peak_kw: float,
        *,
        trim_unused_charge: bool = False,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        charges = np.minimum(np.clip(target_peak_kw - load_kw, 0.0, None) * interval_hours, max_charge_kwh)
        discharges = np.minimum(np.clip(load_kw - target_peak_kw, 0.0, None) * interval_hours, max_discharge_kwh)

        unconstrained_state = np.cumsum(charges - discharges)
        overflow = np.maximum.accumulate(np.maximum(unconstrained_state - battery.capacity_kwh, 0.0))
        actual_charges = charges - np.diff(np.concatenate(([0.0], overflow)))
        state_of_charge = np.cumsum(actual_charges - discharges)
        if (state_of_charge < -1e-9).any():
            return None

        if trim_unused_charge:
            # Keep only charging that is required by a later discharge. This
            # preserves the optimized peak while making each month end empty.
            required_kwh = 0.0
            retained_charges = np.zeros(len(actual_charges), dtype=float)
            for index in range(len(actual_charges) - 1, -1, -1):
                required_kwh += discharges[index]
                retained_charges[index] = min(actual_charges[index], required_kwh)
                required_kwh -= retained_charges[index]
            actual_charges = retained_charges
            state_of_charge = np.cumsum(actual_charges - discharges)

        state_of_charge[np.abs(state_of_charge) < 1e-8] = 0.0
        return actual_charges, discharges, state_of_charge

    original_peak_kw = float(load_kw.max()) if len(load_kw) else 0.0
    low = 0.0
    high = original_peak_kw
    for _ in range(40):
        target = (low + high) / 2.0
        candidate = dispatch(target)
        if candidate is None:
            low = target
        else:
            high = target

    best_dispatch = dispatch(high, trim_unused_charge=True)
    charges, discharges, state_of_charge = best_dispatch or (
        np.zeros(len(load_kwh), dtype=float),
        np.zeros(len(load_kwh), dtype=float),
        np.zeros(len(load_kwh), dtype=float),
    )
    grid_import_kwh = load_kwh + charges - discharges
    simulated_peak_kw = float((grid_import_kwh / interval_hours).max()) if len(grid_import_kwh) else 0.0
    return charges, discharges, state_of_charge, simulated_peak_kw


def simulate_peak_shaving(
    *,
    timestamps: Iterable[object],
    consumption_kwh: Iterable[float],
    battery: BatterySpec = DEFAULT_BATTERY,
) -> BatterySimulation:
    df = _timeseries_frame(timestamps=timestamps, values=consumption_kwh, value_column="consumption_kwh")
    df = df.sort_values("timestamp", ignore_index=True)
    if df.empty:
        return BatterySimulation(
            timeseries=df,
            totals={
                "consumption_kwh": 0.0,
                "battery_charge_kwh": 0.0,
                "battery_discharge_kwh": 0.0,
                "baseline_peak_kw_month_sum": 0.0,
                "battery_peak_kw_month_sum": 0.0,
                "reduced_peak_kw_month_sum": 0.0,
            },
            monthly=[],
        )

    interval_hours = _infer_interval_hours(df["timestamp"])
    df["month"] = df["timestamp"].dt.strftime("%Y-%m")
    frames: list[pd.DataFrame] = []
    monthly: list[dict[str, float | str]] = []
    for month, month_df in df.groupby("month", sort=True):
        month_df = month_df.copy()
        load_kwh = month_df["consumption_kwh"].to_numpy(dtype=float)
        charges, discharges, state_of_charge, simulated_peak_kw = _dispatch_peak_shaving_month(
            load_kwh=load_kwh,
            interval_hours=interval_hours,
            battery=battery,
        )
        baseline_peak_kw = float((load_kwh / interval_hours).max()) if len(load_kwh) else 0.0
        month_df["battery_charge_kwh"] = charges
        month_df["battery_discharge_kwh"] = discharges
        month_df["state_of_charge_kwh"] = state_of_charge
        month_df["grid_import_with_battery_kwh"] = load_kwh + charges - discharges
        frames.append(month_df)
        monthly.append(
            {
                "month": month,
                "baseline_peak_kw": baseline_peak_kw,
                "battery_peak_kw": simulated_peak_kw,
                "reduced_peak_kw": baseline_peak_kw - simulated_peak_kw,
                "battery_charge_kwh": float(charges.sum()),
                "battery_discharge_kwh": float(discharges.sum()),
            }
        )

    result = pd.concat(frames, ignore_index=True)
    totals = {
        "consumption_kwh": float(result["consumption_kwh"].sum()),
        "battery_charge_kwh": float(result["battery_charge_kwh"].sum()),
        "battery_discharge_kwh": float(result["battery_discharge_kwh"].sum()),
        "baseline_peak_kw_month_sum": sum(float(row["baseline_peak_kw"]) for row in monthly),
        "battery_peak_kw_month_sum": sum(float(row["battery_peak_kw"]) for row in monthly),
        "reduced_peak_kw_month_sum": sum(float(row["reduced_peak_kw"]) for row in monthly),
    }
    return BatterySimulation(timeseries=result, totals=totals, monthly=monthly)
