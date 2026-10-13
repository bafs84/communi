use std::{
    env,
    net::SocketAddr,
    sync::Arc,
    time::{Duration, Instant},
};

use axum::{
    extract::{Multipart, Query, State},
    http::{
        header::{AUTHORIZATION, CONTENT_TYPE},
        HeaderMap, Method, StatusCode,
    },
    response::{IntoResponse, Response},
    routing::{get, post, put},
    Json, Router,
};
use base64::Engine;
use chrono::{DateTime, Datelike, NaiveDateTime, TimeZone, Timelike, Utc};
use chrono_tz::Europe::Zurich;
use csv::ReaderBuilder;
use jsonwebtoken::{
    decode, decode_header,
    jwk::JwkSet,
    Algorithm, DecodingKey, Validation,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous}, SqlitePool, Row};
use std::str::FromStr;
use storage::LocalStorage;
use thiserror::Error;
use tower_http::{cors::CorsLayer, limit::RequestBodyLimitLayer, trace::TraceLayer};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};
use uuid::Uuid;

mod ckw;
mod storage;

#[derive(Clone)]
struct AppState {
    config: Arc<Config>,
    db: SqlitePool,
    storage: LocalStorage,
    auth: AuthVerifier,
}

#[derive(Clone)]
pub(crate) struct AuthVerifier {
    config: Arc<Config>,
    http: reqwest::Client,
    jwks: Arc<tokio::sync::RwLock<Option<CachedJwks>>>,
}

#[derive(Clone)]
struct CachedJwks {
    set: JwkSet,
    fetched_at: Instant,
}

#[derive(Clone)]
struct Config {
    database_url: String,
    host: String,
    port: u16,
    cors_origins: Vec<String>,
    max_csv_bytes: usize,
    auth0_audience: String,
    auth0_issuer_base_url: String,
    data_dir: std::path::PathBuf,
}

#[derive(Debug, Error)]
enum ApiError {
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("unauthorized")]
    Unauthorized,
    #[error("payload too large")]
    PayloadTooLarge,
    #[error("not found")]
    NotFound,
    #[error("storage error")]
    Storage,
    #[error("database error")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = match self {
            ApiError::BadRequest(_) => StatusCode::BAD_REQUEST,
            ApiError::Unauthorized => StatusCode::UNAUTHORIZED,
            ApiError::PayloadTooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            ApiError::NotFound => StatusCode::NOT_FOUND,
            ApiError::Storage | ApiError::Database(_) => {
                StatusCode::INTERNAL_SERVER_ERROR
            }
        };

        let body = ErrorBody {
            error: self.to_string(),
        };
        (status, Json(body)).into_response()
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error: String,
}

#[derive(Debug, Clone)]
pub(crate) struct AuthUser {
    sub: String,
    email: Option<String>,
}

#[derive(Debug, Deserialize)]
struct JwtClaims {
    sub: String,
    email: Option<String>,
    exp: usize,
    iss: String,
    aud: Audience,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Audience {
    One(String),
    Many(Vec<String>),
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    ok: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct UploadResult {
    upload_id: String,
    row_count: i32,
    total_kwh: f64,
    first_timestamp: Option<String>,
    last_timestamp: Option<String>,
    detected_timestamp_column: Option<String>,
    detected_consumption_column: Option<String>,
    daylight_consumption_percent: f64,
    solar_proxy_percent: f64,
    series_kind: String,
    series_label: String,
    ingestion_status: String,
    ingestion_message: Option<String>,
    is_authenticated: bool,
    expires_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct WorkspaceResponse {
    auth0_sub: String,
    email: Option<String>,
    profile: Option<Profile>,
    uploads: Vec<UploadSummary>,
    questions: Vec<Question>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Profile {
    municipality_key: Option<String>,
    municipality_label: Option<String>,
    municipality_name: Option<String>,
    canton: Option<String>,
    operator_name: Option<String>,
    operator_website: Option<String>,
    operator_address: Option<String>,
    operator_postal_code: Option<String>,
    has_battery: bool,
    has_ev_charging: bool,
    questions: Option<String>,
    share_with_researchers: bool,
    interested_in_community: bool,
    visible_interest: bool,
    contact_email: Option<String>,
    created_at: Option<DateTime<Utc>>,
    updated_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct UploadSummary {
    id: String,
    file_name: String,
    mime_type: Option<String>,
    row_count: i32,
    total_kwh: f64,
    daylight_consumption_percent: f64,
    solar_proxy_percent: f64,
    detected_timestamp_column: Option<String>,
    detected_consumption_column: Option<String>,
    first_timestamp: Option<String>,
    last_timestamp: Option<String>,
    series_kind: String,
    series_label: String,
    ingestion_status: String,
    ingestion_message: Option<String>,
    expires_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Question {
    id: String,
    municipality_key: Option<String>,
    body: String,
    created_at: DateTime<Utc>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct QuestionInput {
    body: String,
    municipality_key: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct MunicipalitySignalsQuery {
    municipality_key: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct MunicipalitySignals {
    municipality_key: String,
    registered_count: i64,
    visible_contacts: Vec<VisibleContact>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct VisibleContact {
    email: String,
    operator_name: Option<String>,
    updated_at: DateTime<Utc>,
}

#[derive(Debug)]
struct ParsedCsv {
    row_count: i32,
    total_kwh: f64,
    daylight_kwh: f64,
    solar_weighted_kwh: f64,
    detected_timestamp_column: Option<String>,
    detected_consumption_column: Option<String>,
    first_timestamp: Option<DateTime<Utc>>,
    last_timestamp: Option<DateTime<Utc>>,
    series_kind: SeriesKind,
}

impl ParsedCsv {
    fn pending_ingestion() -> Self {
        Self {
            row_count: 0,
            total_kwh: 0.0,
            daylight_kwh: 0.0,
            solar_weighted_kwh: 0.0,
            detected_timestamp_column: None,
            detected_consumption_column: None,
            first_timestamp: None,
            last_timestamp: None,
            series_kind: SeriesKind::Consumption,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SeriesKind {
    Consumption,
    PvExportSursee,
}

impl SeriesKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Consumption => "consumption",
            Self::PvExportSursee => "pv_export_sursee",
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Consumption => "Verbrauchsprofil",
            Self::PvExportSursee => "PV-Rücklieferungsprofil",
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    dotenvy::dotenv().ok();
    tracing_subscriber::registry()
        .with(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
            "communi_backend_api=info,tower_http=info".into()
        }))
        .with(tracing_subscriber::fmt::layer())
        .init();

    let config = Arc::new(Config::from_env()?);
    std::fs::create_dir_all(&config.data_dir)?;
    let options = SqliteConnectOptions::from_str(&config.database_url)?
        .create_if_missing(true)
        .foreign_keys(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Full)
        .busy_timeout(Duration::from_secs(10));
    let db = SqlitePoolOptions::new().max_connections(5).connect_with(options).await?;
    sqlx::migrate!("./migrations").run(&db).await?;
    let storage = LocalStorage::new(config.data_dir.join("objects"))?;

    let auth = AuthVerifier {
        config: Arc::clone(&config),
        http: reqwest::Client::new(),
        jwks: Arc::new(tokio::sync::RwLock::new(None)),
    };
    let state = AppState {
        config: Arc::clone(&config),
        db: db.clone(),
        storage: storage.clone(),
        auth: auth.clone(),
    };
    let ckw_state = ckw::CkwState::from_env(
        db,
        storage,
        auth,
        config.data_dir.clone(),
    ).await?;

    cleanup_expired_uploads(&state.db).await?;
    if env::args().any(|arg| arg == "maintenance" || arg == "cleanup-expired") {
        ckw_state.cleanup_guests().await;
        let deleted = ckw_state.process_deletion_outbox().await?;
        tracing::info!(deleted, "CKW maintenance completed");
        return Ok(());
    }

    let maintenance_interval_seconds = env::var("MAINTENANCE_INTERVAL_SECONDS")
        .unwrap_or_else(|_| "300".to_string())
        .parse::<u64>()?
        .max(1);
    let maintenance_state = ckw_state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(maintenance_interval_seconds));
        loop {
            interval.tick().await;
            maintenance_state.cleanup_guests().await;
            if let Err(error) = maintenance_state.process_deletion_outbox().await {
                tracing::warn!(%error, "CKW storage deletion retry failed");
            }
        }
    });

    let cors = build_cors(&config);
    let body_limit = (config.max_csv_bytes + 512 * 1024).max(ckw_state.request_body_limit());
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/calculators/sunshine-share", post(public_calculator_upload))
        .route("/v1/me/workspace", get(get_workspace))
        .route("/v1/me/profile", put(upsert_profile))
        .route("/v1/me/uploads", post(authenticated_upload).delete(clear_uploads))
        .route("/v1/me/questions", post(add_question).delete(clear_questions))
        .route("/v1/municipality-signals", get(municipality_signals))
        .with_state(state)
        .merge(ckw::router(ckw_state))
        .layer(TraceLayer::new_for_http())
        .layer(RequestBodyLimitLayer::new(body_limit))
        .layer(cors);

    let addr: SocketAddr = format!("{}:{}", config.host, config.port).parse()?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!("Communi backend API running on http://{addr}");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

impl Config {
    fn from_env() -> Result<Self, String> {
        Ok(Self {
            database_url: env::var("DATABASE_URL").unwrap_or_else(|_| format!("sqlite://{}/communi.sqlite", env::var("DATA_DIR").unwrap_or_else(|_| "/data".into()))),
            host: env::var("HOST").unwrap_or_else(|_| "0.0.0.0".to_string()),
            port: env::var("PORT")
                .unwrap_or_else(|_| "4100".to_string())
                .parse()
                .map_err(|_| "PORT must be a number".to_string())?,
            cors_origins: env::var("CORS_ORIGINS")
                .unwrap_or_default()
                .split(',')
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .collect(),
            max_csv_bytes: env::var("MAX_CSV_BYTES")
                .unwrap_or_else(|_| "1048576".to_string())
                .parse()
                .map_err(|_| "MAX_CSV_BYTES must be a number".to_string())?,
            auth0_audience: required_env("AUTH0_AUDIENCE")?,
            auth0_issuer_base_url: required_env("AUTH0_ISSUER_BASE_URL")?,
            data_dir: env::var("DATA_DIR").unwrap_or_else(|_| "/data".into()).into(),
        })
    }
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("missing required environment variable {name}"))
}

fn build_cors(config: &Config) -> CorsLayer {
    let mut cors = CorsLayer::new()
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::PATCH,
            Method::DELETE,
        ])
        .allow_headers([
            AUTHORIZATION,
            CONTENT_TYPE,
            "x-csrf-token".parse().expect("valid CKW CSRF header"),
        ]);

    if config.cors_origins.is_empty() {
        cors = cors.allow_origin(tower_http::cors::Any);
    } else {
        let origins = config
            .cors_origins
            .iter()
            .filter_map(|origin| origin.parse().ok())
            .collect::<Vec<_>>();
        cors = cors.allow_origin(origins);
    }

    cors
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

async fn healthz() -> Json<HealthResponse> {
    Json(HealthResponse { ok: true })
}

async fn public_calculator_upload(
    State(state): State<AppState>,
    headers: HeaderMap,
    multipart: Multipart,
) -> Result<Json<UploadResult>, ApiError> {
    let user = state.auth.optional_user(&headers).await?;
    let result = handle_upload(state, user, multipart, true).await?;
    Ok(Json(result))
}

async fn authenticated_upload(
    State(state): State<AppState>,
    headers: HeaderMap,
    multipart: Multipart,
) -> Result<Json<UploadResult>, ApiError> {
    let user = state.auth.require_user(&headers).await?;
    let result = handle_upload(state, Some(user), multipart, false).await?;
    Ok(Json(result))
}

async fn handle_upload(
    state: AppState,
    user: Option<AuthUser>,
    mut multipart: Multipart,
    allow_anonymous: bool,
) -> Result<UploadResult, ApiError> {
    if user.is_none() && !allow_anonymous {
        return Err(ApiError::Unauthorized);
    }

    let mut file_name = "upload.csv".to_string();
    let mut mime_type = Some("text/csv".to_string());
    let mut file_bytes: Option<Vec<u8>> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| ApiError::BadRequest("invalid multipart payload".to_string()))?
    {
        let name = field.name().unwrap_or("").to_string();
        if name != "file" && name != "files" {
            continue;
        }
        if let Some(value) = field.file_name() {
            file_name = value.to_string();
        }
        if let Some(value) = field.content_type() {
            mime_type = Some(value.to_string());
        }
        let bytes = field
            .bytes()
            .await
            .map_err(|_| ApiError::BadRequest("failed to read uploaded file".to_string()))?;
        if bytes.len() > state.config.max_csv_bytes {
            return Err(ApiError::PayloadTooLarge);
        }
        file_bytes = Some(bytes.to_vec());
        break;
    }

    let bytes = file_bytes.ok_or_else(|| ApiError::BadRequest("missing file field".to_string()))?;
    if bytes.is_empty() {
        return Err(ApiError::BadRequest("uploaded CSV is empty".to_string()));
    }
    if !file_name.to_ascii_lowercase().ends_with(".csv") {
        return Err(ApiError::BadRequest("only CSV files are supported".to_string()));
    }

    let (parsed, ingestion_status, ingestion_message) =
        analyse_csv_for_ingestion(&bytes, &file_name);
    let byte_size = i32::try_from(bytes.len()).unwrap_or(i32::MAX);
    let upload_id = Uuid::new_v4();
    let authenticated = user.is_some();
    if !authenticated {
        return Ok(UploadResult {
            upload_id: upload_id.to_string(),
            row_count: parsed.row_count,
            total_kwh: round2(parsed.total_kwh),
            first_timestamp: parsed.first_timestamp.map(|value| value.to_rfc3339()),
            last_timestamp: parsed.last_timestamp.map(|value| value.to_rfc3339()),
            detected_timestamp_column: parsed.detected_timestamp_column,
            detected_consumption_column: parsed.detected_consumption_column,
            daylight_consumption_percent: round2(percent(parsed.daylight_kwh, parsed.total_kwh)),
            solar_proxy_percent: round2(percent(parsed.solar_weighted_kwh, parsed.total_kwh)),
            series_kind: parsed.series_kind.as_str().to_string(),
            series_label: parsed.series_kind.label().to_string(),
            ingestion_status,
            ingestion_message,
            is_authenticated: false,
            expires_at: None,
        });
    }
    let expires_at: Option<DateTime<Utc>> = None;
    let digest = hex_digest(&bytes);
    let object_key = object_key(upload_id, user.as_ref());

    let user_id = match user.as_ref() {
        Some(auth) => Some(upsert_user(&state.db, auth).await?),
        None => None,
    };
    state.storage.put(&object_key, &bytes).await.map_err(|_| ApiError::Storage)?;

    let inserted_id = sqlx::query_scalar::<_, Uuid>(
        r#"
        insert into workspace_uploads (
          id,
          user_id,
          file_name,
          mime_type,
          byte_size,
          sha256,
          object_bucket,
          object_key,
          row_count,
          total_kwh,
          daylight_consumption_percent,
          solar_proxy_percent,
          detected_timestamp_column,
          detected_consumption_column,
          first_timestamp,
          last_timestamp,
          expires_at
        )
        values (
          $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
          $11, $12, $13, $14, $15, $16, $17
        )
        returning id
        "#,
    )
    .bind(upload_id)
    .bind(user_id)
    .bind(file_name)
    .bind(mime_type)
    .bind(byte_size)
    .bind(digest)
    .bind("local")
    .bind(&object_key)
    .bind(parsed.row_count)
    .bind(parsed.total_kwh)
    .bind(percent(parsed.daylight_kwh, parsed.total_kwh))
    .bind(percent(parsed.solar_weighted_kwh, parsed.total_kwh))
    .bind(parsed.detected_timestamp_column.clone())
    .bind(parsed.detected_consumption_column.clone())
    .bind(parsed.first_timestamp.map(|value| value.to_rfc3339()))
    .bind(parsed.last_timestamp.map(|value| value.to_rfc3339()))
    .bind(expires_at)
    .fetch_one(&state.db)
    .await;
    let inserted_id = match inserted_id {
        Ok(id) => id,
        Err(error) => {
            let _ = state.storage.delete(&object_key).await;
            return Err(error.into());
        }
    };

    Ok(UploadResult {
        upload_id: inserted_id.to_string(),
        row_count: parsed.row_count,
        total_kwh: round2(parsed.total_kwh),
        first_timestamp: parsed.first_timestamp.map(|value| value.to_rfc3339()),
        last_timestamp: parsed.last_timestamp.map(|value| value.to_rfc3339()),
        detected_timestamp_column: parsed.detected_timestamp_column,
        detected_consumption_column: parsed.detected_consumption_column,
        daylight_consumption_percent: round2(percent(parsed.daylight_kwh, parsed.total_kwh)),
        solar_proxy_percent: round2(percent(parsed.solar_weighted_kwh, parsed.total_kwh)),
        series_kind: parsed.series_kind.as_str().to_string(),
        series_label: parsed.series_kind.label().to_string(),
        ingestion_status,
        ingestion_message,
        is_authenticated: authenticated,
        expires_at,
    })
}

async fn get_workspace(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<WorkspaceResponse>, ApiError> {
    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;

    let profile = fetch_profile(&state.db, &user_id).await?;
    let uploads = fetch_uploads(&state.db, &user_id).await?;
    let questions = fetch_questions(&state.db, &user_id).await?;

    Ok(Json(WorkspaceResponse {
        auth0_sub: auth.sub,
        email: auth.email,
        profile,
        uploads,
        questions,
    }))
}

async fn upsert_profile(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(mut profile): Json<Profile>,
) -> Result<Json<Profile>, ApiError> {
    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;

    if !profile.interested_in_community || !profile.visible_interest {
        profile.visible_interest = false;
        profile.contact_email = None;
    } else if profile.contact_email.as_deref().unwrap_or("").trim().is_empty() {
        profile.contact_email = auth.email.clone();
    }

    sqlx::query(
        r#"
        insert into workspace_profiles (
          user_id, municipality_key, municipality_label, municipality_name, canton,
          operator_name, operator_website, operator_address, operator_postal_code,
          has_battery, has_ev_charging, questions, share_with_researchers,
          interested_in_community, visible_interest, contact_email
        )
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        on conflict (user_id) do update set
          municipality_key = excluded.municipality_key,
          municipality_label = excluded.municipality_label,
          municipality_name = excluded.municipality_name,
          canton = excluded.canton,
          operator_name = excluded.operator_name,
          operator_website = excluded.operator_website,
          operator_address = excluded.operator_address,
          operator_postal_code = excluded.operator_postal_code,
          has_battery = excluded.has_battery,
          has_ev_charging = excluded.has_ev_charging,
          questions = excluded.questions,
          share_with_researchers = excluded.share_with_researchers,
          interested_in_community = excluded.interested_in_community,
          visible_interest = excluded.visible_interest,
          contact_email = excluded.contact_email,
          updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
        "#,
    )
    .bind(&user_id)
    .bind(trim_opt(profile.municipality_key.as_deref()))
    .bind(trim_opt(profile.municipality_label.as_deref()))
    .bind(trim_opt(profile.municipality_name.as_deref()))
    .bind(trim_opt(profile.canton.as_deref()))
    .bind(trim_opt(profile.operator_name.as_deref()))
    .bind(trim_opt(profile.operator_website.as_deref()))
    .bind(trim_opt(profile.operator_address.as_deref()))
    .bind(trim_opt(profile.operator_postal_code.as_deref()))
    .bind(profile.has_battery)
    .bind(profile.has_ev_charging)
    .bind(trim_opt(profile.questions.as_deref()))
    .bind(profile.share_with_researchers)
    .bind(profile.interested_in_community)
    .bind(profile.visible_interest)
    .bind(trim_opt(profile.contact_email.as_deref()))
    .execute(&state.db)
    .await?;

    fetch_profile(&state.db, &user_id)
        .await?
        .map(Json)
        .ok_or(ApiError::NotFound)
}

async fn clear_uploads(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, ApiError> {
    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;
    let mut transaction = state.db.begin().await?;
    sqlx::query("insert into storage_deletion_outbox (object_bucket, object_key) select object_bucket, object_key from workspace_uploads where user_id=$1 on conflict do nothing")
        .bind(user_id).execute(&mut *transaction).await?;
    sqlx::query("delete from workspace_uploads where user_id=$1")
        .bind(user_id).execute(&mut *transaction).await?;
    transaction.commit().await?;

    Ok(Json(serde_json::json!({ "ok": true })))
}

async fn add_question(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(input): Json<QuestionInput>,
) -> Result<Json<Question>, ApiError> {
    let body = input.body.trim();
    if body.is_empty() {
        return Err(ApiError::BadRequest("question body is empty".to_string()));
    }
    if body.len() > 4000 {
        return Err(ApiError::BadRequest("question body is too long".to_string()));
    }

    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;
    let row = sqlx::query(
        r#"
        insert into workspace_questions (user_id, municipality_key, body, id)
        values ($1, $2, $3, $4)
        returning id, municipality_key, body, created_at
        "#,
    )
    .bind(&user_id)
    .bind(trim_opt(input.municipality_key.as_deref()))
    .bind(body)
    .bind(Uuid::new_v4())
    .fetch_one(&state.db)
    .await?;

    Ok(Json(question_from_row(row)?))
}

async fn clear_questions(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, ApiError> {
    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;
    sqlx::query("delete from workspace_questions where user_id = $1")
        .bind(&user_id)
        .execute(&state.db)
        .await?;
    Ok(Json(serde_json::json!({ "ok": true })))
}

async fn municipality_signals(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<MunicipalitySignalsQuery>,
) -> Result<Json<MunicipalitySignals>, ApiError> {
    let auth = state.auth.require_user(&headers).await?;
    let user_id = upsert_user(&state.db, &auth).await?;
    let municipality_key = query.municipality_key.unwrap_or_default();
    if municipality_key.trim().is_empty() {
        return Ok(Json(MunicipalitySignals {
            municipality_key: String::new(),
            registered_count: 0,
            visible_contacts: Vec::new(),
        }));
    }

    let registered_count = sqlx::query_scalar::<_, i64>(
        "select count(*) from workspace_profiles where municipality_key = $1",
    )
    .bind(&municipality_key)
    .fetch_one(&state.db)
    .await?;

    let rows = sqlx::query(
        r#"
        select contact_email, operator_name, updated_at
        from workspace_profiles
        where municipality_key = $1
          and interested_in_community = true
          and visible_interest = true
          and contact_email is not null
          and user_id <> $2
        order by updated_at desc
        "#,
    )
    .bind(&municipality_key)
    .bind(&user_id)
    .fetch_all(&state.db)
    .await?;

    let visible_contacts = rows
        .into_iter()
        .filter_map(|row| {
            let email: Option<String> = row.try_get("contact_email").ok()?;
            Some(VisibleContact {
                email: email?,
                operator_name: row.try_get("operator_name").ok(),
                updated_at: row.try_get("updated_at").ok()?,
            })
        })
        .collect();

    Ok(Json(MunicipalitySignals {
        municipality_key,
        registered_count,
        visible_contacts,
    }))
}

impl AuthVerifier {
    pub(crate) async fn optional_user(&self, headers: &HeaderMap) -> Result<Option<AuthUser>, ApiError> {
        let Some(value) = headers.get(AUTHORIZATION) else {
            return Ok(None);
        };
        let token = value
            .to_str()
            .map_err(|_| ApiError::Unauthorized)?
            .strip_prefix("Bearer ")
            .ok_or(ApiError::Unauthorized)?;
        self.verify_token(token).await.map(Some)
    }

    async fn require_user(&self, headers: &HeaderMap) -> Result<AuthUser, ApiError> {
        self.optional_user(headers).await?.ok_or(ApiError::Unauthorized)
    }

    async fn verify_token(&self, token: &str) -> Result<AuthUser, ApiError> {
        let header = decode_header(token).map_err(|_| ApiError::Unauthorized)?;
        let kid = header.kid.ok_or(ApiError::Unauthorized)?;
        let jwks = self.jwks().await?;
        let jwk = jwks
            .keys
            .iter()
            .find(|key| key.common.key_id.as_deref() == Some(kid.as_str()))
            .ok_or(ApiError::Unauthorized)?;
        let decoding_key = DecodingKey::from_jwk(jwk).map_err(|_| ApiError::Unauthorized)?;
        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_audience(&[self.config.auth0_audience.clone()]);
        validation.set_issuer(&[self.config.auth0_issuer_base_url.clone()]);
        let data = decode::<JwtClaims>(token, &decoding_key, &validation)
            .map_err(|_| ApiError::Unauthorized)?;

        let audience_ok = match &data.claims.aud {
            Audience::One(value) => value == &self.config.auth0_audience,
            Audience::Many(values) => values.iter().any(|value| value == &self.config.auth0_audience),
        };
        if !audience_ok || data.claims.iss != self.config.auth0_issuer_base_url || data.claims.exp == 0 {
            return Err(ApiError::Unauthorized);
        }

        Ok(AuthUser {
            sub: data.claims.sub,
            email: data.claims.email,
        })
    }

    async fn jwks(&self) -> Result<JwkSet, ApiError> {
        {
            let guard = self.jwks.read().await;
            if let Some(cached) = guard.as_ref() {
                if cached.fetched_at.elapsed() < Duration::from_secs(3600) {
                    return Ok(cached.set.clone());
                }
            }
        }

        let url = format!(
            "{}/.well-known/jwks.json",
            self.config.auth0_issuer_base_url.trim_end_matches('/')
        );
        let set = self
            .http
            .get(url)
            .send()
            .await
            .map_err(|_| ApiError::Unauthorized)?
            .json::<JwkSet>()
            .await
            .map_err(|_| ApiError::Unauthorized)?;
        *self.jwks.write().await = Some(CachedJwks {
            set: set.clone(),
            fetched_at: Instant::now(),
        });
        Ok(set)
    }
}

pub(crate) async fn upsert_user(pool: &SqlitePool, auth: &AuthUser) -> Result<Uuid, ApiError> {
    sqlx::query_scalar::<_, Uuid>(
        r#"
        insert into app_users (auth0_sub, auth0_email, id)
        values ($1, $2, $3)
        on conflict (auth0_sub) do update
          set auth0_email = coalesce(excluded.auth0_email, app_users.auth0_email),
              updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
        returning id
        "#,
    )
    .bind(&auth.sub)
    .bind(&auth.email)
    .bind(Uuid::new_v4())
    .fetch_one(pool)
    .await
    .map_err(ApiError::from)
}

async fn fetch_profile(pool: &SqlitePool, user_id: &Uuid) -> Result<Option<Profile>, ApiError> {
    let row = sqlx::query("select * from workspace_profiles where user_id = $1")
        .bind(user_id)
        .fetch_optional(pool)
        .await?;
    row.map(profile_from_row).transpose()
}

async fn fetch_uploads(pool: &SqlitePool, user_id: &Uuid) -> Result<Vec<UploadSummary>, ApiError> {
    let rows = sqlx::query(
        r#"
        select id, file_name, mime_type, row_count, total_kwh,
          daylight_consumption_percent, solar_proxy_percent,
          detected_timestamp_column, detected_consumption_column,
          first_timestamp, last_timestamp,
          case
            when row_count > 0 and total_kwh > 0 and detected_timestamp_column is not null and detected_consumption_column is not null
              then 'ingested'
            else 'pending_ingestion'
          end as ingestion_status,
          case
            when row_count > 0 and total_kwh > 0 and detected_timestamp_column is not null and detected_consumption_column is not null
              then null
            else 'CSV stored for review; automatic ingestion is not available for this format yet.'
          end as ingestion_message,
          expires_at, created_at
        from workspace_uploads
        where user_id = $1
        order by created_at desc
        "#,
    )
    .bind(user_id)
    .fetch_all(pool)
    .await?;

    rows.into_iter().map(upload_from_row).collect()
}

async fn fetch_questions(pool: &SqlitePool, user_id: &Uuid) -> Result<Vec<Question>, ApiError> {
    let rows = sqlx::query(
        "select id, municipality_key, body, created_at from workspace_questions where user_id = $1 order by created_at desc",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await?;
    rows.into_iter().map(question_from_row).collect()
}

fn profile_from_row(row: sqlx::sqlite::SqliteRow) -> Result<Profile, ApiError> {
    Ok(Profile {
        municipality_key: row.try_get("municipality_key")?,
        municipality_label: row.try_get("municipality_label")?,
        municipality_name: row.try_get("municipality_name")?,
        canton: row.try_get("canton")?,
        operator_name: row.try_get("operator_name")?,
        operator_website: row.try_get("operator_website")?,
        operator_address: row.try_get("operator_address")?,
        operator_postal_code: row.try_get("operator_postal_code")?,
        has_battery: row.try_get("has_battery")?,
        has_ev_charging: row.try_get("has_ev_charging")?,
        questions: row.try_get("questions")?,
        share_with_researchers: row.try_get("share_with_researchers")?,
        interested_in_community: row.try_get("interested_in_community")?,
        visible_interest: row.try_get("visible_interest")?,
        contact_email: row.try_get("contact_email")?,
        created_at: row.try_get("created_at")?,
        updated_at: row.try_get("updated_at")?,
    })
}

fn upload_from_row(row: sqlx::sqlite::SqliteRow) -> Result<UploadSummary, ApiError> {
    let detected_consumption_column: Option<String> = row.try_get("detected_consumption_column")?;
    let series_kind = series_kind_from_stored_column(detected_consumption_column.as_deref());
    Ok(UploadSummary {
        id: row.try_get::<Uuid, _>("id")?.to_string(),
        file_name: row.try_get("file_name")?,
        mime_type: row.try_get("mime_type")?,
        row_count: row.try_get("row_count")?,
        total_kwh: row.try_get("total_kwh")?,
        daylight_consumption_percent: row.try_get("daylight_consumption_percent")?,
        solar_proxy_percent: row.try_get("solar_proxy_percent")?,
        detected_timestamp_column: row.try_get("detected_timestamp_column")?,
        detected_consumption_column,
        first_timestamp: row.try_get("first_timestamp")?,
        last_timestamp: row.try_get("last_timestamp")?,
        series_kind: series_kind.as_str().to_string(),
        series_label: series_kind.label().to_string(),
        ingestion_status: row.try_get("ingestion_status")?,
        ingestion_message: row.try_get("ingestion_message")?,
        expires_at: row.try_get("expires_at")?,
        created_at: row.try_get("created_at")?,
    })
}

fn pending_ingestion_message() -> String {
    "CSV stored for review; automatic ingestion is not available for this format yet.".to_string()
}

fn analyse_csv_for_ingestion(
    bytes: &[u8],
    _file_name: &str,
) -> (ParsedCsv, String, Option<String>) {
    match std::str::from_utf8(bytes) {
        Ok(raw) => match analyse_csv(raw) {
            Ok(parsed) => (parsed, "ingested".to_string(), None),
            Err(_) => {
                tracing::info!("stored CSV pending automatic ingestion");
                (
                    ParsedCsv::pending_ingestion(),
                    "pending_ingestion".to_string(),
                    Some(pending_ingestion_message()),
                )
            }
        },
        Err(_) => {
            tracing::info!(
                "stored non-UTF-8 CSV pending automatic ingestion"
            );
            (
                ParsedCsv::pending_ingestion(),
                "pending_ingestion".to_string(),
                Some(pending_ingestion_message()),
            )
        }
    }
}

fn question_from_row(row: sqlx::sqlite::SqliteRow) -> Result<Question, ApiError> {
    Ok(Question {
        id: row.try_get::<Uuid, _>("id")?.to_string(),
        municipality_key: row.try_get("municipality_key")?,
        body: row.try_get("body")?,
        created_at: row.try_get("created_at")?,
    })
}

fn analyse_csv(raw: &str) -> Result<ParsedCsv, ApiError> {
    let (csv_data, delimiter) = csv_data_section(raw)?;
    let mut reader = ReaderBuilder::new()
        .delimiter(delimiter)
        .flexible(true)
        .trim(csv::Trim::All)
        .from_reader(csv_data.as_bytes());
    let headers = reader
        .headers()
        .map_err(|_| ApiError::BadRequest("CSV header row could not be read".to_string()))?
        .iter()
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();
    let timestamp_index = detect_timestamp_column(&headers)
        .ok_or_else(|| ApiError::BadRequest("could not detect timestamp column".to_string()))?;
    let consumption_index = detect_energy_column(&headers, raw)
        .ok_or_else(|| ApiError::BadRequest("could not detect consumption/kWh column".to_string()))?;
    let series_kind = detect_series_kind(raw, &headers, consumption_index);

    let mut row_count = 0;
    let mut total_kwh = 0.0;
    let mut daylight_kwh = 0.0;
    let mut solar_weighted_kwh = 0.0;
    let mut first_timestamp: Option<DateTime<Utc>> = None;
    let mut last_timestamp: Option<DateTime<Utc>> = None;

    for record in reader.records() {
        let record =
            record.map_err(|_| ApiError::BadRequest("CSV row could not be read".to_string()))?;
        let Some(timestamp_raw) = record.get(timestamp_index) else {
            continue;
        };
        let Some(consumption_raw) = record.get(consumption_index) else {
            continue;
        };
        let Some(timestamp) = parse_timestamp(timestamp_raw) else {
            continue;
        };
        let Some(kwh) = parse_kwh_value(consumption_raw, series_kind) else {
            continue;
        };
        if !kwh.is_finite() {
            continue;
        }

        row_count += 1;
        total_kwh += kwh;
        if solar_weight(timestamp) > 0.0 {
            daylight_kwh += kwh;
        }
        solar_weighted_kwh += kwh * solar_weight(timestamp);
        first_timestamp = Some(first_timestamp.map_or(timestamp, |value| value.min(timestamp)));
        last_timestamp = Some(last_timestamp.map_or(timestamp, |value| value.max(timestamp)));
    }

    if row_count == 0 || total_kwh <= 0.0 {
        return Err(ApiError::BadRequest(
            "CSV did not contain usable timestamped consumption rows".to_string(),
        ));
    }

    Ok(ParsedCsv {
        row_count,
        total_kwh,
        daylight_kwh,
        solar_weighted_kwh,
        detected_timestamp_column: headers.get(timestamp_index).cloned(),
        detected_consumption_column: headers.get(consumption_index).cloned(),
        first_timestamp,
        last_timestamp,
        series_kind,
    })
}

fn csv_data_section(raw: &str) -> Result<(&str, u8), ApiError> {
    let mut offset = 0;
    for line_with_ending in raw.split_inclusive('\n') {
        let line = line_with_ending.trim_end_matches(['\r', '\n']);
        let delimiter = detect_delimiter_for_line(line);
        let headers = split_header_line(line, delimiter);
        if headers.len() >= 2
            && detect_timestamp_column(&headers).is_some()
            && detect_energy_column(&headers, raw).is_some()
        {
            return Ok((&raw[offset..], delimiter));
        }
        offset += line_with_ending.len();
    }

    Err(ApiError::BadRequest(
        "CSV header row could not be detected".to_string(),
    ))
}

fn detect_delimiter_for_line(line: &str) -> u8 {
    [
        (b';', line.matches(';').count()),
        (b',', line.matches(',').count()),
        (b'\t', line.matches('\t').count()),
    ]
    .into_iter()
    .max_by_key(|(_, count)| *count)
    .map(|(delimiter, _)| delimiter)
    .unwrap_or(b',')
}

fn split_header_line(line: &str, delimiter: u8) -> Vec<String> {
    if delimiter == b'\t' {
        line.split('\t').map(clean_header_cell).collect()
    } else {
        line.split(char::from(delimiter)).map(clean_header_cell).collect()
    }
}

fn clean_header_cell(cell: &str) -> String {
    cell.trim().trim_matches('"').to_string()
}

fn detect_timestamp_column(headers: &[String]) -> Option<usize> {
    headers.iter().position(|header| {
        let normalized = normalize_header(header);
        ["timestamp", "datetime", "date_time", "zeit", "datum", "date", "time"]
            .iter()
            .any(|needle| normalized.contains(needle))
    })
}

fn detect_energy_column(headers: &[String], raw: &str) -> Option<usize> {
    detect_consumption_column(headers).or_else(|| detect_known_pv_export_column(headers, raw))
}

fn detect_consumption_column(headers: &[String]) -> Option<usize> {
    headers.iter().position(|header| {
        let normalized = normalize_header(header);
        !is_export_like_header(&normalized)
            && (normalized.contains("kwh")
            || normalized.contains("energy")
            || normalized.contains("energie")
            || normalized.contains("consumption")
            || normalized.contains("verbrauch")
            || normalized.contains("bezug"))
            && !normalized.contains("price")
            && !normalized.contains("tarif")
    })
}

fn detect_known_pv_export_column(headers: &[String], raw: &str) -> Option<usize> {
    if !has_sursee_pv_export_metadata(raw) {
        return None;
    }
    headers.iter().position(|header| {
        let normalized = normalize_header(header);
        (normalized == "ruecklieferung__kwh" || normalized == "rucklieferung__kwh")
            || ((normalized.contains("ruecklieferung") || normalized.contains("rucklieferung"))
                && normalized.contains("kwh"))
    })
}

fn detect_series_kind(raw: &str, headers: &[String], energy_index: usize) -> SeriesKind {
    if has_sursee_pv_export_metadata(raw)
        && headers
            .get(energy_index)
            .map(|header| detect_known_pv_export_column(&[header.clone()], raw).is_some())
            .unwrap_or(false)
    {
        SeriesKind::PvExportSursee
    } else {
        SeriesKind::Consumption
    }
}

fn has_sursee_pv_export_metadata(raw: &str) -> bool {
    raw.lines()
        .take(12)
        .map(normalize_header)
        .any(|line| line.contains("vertrag:_ueberschuss_pva"))
}

fn is_export_like_header(normalized: &str) -> bool {
    [
        "ruecklieferung",
        "rucklieferung",
        "einspeisung",
        "export",
        "feed_in",
        "production",
        "produktion",
    ]
    .iter()
    .any(|needle| normalized.contains(needle))
}

fn normalize_header(value: &str) -> String {
    value
        .trim()
        .to_lowercase()
        .replace('ä', "ae")
        .replace('ö', "oe")
        .replace('ü', "ue")
        .replace([' ', '-', '.', '/', '(', ')'], "_")
}

fn parse_timestamp(value: &str) -> Option<DateTime<Utc>> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    if let Ok(parsed) = DateTime::parse_from_rfc3339(trimmed) {
        return Some(parsed.with_timezone(&Utc));
    }

    let formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ];
    formats.iter().find_map(|format| {
        NaiveDateTime::parse_from_str(trimmed, format)
            .ok()
            .and_then(|naive| Zurich.from_local_datetime(&naive).single())
            .map(|local| local.with_timezone(&Utc))
    })
}

fn parse_number(value: &str) -> Option<f64> {
    let cleaned = value
        .trim()
        .replace('\'', "")
        .replace(' ', "")
        .replace(',', ".");
    cleaned.parse::<f64>().ok()
}

fn parse_kwh_value(value: &str, series_kind: SeriesKind) -> Option<f64> {
    let parsed = parse_number(value)?;
    if parsed < 0.0 {
        if series_kind == SeriesKind::PvExportSursee {
            Some(parsed.abs())
        } else {
            None
        }
    } else {
        Some(parsed)
    }
}

fn series_kind_from_stored_column(header: Option<&str>) -> SeriesKind {
    header
        .map(normalize_header)
        .filter(|normalized| {
            (normalized.contains("ruecklieferung") || normalized.contains("rucklieferung"))
                && normalized.contains("kwh")
        })
        .map(|_| SeriesKind::PvExportSursee)
        .unwrap_or(SeriesKind::Consumption)
}

fn solar_weight(timestamp: DateTime<Utc>) -> f64 {
    const SWISS_REFERENCE_LATITUDE_DEGREES: f64 = 47.0;
    const SWISS_REFERENCE_LONGITUDE_DEGREES: f64 = 8.3;

    let local = timestamp.with_timezone(&Zurich);
    let day = f64::from(local.ordinal());
    let utc = timestamp.with_timezone(&Utc);
    let utc_hour = f64::from(utc.hour())
        + f64::from(utc.minute()) / 60.0
        + f64::from(utc.second()) / 3600.0;
    let solar_time = utc_hour
        + SWISS_REFERENCE_LONGITUDE_DEGREES / 15.0
        + equation_of_time_minutes(day) / 60.0;
    let hour_angle = (15.0 * (solar_time - 12.0)).to_radians();
    let declination =
        (23.44 * ((std::f64::consts::TAU * (day - 80.0)) / 365.0).sin()).to_radians();
    let latitude = SWISS_REFERENCE_LATITUDE_DEGREES.to_radians();
    let sin_altitude = latitude.sin() * declination.sin()
        + latitude.cos() * declination.cos() * hour_angle.cos();

    sin_altitude.max(0.0)
}

fn equation_of_time_minutes(day: f64) -> f64 {
    let angle = (std::f64::consts::TAU * (day - 81.0)) / 364.0;
    9.87 * (2.0 * angle).sin() - 7.53 * angle.cos() - 1.5 * angle.sin()
}

fn percent(part: f64, total: f64) -> f64 {
    if total <= 0.0 {
        0.0
    } else {
        (part / total) * 100.0
    }
}

fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

fn hex_digest(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest)
}

fn object_key(upload_id: Uuid, user: Option<&AuthUser>) -> String {
    let now = Utc::now();
    match user {
        Some(auth) => {
            let hash = hex_digest(auth.sub.as_bytes());
            format!("users/{hash}/{upload_id}.csv")
        }
        None => format!(
            "anonymous/{:04}/{:02}/{upload_id}.csv",
            now.year(),
            now.month()
        ),
    }
}

fn trim_opt(value: Option<&str>) -> Option<String> {
    value.map(str::trim).filter(|value| !value.is_empty()).map(ToOwned::to_owned)
}

async fn cleanup_expired_uploads(
    pool: &SqlitePool,
) -> Result<(), ApiError> {
    let mut transaction = pool.begin().await?;
    let rows = sqlx::query_scalar::<_, Uuid>(
        "select id from workspace_uploads where user_id is null or (expires_at is not null and julianday(expires_at) < julianday('now'))",
    )
    .fetch_all(&mut *transaction)
    .await?;
    sqlx::query(
        r#"
        insert into storage_deletion_outbox (object_bucket, object_key)
        select object_bucket, object_key
        from workspace_uploads
        where user_id is null or (expires_at is not null and julianday(expires_at) < julianday('now'))
        on conflict (object_bucket, object_key) do nothing
        "#,
    )
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        "delete from workspace_uploads where user_id is null or (expires_at is not null and julianday(expires_at) < julianday('now'))",
    )
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    tracing::info!("removed {} legacy anonymous or expired uploads", rows.len());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_semicolon_csv_and_calculates_totals() {
        let csv = "Datum;Verbrauch kWh\n01.06.2026 10:00;1,5\n01.06.2026 22:00;2,5\n";
        let parsed = analyse_csv(csv).expect("CSV should parse");

        assert_eq!(parsed.row_count, 2);
        assert_eq!(parsed.detected_timestamp_column.as_deref(), Some("Datum"));
        assert_eq!(parsed.detected_consumption_column.as_deref(), Some("Verbrauch kWh"));
        assert!((parsed.total_kwh - 4.0).abs() < 0.001);
        assert!(parsed.daylight_kwh > 0.0);
        assert!(parsed.solar_weighted_kwh > 0.0);
        assert!(parsed.solar_weighted_kwh < parsed.total_kwh);
    }

    #[test]
    fn rejects_csv_without_detectable_consumption_column() {
        let csv = "Datum;Preis\n01.06.2026 10:00;12\n";
        let error = analyse_csv(csv).expect_err("CSV should be rejected");

        assert!(matches!(error, ApiError::BadRequest(_)));
    }

    #[test]
    fn keeps_unknown_csv_pending_ingestion() {
        let csv = "Datum;Preis\n01.06.2026 10:00;12\n";
        let (parsed, status, message) =
            analyse_csv_for_ingestion(csv.as_bytes(), "prices.csv");

        assert_eq!(status, "pending_ingestion");
        assert_eq!(parsed.row_count, 0);
        assert_eq!(parsed.total_kwh, 0.0);
        assert!(message.is_some());
    }

    #[test]
    fn keeps_non_utf8_csv_pending_ingestion() {
        let bytes = b"Datum;Verbrauch kWh\n01.06.2026 10:00;\xff\n";
        let (parsed, status, message) = analyse_csv_for_ingestion(bytes, "latin1.csv");

        assert_eq!(status, "pending_ingestion");
        assert_eq!(parsed.row_count, 0);
        assert_eq!(parsed.total_kwh, 0.0);
        assert!(message.is_some());
    }

    #[test]
    fn detects_header_after_metadata_and_ingests_feed_in_values() {
        let csv = [
            "Kunde: Alimo Sursee AG",
            "Kundennummer: 1151921",
            "Adresse: Allmendstrasse 4 A, 6210 Sursee",
            "Vertrag: Überschuss PVA",
            ";;",
            "Einheit:Viertelstunde",
            "",
            "Zeitraum;Rücklieferung (kWh)",
            "01.01.2025 12:00;-1.5",
            "01.01.2025 12:15;-0.5",
            "Total;-2.0",
        ]
        .join("\n");
        let parsed = analyse_csv(&csv).expect("CSV should parse after metadata rows");

        assert_eq!(parsed.row_count, 2);
        assert_eq!(parsed.detected_timestamp_column.as_deref(), Some("Zeitraum"));
        assert_eq!(
            parsed.detected_consumption_column.as_deref(),
            Some("Rücklieferung (kWh)")
        );
        assert_eq!(parsed.series_kind, SeriesKind::PvExportSursee);
        assert!((parsed.total_kwh - 2.0).abs() < 0.001);
    }

    #[test]
    fn detects_sursee_consumption_file_as_consumption() {
        let csv = [
            "Kunde: Alimo Sursee AG",
            "Kundennummer: 1151921",
            "Adresse: Allmendstrasse 4, 6210 Sursee",
            "Vertrag: WP+Allgemein+PVA Allmendstr. 4",
            ";;",
            "Einheit:Viertelstunde",
            "",
            "Zeitraum;Energieverbrauch (kWh);Blindenergie (kvar)",
            "01.01.2025 00:00;9.975;1.2",
            "01.01.2025 00:15;10.025;1.1",
            "Total;20.0;2.3",
        ]
        .join("\n");
        let parsed = analyse_csv(&csv).expect("consumption CSV should parse after metadata rows");

        assert_eq!(parsed.row_count, 2);
        assert_eq!(
            parsed.detected_consumption_column.as_deref(),
            Some("Energieverbrauch (kWh)")
        );
        assert_eq!(parsed.series_kind, SeriesKind::Consumption);
        assert!((parsed.total_kwh - 20.0).abs() < 0.001);
    }

    #[test]
    fn does_not_treat_generic_export_headers_as_known_pv_production() {
        let csv = "Zeitraum;Einspeisung (kWh)\n01.01.2025 12:00;-1.5\n";
        let error = analyse_csv(csv).expect_err("generic export CSV should stay unknown");

        assert!(matches!(error, ApiError::BadRequest(_)));
    }

    #[test]
    fn solar_weight_is_zero_at_night_and_positive_at_noon() {
        let night = Zurich
            .with_ymd_and_hms(2026, 6, 1, 2, 0, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let noon = Zurich
            .with_ymd_and_hms(2026, 6, 1, 12, 0, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);

        assert_eq!(solar_weight(night), 0.0);
        assert!(solar_weight(noon) > 0.8);
    }

    #[test]
    fn solar_weight_accounts_for_swiss_summer_solar_noon() {
        let noon = Zurich
            .with_ymd_and_hms(2026, 6, 1, 12, 0, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let solar_noon = Zurich
            .with_ymd_and_hms(2026, 6, 1, 13, 30, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);

        assert!(solar_weight(solar_noon) > solar_weight(noon));
    }

    #[test]
    fn solar_weight_reflects_shorter_swiss_winter_days() {
        let summer_morning = Zurich
            .with_ymd_and_hms(2026, 6, 21, 6, 0, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let winter_morning = Zurich
            .with_ymd_and_hms(2026, 12, 21, 6, 0, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let summer_noon = Zurich
            .with_ymd_and_hms(2026, 6, 21, 13, 30, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let winter_noon = Zurich
            .with_ymd_and_hms(2026, 12, 21, 12, 30, 0)
            .single()
            .unwrap()
            .with_timezone(&Utc);

        assert!(solar_weight(summer_morning) > 0.0);
        assert_eq!(solar_weight(winter_morning), 0.0);
        assert!(solar_weight(summer_noon) > solar_weight(winter_noon));
    }
}
