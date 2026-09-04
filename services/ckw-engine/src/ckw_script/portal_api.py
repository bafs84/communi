from __future__ import annotations

from pathlib import Path
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from .portal_analysis import (
    add_cached_project_plant,
    add_manual_project_plant,
    adopt_automatic_project_plants,
    analyze_battery_placements,
    analyze_load_shifting,
    analyze_residual_solar,
    calculate_leg_potential,
    clear_project_plant_selection,
    create_project,
    delete_project,
    delete_portal_file,
    delete_selected_project_plant,
    get_project,
    ingest_portal_file,
    list_projects,
    match_project_plants,
    optimize_leg_combinations,
    project_timeseries,
    set_pv_battery_consumers,
    simulate_project_vzev,
    summarize_project,
    vzev_tariff_comparison_filename,
    vzev_simulation_filename,
)
from .portal_excel_export import EXCEL_MEDIA_TYPE, create_project_excel_export
from .portal_municipalities import list_municipalities, validate_ckw_municipality
from .portal_vzev_word_report import create_vzev_word_report
from .portal_word_report import create_project_word_report
from .vzev_simulation import DEFAULT_VZEV_SIMULATION_TARIFF_YEAR
from .vzev_invoice_bundle import InvoiceSource, create_invoice_batch


class PortalSettings(BaseSettings):
    portal_db_path: str = "data/portal.duckdb"
    pv_plant_cache_path: str = "data/electricity_production_plants.csv"
    frontend_dist_path: str = "frontend/dist"
    word_report_template_path: str | None = None


class ProjectCreate(BaseModel):
    name: str
    municipality: str


class CachedProjectPlantCreate(BaseModel):
    cache_id: str


class ManualProjectPlantCreate(BaseModel):
    address: str
    installed_kwp: float


class PvBatteryConsumersUpdate(BaseModel):
    consumer_file_ids: list[str]


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Projekt wurde nicht gefunden")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def create_app(settings: PortalSettings | None = None) -> FastAPI:
    settings = settings or PortalSettings()
    app = FastAPI(title="CKW LEG Potential Portal", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/vzev/invoices")
    async def vzev_invoices(
        csv_file: Annotated[list[UploadFile], File()],
        issuer: Annotated[str, Form()] = "vZEV",
        invoice_date: Annotated[str | None, Form()] = None,
        payment_terms: Annotated[str, Form()] = "",
        invoice_prefix: Annotated[str, Form()] = "VZEV",
        year: Annotated[int | None, Form()] = None,
        tariff_tier: Annotated[str, Form()] = "auto",
        local_energy_factor: Annotated[float, Form()] = 0.8,
        pv_tariff_rp_per_kwh: Annotated[float | None, Form()] = None,
        pv_tariff_method: Annotated[str, Form()] = "80%-pauschale",
        pv_tariff_note: Annotated[str | None, Form()] = None,
        peak_allocation_mode: Annotated[str, Form()] = "community_peak_share",
        blindenergie_kvarh: Annotated[float | None, Form()] = None,
        direct_meter_count: Annotated[int | None, Form()] = None,
        indirect_meter_count: Annotated[int | None, Form()] = None,
        virtual_meter_count: Annotated[int | None, Form()] = None,
        feed_in_energy_rp_per_kwh: Annotated[float | None, Form()] = None,
        feed_in_hkn_rp_per_kwh: Annotated[float | None, Form()] = None,
        vat_rate: Annotated[float, Form()] = 0.081,
        no_vat: Annotated[bool, Form()] = False,
        exact_billing_basis: Annotated[bool, Form()] = False,
    ) -> Response:
        try:
            sources = [
                InvoiceSource(
                    content=await uploaded_file.read(),
                    original_filename=uploaded_file.filename or "vzev.csv",
                )
                for uploaded_file in csv_file
            ]
            bundle = create_invoice_batch(
                csv_files=sources,
                issuer=issuer,
                invoice_date=invoice_date,
                payment_terms=payment_terms,
                invoice_prefix=invoice_prefix,
                year=year,
                tariff_tier=tariff_tier,
                local_energy_factor=Decimal(str(local_energy_factor)),
                pv_tariff_rp_per_kwh=(
                    Decimal(str(pv_tariff_rp_per_kwh)) if pv_tariff_rp_per_kwh is not None else None
                ),
                pv_tariff_method=pv_tariff_method,
                pv_tariff_note=pv_tariff_note,
                peak_allocation_mode=peak_allocation_mode,
                blindenergie_kvarh=(Decimal(str(blindenergie_kvarh)) if blindenergie_kvarh is not None else None),
                direct_meter_count=direct_meter_count,
                indirect_meter_count=indirect_meter_count,
                virtual_meter_count=virtual_meter_count,
                feed_in_energy_rp_per_kwh=(
                    Decimal(str(feed_in_energy_rp_per_kwh))
                    if feed_in_energy_rp_per_kwh is not None
                    else None
                ),
                feed_in_hkn_rp_per_kwh=(
                    Decimal(str(feed_in_hkn_rp_per_kwh)) if feed_in_hkn_rp_per_kwh is not None else None
                ),
                vat_rate=None if no_vat else Decimal(str(vat_rate)),
                ckw_invoice_rounding=not exact_billing_basis,
            )
            return Response(
                content=bundle.content,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{bundle.filename}"',
                    "X-Invoice-Count": str(bundle.invoice_count),
                    "X-Period-Count": str(bundle.period_count),
                },
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/municipalities")
    def municipalities() -> list[dict[str, object]]:
        return list_municipalities()

    @app.get("/api/projects")
    def projects() -> list[dict[str, object]]:
        return list_projects(db_path=settings.portal_db_path)

    @app.post("/api/projects")
    def new_project(payload: ProjectCreate) -> dict[str, object]:
        try:
            municipality = validate_ckw_municipality(payload.municipality)
            return create_project(
                name=payload.name,
                municipality=municipality.name,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict[str, object]:
        try:
            return get_project(project_id=project_id, db_path=settings.portal_db_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/projects/{project_id}")
    def remove_project(project_id: str) -> dict[str, object]:
        try:
            return delete_project(project_id=project_id, db_path=settings.portal_db_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    async def _upload_files(project_id: str, files: list[UploadFile], upload_kind: str) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for upload in files:
            content = await upload.read()
            result = ingest_portal_file(
                project_id=project_id,
                original_filename=upload.filename or "upload.csv",
                content=content,
                upload_kind=upload_kind,
                db_path=settings.portal_db_path,
            )
            results.append(
                {
                    "file_id": result.file_id,
                    "original_filename": result.original_filename,
                    "upload_kind": result.upload_kind,
                    "flow_kind": result.flow_kind,
                    "rows_inserted": result.rows_inserted,
                    "total_kwh": result.total_kwh,
                    "warnings": result.warnings,
                    "duplicate": result.duplicate,
                }
            )
        return results

    @app.post("/api/projects/{project_id}/solar-files")
    async def upload_solar_files(
        project_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> list[dict[str, object]]:
        try:
            return await _upload_files(project_id, files, "solar")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/projects/{project_id}/consumption-files")
    async def upload_consumption_files(
        project_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> list[dict[str, object]]:
        try:
            return await _upload_files(project_id, files, "consumption")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/projects/{project_id}/zev-files")
    async def upload_zev_files(
        project_id: str,
        files: Annotated[list[UploadFile], File()],
    ) -> list[dict[str, object]]:
        try:
            return await _upload_files(project_id, files, "zev")
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/projects/{project_id}/files/{file_id}")
    def delete_file(project_id: str, file_id: str) -> dict[str, object]:
        try:
            return delete_portal_file(
                project_id=project_id,
                file_id=file_id,
                db_path=settings.portal_db_path,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Datei wurde nicht gefunden") from exc
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/summary")
    def summary(project_id: str) -> dict[str, object]:
        try:
            return summarize_project(project_id=project_id, db_path=settings.portal_db_path)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/timeseries")
    def timeseries(project_id: str, kind: str = "leg", resolution: str = "day") -> list[dict[str, object]]:
        try:
            return project_timeseries(
                project_id=project_id,
                kind=kind,
                resolution=resolution,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/plants")
    def plants(project_id: str) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return match_project_plants(
                project_id=project_id,
                municipality=str(project_data["municipality"]),
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/projects/{project_id}/plants/adopt-automatic")
    def adopt_automatic_plants(project_id: str) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return adopt_automatic_project_plants(
                project_id=project_id,
                municipality=str(project_data["municipality"]),
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/projects/{project_id}/plants/cache")
    def add_cached_plant(project_id: str, payload: CachedProjectPlantCreate) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return add_cached_project_plant(
                project_id=project_id,
                municipality=str(project_data["municipality"]),
                cache_id=payload.cache_id,
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/projects/{project_id}/plants/manual")
    def add_manual_plant(project_id: str, payload: ManualProjectPlantCreate) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return add_manual_project_plant(
                project_id=project_id,
                municipality=str(project_data["municipality"]),
                address=payload.address,
                installed_kwp=payload.installed_kwp,
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/projects/{project_id}/plants/selection")
    def clear_plant_selection(project_id: str) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return clear_project_plant_selection(
                project_id=project_id,
                municipality=str(project_data["municipality"]),
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.delete("/api/projects/{project_id}/plants/selection/{project_plant_id}")
    def delete_selected_plant(project_id: str, project_plant_id: str) -> dict[str, object]:
        try:
            project_data = get_project(project_id=project_id, db_path=settings.portal_db_path)
            return delete_selected_project_plant(
                project_id=project_id,
                project_plant_id=project_plant_id,
                municipality=str(project_data["municipality"]),
                db_path=settings.portal_db_path,
                cache_path=settings.pv_plant_cache_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/calculation")
    def calculation(
        project_id: str,
        tariff_year: int | None = None,
        network_discount: float = 0.40,
        leg_energy_price_rp_per_kwh: float | None = None,
    ) -> dict[str, object]:
        try:
            return calculate_leg_potential(
                project_id=project_id,
                tariff_year=tariff_year,
                network_discount=Decimal(str(network_discount)),
                leg_energy_price_rp_per_kwh=(
                    Decimal(str(leg_energy_price_rp_per_kwh))
                    if leg_energy_price_rp_per_kwh is not None
                    else None
                ),
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/residual-solar")
    def residual_solar(project_id: str, solar_file_id: list[str] | None = Query(default=None)) -> dict[str, object]:
        try:
            return analyze_residual_solar(
                project_id=project_id,
                solar_file_ids=solar_file_id,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/optimization")
    def optimization(
        project_id: str,
        tariff_year: int | None = None,
        network_discount: float = 0.40,
        leg_energy_price_rp_per_kwh: float | None = None,
    ) -> dict[str, object]:
        try:
            return optimize_leg_combinations(
                project_id=project_id,
                tariff_year=tariff_year,
                network_discount=Decimal(str(network_discount)),
                leg_energy_price_rp_per_kwh=(
                    Decimal(str(leg_energy_price_rp_per_kwh))
                    if leg_energy_price_rp_per_kwh is not None
                    else None
                ),
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/battery-placements")
    def battery_placements(project_id: str, tariff_year: int | None = None) -> dict[str, object]:
        try:
            return analyze_battery_placements(
                project_id=project_id,
                tariff_year=tariff_year,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/load-shifting")
    def load_shifting(
        project_id: str,
        consumption_file_id: str | None = None,
        year: int | None = None,
    ) -> dict[str, object]:
        try:
            return analyze_load_shifting(
                project_id=project_id,
                consumption_file_id=consumption_file_id,
                year=year,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.put("/api/projects/{project_id}/battery-placements/pv-sites/{solar_file_id}/consumers")
    def update_pv_battery_consumers(
        project_id: str,
        solar_file_id: str,
        payload: PvBatteryConsumersUpdate,
        tariff_year: int | None = None,
    ) -> dict[str, object]:
        try:
            return set_pv_battery_consumers(
                project_id=project_id,
                solar_file_id=solar_file_id,
                consumer_file_ids=payload.consumer_file_ids,
                tariff_year=tariff_year,
                db_path=settings.portal_db_path,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/vzev-simulation")
    def vzev_simulation(
        project_id: str,
        tariff_year: int = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
        fixed_pv_tariff_rp_per_kwh: float | None = None,
    ) -> dict[str, object]:
        try:
            return simulate_project_vzev(
                project_id=project_id,
                tariff_year=tariff_year,
                fixed_local_energy_rp_per_kwh=(
                    Decimal(str(fixed_pv_tariff_rp_per_kwh))
                    if fixed_pv_tariff_rp_per_kwh is not None
                    else None
                ),
                db_path=settings.portal_db_path,
            ).summary
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/vzev-simulation.csv")
    def vzev_simulation_csv(
        project_id: str,
        tariff_year: int = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
    ) -> Response:
        try:
            simulation = simulate_project_vzev(
                project_id=project_id,
                tariff_year=tariff_year,
                db_path=settings.portal_db_path,
            )
            filename = vzev_simulation_filename(project_id=project_id, db_path=settings.portal_db_path)
            return Response(
                content=simulation.csv_bytes(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/vzev-tariff-comparison.csv")
    def vzev_tariff_comparison_csv(
        project_id: str,
        tariff_year: int = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
        fixed_pv_tariff_rp_per_kwh: float | None = None,
    ) -> Response:
        try:
            simulation = simulate_project_vzev(
                project_id=project_id,
                tariff_year=tariff_year,
                fixed_local_energy_rp_per_kwh=(
                    Decimal(str(fixed_pv_tariff_rp_per_kwh))
                    if fixed_pv_tariff_rp_per_kwh is not None
                    else None
                ),
                db_path=settings.portal_db_path,
            )
            filename = vzev_tariff_comparison_filename(project_id=project_id, db_path=settings.portal_db_path)
            return Response(
                content=simulation.tariff_comparison_csv_bytes(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/word-report")
    def word_report(
        project_id: str,
        network_discount: float = 0.40,
        leg_energy_price_rp_per_kwh: float | None = None,
    ) -> Response:
        try:
            report = create_project_word_report(
                project_id=project_id,
                db_path=settings.portal_db_path,
                plant_cache_path=settings.pv_plant_cache_path,
                template_path=settings.word_report_template_path,
                network_discount=Decimal(str(network_discount)),
                leg_energy_price_rp_per_kwh=(
                    Decimal(str(leg_energy_price_rp_per_kwh))
                    if leg_energy_price_rp_per_kwh is not None
                    else None
                ),
            )
            return Response(
                content=report.content,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/vzev-word-report")
    def vzev_word_report(
        project_id: str,
        tariff_year: int = DEFAULT_VZEV_SIMULATION_TARIFF_YEAR,
        consumption_file_id: str | None = None,
        load_shifting_year: int | None = None,
        fixed_pv_tariff_rp_per_kwh: float | None = None,
    ) -> Response:
        try:
            report = create_vzev_word_report(
                project_id=project_id,
                db_path=settings.portal_db_path,
                plant_cache_path=settings.pv_plant_cache_path,
                tariff_year=tariff_year,
                consumption_file_id=consumption_file_id,
                load_shifting_year=load_shifting_year,
                fixed_local_energy_rp_per_kwh=(
                    Decimal(str(fixed_pv_tariff_rp_per_kwh))
                    if fixed_pv_tariff_rp_per_kwh is not None
                    else None
                ),
            )
            return Response(
                content=report.content,
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/projects/{project_id}/excel-export")
    def excel_export(
        project_id: str,
        tariff_year: int | None = None,
        network_discount: float = 0.40,
        leg_energy_price_rp_per_kwh: float | None = None,
    ) -> Response:
        try:
            export = create_project_excel_export(
                project_id=project_id,
                tariff_year=tariff_year,
                network_discount=Decimal(str(network_discount)),
                leg_energy_price_rp_per_kwh=(
                    Decimal(str(leg_energy_price_rp_per_kwh))
                    if leg_energy_price_rp_per_kwh is not None
                    else None
                ),
                db_path=settings.portal_db_path,
            )
            return Response(
                content=export.content,
                media_type=EXCEL_MEDIA_TYPE,
                headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    frontend_dist = Path(settings.frontend_dist_path)
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


app = create_app()
