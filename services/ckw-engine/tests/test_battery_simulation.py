from __future__ import annotations

import pytest

from ckw_script.battery_simulation import simulate_peak_shaving, simulate_pv_battery


def test_simulate_pv_battery_shifts_excess_into_later_consumption() -> None:
    result = simulate_pv_battery(
        timestamps=["2025-01-01T10:00", "2025-01-01T10:15", "2025-01-01T10:30"],
        pv_kwh=[20.0, 0.0, 0.0],
        consumption_kwh=[0.0, 5.0, 20.0],
    )

    assert result.totals["battery_charge_kwh"] == pytest.approx(7.5)
    assert result.totals["battery_discharge_kwh"] == pytest.approx(7.5)
    assert result.totals["additional_local_solar_kwh"] == pytest.approx(7.5)
    assert result.totals["remaining_export_kwh"] == pytest.approx(12.5)
    assert result.totals["remaining_grid_import_kwh"] == pytest.approx(17.5)
    assert result.totals["ending_state_of_charge_kwh"] == pytest.approx(0.0)


def test_simulate_peak_shaving_minimizes_monthly_peak_and_ends_empty() -> None:
    result = simulate_peak_shaving(
        timestamps=["2025-01-01T10:00", "2025-01-01T10:15", "2025-01-01T10:30"],
        consumption_kwh=[2.5, 2.5, 10.0],
    )

    assert result.monthly[0]["baseline_peak_kw"] == pytest.approx(40.0)
    assert result.monthly[0]["battery_peak_kw"] == pytest.approx(20.0)
    assert result.monthly[0]["reduced_peak_kw"] == pytest.approx(20.0)
    assert result.totals["battery_charge_kwh"] == pytest.approx(5.0)
    assert result.totals["battery_discharge_kwh"] == pytest.approx(5.0)
    assert result.timeseries.iloc[-1]["state_of_charge_kwh"] == pytest.approx(0.0)


def test_simulate_peak_shaving_resets_state_of_charge_for_each_month() -> None:
    result = simulate_peak_shaving(
        timestamps=[
            "2025-01-31T23:30",
            "2025-01-31T23:45",
            "2025-02-01T00:00",
        ],
        consumption_kwh=[0.0, 10.0, 10.0],
    )

    assert result.monthly[0]["battery_peak_kw"] == pytest.approx(20.0)
    assert result.monthly[1]["battery_peak_kw"] == pytest.approx(40.0)
