import { reportError } from "./observability";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { Download, FolderOpen, PlugZap, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { Background, Controls, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { BillingWorkspace } from "./BillingWorkspace";
import { WorkspaceNavigation } from "./WorkspaceNavigation";
import { apiFetch } from "./access";
import {
  hasVzevSimulationInputs,
  workspaceFromHash,
  workspaceRoutes,
  type Workspace
} from "./workspaceRouting";

type Municipality = { name: string; canton: string; ckw_area: boolean };
type Project = {
  project_id: string;
  name: string;
  municipality: string;
  created_at: string;
  file_count?: number;
  config?: {
    leg?: { networkDiscount?: string; energyPriceRpPerKwh?: string | null };
    vzev?: {
      tariffMode?: "participant_80_pct" | "fixed";
      fixedPvTariffRpPerKwh?: string | number | null;
    };
    billing?: Record<string, unknown>;
    [key: string]: unknown;
  };
};
type SavedResult = {
  result_id: string;
  filename: string;
  operation: string;
  byte_size: number;
  created_at: string;
  download_url: string;
};
type UploadResult = {
  original_filename: string;
  rows_inserted: number;
  duplicate: boolean;
};
type Summary = Project & {
  files: Array<{
    file_id: string;
    original_filename: string;
    upload_kind: string;
    flow_kind: string;
    total_kwh: number;
    warnings: string[];
  }>;
  zev_flow: FlowGraph | null;
  warnings: string[];
  totals: Record<string, number>;
};
type FlowGraph = {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
};
type FlowPosition = { x: number; y: number };
type Plant = {
  project_plant_id: string | null;
  cache_id: string | null;
  address: string;
  municipality: string;
  installed_kwp: number;
  max_connection_ampere: number;
  confidence: string;
  source: string;
};
type PlantResult = {
  cache_available: boolean;
  selection_mode: "automatic" | "curated";
  matches: Plant[];
  automatic_matches: Plant[];
  available_plants: Plant[];
  total_installed_kwp: number;
  max_connection_ampere: number;
  warnings: string[];
};
type Calculation = {
  assumptions: string[];
  warnings: string[];
  tariff_year: number;
  rates?: Record<string, number>;
  totals: Record<string, number>;
  monthly: Array<Record<string, number | string>>;
  participants: Array<{
    file_id: string;
    label: string;
    measured_consumption_kwh: number;
    annualized_consumption_kwh: number;
    tariff_tier: string;
    allocated_local_solar_kwh: number;
    external_energy_rp_per_kwh: number;
    leg_energy_price_rp_per_kwh: number;
    discountable_netznutzung_rp_per_kwh: number;
    network_discount_rp_per_kwh: number;
    producer_revenue_chf: number;
    energy_price_savings_chf: number;
    network_discount_chf: number;
    consumer_total_savings_chf: number;
  }>;
};
type ResidualSolarHeatmap = {
  year: number | null;
  days: string[];
  hours: string[];
  scale_max_residual_solar_kwh: number;
  selected_peak: {
    date: string;
    hour: number;
    residual_solar_kwh: number;
  } | null;
  values: Array<{
    date: string;
    hour: number;
    residual_solar_kwh: number | null;
    interval_count: number;
  }>;
};
type ResidualSolarAnalysis = {
  totals: Record<string, number>;
  peak_interval: Record<string, number | string> | null;
  sources: Array<{
    file_id: string;
    label: string;
    address: string | null;
    flow_kind: string;
  }>;
  selected_solar_file_ids: string[] | null;
  monthly: Array<Record<string, number | string>>;
  hourly: Array<Record<string, number | string>>;
  heatmap: ResidualSolarHeatmap;
  peak_intervals: Array<Record<string, number | string>>;
};
type LoadShiftingProfile = {
  file_id: string;
  label: string;
  address: string | null;
  is_zev: boolean;
  annual_demand_kwh: number;
  nighttime_demand_kwh: number;
  nighttime_share: number;
  daytime_demand_kwh: number;
  average_nightly_demand_kwh: number;
  peak_hour_demand_kwh: number;
  coverage_days: number;
};
type LoadShiftingHeatmap = {
  year: number | null;
  days: string[];
  hours: string[];
  scale_max_demand_kwh: number;
  selected_peak: {
    date: string;
    hour: number;
    demand_kwh: number;
  } | null;
  values: Array<{
    date: string;
    hour: number;
    demand_kwh: number | null;
    interval_count: number;
  }>;
};
type LoadShiftingAnalysis = {
  assumptions: string[];
  years: number[];
  selected_year: number | null;
  selected_consumption_file_id: string | null;
  totals: Record<string, number>;
  profiles: LoadShiftingProfile[];
  heatmap: LoadShiftingHeatmap;
};
type OptimizationMember = {
  file_id: string;
  label: string;
  tariff_tier: string;
  measured_kwh: number;
  annualized_kwh: number;
  allocated_local_solar_kwh: number;
  tariff_switch_effect_chf: number;
  leg_network_discount_chf: number;
  energy_price_savings_chf: number;
  cost_savings_chf: number;
  producer_revenue_chf: number;
  energy_rp_per_kwh: number;
  leg_energy_price_rp_per_kwh: number;
  discountable_netznutzung_rp_per_kwh: number;
  local_solar_energy_value_chf: number;
  total_value_chf: number;
};
type OptimizationCombination = {
  participant_count: number;
  participant_labels: string[];
  consumption_kwh: number;
  annualized_kwh: number;
  local_solar_kwh: number;
  combined_tariff_tier: string;
  combined_energy_rp_per_kwh: number;
  combined_variable_netznutzung_rp_per_kwh: number;
  combined_discountable_netznutzung_rp_per_kwh: number;
  leg_energy_price_rp_per_kwh: number;
  tariff_switch_effect_chf: number;
  leg_network_discount_chf: number;
  energy_price_savings_chf: number;
  cost_savings_chf: number;
  producer_revenue_chf: number;
  local_solar_energy_value_chf: number;
  total_value_chf: number;
};
type Optimization = {
  assumptions: string[];
  warnings: string[];
  combination_count: number;
  evaluated_all: boolean;
  optimization_mode: "exhaustive" | "all_participants_assumed_optimal";
  best: (OptimizationCombination & {
    members: OptimizationMember[];
    solar_sources: Array<{
      file_id: string;
      label: string;
      available_solar_kwh: number;
      allocated_local_solar_kwh: number;
      residual_solar_kwh: number;
    }>;
    flow: FlowGraph;
  }) | null;
  combinations: OptimizationCombination[];
};
type BatteryPvSite = {
  file_id: string;
  label: string;
  address: string | null;
  flow_kind: string;
  configured: boolean;
  assignment_mode: "manual" | "implicit_zev";
  selected_consumer_file_ids: string[];
  assigned_consumers: BatteryConsumerOption[];
  pv_kwh: number;
  additional_local_solar_kwh: number;
  additional_local_solar_pct: number;
  local_solar_with_battery_kwh: number;
  remaining_export_kwh: number;
  ending_state_of_charge_kwh: number;
};
type BatteryConsumptionSite = {
  file_id: string;
  label: string;
  address: string | null;
  tariff_tier: string;
  analyzed_months: number;
  demand_rate_chf_per_kw_month: number;
  baseline_peak_kw_month_sum: number;
  battery_peak_kw_month_sum: number;
  reduced_peak_kw_month_sum: number;
  demand_charge_savings_chf: number;
};
type BatteryConsumerOption = {
  file_id: string;
  label: string;
  address: string | null;
  is_zev: boolean;
};
type BatteryPlacements = {
  battery: {
    capacity_kwh: number;
    max_charge_kw: number;
    max_discharge_kw: number;
  };
  assumptions: string[];
  pv_sites: BatteryPvSite[];
  consumer_options: BatteryConsumerOption[];
  consumption_sites: BatteryConsumptionSite[];
  best_pv_site: BatteryPvSite | null;
  best_consumption_site: BatteryConsumptionSite | null;
};
type VzevSimulation = {
  tariff_year: number;
  pv_tariff: {
    mode: "participant_80_pct" | "fixed";
    fixed_rp_per_kwh: number | null;
    minimum_participant_cap_rp_per_kwh: number;
    exceeds_80_percent_cap: boolean;
  };
  warnings: string[];
  assumptions: string[];
  totals: Record<string, number>;
  participants: Array<{
    participant: string;
    metering_code: string;
    consumption_kwh: number;
    allocated_grid_import_kwh: number;
    allocated_local_pv_kwh: number;
    without_vzev_total_chf: number;
    with_vzev_ckw_invoice_chf: number;
    local_pv_energy_cost_chf: number;
    with_vzev_total_chf: number;
    savings_chf: number;
    standard_product_reference_rp_per_kwh: number;
    local_energy_cap_rp_per_kwh: number;
    local_energy_rp_per_kwh: number;
    local_energy_tariff_exceeds_cap: boolean;
    standalone_annualized_kwh: number;
    community_annualized_grid_import_kwh: number;
    standalone_tariff_tier: string;
    community_tariff_tier: string;
    tariff_changed: boolean;
    standalone_demand_rate_chf_per_kw_month: number;
    community_demand_rate_chf_per_kw_month: number;
    standalone_demand_cost_chf: number;
    allocated_community_demand_cost_chf: number;
    demand_cost_change_chf: number;
    other_ckw_cost_change_chf: number;
    ckw_cost_change_chf: number;
    total_cost_change_chf: number;
  }>;
  monthly: Array<Record<string, number | string>>;
};

const FLOW_COLUMN_GAP = 340;
const FLOW_ROW_GAP = 128;

function flowNodeKind(node: Record<string, unknown>) {
  const data = (node.data ?? {}) as Record<string, unknown>;
  return typeof data.kind === "string" ? data.kind : "";
}

function placeLeafGrid(
  nodes: Array<Record<string, unknown>>,
  firstColumnX: number,
  direction: -1 | 1
) {
  const positions = new Map<string, FlowPosition>();
  if (!nodes.length) return { positions, columnCount: 0 };

  const columnCount = Math.max(1, Math.ceil(Math.sqrt(nodes.length / 1.5)));
  const rowsPerColumn = Math.ceil(nodes.length / columnCount);
  nodes.forEach((node, index) => {
    const column = Math.floor(index / rowsPerColumn);
    const row = index % rowsPerColumn;
    const nodesInColumn = Math.min(rowsPerColumn, nodes.length - column * rowsPerColumn);
    positions.set(String(node.id), {
      x: firstColumnX + direction * column * FLOW_COLUMN_GAP,
      y: row * FLOW_ROW_GAP - ((nodesInColumn - 1) * FLOW_ROW_GAP) / 2
    });
  });
  return { positions, columnCount };
}

function layoutOptimizationFlow(nodes: Array<Record<string, unknown>>) {
  const kinds = new Set(nodes.map(flowNodeKind));
  if (!kinds.has("solar") || !kinds.has("leg")) return nodes;

  const positions = new Map<string, FlowPosition>();
  const sources = nodes.filter((node) => flowNodeKind(node) === "source");
  const participants = nodes.filter((node) => flowNodeKind(node) === "participant");
  const sourceGrid = placeLeafGrid(sources, -FLOW_COLUMN_GAP, -1);
  const participantGrid = placeLeafGrid(participants, FLOW_COLUMN_GAP * 2, 1);

  sourceGrid.positions.forEach((position, id) => positions.set(id, position));
  participantGrid.positions.forEach((position, id) => positions.set(id, position));
  positions.set("solar", { x: 0, y: 0 });
  positions.set("leg", { x: FLOW_COLUMN_GAP, y: -80 });
  positions.set("ckw", { x: FLOW_COLUMN_GAP, y: 120 });

  const leftmostSourceX = sourceGrid.columnCount
    ? -sourceGrid.columnCount * FLOW_COLUMN_GAP
    : 0;
  const rightmostParticipantX = participantGrid.columnCount
    ? (participantGrid.columnCount + 1) * FLOW_COLUMN_GAP
    : FLOW_COLUMN_GAP;
  positions.set("zev-production", { x: leftmostSourceX - FLOW_COLUMN_GAP, y: 0 });
  positions.set("zev-consumption", { x: rightmostParticipantX + FLOW_COLUMN_GAP, y: 0 });

  return nodes.map((node) => ({
    ...node,
    position: positions.get(String(node.id)) ?? node.position
  }));
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail ?? response.statusText);
  }
  return response.json() as Promise<T>;
}

function fmtKwh(value?: number) {
  return `${Math.round(value ?? 0).toLocaleString("de-CH")} kWh`;
}

function fmtHeatmapKwh(value?: number) {
  return `${Math.round(value ?? 0).toLocaleString("de-CH")} kWh`;
}

function fmtMoney(value?: number) {
  return `CHF ${(value ?? 0).toLocaleString("de-CH", { maximumFractionDigits: 0 })}`;
}

function fmtMoneyExact(value?: number) {
  return `CHF ${(value ?? 0).toLocaleString("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtCostChange(value?: number) {
  const amount = value ?? 0;
  const sign = amount > 0.004 ? "+" : amount < -0.004 ? "−" : "";
  return `${sign}${fmtMoneyExact(Math.abs(amount))}`;
}

function costChangeClass(value?: number) {
  const amount = value ?? 0;
  return amount > 0.004 ? "costIncrease" : amount < -0.004 ? "costReduction" : "";
}

function tariffTierLabel(value?: string) {
  if (value === "gt_50000") return "> 50 MWh/Jahr";
  if (value === "lt_50000") return "≤ 50 MWh/Jahr";
  return value || "–";
}

function fmtRp(value?: number) {
  return `${(value ?? 0).toLocaleString("de-CH", { maximumFractionDigits: 2 })} Rp./kWh`;
}

function fmtKw(value?: number) {
  return `${(value ?? 0).toLocaleString("de-CH", { maximumFractionDigits: 1 })} kW`;
}

function fmtNumber(value?: number, digits = 1) {
  return (value ?? 0).toLocaleString("de-CH", { maximumFractionDigits: digits });
}

function fmtPct(value?: number) {
  return `${((value ?? 0) * 100).toLocaleString("de-CH", { maximumFractionDigits: 1 })}%`;
}

function fmtTimestamp(value?: number | string) {
  if (!value) return "–";
  return new Date(value).toLocaleString("de-CH", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function flowLabel(value: string) {
  const labels: Record<string, string> = {
    consumption: "Verbrauch",
    pv_production: "PV-Produktion",
    pv_excess: "PV-Überschuss",
    zev_aggregate: "CKW ZEV-Aggregat"
  };
  return labels[value] ?? value;
}

function Chart({
  rows,
  series,
  categoryKey = "timestamp"
}: {
  rows: Array<Record<string, number | string>>;
  series: Array<{ key: string; label: string }>;
  categoryKey?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      animation: false,
      color: ["#287c71", "#d79c24", "#5167a8", "#9b4d55"],
      tooltip: { trigger: "axis" },
      legend: { top: 0 },
      grid: { left: 54, right: 20, bottom: 42, top: 36 },
      xAxis: {
        type: "category",
        data: rows.map((row) => String(row[categoryKey] ?? "").slice(0, 10)),
        axisLabel: { hideOverlap: true }
      },
      yAxis: { type: "value", name: "kWh" },
      series: series.map((item) => ({
        name: item.label,
        type: "line",
        showSymbol: false,
        areaStyle: { opacity: 0.08 },
        data: rows.map((row) => Number(row[item.key] ?? 0))
      }))
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [categoryKey, rows, series]);

  return <div ref={ref} className="chart" />;
}

function VzevCostChangeChart({ participants }: { participants: VzevSimulation["participants"] }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        valueFormatter: (value: number) => fmtCostChange(value)
      },
      grid: { left: 62, right: 24, bottom: 105, top: 32 },
      xAxis: {
        type: "category",
        data: participants.map((participant) => participant.participant),
        axisLabel: { interval: 0, rotate: participants.length > 6 ? 35 : 0, width: 150, overflow: "truncate" }
      },
      yAxis: { type: "value", name: "CHF", axisLine: { show: true } },
      series: [
        {
          name: "Gesamtkostenänderung",
          type: "bar",
          data: participants.map((participant) => ({
            value: participant.total_cost_change_chf,
            itemStyle: { color: participant.total_cost_change_chf > 0 ? "#a53b3b" : "#287c71" }
          })),
          markLine: { silent: true, symbol: "none", data: [{ yAxis: 0 }], lineStyle: { color: "#7b8a84" } }
        }
      ]
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [participants]);

  return <div ref={ref} className="chart vzevCostChangeChart" />;
}

function ResidualSolarHeatmap({ heatmap }: { heatmap?: ResidualSolarHeatmap }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const days = heatmap?.days ?? [];
    const hours = heatmap?.hours ?? [];
    const dayIndexes = new Map(days.map((day, index) => [day, index]));
    const hourIndexes = new Map(hours.map((hour, index) => [hour, index]));
    const values = (heatmap?.values ?? [])
      .filter((item) => item.interval_count > 0)
      .map((item) => [
        dayIndexes.get(item.date) ?? 0,
        hourIndexes.get(`${String(item.hour).padStart(2, "0")}:00`) ?? 0,
        item.residual_solar_kwh ?? 0
      ]);
    const maximum = Math.max(1, heatmap?.scale_max_residual_solar_kwh ?? 0);
    const selectedPeak = heatmap?.selected_peak;
    const selectedPeakValue = selectedPeak?.residual_solar_kwh ?? 0;
    const selectedPeakData = selectedPeak
      ? [
          [
            dayIndexes.get(selectedPeak.date) ?? 0,
            hourIndexes.get(`${String(selectedPeak.hour).padStart(2, "0")}:00`) ?? 0,
            selectedPeakValue
          ]
        ]
      : [];
    chart.setOption({
      animation: false,
      tooltip: {
        position: "top",
        formatter: (params: { data: [number, number, number] }) => {
          const [dayIndex, hourIndex, value] = params.data;
          return `${days[dayIndex] ?? ""} ${hours[hourIndex] ?? ""}<br/><strong>${fmtHeatmapKwh(value)}</strong>`;
        }
      },
      grid: { left: 54, right: 20, bottom: 82, top: 52 },
      xAxis: {
        type: "category",
        data: days,
        splitArea: { show: true },
        axisLabel: {
          interval: 0,
          formatter: (value: string) =>
            value.endsWith("-01")
              ? new Date(`${value}T00:00:00`).toLocaleDateString("de-CH", { month: "short" })
              : ""
        }
      },
      yAxis: {
        type: "category",
        data: hours,
        splitArea: { show: true }
      },
      visualMap: {
        min: 0,
        max: maximum,
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: 4,
        text: [fmtHeatmapKwh(maximum), "0 kWh"],
        inRange: { color: ["#f7f4ef", "#f5d88d", "#d79c24", "#9b4d55"] }
      },
      series: [
        {
          name: "Verbleibender Solarüberschuss",
          type: "heatmap",
          data: values,
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0, 0, 0, 0.35)" } }
        },
        {
          name: "Maximum Auswahl",
          type: "scatter",
          data: selectedPeakData,
          symbol: "circle",
          symbolSize: 16,
          itemStyle: {
            color: "#17201d",
            borderColor: "#fff",
            borderWidth: 2
          },
          label: {
            show: Boolean(selectedPeak),
            formatter: `Maximum Auswahl\n${fmtHeatmapKwh(selectedPeakValue)}`,
            position: "top",
            color: "#17201d",
            backgroundColor: "rgba(255,255,255,0.94)",
            borderColor: "#9fb2aa",
            borderWidth: 1,
            borderRadius: 4,
            padding: [4, 6]
          },
          z: 4
        }
      ]
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [heatmap]);

  return <div ref={ref} className="chart heatmapChart" />;
}

function LoadShiftingHeatmap({ heatmap }: { heatmap?: LoadShiftingHeatmap }) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const days = heatmap?.days ?? [];
    const hours = heatmap?.hours ?? [];
    const dayIndexes = new Map(days.map((day, index) => [day, index]));
    const hourIndexes = new Map(hours.map((hour, index) => [hour, index]));
    const values = (heatmap?.values ?? [])
      .filter((item) => item.interval_count > 0)
      .map((item) => [
        dayIndexes.get(item.date) ?? 0,
        hourIndexes.get(`${String(item.hour).padStart(2, "0")}:00`) ?? 0,
        item.demand_kwh ?? 0
      ]);
    const maximum = Math.max(1, heatmap?.scale_max_demand_kwh ?? 0);
    const selectedPeak = heatmap?.selected_peak;
    const selectedPeakValue = selectedPeak?.demand_kwh ?? 0;
    const selectedPeakData = selectedPeak
      ? [
          [
            dayIndexes.get(selectedPeak.date) ?? 0,
            hourIndexes.get(`${String(selectedPeak.hour).padStart(2, "0")}:00`) ?? 0,
            selectedPeakValue
          ]
        ]
      : [];
    chart.setOption({
      animation: false,
      tooltip: {
        position: "top",
        formatter: (params: { data: [number, number, number] }) => {
          const [dayIndex, hourIndex, value] = params.data;
          return `${days[dayIndex] ?? ""} ${hours[hourIndex] ?? ""}<br/><strong>${fmtHeatmapKwh(value)}</strong>`;
        }
      },
      grid: { left: 54, right: 20, bottom: 82, top: 52 },
      xAxis: {
        type: "category",
        data: days,
        splitArea: { show: true },
        axisLabel: {
          interval: 0,
          formatter: (value: string) =>
            value.endsWith("-01")
              ? new Date(`${value}T00:00:00`).toLocaleDateString("de-CH", { month: "short" })
              : ""
        }
      },
      yAxis: {
        type: "category",
        data: hours,
        splitArea: { show: true }
      },
      visualMap: {
        min: 0,
        max: maximum,
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: 4,
        text: [fmtHeatmapKwh(maximum), "0 kWh"],
        inRange: { color: ["#f7f4ef", "#cfe7df", "#5da99a", "#1f6f64"] }
      },
      series: [
        {
          name: "Stündlicher Verbrauch",
          type: "heatmap",
          data: values,
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0, 0, 0, 0.35)" } }
        },
        {
          name: "Maximum Auswahl",
          type: "scatter",
          data: selectedPeakData,
          symbol: "circle",
          symbolSize: 16,
          itemStyle: {
            color: "#17201d",
            borderColor: "#fff",
            borderWidth: 2
          },
          label: {
            show: Boolean(selectedPeak),
            formatter: `Maximum Auswahl\n${fmtHeatmapKwh(selectedPeakValue)}`,
            position: "top",
            color: "#17201d",
            backgroundColor: "rgba(255,255,255,0.94)",
            borderColor: "#9fb2aa",
            borderWidth: 1,
            borderRadius: 4,
            padding: [4, 6]
          },
          z: 4
        }
      ]
    });
    const resize = () => chart.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chart.dispose();
    };
  }, [heatmap]);

  return <div ref={ref} className="chart loadShiftingHeatmap" />;
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {hint ? <small>{hint}</small> : null}
    </div>
  );
}

function FileUpload({
  title,
  acceptLabel,
  onUpload,
  disabled = false
}: {
  title: string;
  acceptLabel: string;
  onUpload: (files: FileList) => Promise<void>;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<FileList | null>(null);

  async function submit() {
    if (!files?.length) return;
    setBusy(true);
    try {
      await onUpload(files);
      setFiles(null);
    } catch {
      // The parent renders the API error and the selection remains available
      // so the user can retry without choosing the files again.
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="uploadBlock">
      <div>
        <h3>{title}</h3>
        <p>{acceptLabel}</p>
      </div>
      <label className="fileInput">
        <Upload size={18} />
        <input
          type="file"
          multiple
          accept=".csv,text/csv"
          disabled={disabled}
          onChange={(event) => setFiles(event.target.files)}
        />
        <span>{disabled ? "Zuerst Projekt anlegen" : files?.length ? `${files.length} Datei(en)` : "CSV wählen"}</span>
      </label>
      <button type="button" onClick={submit} disabled={disabled || !files?.length || busy}>
        <Upload size={16} />
        {busy ? "Hochladen..." : "Hochladen"}
      </button>
    </div>
  );
}

export function App() {
  const [workspace, setWorkspace] = useState<Workspace>(() => workspaceFromHash(window.location.hash));
  const [municipalities, setMunicipalities] = useState<Municipality[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [plants, setPlants] = useState<PlantResult | null>(null);
  const [calculation, setCalculation] = useState<Calculation | null>(null);
  const [residualSolar, setResidualSolar] = useState<ResidualSolarAnalysis | null>(null);
  const [loadShifting, setLoadShifting] = useState<LoadShiftingAnalysis | null>(null);
  const [loadShiftingBusy, setLoadShiftingBusy] = useState(false);
  const [batteryPlacements, setBatteryPlacements] = useState<BatteryPlacements | null>(null);
  const [batteryPlacementsBusy, setBatteryPlacementsBusy] = useState(false);
  const [batteryAssignmentBusyId, setBatteryAssignmentBusyId] = useState("");
  const [vzevSimulation, setVzevSimulation] = useState<VzevSimulation | null>(null);
  const [vzevSimulationBusy, setVzevSimulationBusy] = useState(false);
  const [vzevPvTariffMode, setVzevPvTariffMode] = useState<"participant_80_pct" | "fixed">("participant_80_pct");
  const [fixedVzevPvTariff, setFixedVzevPvTariff] = useState("");
  const [vzevPvTariffBusy, setVzevPvTariffBusy] = useState(false);
  const [vzevPvTariffWarning, setVzevPvTariffWarning] = useState("");
  const [exportingVzevCsv, setExportingVzevCsv] = useState(false);
  const [exportingVzevTariffCsv, setExportingVzevTariffCsv] = useState(false);
  const [exportingVzevWord, setExportingVzevWord] = useState(false);
  const [selectedResidualSolarSourceIds, setSelectedResidualSolarSourceIds] = useState<string[]>([]);
  const [residualSolarBusy, setResidualSolarBusy] = useState(false);
  const [optimization, setOptimization] = useState<Optimization | null>(null);
  const [legNetworkDiscount, setLegNetworkDiscount] = useState("0.40");
  const [legEnergyPrice, setLegEnergyPrice] = useState("");
  const [legPricingBusy, setLegPricingBusy] = useState(false);
  const [solarRows, setSolarRows] = useState<Array<Record<string, number | string>>>([]);
  const [legRows, setLegRows] = useState<Array<Record<string, number | string>>>([]);
  const [newName, setNewName] = useState("LEG Potenzial");
  const [municipality, setMunicipality] = useState("Sursee");
  const [error, setError] = useState<string>("");
  const [success, setSuccess] = useState<string>("");
  const [deletingFileId, setDeletingFileId] = useState<string>("");
  const [deletingProjectId, setDeletingProjectId] = useState<string>("");
  const [plantBusy, setPlantBusy] = useState(false);
  const [selectedCachePlantId, setSelectedCachePlantId] = useState("");
  const [manualPlantAddress, setManualPlantAddress] = useState("");
  const [manualPlantKwp, setManualPlantKwp] = useState("");
  const [exportingWord, setExportingWord] = useState(false);
  const [exportingExcel, setExportingExcel] = useState(false);
  const [savedResults, setSavedResults] = useState<SavedResult[]>([]);

  const selectedProject = useMemo(
    () => projects.find((project) => project.project_id === selectedId) ?? summary,
    [projects, selectedId, summary]
  );
  const visualizationFlow = useMemo(() => {
    const optimizationGraph = optimization?.best?.flow;
    const zevGraph = optimizationGraph ? null : summary?.zev_flow;
    const flow: FlowGraph = {
      nodes: layoutOptimizationFlow([...(optimizationGraph?.nodes ?? []), ...(zevGraph?.nodes ?? [])]),
      edges: [...(optimizationGraph?.edges ?? []), ...(zevGraph?.edges ?? [])]
    };
    function positions(kind?: string) {
      if (kind === "zev-source" || kind === "zev-grid-import") {
        return { sourcePosition: Position.Right };
      }
      if (kind === "grid" || kind === "zev-consumption" || kind === "zev-grid-export") {
        return { targetPosition: Position.Left };
      }
      return { sourcePosition: Position.Right, targetPosition: Position.Left };
    }
    const nodes = flow.nodes.map((node) => {
      const data = (node.data ?? {}) as Record<string, string>;
      const kind = data.kind;
      const nextNode = {
        ...node,
        ...positions(kind),
        className: `flowNodeShell flowNode-${kind ?? "standard"}`,
        data: {
          ...data,
          label: (
            <div className="flowNode">
              <strong>{data.label}</strong>
              <span>{data.value}</span>
              {data.subtitle ? <small>{data.subtitle}</small> : null}
              {data.tariff ? <small>{data.tariff}</small> : null}
            </div>
          )
        }
      };
      return nextNode as unknown as Node;
    });
    const edges = flow.edges.map((edge) => ({ type: "smoothstep", ...edge })) as Edge[];
    return { nodes, edges };
  }, [optimization, summary]);
  const selectablePlants = useMemo(() => {
    const selectedIds = new Set(plants?.matches.map((plant) => plant.cache_id).filter(Boolean));
    return plants?.available_plants.filter((plant) => !selectedIds.has(plant.cache_id)) ?? [];
  }, [plants]);
  const vzevSimulationInputsReady = hasVzevSimulationInputs(summary?.files ?? []);

  useEffect(() => {
    const project = projects.find((item) => item.project_id === selectedId);
    if (!project?.config) return;
    setLegNetworkDiscount(project.config.leg?.networkDiscount ?? "0.40");
    setLegEnergyPrice(project.config.leg?.energyPriceRpPerKwh ?? "");
    const mode = project.config.vzev?.tariffMode ?? "participant_80_pct";
    setVzevPvTariffMode(mode);
    const fixed = project.config.vzev?.fixedPvTariffRpPerKwh;
    setFixedVzevPvTariff(fixed === null || fixed === undefined ? "" : String(fixed));
  }, [projects, selectedId]);

  async function refreshProjects() {
    const [municipalityList, projectList] = await Promise.all([
      api<Municipality[]>("/api/municipalities"),
      api<Project[]>("/api/projects")
    ]);
    const planningProjects = projectList.filter((project) => !project.config?.billing || Object.keys(project.config.billing).length === 0);
    setMunicipalities(municipalityList);
    setProjects(planningProjects);
    if (!selectedId && planningProjects.length) setSelectedId(planningProjects[0].project_id);
    return planningProjects;
  }

  function navigateWorkspace(nextWorkspace: Workspace) {
    const nextHash = workspaceRoutes[nextWorkspace];
    if (window.location.hash !== nextHash) window.location.hash = nextHash;
    setWorkspace(nextWorkspace);
  }

  function clearProjectData() {
    setSummary(null);
    setPlants(null);
    setCalculation(null);
    setResidualSolar(null);
    setLoadShifting(null);
    setBatteryPlacements(null);
    setVzevSimulation(null);
    setSelectedResidualSolarSourceIds([]);
    setOptimization(null);
    setSolarRows([]);
    setLegRows([]);
    setSavedResults([]);
  }

  async function refreshSavedResults(projectId = selectedId) {
    if (!projectId) return;
    setSavedResults(await api<SavedResult[]>(`/v1/ckw/projects/${projectId}/results`));
  }

  async function saveProjectConfig(section: "leg" | "vzev", value: Record<string, unknown>) {
    if (!selectedId) return;
    const current = projects.find((project) => project.project_id === selectedId);
    const updated = await api<Project>(`/api/projects/${selectedId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: { ...(current?.config ?? {}), [section]: value }
      })
    });
    setProjects((items) => items.map((item) => item.project_id === updated.project_id ? updated : item));
  }

  async function downloadSavedResult(result: SavedResult) {
    const response = await apiFetch(result.download_url);
    if (!response.ok) throw new Error("Das gespeicherte Resultat konnte nicht geladen werden.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = result.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  async function refreshSharedProjectData(projectId: string) {
    const [nextSummary, nextPlants, nextSolarRows] = await Promise.all([
      api<Summary>(`/api/projects/${projectId}/summary`),
      api<PlantResult>(`/api/projects/${projectId}/plants`),
      api<Array<Record<string, number | string>>>(`/api/projects/${projectId}/timeseries?kind=solar&resolution=day`)
    ]);
    setSummary(nextSummary);
    setPlants(nextPlants);
    setSelectedCachePlantId("");
    setSolarRows(nextSolarRows);
    return nextSummary;
  }

  async function refreshLegProjectData(projectId: string) {
    const [nextCalculation, nextResidualSolar, nextOptimization, nextLegRows] = await Promise.all([
      api<Calculation>(`/api/projects/${projectId}/calculation?${legPricingQuery()}`),
      api<ResidualSolarAnalysis>(`/api/projects/${projectId}/residual-solar`),
      api<Optimization>(`/api/projects/${projectId}/optimization?${legPricingQuery()}`),
      api<Array<Record<string, number | string>>>(`/api/projects/${projectId}/timeseries?kind=leg&resolution=day`)
    ]);
    setCalculation(nextCalculation);
    setResidualSolar(nextResidualSolar);
    setSelectedResidualSolarSourceIds([]);
    setOptimization(nextOptimization);
    setLegRows(nextLegRows);
  }

  async function refreshVzevProjectData(projectId: string, projectSummary: Summary) {
    const canSimulate = hasVzevSimulationInputs(projectSummary.files);
    setVzevSimulationBusy(canSimulate);
    try {
      const [nextLoadShifting, nextVzevSimulation] = await Promise.all([
        api<LoadShiftingAnalysis>(`/api/projects/${projectId}/load-shifting`),
        canSimulate
          ? api<VzevSimulation>(`/api/projects/${projectId}/vzev-simulation`)
          : Promise.resolve(null)
      ]);
      setLoadShifting(nextLoadShifting);
      setVzevSimulation(nextVzevSimulation);
      setVzevPvTariffWarning("");
    } finally {
      setVzevSimulationBusy(false);
    }
  }

  async function refreshProject(projectId = selectedId, targetWorkspace = workspace) {
    if (!projectId || targetWorkspace === "billing") return;

    setBatteryPlacements(null);
    setVzevSimulation(null);
    if (targetWorkspace === "leg") {
      setLoadShifting(null);
      await Promise.all([
        refreshSharedProjectData(projectId),
        refreshLegProjectData(projectId),
        refreshSavedResults(projectId)
      ]);
      return;
    }

    setCalculation(null);
    setResidualSolar(null);
    setSelectedResidualSolarSourceIds([]);
    setOptimization(null);
    setLegRows([]);
    const nextSummary = await refreshSharedProjectData(projectId);
    await Promise.all([refreshVzevProjectData(projectId, nextSummary), refreshSavedResults(projectId)]);
  }

  function legPricingQuery(): string {
    const query = new URLSearchParams({ network_discount: legNetworkDiscount });
    if (legEnergyPrice.trim()) query.set("leg_energy_price_rp_per_kwh", legEnergyPrice.trim());
    return query.toString();
  }

  async function refreshLegPricing() {
    if (!selectedId) return;
    setError("");
    setLegPricingBusy(true);
    try {
      const query = legPricingQuery();
      const [nextCalculation, nextOptimization] = await Promise.all([
        api<Calculation>(`/api/projects/${selectedId}/calculation?${query}`),
        api<Optimization>(`/api/projects/${selectedId}/optimization?${query}`)
      ]);
      setCalculation(nextCalculation);
      setOptimization(nextOptimization);
      await saveProjectConfig("leg", {
        networkDiscount: legNetworkDiscount,
        energyPriceRpPerKwh: legEnergyPrice.trim() || null
      });
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setLegPricingBusy(false);
    }
  }

  async function applyVzevPvTariff() {
    if (!selectedId) return;
    const query = new URLSearchParams({ tariff_year: String(vzevSimulation?.tariff_year ?? 2026) });
    if (vzevPvTariffMode === "fixed") {
      const fixedTariff = Number(fixedVzevPvTariff);
      if (!fixedVzevPvTariff.trim() || !Number.isFinite(fixedTariff) || fixedTariff < 0) {
        setVzevPvTariffWarning("Bitte einen fixen PV-Tarif von mindestens 0 Rp./kWh eingeben.");
        return;
      }
      query.set("fixed_pv_tariff_rp_per_kwh", String(fixedTariff));
    }
    setVzevPvTariffBusy(true);
    setVzevPvTariffWarning("");
    try {
      setVzevSimulation(await api<VzevSimulation>(`/api/projects/${selectedId}/vzev-simulation?${query}`));
      await saveProjectConfig("vzev", {
        tariffMode: vzevPvTariffMode,
        fixedPvTariffRpPerKwh: vzevPvTariffMode === "fixed" ? fixedVzevPvTariff : null
      });
    } catch (exc) {
      reportError(exc, "calculator");
      setVzevPvTariffWarning(String((exc as Error).message ?? exc));
    } finally {
      setVzevPvTariffBusy(false);
    }
  }

  useEffect(() => {
    refreshProjects().catch((exc) => { reportError(exc, "calculator"); setError(String(exc.message ?? exc)); });
  }, []);

  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, "", workspaceRoutes.leg);
    const handleHashChange = () => setWorkspace(workspaceFromHash(window.location.hash));
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => {
    refreshProject(selectedId, workspace).catch((exc) => { reportError(exc, "calculator"); setError(String(exc.message ?? exc)); });
  }, [selectedId, workspace]);

  useEffect(() => {
    setNewName((currentName) => {
      if (workspace === "vzev" && currentName === "LEG Potenzial") return "vZEV Simulation";
      if (workspace === "leg" && currentName === "vZEV Simulation") return "LEG Potenzial";
      return currentName;
    });
  }, [workspace]);

  async function createProject(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const project = await api<Project>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName, municipality })
      });
      await refreshProjects();
      setSelectedId(project.project_id);
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    }
  }

  async function upload(files: FileList, endpoint: "solar-files" | "consumption-files" | "zev-files") {
    if (!selectedId) {
      setError("Bitte zuerst links ein Projekt anlegen oder auswählen.");
      return;
    }
    setError("");
    setSuccess("");
    const form = new FormData();
    Array.from(files).forEach((file) => form.append("files", file));
    try {
      const results = await api<UploadResult[]>(`/api/projects/${selectedId}/${endpoint}`, { method: "POST", body: form });
      await refreshProject();
      await refreshProjects();
      const imported = results.filter((result) => !result.duplicate);
      const duplicates = results.length - imported.length;
      const rowsInserted = imported.reduce((total, result) => total + result.rows_inserted, 0);
      const parts: string[] = [];
      if (imported.length) {
        parts.push(`${imported.length} ${imported.length === 1 ? "Datei" : "Dateien"} mit ${fmtNumber(rowsInserted, 0)} Messwerten importiert`);
      }
      if (duplicates) {
        parts.push(`${duplicates} ${duplicates === 1 ? "bereits vorhandene Datei wurde" : "bereits vorhandene Dateien wurden"} nicht dupliziert`);
      }
      setSuccess(`${parts.join("; ")}.`);
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
      throw exc;
    }
  }

  async function deleteFile(file: Summary["files"][number]) {
    if (!selectedId || !window.confirm(`Datei "${file.original_filename}" wirklich löschen?`)) return;
    setError("");
    setDeletingFileId(file.file_id);
    try {
      await api(`/api/projects/${selectedId}/files/${file.file_id}`, { method: "DELETE" });
      await Promise.all([refreshProject(selectedId), refreshProjects()]);
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setDeletingFileId("");
    }
  }

  async function deleteProject(project: Project) {
    if (!window.confirm(`Projekt "${project.name}" wirklich löschen? Alle importierten Dateien werden entfernt.`)) return;
    setError("");
    setDeletingProjectId(project.project_id);
    try {
      await api(`/api/projects/${project.project_id}`, { method: "DELETE" });
      const projectList = await refreshProjects();
      if (project.project_id === selectedId) {
        clearProjectData();
        setSelectedId(projectList[0]?.project_id ?? "");
      }
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setDeletingProjectId("");
    }
  }

  async function mutatePlants(path: string, init?: RequestInit) {
    if (!selectedId) return false;
    setError("");
    setPlantBusy(true);
    try {
      const nextPlants = await api<PlantResult>(`/api/projects/${selectedId}/plants${path}`, init);
      setPlants(nextPlants);
      setSelectedCachePlantId("");
      return true;
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
      return false;
    } finally {
      setPlantBusy(false);
    }
  }

  async function selectResidualSolarSources(nextSourceIds: string[]) {
    if (!selectedId) return;
    const previousSourceIds = selectedResidualSolarSourceIds;
    setError("");
    setResidualSolarBusy(true);
    setSelectedResidualSolarSourceIds(nextSourceIds);
    try {
      const query = new URLSearchParams();
      nextSourceIds.forEach((fileId) => query.append("solar_file_id", fileId));
      const suffix = query.size ? `?${query.toString()}` : "";
      setResidualSolar(await api<ResidualSolarAnalysis>(`/api/projects/${selectedId}/residual-solar${suffix}`));
    } catch (exc) {
      reportError(exc, "calculator");
      setSelectedResidualSolarSourceIds(previousSourceIds);
      setError(String((exc as Error).message ?? exc));
    } finally {
      setResidualSolarBusy(false);
    }
  }

  async function loadBatteryPlacements() {
    if (!selectedId) return;
    setError("");
    setBatteryPlacementsBusy(true);
    try {
      setBatteryPlacements(await api<BatteryPlacements>(`/api/projects/${selectedId}/battery-placements`));
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setBatteryPlacementsBusy(false);
    }
  }

  async function selectLoadShiftingProfile(consumptionFileId: string, year = loadShifting?.selected_year) {
    if (!selectedId) return;
    setError("");
    setLoadShiftingBusy(true);
    try {
      const query = new URLSearchParams();
      if (consumptionFileId) query.set("consumption_file_id", consumptionFileId);
      if (year) query.set("year", String(year));
      const suffix = query.size ? `?${query.toString()}` : "";
      setLoadShifting(await api<LoadShiftingAnalysis>(`/api/projects/${selectedId}/load-shifting${suffix}`));
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setLoadShiftingBusy(false);
    }
  }

  async function updatePvBatteryConsumers(solarFileId: string, consumerFileIds: string[]) {
    if (!selectedId) return;
    setError("");
    setBatteryAssignmentBusyId(solarFileId);
    try {
      setBatteryPlacements(
        await api<BatteryPlacements>(`/api/projects/${selectedId}/battery-placements/pv-sites/${solarFileId}/consumers`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ consumer_file_ids: consumerFileIds })
        })
      );
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setBatteryAssignmentBusyId("");
    }
  }

  async function exportVzevCsv() {
    if (!selectedId) return;
    setError("");
    setExportingVzevCsv(true);
    try {
      const response = await apiFetch(`/api/projects/${selectedId}/vzev-simulation.csv`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? response.statusText);
      }
      const contentDisposition = response.headers.get("Content-Disposition") ?? "";
      const filename = contentDisposition.match(/filename="([^"]+)"/)?.[1] ?? "vzev-simulation.csv";
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await refreshSavedResults();
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setExportingVzevCsv(false);
    }
  }

  async function exportVzevTariffComparisonCsv() {
    if (!selectedId) return;
    setError("");
    setExportingVzevTariffCsv(true);
    try {
      const params = new URLSearchParams({ tariff_year: String(vzevSimulation?.tariff_year ?? 2026) });
      if (vzevSimulation?.pv_tariff.mode === "fixed" && vzevSimulation.pv_tariff.fixed_rp_per_kwh !== null) {
        params.set("fixed_pv_tariff_rp_per_kwh", String(vzevSimulation.pv_tariff.fixed_rp_per_kwh));
      }
      const response = await apiFetch(
        `/api/projects/${selectedId}/vzev-tariff-comparison.csv?${params}`
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? response.statusText);
      }
      const contentDisposition = response.headers.get("Content-Disposition") ?? "";
      const filename = contentDisposition.match(/filename="([^"]+)"/)?.[1] ?? "vzev-tarifvergleich.csv";
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await refreshSavedResults();
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setExportingVzevTariffCsv(false);
    }
  }

  async function exportVzevWordReport() {
    if (!selectedId) return;
    setError("");
    setExportingVzevWord(true);
    try {
      const params = new URLSearchParams({
        tariff_year: String(vzevSimulation?.tariff_year ?? 2026)
      });
      if (loadShifting?.selected_consumption_file_id) {
        params.set("consumption_file_id", loadShifting.selected_consumption_file_id);
      }
      if (loadShifting?.selected_year) {
        params.set("load_shifting_year", String(loadShifting.selected_year));
      }
      if (vzevSimulation?.pv_tariff.mode === "fixed" && vzevSimulation.pv_tariff.fixed_rp_per_kwh !== null) {
        params.set("fixed_pv_tariff_rp_per_kwh", String(vzevSimulation.pv_tariff.fixed_rp_per_kwh));
      }
      const response = await apiFetch(`/api/projects/${selectedId}/vzev-word-report?${params}`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? response.statusText);
      }
      const contentDisposition = response.headers.get("Content-Disposition") ?? "";
      const filename = contentDisposition.match(/filename="([^"]+)"/)?.[1] ?? "vzev-simulationsbericht.docx";
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await refreshSavedResults();
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setExportingVzevWord(false);
    }
  }

  async function addCachedPlant() {
    if (!selectedCachePlantId) return;
    await mutatePlants("/cache", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cache_id: selectedCachePlantId })
    });
  }

  async function addManualPlant(event: FormEvent) {
    event.preventDefault();
    const installedKwp = Number(manualPlantKwp);
    if (!manualPlantAddress.trim() || !Number.isFinite(installedKwp) || installedKwp <= 0) {
      setError("Für eine manuelle PV-Anlage werden Adresse und installierte Leistung grösser als 0 kWp benötigt.");
      return;
    }
    const added = await mutatePlants("/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address: manualPlantAddress, installed_kwp: installedKwp })
    });
    if (added) {
      setManualPlantAddress("");
      setManualPlantKwp("");
    }
  }

  async function exportWordReport() {
    if (!selectedId) return;
    setError("");
    setExportingWord(true);
    try {
      const response = await apiFetch(`/api/projects/${selectedId}/word-report?${legPricingQuery()}`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? response.statusText);
      }
      const contentDisposition = response.headers.get("Content-Disposition") ?? "";
      const filename = contentDisposition.match(/filename="([^"]+)"/)?.[1] ?? "leg-potenzial.docx";
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await refreshSavedResults();
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setExportingWord(false);
    }
  }

  async function exportExcelValidation() {
    if (!selectedId) return;
    setError("");
    setExportingExcel(true);
    try {
      const response = await apiFetch(`/api/projects/${selectedId}/excel-export?${legPricingQuery()}`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? response.statusText);
      }
      const contentDisposition = response.headers.get("Content-Disposition") ?? "";
      const filename = contentDisposition.match(/filename="([^"]+)"/)?.[1] ?? "leg-validierung.xlsx";
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      await refreshSavedResults();
    } catch (exc) {
      reportError(exc, "calculator");
      setError(String((exc as Error).message ?? exc));
    } finally {
      setExportingExcel(false);
    }
  }

  if (workspace === "billing") {
    return <BillingWorkspace onNavigate={navigateWorkspace} onGenerated={refreshProjects} />;
  }

  return (
    <main>
      <aside>
        <div className="brand">
          <PlugZap size={24} />
          <div>
            <strong>Communi</strong>
            <span>CKW Werkzeuge</span>
          </div>
        </div>

        <WorkspaceNavigation current={workspace} onNavigate={navigateWorkspace} />

        <div className="workspaceHint">
          <strong>{workspace === "leg" ? "Lokale Elektrizitätsgemeinschaft" : "vZEV planen und optimieren"}</strong>
          <span>
            {workspace === "leg"
              ? "Potenzial, lokaler Energiepreis, Netzrabatt und optimale Teilnehmerzusammensetzung untersuchen."
              : "vZEV-Energieflüsse simulieren, Lastverschiebung prüfen und den besten Batterieeinsatz vergleichen."}
          </span>
        </div>

        <form onSubmit={createProject} className="newProject">
          <label>
            Projektname
            <input value={newName} onChange={(event) => setNewName(event.target.value)} />
          </label>
          <label>
            Gemeinde
            <select value={municipality} onChange={(event) => setMunicipality(event.target.value)}>
              {municipalities.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <button type="submit">
            <Plus size={16} />
            Neues Projekt
          </button>
        </form>

        <section className="projectList">
          <div className="sectionTitle">
            <FolderOpen size={16} />
            Projekte
          </div>
          {projects.map((project) => (
            <div
              key={project.project_id}
              className={`projectListItem ${project.project_id === selectedId ? "active" : ""}`}
            >
              <button type="button" className="projectSelectButton" onClick={() => setSelectedId(project.project_id)}>
                <span>{project.name}</span>
                <small>
                  {project.municipality} · {project.file_count ?? 0} Dateien
                </small>
              </button>
              <button
                type="button"
                className="projectDeleteButton"
                title={`${project.name} löschen`}
                aria-label={`${project.name} löschen`}
                disabled={Boolean(deletingProjectId)}
                onClick={() => deleteProject(project)}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </section>
      </aside>

      <section className="content">
        <header>
          <div>
            <span className="eyebrow">
              {workspace === "leg" ? "LEG-Potenzial" : "vZEV-Simulation & Batterie"}
              {selectedProject?.municipality ? ` · ${selectedProject.municipality}` : ""}
            </span>
            <h1>{selectedProject?.name ?? "Projekt anlegen"}</h1>
          </div>
          <div className="headerActions">
            {workspace === "leg" ? (
              <>
                <button
                  type="button"
                  className="secondaryButton"
                  onClick={exportExcelValidation}
                  disabled={!selectedId || exportingExcel}
                >
                  <Download size={16} />
                  {exportingExcel ? "Excel wird erstellt..." : "Validierungs-Excel"}
                </button>
                <button
                  type="button"
                  className="secondaryButton"
                  onClick={exportWordReport}
                  disabled={!selectedId || exportingWord}
                >
                  <Download size={16} />
                  {exportingWord ? "Word wird erstellt..." : "Als Word exportieren"}
                </button>
              </>
            ) : (
              <button
                type="button"
                className="secondaryButton"
                onClick={exportVzevWordReport}
                disabled={!selectedId || exportingVzevWord}
              >
                <Download size={16} />
                {exportingVzevWord ? "Word wird erstellt..." : "vZEV-Bericht als Word"}
              </button>
            )}
            <button type="button" onClick={() => refreshProject()} disabled={!selectedId}>
              <RefreshCw size={16} />
              Aktualisieren
            </button>
          </div>
        </header>

        {error ? <div className="alert error">{error}</div> : null}
        {success ? <div className="alert success">{success}</div> : null}
        {savedResults.length ? (
          <details className="panel savedResults">
            <summary>Gespeicherte Resultate ({savedResults.length})</summary>
            <div className="savedResultList">
              {savedResults.map((result) => (
                <button
                  type="button"
                  className="secondaryButton"
                  key={result.result_id}
                  onClick={() => downloadSavedResult(result).catch((reason) => { reportError(reason, "calculator"); setError(String(reason?.message ?? reason)); })}
                >
                  <Download size={15} />
                  {result.filename}
                </button>
              ))}
            </div>
          </details>
        ) : null}
        {!selectedId ? (
          <div className="alert">Vor dem Dateiimport bitte links ein neues Projekt anlegen oder ein bestehendes auswählen.</div>
        ) : null}
        {summary?.warnings?.map((warning) => (
          <div className="alert" key={warning}>
            {warning}
          </div>
        ))}
        {plants?.warnings?.map((warning) => (
          <div className="alert" key={warning}>
            {warning}
          </div>
        ))}
        {workspace === "leg" ? calculation?.warnings?.map((warning) => (
          <div className="alert" key={warning}>
            {warning}
          </div>
        )) : null}
        {workspace === "leg" ? optimization?.warnings?.map((warning) => (
          <div className="alert" key={warning}>
            {warning}
          </div>
        )) : null}

        <section className="metrics">
          <Metric label="Verbrauch" value={fmtKwh(summary?.totals.consumption_kwh)} />
          <Metric
            label="PV-Produktion gemessen"
            value={fmtKwh(summary?.totals.pv_production_kwh)}
            hint="Summe der hochgeladenen Brutto-PV-Zeitreihen"
          />
          <Metric
            label="PV-Überschuss gemessen"
            value={fmtKwh(summary?.totals.pv_excess_kwh)}
            hint="Summe der hochgeladenen Überschuss-Zeitreihen"
          />
          {workspace === "vzev" && summary?.files.some((file) => file.flow_kind === "zev_aggregate") ? (
            <>
              <Metric label="Netzbezug gemessen" value={fmtKwh(summary?.totals.grid_import_kwh)} />
              <Metric label="Eigenverbrauch gemessen" value={fmtKwh(summary?.totals.local_pv_consumption_kwh)} />
            </>
          ) : null}
          {workspace === "leg" ? (
            <>
              <Metric label="Lokaler Solarstrom" value={fmtKwh(calculation?.totals.local_solar_consumed_kwh)} />
              <Metric
                label="Produzentenerlös lokal"
                value={fmtMoney(calculation?.totals.producer_revenue_chf)}
                hint={`${fmtRp(calculation?.rates?.leg_energy_price_rp_per_kwh)} × lokale kWh`}
              />
              <Metric label="LEG-Netzkostenrabatt" value={fmtMoney(calculation?.totals.network_discount_chf)} />
              <Metric label="Energiepreisvorteil" value={fmtMoney(calculation?.totals.energy_price_savings_chf)} />
              <Metric label="Verbrauchervorteil total" value={fmtMoney(calculation?.totals.consumer_total_savings_chf)} />
            </>
          ) : null}
          <Metric
            label="Anschlussleistung"
            value={`${fmtNumber(plants?.max_connection_ampere, 0)} A`}
            hint={`${fmtNumber(plants?.total_installed_kwp, 1)} kWp`}
          />
        </section>

        <section className="workflow">
          <FileUpload
            title="1. Solarzeitreihen"
            acceptLabel="CKW PV-Produktion oder PV-Überschuss hochladen."
            onUpload={(files) => upload(files, "solar-files")}
            disabled={!selectedId}
          />
          <FileUpload
            title="2. Verbrauchsdaten"
            acceptLabel="CKW Verbrauchsdaten der Liegenschaften hochladen."
            onUpload={(files) => upload(files, "consumption-files")}
            disabled={!selectedId}
          />
          {workspace === "vzev" ? (
            <FileUpload
              title="3. CKW ZEV-Aggregat"
              acceptLabel="CKW ZEV-Datei mit Verbrauch, PV-Produktion und PV-Überschuss hochladen."
              onUpload={(files) => upload(files, "zev-files")}
              disabled={!selectedId}
            />
          ) : null}
        </section>

        <section className="chartRow">
          <div className="panel">
            <h2>Solarenergie</h2>
            <Chart
              rows={solarRows}
              series={[
                { key: "pv_production", label: "PV-Produktion" },
                { key: "pv_excess", label: "PV-Überschuss" },
                { key: "available_solar_kwh", label: "Verfügbarer Solarstrom" }
              ]}
            />
          </div>
          {workspace === "leg" ? (
            <div className="panel">
              <h2>LEG-Simulation</h2>
              <Chart
                rows={legRows}
                series={[
                  { key: "local_solar_kwh", label: "Lokal verbrauchter Solarstrom" },
                  { key: "residual_solar_kwh", label: "Restlicher Solarstrom" },
                  { key: "grid_import_kwh", label: "Verbleibender Netzbezug" }
                ]}
              />
            </div>
          ) : (
            <div className="panel">
              <h2>Gemessener vZEV-Energiefluss</h2>
              {visualizationFlow.nodes.length ? (
                <div className="flowBox">
                  <ReactFlow nodes={visualizationFlow.nodes} edges={visualizationFlow.edges} fitView>
                    <Background />
                    <Controls />
                  </ReactFlow>
                </div>
              ) : (
                <div className="emptyState">Noch kein CKW-ZEV-Aggregat hochgeladen.</div>
              )}
            </div>
          )}
        </section>

        {workspace === "vzev" ? (
          <>
        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>Lastverschiebung Nacht zu Tag</h2>
              <p>
                Das Screening zeigt Verbrauchsmuster über ein Kalenderjahr und identifiziert Profile mit
                Strombezug zwischen 22:00 und 06:00 Uhr. Typische Kandidaten sind Boiler, Warmwasseraufbereitung
                und andere zeitlich steuerbare Verbraucher.
              </p>
            </div>
            <div className="pill">Screening-Obergrenze</div>
          </div>
          <div className="loadShiftingControls">
            <label>
              Visualisierte Verbrauchszeitreihe
              <select
                value={loadShifting?.selected_consumption_file_id ?? ""}
                disabled={loadShiftingBusy}
                onChange={(event) => void selectLoadShiftingProfile(event.target.value)}
              >
                <option value="">Alle Verbrauchszeitreihen</option>
                {loadShifting?.profiles.map((profile) => (
                  <option key={profile.file_id} value={profile.file_id}>
                    {profile.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Kalenderjahr
              <select
                value={loadShifting?.selected_year ?? ""}
                disabled={loadShiftingBusy}
                onChange={(event) =>
                  void selectLoadShiftingProfile(
                    loadShifting?.selected_consumption_file_id ?? "",
                    Number(event.target.value)
                  )
                }
              >
                {loadShifting?.years.map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="optimizationMetrics">
            <Metric label="Verbrauch Auswahl" value={fmtKwh(loadShifting?.totals.annual_demand_kwh)} />
            <Metric
              label="Nachtverbrauch 22:00-06:00"
              value={fmtKwh(loadShifting?.totals.nighttime_demand_kwh)}
              hint={`${fmtPct(loadShifting?.totals.nighttime_share)} des Verbrauchs`}
            />
            <Metric label="Tagesverbrauch 08:00-18:00" value={fmtKwh(loadShifting?.totals.daytime_demand_kwh)} />
            <Metric label="Profile Auswahl" value={fmtNumber(loadShifting?.totals.profile_count, 0)} />
          </div>
          <h3>Stündliches Verbrauchsmuster über das Jahr</h3>
          <LoadShiftingHeatmap heatmap={loadShifting?.heatmap} />
          <h3 className="subsectionTitle">Verbrauchsprofile nach Nachtverbrauch</h3>
          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Rang</th>
                  <th>Verbrauchszeitreihe</th>
                  <th>Gesamtverbrauch</th>
                  <th>Nachtverbrauch</th>
                  <th>Nachtanteil</th>
                  <th>Durchschnitt pro Nacht</th>
                  <th>Tagesverbrauch</th>
                  <th>Tage</th>
                </tr>
              </thead>
              <tbody>
                {loadShifting?.profiles.map((profile, index) => (
                  <tr key={profile.file_id}>
                    <td>{index + 1}</td>
                    <td>
                      {profile.label}
                      {profile.address ? <small className="tableHint">{profile.address}</small> : null}
                    </td>
                    <td>{fmtKwh(profile.annual_demand_kwh)}</td>
                    <td>{fmtKwh(profile.nighttime_demand_kwh)}</td>
                    <td>{fmtPct(profile.nighttime_share)}</td>
                    <td>{fmtKwh(profile.average_nightly_demand_kwh)}</td>
                    <td>{fmtKwh(profile.daytime_demand_kwh)}</td>
                    <td>{fmtNumber(profile.coverage_days, 0)}</td>
                  </tr>
                ))}
                {!loadShifting?.profiles.length ? (
                  <tr>
                    <td colSpan={8}>Noch keine Verbrauchszeitreihe für die Lastverschiebungsanalyse verfügbar.</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
          <ul className="assumptions">
            {loadShifting?.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </section>

        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>vZEV-Simulation</h2>
              <p>
                Erstellt aus den hochgeladenen klassischen Solarzeitreihen und Verbrauchsdaten ein synthetisches
                CKW-ZEV-Aggregat. Die finanzielle Bewertung verwendet die bestehende monatliche vZEV-Abrechnung.
              </p>
            </div>
            <div className="panelHeaderActions">
              <div className="pill">{vzevSimulationBusy ? "wird berechnet" : "automatisch berechnet"}</div>
              <button
                type="button"
                className="secondaryButton"
                onClick={exportVzevCsv}
                disabled={!selectedId || !vzevSimulation || exportingVzevCsv}
              >
                <Download size={16} />
                {exportingVzevCsv ? "CSV wird erstellt..." : "Synthetisches CSV herunterladen"}
              </button>
              <button
                type="button"
                className="secondaryButton"
                onClick={exportVzevTariffComparisonCsv}
                disabled={!selectedId || !vzevSimulation || exportingVzevTariffCsv}
              >
                <Download size={16} />
                {exportingVzevTariffCsv ? "CSV wird erstellt..." : "Tarifvergleich CSV"}
              </button>
            </div>
          </div>

          <div className="vzevTariffPanel">
            <div>
              <strong>PV-Tarif der Simulation</strong>
              <p>
                Die teilnehmerspezifischen 80%-Obergrenzen bleiben unten sichtbar. Alternativ kann ein fixer
                Tarif für alle Teilnehmer geprüft und angewendet werden.
              </p>
            </div>
            <label>
              Tarifmodell
              <select
                value={vzevPvTariffMode}
                disabled={vzevPvTariffBusy}
                onChange={(event) => setVzevPvTariffMode(event.target.value as "participant_80_pct" | "fixed")}
              >
                <option value="participant_80_pct">80% je Teilnehmer</option>
                <option value="fixed">Fixer PV-Tarif</option>
              </select>
            </label>
            <label>
              Fixer Tarif (Rp./kWh)
              <input
                type="number"
                min="0"
                step="0.01"
                inputMode="decimal"
                value={fixedVzevPvTariff}
                disabled={vzevPvTariffMode !== "fixed" || vzevPvTariffBusy}
                placeholder={fmtNumber(vzevSimulation?.pv_tariff.minimum_participant_cap_rp_per_kwh, 2)}
                onChange={(event) => setFixedVzevPvTariff(event.target.value)}
              />
              <span className="tariffLimitHint">
                Strengste 80%-Obergrenze: {fmtRp(vzevSimulation?.pv_tariff.minimum_participant_cap_rp_per_kwh ?? 0)}.
                Eine Überschreitung ist mit Warnung möglich.
              </span>
            </label>
            <button
              type="button"
              onClick={applyVzevPvTariff}
              disabled={!selectedId || !vzevSimulationInputsReady || vzevPvTariffBusy}
            >
              {vzevPvTariffBusy ? "Tarif wird geprüft..." : "PV-Tarif anwenden"}
            </button>
            <div className="pill">
              {vzevSimulation?.pv_tariff.mode === "fixed"
                ? `Aktiv: ${fmtRp(vzevSimulation.pv_tariff.fixed_rp_per_kwh ?? 0)}`
                : "Aktiv: 80% je Teilnehmer"}
            </div>
          </div>
          {vzevPvTariffWarning ? <div className="alert">{vzevPvTariffWarning}</div> : null}

          {vzevSimulation ? (
            <>
              {vzevSimulation.warnings.map((warning) => (
                <div className="alert" key={warning}>
                  {warning}
                </div>
              ))}
              <div className="optimizationMetrics">
                <Metric label="Ersparnis durch vZEV" value={fmtMoney(vzevSimulation.totals.savings_chf)} />
                <Metric label="Ersparnis Anteil" value={fmtPct(vzevSimulation.totals.savings_pct)} />
                <Metric label="Kosten ohne vZEV" value={fmtMoney(vzevSimulation.totals.without_vzev_total_chf)} />
                <Metric
                  label="Kosten mit vZEV total"
                  value={fmtMoney(vzevSimulation.totals.with_vzev_total_chf)}
                  hint="CKW-Kosten plus lokale PV-Energie"
                />
                <Metric label="Kosten lokale PV" value={fmtMoney(vzevSimulation.totals.local_pv_energy_cost_chf)} />
                <Metric label="Lokal genutzte PV" value={fmtKwh(vzevSimulation.totals.local_pv_consumption_kwh)} />
                <Metric label="Verbleibender Netzbezug" value={fmtKwh(vzevSimulation.totals.grid_import_kwh)} />
                <Metric label="Rückspeisung" value={fmtKwh(vzevSimulation.totals.pv_excess_kwh)} />
                <Metric label="Eigenverbrauch Anteil Bedarf" value={fmtPct(vzevSimulation.totals.self_consumption_pct)} />
              </div>
              <div className="tableScroll">
                <table>
                  <thead>
                    <tr>
                      <th>Teilnehmer</th>
                      <th>Verbrauch</th>
                      <th>Lokale PV</th>
                      <th>Standardprodukt</th>
                      <th>80%-Obergrenze</th>
                      <th>PV-Tarif</th>
                      <th>Prüfung</th>
                      <th>Ohne vZEV</th>
                      <th>CKW-Anteil</th>
                      <th>Kosten lokale PV</th>
                      <th>Mit vZEV total</th>
                      <th>Ersparnis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vzevSimulation.participants.map((participant) => (
                      <tr key={participant.participant}>
                        <td>{participant.participant}</td>
                        <td>{fmtKwh(participant.consumption_kwh)}</td>
                        <td>{fmtKwh(participant.allocated_local_pv_kwh)}</td>
                        <td>{fmtRp(participant.standard_product_reference_rp_per_kwh)}</td>
                        <td>{fmtRp(participant.local_energy_cap_rp_per_kwh)}</td>
                        <td className={participant.local_energy_tariff_exceeds_cap ? "costIncrease" : ""}>
                          {fmtRp(participant.local_energy_rp_per_kwh)}
                        </td>
                        <td>{participant.local_energy_tariff_exceeds_cap ? "Über 80%" : "Innerhalb 80%"}</td>
                        <td>{fmtMoney(participant.without_vzev_total_chf)}</td>
                        <td>{fmtMoney(participant.with_vzev_ckw_invoice_chf)}</td>
                        <td>{fmtMoney(participant.local_pv_energy_cost_chf)}</td>
                        <td>{fmtMoney(participant.with_vzev_total_chf)}</td>
                        <td>{fmtMoney(participant.savings_chf)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <h3 className="subsectionTitle">Tarif- und Kostenänderung pro Messpunkt</h3>
              <p className="sectionIntro">
                Verglichen werden die eigenständige CKW-Abrechnung jedes Messpunkts und sein Anteil an der
                gemeinsamen CKW-vZEV-Rechnung. Bei den Änderungswerten bedeutet <strong>Plus Mehrkosten</strong>
                {" und "}<strong>Minus Kostenreduktion</strong>. Die Gesamtkostenänderung enthält zusätzlich die
                Bezahlung der lokal genutzten PV-Energie.
              </p>
              <div className="optimizationMetrics">
                <Metric
                  label="Leistungspreisänderung"
                  value={fmtCostChange(vzevSimulation.totals.demand_cost_change_chf)}
                  hint="vZEV-Anteil minus eigenständige Leistungskosten"
                />
                <Metric
                  label="Übrige CKW-Kostenänderung"
                  value={fmtCostChange(vzevSimulation.totals.other_ckw_cost_change_chf)}
                  hint="Energie, Netz, Messung, Abgaben und MWST"
                />
                <Metric
                  label="CKW-Kostenänderung total"
                  value={fmtCostChange(vzevSimulation.totals.ckw_cost_change_chf)}
                  hint="ohne interne PV-Bezahlung"
                />
                <Metric
                  label="Gesamtkostenänderung"
                  value={fmtCostChange(vzevSimulation.totals.total_cost_change_chf)}
                  hint="inklusive lokaler PV-Energie"
                />
              </div>
              <VzevCostChangeChart participants={vzevSimulation.participants} />
              <div className="tableScroll">
                <table>
                  <thead>
                    <tr>
                      <th>Teilnehmer / Messpunkt</th>
                      <th>Jahresverbrauch einzeln</th>
                      <th>Tarif einzeln</th>
                      <th>vZEV-Jahresnetzbezug</th>
                      <th>Tarif vZEV</th>
                      <th>Leistungspreis einzeln</th>
                      <th>Leistungspreis vZEV</th>
                      <th>Tarifwechsel</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vzevSimulation.participants.map((participant) => (
                      <tr key={`tariff-${participant.participant}`}>
                        <td>
                          {participant.participant}
                          <small className="tableHint">{participant.metering_code || "Messpunkt nicht angegeben"}</small>
                        </td>
                        <td>{fmtKwh(participant.standalone_annualized_kwh)}</td>
                        <td>{tariffTierLabel(participant.standalone_tariff_tier)}</td>
                        <td>{fmtKwh(participant.community_annualized_grid_import_kwh)}</td>
                        <td>{tariffTierLabel(participant.community_tariff_tier)}</td>
                        <td>{fmtMoneyExact(participant.standalone_demand_rate_chf_per_kw_month)} / kW-Mt.</td>
                        <td>{fmtMoneyExact(participant.community_demand_rate_chf_per_kw_month)} / kW-Mt.</td>
                        <td>
                          {participant.tariff_changed ? <span className="tariffChangeBadge">Ja</span> : "Nein"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="tableScroll">
                <table>
                  <thead>
                    <tr>
                      <th>Teilnehmer / Messpunkt</th>
                      <th>Leistungskosten einzeln</th>
                      <th>Leistungskosten vZEV-Anteil</th>
                      <th>Änderung Leistung</th>
                      <th>Änderung übrige CKW-Kosten</th>
                      <th>Änderung CKW total</th>
                      <th>Lokale PV</th>
                      <th>Änderung total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {vzevSimulation.participants.map((participant) => (
                      <tr key={`cost-change-${participant.participant}`}>
                        <td>
                          {participant.participant}
                          <small className="tableHint">{participant.metering_code || "Messpunkt nicht angegeben"}</small>
                        </td>
                        <td>{fmtMoneyExact(participant.standalone_demand_cost_chf)}</td>
                        <td>{fmtMoneyExact(participant.allocated_community_demand_cost_chf)}</td>
                        <td className={costChangeClass(participant.demand_cost_change_chf)}>
                          {fmtCostChange(participant.demand_cost_change_chf)}
                        </td>
                        <td className={costChangeClass(participant.other_ckw_cost_change_chf)}>
                          {fmtCostChange(participant.other_ckw_cost_change_chf)}
                        </td>
                        <td className={costChangeClass(participant.ckw_cost_change_chf)}>
                          {fmtCostChange(participant.ckw_cost_change_chf)}
                        </td>
                        <td>{fmtMoneyExact(participant.local_pv_energy_cost_chf)}</td>
                        <td className={costChangeClass(participant.total_cost_change_chf)}>
                          {fmtCostChange(participant.total_cost_change_chf)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ul className="assumptions">
                {vzevSimulation.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
              </ul>
              <p className="sectionIntro">
                Preisregel: <a href="https://www.fedlex.admin.ch/eli/cc/2017/763/de#art_16" target="_blank" rel="noreferrer">EnV Art. 16</a>
                {" · "}<a href="https://pubdb.bfe.admin.ch/de/publication/download/9329" target="_blank" rel="noreferrer">BFE-Leitfaden</a>
              </p>
            </>
          ) : (
            <div className="emptyState">
              {vzevSimulationBusy
                ? "Die vZEV-Simulation wird automatisch berechnet."
                : vzevSimulationInputsReady
                  ? "Die automatische vZEV-Simulation konnte noch nicht dargestellt werden."
                  : "Für die automatische vZEV-Simulation Solarzeitreihen und Verbrauchsdaten hochladen."}
            </div>
          )}
        </section>

        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>Batterieplatzierung</h2>
              <p>
                Die feste Batterie mit {fmtKwh(batteryPlacements?.battery.capacity_kwh)} Kapazität und{" "}
                {fmtKw(batteryPlacements?.battery.max_charge_kw)} Lade- sowie Entladeleistung wird an jedem
                Datenstrom einzeln simuliert. Die beiden Einsatzfälle haben unterschiedliche Ziele und werden
                deshalb getrennt gerankt.
              </p>
            </div>
            <div className="panelHeaderActions">
              <div className="pill">100 kWh · 30 kW</div>
              <button type="button" onClick={loadBatteryPlacements} disabled={!selectedId || batteryPlacementsBusy}>
                {batteryPlacementsBusy ? "Berechnung läuft..." : "Batterieplatzierung berechnen"}
              </button>
            </div>
          </div>

          <div className="optimizationMetrics">
            <Metric
              label="Beste PV-Platzierung"
              value={batteryPlacements?.best_pv_site?.label ?? "kein Ergebnis"}
              hint={`+ ${fmtKwh(batteryPlacements?.best_pv_site?.additional_local_solar_kwh)} lokal nutzbar`}
            />
            <Metric
              label="Bestes Peak Shaving"
              value={batteryPlacements?.best_consumption_site?.label ?? "kein Ergebnis"}
              hint={`${fmtMoney(batteryPlacements?.best_consumption_site?.demand_charge_savings_chf)} Leistungstarif-Einsparung`}
            />
          </div>

          <h3>Fall 1: Batterie bei der PV-Produktion</h3>
          <p className="sectionIntro">
            Rangfolge nach zusätzlichem Solarstrom, der durch zeitliche Verschiebung lokal genutzt werden kann.
            Für klassische PV-Zeitreihen muss zuerst festgelegt werden, welche Verbraucher hinter derselben
            Eigenverbrauchslösung liegen.
          </p>
          {batteryPlacements?.pv_sites.length ? (
            <div className="batteryAssignments">
              {batteryPlacements.pv_sites.map((site) => (
                <div className="batteryAssignment" key={site.file_id}>
                  <strong>{site.label}</strong>
                  {site.address ? <small>{site.address}</small> : null}
                  {site.assignment_mode === "implicit_zev" ? (
                    <span>
                      Automatisch zugeordnet: {site.assigned_consumers.map((consumer) => consumer.label).join(", ")}
                    </span>
                  ) : (
                    <>
                      <span>Verbraucher für PV-Eigenverbrauch mit Batterie auswählen:</span>
                      <div className="batteryAssignmentOptions">
                        {batteryPlacements.consumer_options.map((consumer) => (
                          <label key={consumer.file_id}>
                            <input
                              type="checkbox"
                              checked={site.selected_consumer_file_ids.includes(consumer.file_id)}
                              disabled={Boolean(batteryAssignmentBusyId)}
                              onChange={(event) => {
                                const nextConsumerIds = event.target.checked
                                  ? [...site.selected_consumer_file_ids, consumer.file_id]
                                  : site.selected_consumer_file_ids.filter((fileId) => fileId !== consumer.file_id);
                                void updatePvBatteryConsumers(site.file_id, nextConsumerIds);
                              }}
                            />
                            {consumer.label}
                          </label>
                        ))}
                        {!batteryPlacements.consumer_options.length ? <small>Keine Verbrauchszeitreihe verfügbar.</small> : null}
                      </div>
                    </>
                  )}
                </div>
              ))}
            </div>
          ) : null}
          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Rang</th>
                  <th>PV-Datenstrom</th>
                  <th>Zugeordnete Verbraucher</th>
                  <th>PV-Energie</th>
                  <th>Zusätzlich lokal</th>
                  <th>Anteil PV-Energie</th>
                  <th>Restliche Rückspeisung</th>
                  <th>Speicherstand am Ende</th>
                </tr>
              </thead>
              <tbody>
                {batteryPlacements?.pv_sites.map((site, index) => (
                  <tr key={site.file_id}>
                    <td>{site.configured ? index + 1 : "–"}</td>
                    <td>
                      {site.label}
                      {site.address ? <small className="tableHint">{site.address}</small> : null}
                    </td>
                    <td>
                      {site.configured
                        ? site.assigned_consumers.map((consumer) => consumer.label).join(", ")
                        : "Zuordnung fehlt"}
                    </td>
                    <td>{fmtKwh(site.pv_kwh)}</td>
                    <td>{site.configured ? fmtKwh(site.additional_local_solar_kwh) : "–"}</td>
                    <td>{site.configured ? fmtPct(site.additional_local_solar_pct) : "–"}</td>
                    <td>{site.configured ? fmtKwh(site.remaining_export_kwh) : "–"}</td>
                    <td>{site.configured ? fmtKwh(site.ending_state_of_charge_kwh) : "–"}</td>
                  </tr>
                ))}
                {!batteryPlacements?.pv_sites.length ? (
                  <tr>
                    <td colSpan={8}>Noch keine PV-Zeitreihe für die Batteriesimulation verfügbar.</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <h3 className="subsectionTitle">Fall 2: Peak Shaving beim Verbraucher</h3>
          <p className="sectionIntro">
            Rangfolge nach reduzierter Leistungstarif-Komponente. Ausgewiesen ist die Summe der monatlichen
            Leistungsspitzen im analysierten Zeitraum.
          </p>
          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Rang</th>
                  <th>Verbrauchsdatenstrom</th>
                  <th>Tarif</th>
                  <th>Rate</th>
                  <th>Monatsspitzen vorher</th>
                  <th>Monatsspitzen mit Batterie</th>
                  <th>Reduktion</th>
                  <th>Einsparung</th>
                </tr>
              </thead>
              <tbody>
                {batteryPlacements?.consumption_sites.map((site, index) => (
                  <tr key={site.file_id}>
                    <td>{index + 1}</td>
                    <td>
                      {site.label}
                      {site.address ? <small className="tableHint">{site.address}</small> : null}
                    </td>
                    <td>{site.tariff_tier}</td>
                    <td>{fmtMoney(site.demand_rate_chf_per_kw_month)} / kW-Mt.</td>
                    <td>{fmtKw(site.baseline_peak_kw_month_sum)}</td>
                    <td>{fmtKw(site.battery_peak_kw_month_sum)}</td>
                    <td>{fmtKw(site.reduced_peak_kw_month_sum)}</td>
                    <td>{fmtMoney(site.demand_charge_savings_chf)}</td>
                  </tr>
                ))}
                {!batteryPlacements?.consumption_sites.length ? (
                  <tr>
                    <td colSpan={8}>Noch keine Verbrauchszeitreihe für die Peak-Shaving-Simulation verfügbar.</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
          <ul className="assumptions">
            {batteryPlacements?.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </section>

          </>
        ) : null}

        {workspace === "leg" ? (
          <>
        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>Verbleibender Solarüberschuss</h2>
              <p>
                Überschuss bleibt in einem 15-Minuten-Intervall dann bestehen, wenn der verfügbare Solarstrom
                grösser ist als der gleichzeitig durch die LEG nutzbare Bedarf. Die Auswertung zeigt Menge,
                saisonale Verteilung, Tageszeit und die höchsten beobachteten Einzelintervalle.
              </p>
            </div>
          </div>
          <div className="optimizationMetrics">
            <Metric
              label="Verbleibender Solarüberschuss"
              value={fmtKwh(residualSolar?.totals.residual_solar_kwh)}
              hint={`${fmtPct(residualSolar?.totals.residual_solar_pct)} des verfügbaren Solarstroms`}
            />
            <Metric
              label="Intervalle mit Überschuss"
              value={`${fmtNumber(residualSolar?.totals.residual_interval_count, 0)}`}
              hint={`${fmtPct(residualSolar?.totals.residual_interval_pct)} aller 15-Minuten-Intervalle`}
            />
            <Metric
              label="Höchster Überschuss in 15 Minuten"
              value={fmtKwh(residualSolar?.totals.peak_residual_solar_kwh)}
              hint={fmtTimestamp(residualSolar?.peak_interval?.timestamp)}
            />
          </div>
          {residualSolar?.sources.length ? (
            <div className="residualSolarControls">
              <strong>Visualisierte PV-Produktion</strong>
              <span>
                Alle PV-Zeitreihen oder eine beliebige Kombination auswählen. Der verbleibende Überschuss des
                Gesamtsystems wird pro Intervall proportional auf die ausgewählten PV-Zeitreihen verteilt.
              </span>
              <div className="residualSolarCheckboxes">
                <label>
                  <input
                    type="checkbox"
                    checked={!selectedResidualSolarSourceIds.length}
                    disabled={residualSolarBusy}
                    onChange={(event) => {
                      if (event.target.checked) void selectResidualSolarSources([]);
                    }}
                  />
                  Alle
                </label>
                {residualSolar.sources.map((source) => (
                  <label key={source.file_id}>
                    <input
                      type="checkbox"
                      checked={selectedResidualSolarSourceIds.includes(source.file_id)}
                      disabled={residualSolarBusy}
                      onChange={(event) => {
                        const nextSourceIds = event.target.checked
                          ? [...selectedResidualSolarSourceIds, source.file_id]
                          : selectedResidualSolarSourceIds.filter((fileId) => fileId !== source.file_id);
                        void selectResidualSolarSources(nextSourceIds);
                      }}
                    />
                    {source.label}
                    {source.address ? <small>{source.address}</small> : null}
                  </label>
                ))}
              </div>
            </div>
          ) : null}
          <section className="chartRow">
            <div>
              <h3>Nach Monat</h3>
              <Chart
                rows={residualSolar?.monthly ?? []}
                series={[
                  { key: "available_solar_kwh", label: "Verfügbarer Solarstrom" },
                  { key: "local_solar_kwh", label: "Lokal genutzt" },
                  { key: "residual_solar_kwh", label: "Verbleibender Überschuss" }
                ]}
              />
            </div>
            <div>
              <h3>Nach Datum und Tageszeit</h3>
              <ResidualSolarHeatmap heatmap={residualSolar?.heatmap} />
            </div>
          </section>
          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Höchste 15-Minuten-Intervalle</th>
                  <th>Verfügbarer Solarstrom</th>
                  <th>Gleichzeitiger Bedarf</th>
                  <th>Verbleibender Überschuss</th>
                </tr>
              </thead>
              <tbody>
                {residualSolar?.peak_intervals.map((interval) => (
                  <tr key={String(interval.timestamp)}>
                    <td>{fmtTimestamp(interval.timestamp)}</td>
                    <td>{fmtKwh(Number(interval.available_solar_kwh))}</td>
                    <td>{fmtKwh(Number(interval.consumption_kwh))}</td>
                    <td>{fmtKwh(Number(interval.residual_solar_kwh))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>LEG-Preismodell</h2>
              <p>Netzrabatt und vereinbarten lokalen Energiepreis getrennt festlegen.</p>
            </div>
            <div className="panelHeaderActions">
              <label>
                Netztarifrabatt
                <select value={legNetworkDiscount} onChange={(event) => setLegNetworkDiscount(event.target.value)}>
                  <option value="0.40">40% · eine Netzebene</option>
                  <option value="0.20">20% · mehrere Netzebenen</option>
                </select>
              </label>
              <label>
                Lokaler Energiepreis [Rp./kWh]
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="Wie externer Energiepreis"
                  value={legEnergyPrice}
                  onChange={(event) => setLegEnergyPrice(event.target.value)}
                />
              </label>
              <button type="button" onClick={refreshLegPricing} disabled={!selectedId || legPricingBusy}>
                {legPricingBusy ? "Berechnet..." : "Preise anwenden"}
              </button>
            </div>
          </div>
          <div className="explainGrid">
            <div>
              <strong>{fmtRp(calculation?.rates?.leg_energy_price_rp_per_kwh)}</strong>
              <span>
                Vereinbarter Preis für {fmtKwh(calculation?.totals.local_solar_consumed_kwh)} lokalen Solarstrom:
                Produzentenerlös {fmtMoney(calculation?.totals.producer_revenue_chf)}.
              </span>
            </div>
            <div>
              <strong>{fmtMoney(calculation?.totals.network_discount_chf)}</strong>
              <span>
                {fmtPct(calculation?.rates?.network_discount)} von {fmtRp(calculation?.rates?.discountable_netznutzung_rp_per_kwh)}
                Netznutzung = {fmtRp(calculation?.rates?.network_discount_rp_per_kwh)} Rabatt pro lokaler kWh.
              </span>
            </div>
            <div>
              <strong>{fmtMoney(calculation?.totals.energy_price_savings_chf)}</strong>
              <span>
                Energiepreisvorteil: externer Preis {fmtRp(calculation?.rates?.external_energy_rp_per_kwh)} minus lokaler Preis
                {" "}{fmtRp(calculation?.rates?.leg_energy_price_rp_per_kwh)}, multipliziert mit den lokalen kWh.
              </span>
            </div>
          </div>
          {calculation?.participants?.length ? (
            <div className="tableScroll">
              <table>
                <thead>
                  <tr>
                    <th>Teilnehmer</th>
                    <th>Verbrauch</th>
                    <th>Lokaler Solarstrom</th>
                    <th>Energie extern / lokal</th>
                    <th>Netztarif / Rabatt</th>
                    <th>Produzentenerlös</th>
                    <th>Energiepreisvorteil</th>
                    <th>Netzrabatt</th>
                    <th>Verbrauchervorteil</th>
                  </tr>
                </thead>
                <tbody>
                  {calculation.participants.map((participant) => (
                    <tr key={participant.file_id}>
                      <td>{participant.label}</td>
                      <td>{fmtKwh(participant.measured_consumption_kwh)}</td>
                      <td>{fmtKwh(participant.allocated_local_solar_kwh)}</td>
                      <td>{fmtRp(participant.external_energy_rp_per_kwh)} / {fmtRp(participant.leg_energy_price_rp_per_kwh)}</td>
                      <td>{fmtRp(participant.discountable_netznutzung_rp_per_kwh)} / −{fmtRp(participant.network_discount_rp_per_kwh)}</td>
                      <td>{fmtMoney(participant.producer_revenue_chf)}</td>
                      <td>{fmtMoney(participant.energy_price_savings_chf)}</td>
                      <td>{fmtMoney(participant.network_discount_chf)}</td>
                      <td>{fmtMoney(participant.consumer_total_savings_chf)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <ul className="assumptions">
            {calculation?.assumptions?.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </section>

        <section className="panel">
          <div className="panelHeader">
            <div>
              <h2>Optimale LEG-Zusammensetzung</h2>
              <p>
                {optimization?.optimization_mode === "all_participants_assumed_optimal"
                  ? `Für die grosse Gemeinschaft wurde eine LEG mit allen ${optimization.best?.participant_count ?? 0} Teilnehmern unter der Annahme bewertet, dass diese Zusammensetzung optimal ist.`
                  : `Bewertet wurden ${optimization?.combination_count ?? 0} Kombinationen.`}{" "}
                Die Darstellung zeigt Optimierung und gemessene CKW-ZEV-Flüsse.
              </p>
            </div>
            <div className="pill">
              {optimization?.best ? `Verbrauchsklasse ${optimization.best.combined_tariff_tier}` : "kein Ergebnis"}
            </div>
          </div>

          <div className="optimizationMetrics">
            <Metric label="Verbrauchervorteil" value={fmtMoney(optimization?.best?.cost_savings_chf)} />
            <Metric label="LEG-Netzkostenrabatt" value={fmtMoney(optimization?.best?.leg_network_discount_chf)} />
            <Metric label="Energiepreisvorteil" value={fmtMoney(optimization?.best?.energy_price_savings_chf)} />
            <Metric label="Produzentenerlös" value={fmtMoney(optimization?.best?.producer_revenue_chf)} />
            <Metric
              label="Lokaler Energiepreis"
              value={fmtRp(optimization?.best?.leg_energy_price_rp_per_kwh)}
              hint={`extern Ø ${fmtRp(optimization?.best?.combined_energy_rp_per_kwh)}`}
            />
            <Metric
              label="Rabatt je lokaler kWh"
              value={fmtRp(
                optimization?.best
                  ? optimization.best.combined_discountable_netznutzung_rp_per_kwh * Number(legNetworkDiscount)
                  : undefined
              )}
              hint={`${fmtPct(Number(legNetworkDiscount))} des Netznutzungstarifs`}
            />
            <Metric label="Teilnehmer" value={`${optimization?.best?.participant_count ?? 0}`} />
          </div>

          <p className="sectionIntro">
            Die LEG löst keinen Tarifwechsel auf eine gemeinsame Tarifklasse aus. Die Verbrauchsklasse beschreibt
            nur den aggregierten Jahresverbrauch der gewählten Zusammensetzung; der Netzkostenrabatt entsteht aus
            dem gewählten 40%- beziehungsweise 20%-Rabatt auf den Netznutzungseinheitstarif des zugeordneten
            lokalen Solarstroms. Der Solarstrompreis ist ein separater Geldfluss vom Verbraucher zum Produzenten.
            {" "}<a href="https://www.ckw.ch/energie/strom-teilen/zev-faq" target="_blank" rel="noreferrer">CKW-Regelung zu LEG und vZEV</a>
          </p>

          {visualizationFlow.nodes.length ? (
            <div className="flowBox">
              <ReactFlow
                nodes={visualizationFlow.nodes}
                edges={visualizationFlow.edges}
                fitView
              >
                <Background />
                <Controls />
              </ReactFlow>
            </div>
          ) : (
            <div className="emptyState">Noch keine Optimierung oder CKW-ZEV-Messung verfügbar.</div>
          )}

          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Gewählter Verbraucher</th>
                  <th>Einzeltarif</th>
                  <th>Jahresverbrauch</th>
                  <th>Zugeordneter Solarstrom</th>
                  <th>Energiepreise extern / lokal</th>
                  <th>Netzkostenrabatt</th>
                  <th>Energiepreisvorteil</th>
                  <th>Produzentenerlös</th>
                  <th>Verbrauchervorteil</th>
                </tr>
              </thead>
              <tbody>
                {optimization?.best?.members.map((member) => (
                  <tr key={member.file_id}>
                    <td>{member.label}</td>
                    <td>{member.tariff_tier}</td>
                    <td>{fmtKwh(member.annualized_kwh)}</td>
                    <td>{fmtKwh(member.allocated_local_solar_kwh)}</td>
                    <td>{fmtRp(member.energy_rp_per_kwh)} / {fmtRp(member.leg_energy_price_rp_per_kwh)}</td>
                    <td>{fmtMoney(member.leg_network_discount_chf)}</td>
                    <td>{fmtMoney(member.energy_price_savings_chf)}</td>
                    <td>{fmtMoney(member.producer_revenue_chf)}</td>
                    <td>{fmtMoney(member.cost_savings_chf)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="tableScroll">
            <table>
              <thead>
                <tr>
                  <th>Rang</th>
                  <th>Teilnehmer</th>
                  <th>Verbrauchsklasse</th>
                  <th>Solar lokal</th>
                  <th>Netzkostenrabatt</th>
                  <th>Energiepreisvorteil</th>
                  <th>Produzentenerlös</th>
                  <th>Verbrauchervorteil</th>
                </tr>
              </thead>
              <tbody>
                {optimization?.combinations.map((combo, index) => (
                  <tr key={`${combo.participant_labels.join("|")}-${index}`}>
                    <td>{index + 1}</td>
                    <td>{combo.participant_labels.join(", ")}</td>
                    <td>{combo.combined_tariff_tier}</td>
                    <td>{fmtKwh(combo.local_solar_kwh)}</td>
                    <td>{fmtMoney(combo.leg_network_discount_chf)}</td>
                    <td>{fmtMoney(combo.energy_price_savings_chf)}</td>
                    <td>{fmtMoney(combo.producer_revenue_chf)}</td>
                    <td>{fmtMoney(combo.cost_savings_chf)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="assumptions">
            {optimization?.assumptions?.map((assumption) => <li key={assumption}>{assumption}</li>)}
          </ul>
        </section>

          </>
        ) : null}

        <section className="tables">
          <div className="panel">
            <h2>Dateien</h2>
            <table>
              <thead>
                <tr>
                  <th>Datei</th>
                  <th>Typ</th>
                  <th>Energie</th>
                  <th aria-label="Aktionen" />
                </tr>
              </thead>
              <tbody>
                {summary?.files.map((file) => (
                  <tr key={file.file_id}>
                    <td>{file.original_filename}</td>
                    <td>{flowLabel(file.flow_kind)}</td>
                    <td>{fmtKwh(file.total_kwh)}</td>
                    <td className="fileActions">
                      <button
                        type="button"
                        className="deleteButton"
                        title={`${file.original_filename} löschen`}
                        aria-label={`${file.original_filename} löschen`}
                        disabled={Boolean(deletingFileId)}
                        onClick={() => deleteFile(file)}
                      >
                        <Trash2 size={16} />
                        {deletingFileId === file.file_id ? "Löschen..." : "Löschen"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <div className="panelHeader">
              <div>
                <h2>PV-Anlagen</h2>
                <p>
                  {plants?.selection_mode === "curated"
                    ? "Explizit ausgewählte Anlagen. Diese Werte bestimmen die Anschlussleistung."
                    : "Automatisch aus den Adressen der hochgeladenen PV-Messwerte erkannt."}
                </p>
              </div>
              <div className="pill">{plants?.selection_mode === "curated" ? "manuelle Auswahl" : "automatisch"}</div>
            </div>

            {plants?.selection_mode === "curated" ? (
              <div className="plantControls">
                <div className="plantControlHeader">
                  <strong>PV-Auswahl bearbeiten</strong>
                  <button
                    type="button"
                    className="secondaryButton"
                    disabled={plantBusy}
                    onClick={() => mutatePlants("/selection", { method: "DELETE" })}
                  >
                    Automatik verwenden
                  </button>
                </div>
                <div className="plantAddGrid">
                  <div>
                    <h3>Aus vollständiger Gemeindeliste</h3>
                    <div className="inlinePlantForm">
                      <select
                        value={selectedCachePlantId}
                        onChange={(event) => setSelectedCachePlantId(event.target.value)}
                      >
                        <option value="">PV-Anlage wählen</option>
                        {selectablePlants.map((plant) => (
                          <option key={plant.cache_id ?? `${plant.address}-${plant.installed_kwp}`} value={plant.cache_id ?? ""}>
                            {plant.address || plant.municipality} · {fmtNumber(plant.installed_kwp, 1)} kWp
                          </option>
                        ))}
                      </select>
                      <button type="button" disabled={!selectedCachePlantId || plantBusy} onClick={addCachedPlant}>
                        <Plus size={16} />
                        Hinzufügen
                      </button>
                    </div>
                  </div>
                  <form onSubmit={addManualPlant}>
                    <h3>Komplett manuell</h3>
                    <div className="inlinePlantForm">
                      <input
                        value={manualPlantAddress}
                        placeholder="Adresse"
                        onChange={(event) => setManualPlantAddress(event.target.value)}
                      />
                      <input
                        value={manualPlantKwp}
                        type="number"
                        min="0.1"
                        step="0.1"
                        placeholder="kWp"
                        onChange={(event) => setManualPlantKwp(event.target.value)}
                      />
                      <button type="submit" disabled={plantBusy}>
                        <Plus size={16} />
                        Hinzufügen
                      </button>
                    </div>
                  </form>
                </div>
              </div>
            ) : (
              <div className="automaticPlantAction">
                <span>Fehlende oder falsche Treffer können in einer expliziten Auswahl korrigiert werden.</span>
                <button
                  type="button"
                  className="secondaryButton"
                  disabled={plantBusy}
                  onClick={() => mutatePlants("/adopt-automatic", { method: "POST" })}
                >
                  Auswahl bearbeiten
                </button>
              </div>
            )}

            <table>
              <thead>
                <tr>
                  <th>Adresse</th>
                  <th>kWp</th>
                  <th>A</th>
                  <th>Treffer</th>
                  {plants?.selection_mode === "curated" ? <th aria-label="Aktionen" /> : null}
                </tr>
              </thead>
              <tbody>
                {plants?.matches.map((plant, index) => (
                  <tr key={plant.project_plant_id ?? `${plant.address}-${index}`}>
                    <td>{plant.address || plant.municipality}</td>
                    <td>{fmtNumber(plant.installed_kwp, 1)}</td>
                    <td>{fmtNumber(plant.max_connection_ampere, 0)}</td>
                    <td>{plant.confidence}</td>
                    {plants.selection_mode === "curated" ? (
                      <td className="fileActions">
                        <button
                          type="button"
                          className="deleteButton iconButton"
                          title={`${plant.address} entfernen`}
                          aria-label={`${plant.address} entfernen`}
                          disabled={plantBusy || !plant.project_plant_id}
                          onClick={() => mutatePlants(`/selection/${plant.project_plant_id}`, { method: "DELETE" })}
                        >
                          <Trash2 size={16} />
                        </button>
                      </td>
                    ) : null}
                  </tr>
                ))}
                {!plants?.matches.length ? (
                  <tr>
                    <td colSpan={plants?.selection_mode === "curated" ? 5 : 4}>Keine PV-Anlage ausgewählt.</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>
      </section>
    </main>
  );
}
