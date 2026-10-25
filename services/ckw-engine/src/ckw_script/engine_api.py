from __future__ import annotations

import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Request, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import sentry_sdk
from .observability import initialize_observability, report_exception

initialize_observability()

from .portal_analysis import (
    add_cached_project_plant,
    add_manual_project_plant,
    analyze_battery_placements,
    analyze_load_shifting,
    analyze_residual_solar,
    calculate_leg_potential,
    clear_project_plant_selection,
    create_project,
    ingest_portal_file,
    match_project_plants,
    optimize_leg_combinations,
    project_timeseries,
    set_pv_battery_consumers,
    simulate_project_vzev,
    summarize_project,
    vzev_simulation_filename,
    vzev_tariff_comparison_filename,
)
from .portal_excel_export import EXCEL_MEDIA_TYPE, create_project_excel_export
from .portal_municipalities import list_municipalities
from .portal_vzev_word_report import create_vzev_word_report
from .portal_word_report import create_project_word_report
from .vzev_invoice_bundle import InvoiceSource, create_invoice_batch
from .vzev_simulation import DEFAULT_VZEV_SIMULATION_TARIFF_YEAR


class EngineSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CKW_")

    internal_token: str = "development-internal-token"
    internal_token_file: str | None = None
    tmp_dir: str = "/tmp/communi-ckw"
    pv_plant_cache_path: str = "/tmp/communi-ckw/public-data/electricity-production-plants.csv"
    word_report_template_path: str | None = None

    def resolved_token(self) -> str:
        if self.internal_token_file:
            return Path(self.internal_token_file).read_text(encoding="utf-8").strip()
        return self.internal_token


class ManifestProject(BaseModel):
    id: str
    name: str
    municipality: str


class ManifestFile(BaseModel):
    id: str
    original_filename: str
    upload_kind: str


class ExecuteManifest(BaseModel):
    operation: str
    project: ManifestProject | None = None
    files: list[ManifestFile] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)


@dataclass
class MaterializedProject:
    project_id: str
    external_project_id: str
    db_path: Path
    external_to_internal_file_id: dict[str, str]
    external_to_internal_plant_id: dict[str, str]


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _decimal(value: Any, default: str) -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _require_token(actual: str | None, expected: str) -> None:
    if not actual or actual != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized internal request")


def _binary_response(content: bytes, *, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "X-Artifact-Filename": filename,
            "X-Artifact-Media-Type": media_type,
        },
    )


def _apply_project_configuration(
    *,
    manifest: ExecuteManifest,
    project: MaterializedProject,
    cache_path: str,
) -> None:
    selection = manifest.config.get("plantSelection") or {}
    mode = selection.get("mode", "automatic")
    plants = selection.get("plants") or []
    if mode == "curated":
        clear_project_plant_selection(
            project_id=project.project_id,
            municipality=manifest.project.municipality if manifest.project else "",
            db_path=project.db_path,
            cache_path=cache_path,
        )
        for plant in plants:
            result: dict[str, Any] | None = None
            if plant.get("source") == "cache" and plant.get("cacheId"):
                result = add_cached_project_plant(
                    project_id=project.project_id,
                    municipality=manifest.project.municipality if manifest.project else "",
                    cache_id=str(plant["cacheId"]),
                    db_path=project.db_path,
                    cache_path=cache_path,
                )
            elif plant.get("address") and plant.get("installedKwp") is not None:
                result = add_manual_project_plant(
                    project_id=project.project_id,
                    municipality=manifest.project.municipality if manifest.project else "",
                    address=str(plant["address"]),
                    installed_kwp=float(plant["installedKwp"]),
                    db_path=project.db_path,
                    cache_path=cache_path,
                )
            if result and plant.get("id"):
                matches = result.get("matches") or []
                if plant.get("cacheId"):
                    added = next(
                        (item for item in matches if item.get("cache_id") == plant.get("cacheId")),
                        None,
                    )
                else:
                    added = next(
                        (
                            item
                            for item in matches
                            if item.get("source") == "manual"
                            and item.get("address") == plant.get("address")
                            and float(item.get("installed_kwp") or 0) == float(plant.get("installedKwp") or 0)
                        ),
                        None,
                    )
                if added and added.get("project_plant_id"):
                    project.external_to_internal_plant_id[str(plant["id"])] = str(added["project_plant_id"])

    battery_consumers = manifest.config.get("batteryConsumers") or {}
    for external_solar_id, external_consumer_ids in battery_consumers.items():
        solar_id = project.external_to_internal_file_id.get(str(external_solar_id))
        if not solar_id:
            continue
        consumer_ids = [
            project.external_to_internal_file_id[item]
            for item in map(str, external_consumer_ids or [])
            if item in project.external_to_internal_file_id
        ]
        set_pv_battery_consumers(
            project_id=project.project_id,
            solar_file_id=solar_id,
            consumer_file_ids=consumer_ids,
            db_path=project.db_path,
        )


def _materialize_project(
    *,
    manifest: ExecuteManifest,
    uploads: list[tuple[ManifestFile, bytes]],
    directory: Path,
    settings: EngineSettings,
) -> MaterializedProject:
    if manifest.project is None:
        raise ValueError("project is required for this operation")
    db_path = directory / "project.duckdb"
    created = create_project(
        name=manifest.project.name,
        municipality=manifest.project.municipality,
        db_path=db_path,
    )
    mapping: dict[str, str] = {}
    for metadata, content in uploads:
        result = ingest_portal_file(
            project_id=str(created["project_id"]),
            original_filename=metadata.original_filename,
            content=content,
            upload_kind=metadata.upload_kind,
            db_path=db_path,
        )
        mapping[metadata.id] = result.file_id
    project = MaterializedProject(
        project_id=str(created["project_id"]),
        external_project_id=manifest.project.id,
        db_path=db_path,
        external_to_internal_file_id=mapping,
        external_to_internal_plant_id={},
    )
    _apply_project_configuration(
        manifest=manifest,
        project=project,
        cache_path=settings.pv_plant_cache_path,
    )
    return project


def _mapped_parameter_file_id(
    project: MaterializedProject,
    parameters: dict[str, Any],
    key: str,
) -> str | None:
    value = parameters.get(key)
    if not value:
        return None
    return project.external_to_internal_file_id.get(str(value))


def _externalize_ids(value: Any, project: MaterializedProject) -> Any:
    reverse_file_ids = {internal: external for external, internal in project.external_to_internal_file_id.items()}
    reverse_plant_ids = {internal: external for external, internal in project.external_to_internal_plant_id.items()}

    def replace(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, tuple):
            return [replace(child) for child in item]
        if item == project.project_id:
            return project.external_project_id
        if isinstance(item, str) and item in reverse_file_ids:
            return reverse_file_ids[item]
        if isinstance(item, str) and item in reverse_plant_ids:
            return reverse_plant_ids[item]
        return item

    result = replace(jsonable_encoder(value))
    return result


def _json_result(value: Any, *, project: MaterializedProject | None = None) -> JSONResponse:
    content = _externalize_ids(value, project) if project else jsonable_encoder(value)
    return JSONResponse(content=content)


def _run_project_operation(
    *,
    manifest: ExecuteManifest,
    project: MaterializedProject,
    settings: EngineSettings,
) -> Response:
    op = manifest.operation
    p = manifest.parameters
    project_id = project.project_id
    db_path = project.db_path
    network_discount = _decimal(p.get("networkDiscount"), "0.40")
    leg_energy_price = _optional_decimal(p.get("legEnergyPriceRpPerKwh"))
    tariff_year = _optional_int(p.get("tariffYear"))

    if op == "summary":
        return _json_result(summarize_project(project_id=project_id, db_path=db_path), project=project)
    if op == "timeseries":
        return _json_result(
            project_timeseries(
                project_id=project_id,
                kind=str(p.get("kind", "leg")),
                resolution=str(p.get("resolution", "day")),
                db_path=db_path,
            ),
            project=project,
        )
    if op == "plants":
        return _json_result(
            match_project_plants(
                project_id=project_id,
                municipality=manifest.project.municipality if manifest.project else "",
                db_path=db_path,
                cache_path=settings.pv_plant_cache_path,
            ),
            project=project,
        )
    if op == "calculation":
        return _json_result(
            calculate_leg_potential(
                project_id=project_id,
                tariff_year=tariff_year,
                network_discount=network_discount,
                leg_energy_price_rp_per_kwh=leg_energy_price,
                db_path=db_path,
            ),
            project=project,
        )
    if op == "residual-solar":
        requested = p.get("solarFileIds") or []
        mapped = [
            project.external_to_internal_file_id[item]
            for item in map(str, requested)
            if item in project.external_to_internal_file_id
        ]
        return _json_result(
            analyze_residual_solar(
                project_id=project_id,
                solar_file_ids=mapped or None,
                db_path=db_path,
            ),
            project=project,
        )
    if op == "optimization":
        return _json_result(
            optimize_leg_combinations(
                project_id=project_id,
                tariff_year=tariff_year,
                network_discount=network_discount,
                leg_energy_price_rp_per_kwh=leg_energy_price,
                db_path=db_path,
            ),
            project=project,
        )
    if op == "battery-placements":
        return _json_result(
            analyze_battery_placements(
                project_id=project_id,
                tariff_year=tariff_year,
                db_path=db_path,
            ),
            project=project,
        )
    if op == "load-shifting":
        return _json_result(
            analyze_load_shifting(
                project_id=project_id,
                consumption_file_id=_mapped_parameter_file_id(project, p, "consumptionFileId"),
                year=_optional_int(p.get("year")),
                db_path=db_path,
            ),
            project=project,
        )
    if op == "vzev-simulation":
        result = simulate_project_vzev(
            project_id=project_id,
            tariff_year=tariff_year or DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
            fixed_local_energy_rp_per_kwh=_optional_decimal(p.get("fixedPvTariffRpPerKwh")),
            db_path=db_path,
        )
        return _json_result(result.summary, project=project)
    if op == "vzev-simulation-csv":
        result = simulate_project_vzev(
            project_id=project_id,
            tariff_year=tariff_year or DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
            fixed_local_energy_rp_per_kwh=_optional_decimal(p.get("fixedPvTariffRpPerKwh")),
            db_path=db_path,
        )
        return _binary_response(
            result.csv_bytes(),
            filename=vzev_simulation_filename(project_id=project_id, db_path=db_path),
            media_type="text/csv; charset=utf-8",
        )
    if op == "vzev-tariff-comparison-csv":
        result = simulate_project_vzev(
            project_id=project_id,
            tariff_year=tariff_year or DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
            fixed_local_energy_rp_per_kwh=_optional_decimal(p.get("fixedPvTariffRpPerKwh")),
            db_path=db_path,
        )
        return _binary_response(
            result.tariff_comparison_csv_bytes(),
            filename=vzev_tariff_comparison_filename(project_id=project_id, db_path=db_path),
            media_type="text/csv; charset=utf-8",
        )
    if op == "leg-word-report":
        report = create_project_word_report(
            project_id=project_id,
            db_path=db_path,
            plant_cache_path=settings.pv_plant_cache_path,
            template_path=settings.word_report_template_path,
            network_discount=network_discount,
            leg_energy_price_rp_per_kwh=leg_energy_price,
        )
        return _binary_response(report.content, filename=report.filename, media_type=DOCX_MEDIA_TYPE)
    if op == "vzev-word-report":
        report = create_vzev_word_report(
            project_id=project_id,
            db_path=db_path,
            plant_cache_path=settings.pv_plant_cache_path,
            tariff_year=tariff_year or DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
            consumption_file_id=_mapped_parameter_file_id(project, p, "consumptionFileId"),
            load_shifting_year=_optional_int(p.get("loadShiftingYear")),
            fixed_local_energy_rp_per_kwh=_optional_decimal(p.get("fixedPvTariffRpPerKwh")),
        )
        return _binary_response(report.content, filename=report.filename, media_type=DOCX_MEDIA_TYPE)
    if op == "excel-export":
        export = create_project_excel_export(
            project_id=project_id,
            tariff_year=tariff_year,
            network_discount=network_discount,
            leg_energy_price_rp_per_kwh=leg_energy_price,
            db_path=db_path,
        )
        return _binary_response(export.content, filename=export.filename, media_type=EXCEL_MEDIA_TYPE)
    raise ValueError(f"unsupported operation: {op}")


def _run_billing_operation(
    *,
    manifest: ExecuteManifest,
    uploads: list[tuple[ManifestFile, bytes]],
) -> Response:
    p = manifest.parameters
    sources = [
        InvoiceSource(content=content, original_filename=metadata.original_filename)
        for metadata, content in uploads
    ]
    bundle = create_invoice_batch(
        csv_files=sources,
        issuer=str(p.get("issuer", "vZEV")),
        invoice_date=p.get("invoiceDate") or None,
        payment_terms=str(p.get("paymentTerms", "")),
        invoice_prefix=str(p.get("invoicePrefix", "VZEV")),
        year=_optional_int(p.get("year")),
        tariff_tier=p.get("tariffTier"),
        local_energy_factor=_decimal(p.get("localEnergyFactor"), "0.8"),
        pv_tariff_rp_per_kwh=_optional_decimal(p.get("pvTariffRpPerKwh")),
        pv_tariff_method=str(p.get("pvTariffMethod", "80%-pauschale")),
        pv_tariff_note=p.get("pvTariffNote") or None,
        peak_allocation_mode=str(p.get("peakAllocationMode", "community_peak_share")),
        blindenergie_kvarh=_optional_decimal(p.get("blindenergieKvarh")),
        direct_meter_count=_optional_int(p.get("directMeterCount")),
        indirect_meter_count=_optional_int(p.get("indirectMeterCount")),
        virtual_meter_count=_optional_int(p.get("virtualMeterCount")),
        feed_in_energy_rp_per_kwh=_optional_decimal(p.get("feedInEnergyRpPerKwh")),
        feed_in_hkn_rp_per_kwh=_optional_decimal(p.get("feedInHknRpPerKwh")),
        vat_rate=None if bool(p.get("noVat", False)) else _decimal(p.get("vatRate"), "0.081"),
        ckw_invoice_rounding=not bool(p.get("exactBillingBasis", False)),
    )
    response = _binary_response(
        bundle.content,
        filename=bundle.filename,
        media_type="application/zip",
    )
    response.headers["X-Invoice-Count"] = str(bundle.invoice_count)
    response.headers["X-Period-Count"] = str(bundle.period_count)
    return response


def create_engine_app(settings: EngineSettings | None = None) -> FastAPI:
    settings = settings or EngineSettings()
    expected_token = settings.resolved_token()
    Path(settings.tmp_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.pv_plant_cache_path).parent.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Communi CKW calculation engine", version="1.0.0")

    @app.middleware("http")
    async def track_errors(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            report_exception(error, request)
            raise
        if response.status_code >= 400 and not getattr(request.state, "error_reported", False):
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("endpoint", request.url.path)
                scope.set_tag("method", request.method)
                scope.set_tag("status", response.status_code)
                sentry_sdk.capture_message("Engine HTTP failure", level="error" if response.status_code >= 500 else "warning")
        return response

    @app.get("/internal/health")
    def health(authorization: Annotated[str | None, Header()] = None) -> dict[str, str]:
        _require_token(authorization, expected_token)
        return {"status": "ok"}

    @app.get("/internal/v1/municipalities")
    def municipalities(authorization: Annotated[str | None, Header()] = None) -> list[dict[str, object]]:
        _require_token(authorization, expected_token)
        return list_municipalities()

    @app.post("/internal/v1/execute")
    async def execute(
        request: Request,
        manifest: Annotated[str, Form()],
        files: Annotated[list[UploadFile] | None, File()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        _require_token(authorization, expected_token)
        try:
            parsed = ExecuteManifest.model_validate_json(manifest)
            received_files = files or []
            if len(received_files) != len(parsed.files):
                raise ValueError("file manifest does not match uploaded files")
            uploads = [
                (metadata, await upload.read())
                for metadata, upload in zip(parsed.files, received_files, strict=True)
            ]
            if parsed.operation == "billing-invoices":
                return _run_billing_operation(manifest=parsed, uploads=uploads)
            with tempfile.TemporaryDirectory(dir=settings.tmp_dir, prefix="request-") as temp_dir:
                project = _materialize_project(
                    manifest=parsed,
                    uploads=uploads,
                    directory=Path(temp_dir),
                    settings=settings,
                )
                return _run_project_operation(manifest=parsed, project=project, settings=settings)
        except HTTPException:
            raise
        except (KeyError, ValueError) as exc:
            report_exception(exc, request)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            report_exception(exc, request)
            raise HTTPException(status_code=500, detail="calculation failed") from exc

    return app


app = create_engine_app()
