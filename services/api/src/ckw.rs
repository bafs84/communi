use std::{
    collections::HashMap,
    env,
    path::PathBuf,
    sync::Arc,
};

use aes_gcm::{
    aead::{Aead, KeyInit},
    Aes256Gcm, Nonce,
};
use axum::{
    body::Body,
    extract::{Multipart, Path, Query, State},
    http::{
        header::{CACHE_CONTROL, CONTENT_DISPOSITION, CONTENT_TYPE, COOKIE, SET_COOKIE},
        HeaderMap, HeaderValue, StatusCode,
    },
    response::{IntoResponse, Response},
    routing::{delete, get, post, put},
    Json, Router,
};
use base64::{engine::general_purpose, Engine as _};
use chrono::{DateTime, Duration, Utc};
use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, Row};
use tokio::sync::Mutex;
use thiserror::Error;
use uuid::Uuid;

use crate::{upsert_user, AuthVerifier, storage::{LocalStorage, encryption_key}};

const CSRF_HEADER: &str = "x-csrf-token";
const CONFIG_SCHEMA_VERSION: i32 = 1;

#[derive(Debug, Error)]
pub(crate) enum CkwError {
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("unauthorized")]
    Unauthorized,
    #[error("not found")]
    NotFound,
    #[error("payload too large")]
    PayloadTooLarge,
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    #[error("dependency unavailable: {0}")]
    Dependency(String),
    #[error("internal error")]
    Internal,
}

impl IntoResponse for CkwError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            Self::BadRequest(message) => (StatusCode::BAD_REQUEST, message),
            Self::Unauthorized => (StatusCode::UNAUTHORIZED, "unauthorized".to_string()),
            Self::NotFound => (StatusCode::NOT_FOUND, "not found".to_string()),
            Self::PayloadTooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "payload too large".to_string()),
            Self::QuotaExceeded(message) => (
                StatusCode::from_u16(507).expect("507 is a valid status code"),
                message,
            ),
            Self::Dependency(message) => (StatusCode::BAD_GATEWAY, message),
            Self::Internal => (StatusCode::INTERNAL_SERVER_ERROR, "internal error".to_string()),
        };
        (status, Json(json!({ "error": message, "detail": message }))).into_response()
    }
}

impl From<sqlx::Error> for CkwError {
    fn from(_: sqlx::Error) -> Self {
        Self::Internal
    }
}

#[derive(Clone)]
struct DataCrypto {
    cipher: Aes256Gcm,
}

impl DataCrypto {
    fn from_secret(value: &str) -> Result<Self, String> {
        let decoded = general_purpose::STANDARD
            .decode(value.trim())
            .or_else(|_| general_purpose::URL_SAFE_NO_PAD.decode(value.trim()))
            .map_err(|_| "DATA_ENCRYPTION_KEY must be base64".to_string())?;
        if decoded.len() != 32 {
            return Err("DATA_ENCRYPTION_KEY must decode to exactly 32 bytes".to_string());
        }
        Ok(Self {
            cipher: Aes256Gcm::new_from_slice(&decoded)
                .map_err(|_| "invalid DATA_ENCRYPTION_KEY".to_string())?,
        })
    }

    fn encrypt(&self, plaintext: &[u8]) -> Result<Vec<u8>, CkwError> {
        let mut nonce_bytes = [0_u8; 12];
        OsRng.fill_bytes(&mut nonce_bytes);
        let ciphertext = self
            .cipher
            .encrypt(Nonce::from_slice(&nonce_bytes), plaintext)
            .map_err(|_| CkwError::Internal)?;
        let mut output = Vec::with_capacity(1 + nonce_bytes.len() + ciphertext.len());
        output.push(1);
        output.extend_from_slice(&nonce_bytes);
        output.extend_from_slice(&ciphertext);
        Ok(output)
    }

    fn decrypt(&self, encrypted: &[u8]) -> Result<Vec<u8>, CkwError> {
        if encrypted.len() < 14 || encrypted[0] != 1 {
            return Err(CkwError::Internal);
        }
        self.cipher
            .decrypt(Nonce::from_slice(&encrypted[1..13]), &encrypted[13..])
            .map_err(|_| CkwError::Internal)
    }

    fn encrypt_text(&self, value: &str) -> Result<Vec<u8>, CkwError> {
        self.encrypt(value.as_bytes())
    }

    fn decrypt_text(&self, value: &[u8]) -> Result<String, CkwError> {
        String::from_utf8(self.decrypt(value)?).map_err(|_| CkwError::Internal)
    }

    fn encrypt_json(&self, value: &Value) -> Result<Vec<u8>, CkwError> {
        let bytes = serde_json::to_vec(value).map_err(|_| CkwError::Internal)?;
        self.encrypt(&bytes)
    }

    fn decrypt_json(&self, value: &[u8]) -> Result<Value, CkwError> {
        serde_json::from_slice(&self.decrypt(value)?).map_err(|_| CkwError::Internal)
    }
}

#[derive(Clone)]
pub(crate) struct CkwState {
    db: SqlitePool,
    storage: LocalStorage,
    auth: AuthVerifier,
    http: reqwest::Client,
    crypto: DataCrypto,
    bucket: String,
    engine_url: String,
    engine_token: String,
    max_file_bytes: usize,
    guest_max_bytes: usize,
    registered_max_bytes: i64,
    guest_idle: Duration,
    guest_max_age: Duration,
    guest_tmp_dir: PathBuf,
    cookie_name: String,
    cookie_secure: bool,
    guests: Arc<Mutex<HashMap<String, GuestSession>>>,
}

impl CkwState {
    pub(crate) async fn from_env(
        db: SqlitePool,
        storage: LocalStorage,
        auth: AuthVerifier,
        data_dir: PathBuf,
    ) -> Result<Self, String> {
        let crypto_secret = if env::var("DATA_ENCRYPTION_KEY").is_ok() || env::var("DATA_ENCRYPTION_KEY_FILE").is_ok() {
            read_secret("DATA_ENCRYPTION_KEY", "DATA_ENCRYPTION_KEY_FILE")?
        } else {
            let key_path = data_dir.join("secrets/data_encryption_key");
            if !key_path.exists() && sqlx::query_scalar::<_, i64>("select count(*) from ckw_projects")
                .fetch_one(&db).await.map_err(|_| "could not inspect existing encrypted projects")? > 0 {
                return Err("encryption key is missing for existing projects; restore it from backup".into());
            }
            encryption_key(&key_path)?
        };
        let engine_token = read_secret("CKW_INTERNAL_TOKEN", "CKW_INTERNAL_TOKEN_FILE")?;
        let guest_tmp_dir = PathBuf::from(
            env::var("GUEST_TMP_DIR").unwrap_or_else(|_| "/tmp/communi-guests".to_string()),
        );
        clear_orphaned_guest_data(&guest_tmp_dir)?;
        std::fs::create_dir_all(&guest_tmp_dir)
            .map_err(|error| format!("could not create guest tmp directory: {error}"))?;
        Ok(Self {
            db,
            storage,
            auth,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(180))
                .build()
                .map_err(|error| format!("could not build CKW HTTP client: {error}"))?,
            crypto: DataCrypto::from_secret(&crypto_secret)?,
            bucket: "local".into(),
            engine_url: env::var("CKW_ENGINE_URL")
                .unwrap_or_else(|_| "http://ckw-engine:8000".to_string())
                .trim_end_matches('/')
                .to_string(),
            engine_token,
            max_file_bytes: env_usize("MAX_FILE_BYTES", 10 * 1024 * 1024)?,
            guest_max_bytes: env_usize("GUEST_SESSION_MAX_BYTES", 50 * 1024 * 1024)?,
            registered_max_bytes: env_i64("REGISTERED_USER_MAX_BYTES", 1024 * 1024 * 1024)?,
            guest_idle: Duration::minutes(env_i64("GUEST_IDLE_MINUTES", 60)?),
            guest_max_age: Duration::hours(env_i64("GUEST_MAX_HOURS", 24)?),
            guest_tmp_dir,
            cookie_name: env::var("CKW_GUEST_COOKIE_NAME")
                .unwrap_or_else(|_| "ckw_guest".to_string()),
            cookie_secure: env::var("CKW_COOKIE_SECURE")
                .unwrap_or_else(|_| "false".to_string())
                .eq_ignore_ascii_case("true"),
            guests: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    pub(crate) fn request_body_limit(&self) -> usize {
        self.guest_max_bytes + 2 * 1024 * 1024
    }

    pub(crate) async fn ready(&self) -> bool {
        if sqlx::query_scalar::<_, i32>("select 1")
            .fetch_one(&self.db)
            .await
            .is_err()
        {
            return false;
        }
        if !self.storage.ready().await {
            return false;
        }
        let response = self
            .http
            .get(format!("{}/internal/health", self.engine_url))
            .bearer_auth(&self.engine_token)
            .send()
            .await;
        matches!(response, Ok(value) if value.status().is_success())
    }

    pub(crate) async fn cleanup_guests(&self) {
        let now = Utc::now();
        let expired = {
            let mut guests = self.guests.lock().await;
            let keys = guests
                .iter()
                .filter(|(_, session)| {
                    now - session.last_activity > self.guest_idle
                        || now - session.created_at > self.guest_max_age
                })
                .map(|(key, _)| key.clone())
                .collect::<Vec<_>>();
            for key in &keys {
                guests.remove(key);
            }
            keys
        };
        for key in expired {
            let _ = tokio::fs::remove_dir_all(self.guest_tmp_dir.join(key)).await;
        }
    }

    pub(crate) async fn process_deletion_outbox(&self) -> Result<u64, CkwError> {
        let rows = sqlx::query(
            r#"
            select id, object_bucket, object_key, attempts
            from storage_deletion_outbox
            where julianday(next_attempt_at) <= julianday('now')
            order by id
            limit 100
            "#,
        )
        .fetch_all(&self.db)
        .await?;
        let mut deleted = 0_u64;
        for row in rows {
            let id: i64 = row.try_get("id").map_err(|_| CkwError::Internal)?;
            let key: String = row.try_get("object_key").map_err(|_| CkwError::Internal)?;
            let attempts: i32 = row.try_get("attempts").map_err(|_| CkwError::Internal)?;
            match self.storage.delete(&key).await {
                Ok(_) => {
                    sqlx::query("delete from storage_deletion_outbox where id = $1")
                        .bind(id)
                        .execute(&self.db)
                        .await?;
                    deleted += 1;
                }
                Err(_) => {
                    let delay_minutes = 2_i64.pow((attempts + 1).min(8) as u32);
                    sqlx::query(
                        "update storage_deletion_outbox set attempts = attempts + 1, next_attempt_at = strftime('%Y-%m-%dT%H:%M:%f+00:00','now', '+' || $2 || ' minutes') where id = $1",
                    )
                    .bind(id)
                    .bind(delay_minutes)
                    .execute(&self.db)
                    .await?;
                }
            }
        }
        Ok(deleted)
    }
}

fn is_guest_session_key(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn clear_orphaned_guest_data(root: &std::path::Path) -> Result<(), String> {
    if !root.exists() {
        return Ok(());
    }
    let entries = std::fs::read_dir(root)
        .map_err(|error| format!("could not inspect guest tmp directory: {error}"))?;
    for entry in entries {
        let entry = entry.map_err(|error| format!("could not inspect guest tmp entry: {error}"))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let file_type = entry
            .file_type()
            .map_err(|error| format!("could not inspect guest tmp entry type: {error}"))?;
        if file_type.is_dir() && is_guest_session_key(name) {
            std::fs::remove_dir_all(entry.path())
                .map_err(|error| format!("could not remove orphaned guest data: {error}"))?;
        }
    }
    Ok(())
}

fn read_secret(value_name: &str, file_name: &str) -> Result<String, String> {
    if let Ok(path) = env::var(file_name) {
        return std::fs::read_to_string(&path)
            .map(|value| value.trim().to_string())
            .map_err(|error| format!("could not read {file_name} at {path}: {error}"));
    }
    env::var(value_name).map_err(|_| format!("missing {value_name} or {file_name}"))
}

fn env_usize(name: &str, default: usize) -> Result<usize, String> {
    env::var(name)
        .unwrap_or_else(|_| default.to_string())
        .parse()
        .map_err(|_| format!("{name} must be a positive integer"))
}

fn env_i64(name: &str, default: i64) -> Result<i64, String> {
    env::var(name)
        .unwrap_or_else(|_| default.to_string())
        .parse()
        .map_err(|_| format!("{name} must be an integer"))
}

#[derive(Clone)]
struct GuestSession {
    csrf_token: String,
    created_at: DateTime<Utc>,
    last_activity: DateTime<Utc>,
    projects: HashMap<Uuid, GuestProject>,
}

#[derive(Clone)]
struct GuestProject {
    id: Uuid,
    name: String,
    municipality: String,
    config: Value,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
    files: Vec<GuestFile>,
    results: Vec<GuestResult>,
}

#[derive(Clone)]
struct GuestFile {
    id: Uuid,
    original_filename: String,
    upload_kind: String,
    media_type: String,
    byte_size: usize,
    sha256: String,
    path: PathBuf,
}

#[derive(Clone)]
struct GuestResult {
    id: Uuid,
    operation: String,
    filename: String,
    media_type: String,
    byte_size: usize,
    path: PathBuf,
    created_at: DateTime<Utc>,
}

#[derive(Clone)]
enum Actor {
    Registered { user_id: Uuid },
    Guest { session_key: String },
}

#[derive(Clone)]
struct EngineFile {
    id: Uuid,
    original_filename: String,
    upload_kind: String,
    media_type: String,
    bytes: Vec<u8>,
}

#[derive(Clone)]
struct EngineProject {
    id: Uuid,
    name: String,
    municipality: String,
    config: Value,
    files: Vec<EngineFile>,
}

#[derive(Debug)]
struct EngineResponse {
    body: Vec<u8>,
    media_type: String,
    filename: Option<String>,
    invoice_count: Option<String>,
    period_count: Option<String>,
}

#[derive(Serialize)]
struct GuestSessionResponse {
    #[serde(rename = "csrfToken")]
    csrf_token: String,
    #[serde(rename = "idleExpiresAt")]
    idle_expires_at: DateTime<Utc>,
    #[serde(rename = "hardExpiresAt")]
    hard_expires_at: DateTime<Utc>,
}

#[derive(Serialize)]
struct ProjectResponse {
    project_id: String,
    name: String,
    municipality: String,
    config: Value,
    storage_mode: &'static str,
    file_count: i64,
    result_count: i64,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}

#[derive(Serialize)]
struct ResultResponse {
    result_id: String,
    project_id: String,
    operation: String,
    filename: String,
    media_type: String,
    byte_size: i64,
    created_at: DateTime<Utc>,
    download_url: String,
}

#[derive(Deserialize)]
struct CreateProjectInput {
    name: String,
    municipality: String,
}

#[derive(Deserialize)]
struct PatchProjectInput {
    name: Option<String>,
    municipality: Option<String>,
    config: Option<Value>,
}

#[derive(Deserialize)]
struct OperationInput {
    #[serde(default)]
    parameters: Value,
}

#[derive(Deserialize)]
struct CachedPlantInput {
    cache_id: String,
}

#[derive(Deserialize)]
struct ManualPlantInput {
    address: String,
    installed_kwp: f64,
}

#[derive(Deserialize)]
struct BatteryConsumersInput {
    consumer_file_ids: Vec<String>,
}

pub(crate) fn router(state: CkwState) -> Router {
    Router::new()
        .route("/health/live", get(health_live))
        .route("/health/ready", get(health_ready))
        .route("/v1/ckw/guest/session", post(create_guest_session).delete(delete_guest_session))
        .route("/v1/ckw/guest/claim", post(claim_guest_session))
        .route("/v1/ckw/municipalities", get(municipalities))
        .route("/v1/ckw/projects", get(list_projects).post(create_project_handler))
        .route(
            "/v1/ckw/projects/:project_id",
            get(get_project_handler).patch(patch_project_handler).delete(delete_project_handler),
        )
        .route(
            "/v1/ckw/projects/:project_id/uploads/:upload_kind",
            post(upload_files),
        )
        .route(
            "/v1/ckw/projects/:project_id/files/:file_id",
            delete(delete_file_handler),
        )
        .route(
            "/v1/ckw/projects/:project_id/operations/:operation",
            post(versioned_operation),
        )
        .route(
            "/v1/ckw/projects/:project_id/results",
            get(list_results),
        )
        .route(
            "/v1/ckw/projects/:project_id/results/:result_id/download",
            get(download_result),
        )
        // Compatibility routes used by the imported CKW React application.
        .route("/api/municipalities", get(municipalities))
        .route("/api/projects", get(list_projects).post(create_project_handler))
        .route(
            "/api/projects/:project_id",
            get(get_project_handler).patch(patch_project_handler).delete(delete_project_handler),
        )
        .route("/api/projects/:project_id/solar-files", post(upload_solar_files))
        .route(
            "/api/projects/:project_id/consumption-files",
            post(upload_consumption_files),
        )
        .route("/api/projects/:project_id/zev-files", post(upload_zev_files))
        .route(
            "/api/projects/:project_id/files/:file_id",
            delete(delete_file_handler),
        )
        .route(
            "/api/projects/:project_id/plants/adopt-automatic",
            post(adopt_automatic_plants),
        )
        .route(
            "/api/projects/:project_id/plants/cache",
            post(add_cached_plant),
        )
        .route(
            "/api/projects/:project_id/plants/manual",
            post(add_manual_plant),
        )
        .route(
            "/api/projects/:project_id/plants/selection",
            delete(clear_plant_selection),
        )
        .route(
            "/api/projects/:project_id/plants/selection/:plant_id",
            delete(delete_selected_plant),
        )
        .route(
            "/api/projects/:project_id/battery-placements/pv-sites/:solar_file_id/consumers",
            put(update_battery_consumers),
        )
        .route(
            "/api/projects/:project_id/:operation",
            get(compatibility_operation),
        )
        .route("/api/vzev/invoices", post(billing_invoices))
        .with_state(state)
}

async fn health_live() -> Json<Value> {
    Json(json!({ "ok": true }))
}

async fn health_ready(State(state): State<CkwState>) -> Response {
    if state.ready().await {
        (StatusCode::OK, Json(json!({ "ok": true }))).into_response()
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({ "ok": false, "reason": "dependency_unavailable" })),
        )
            .into_response()
    }
}

fn default_config() -> Value {
    json!({
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "leg": {
            "networkDiscount": "0.40",
            "energyPriceRpPerKwh": null
        },
        "vzev": {
            "tariffMode": "participant_80_pct",
            "fixedPvTariffRpPerKwh": null
        },
        "plantSelection": {
            "mode": "automatic",
            "plants": []
        },
        "batteryConsumers": {},
        "billing": {}
    })
}

fn random_token() -> String {
    let mut bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut bytes);
    general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

fn token_key(token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn content_digest(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .filter_map(|part| part.trim().split_once('='))
        .find(|(key, _)| *key == name)
        .map(|(_, value)| value.to_string())
}

fn guest_cookie(state: &CkwState, token: &str, max_age_seconds: i64) -> String {
    let secure = if state.cookie_secure { "; Secure" } else { "" };
    format!(
        "{}={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}{}",
        state.cookie_name, token, max_age_seconds, secure
    )
}

async fn actor_from_headers(
    state: &CkwState,
    headers: &HeaderMap,
    require_csrf: bool,
) -> Result<Actor, CkwError> {
    state.cleanup_guests().await;
    if let Some(user) = state
        .auth
        .optional_user(headers)
        .await
        .map_err(|_| CkwError::Unauthorized)?
    {
        let user_id = upsert_user(&state.db, &user)
            .await
            .map_err(|_| CkwError::Internal)?;
        return Ok(Actor::Registered { user_id });
    }
    let token = cookie_value(headers, &state.cookie_name).ok_or(CkwError::Unauthorized)?;
    let key = token_key(&token);
    let mut guests = state.guests.lock().await;
    let session = guests.get_mut(&key).ok_or(CkwError::Unauthorized)?;
    if require_csrf {
        let supplied = headers
            .get(CSRF_HEADER)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default();
        if supplied != session.csrf_token {
            return Err(CkwError::Unauthorized);
        }
    }
    session.last_activity = Utc::now();
    Ok(Actor::Guest { session_key: key })
}

async fn create_guest_session(State(state): State<CkwState>) -> Result<Response, CkwError> {
    state.cleanup_guests().await;
    let token = random_token();
    let key = token_key(&token);
    let csrf_token = random_token();
    let now = Utc::now();
    tokio::fs::create_dir_all(state.guest_tmp_dir.join(&key))
        .await
        .map_err(|_| CkwError::Internal)?;
    state.guests.lock().await.insert(
        key,
        GuestSession {
            csrf_token: csrf_token.clone(),
            created_at: now,
            last_activity: now,
            projects: HashMap::new(),
        },
    );
    let body = GuestSessionResponse {
        csrf_token,
        idle_expires_at: now + state.guest_idle,
        hard_expires_at: now + state.guest_max_age,
    };
    let mut response = Json(body).into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_str(&guest_cookie(
            &state,
            &token,
            state.guest_max_age.num_seconds(),
        ))
        .map_err(|_| CkwError::Internal)?,
    );
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(response)
}

async fn delete_guest_session(
    State(state): State<CkwState>,
    headers: HeaderMap,
) -> Result<Response, CkwError> {
    let actor = actor_from_headers(&state, &headers, true).await?;
    let Actor::Guest { session_key } = actor else {
        return Err(CkwError::BadRequest("authenticated sessions are deleted from account settings".to_string()));
    };
    state.guests.lock().await.remove(&session_key);
    let _ = tokio::fs::remove_dir_all(state.guest_tmp_dir.join(&session_key)).await;
    let mut response = Json(json!({ "ok": true })).into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_str(&guest_cookie(&state, "", 0)).map_err(|_| CkwError::Internal)?,
    );
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(response)
}

async fn municipalities(
    State(state): State<CkwState>,
    headers: HeaderMap,
) -> Result<Response, CkwError> {
    let _ = actor_from_headers(&state, &headers, false).await?;
    let response = state
        .http
        .get(format!("{}/internal/v1/municipalities", state.engine_url))
        .bearer_auth(&state.engine_token)
        .send()
        .await
        .map_err(|_| CkwError::Dependency("calculation engine unavailable".to_string()))?;
    let status = response.status();
    let bytes = response.bytes().await.map_err(|_| CkwError::Internal)?;
    if !status.is_success() {
        return Err(CkwError::Dependency("calculation engine unavailable".to_string()));
    }
    Ok(json_bytes_response(bytes.to_vec()))
}

fn validate_project_input(name: &str, municipality: &str) -> Result<(String, String), CkwError> {
    let name = name.trim();
    let municipality = municipality.trim();
    if name.is_empty() || name.chars().count() > 120 {
        return Err(CkwError::BadRequest("project name must contain 1 to 120 characters".to_string()));
    }
    if municipality.is_empty() || municipality.chars().count() > 120 {
        return Err(CkwError::BadRequest("municipality is required".to_string()));
    }
    Ok((name.to_string(), municipality.to_string()))
}

async fn create_project_for_actor(
    state: &CkwState,
    actor: &Actor,
    name: &str,
    municipality: &str,
    config: Value,
) -> Result<ProjectResponse, CkwError> {
    let (name, municipality) = validate_project_input(name, municipality)?;
    let now = Utc::now();
    match actor {
        Actor::Registered { user_id } => {
            let id = Uuid::new_v4();
            sqlx::query(
                r#"
                insert into ckw_projects (
                  id, user_id, name_ciphertext, municipality_ciphertext, config_ciphertext,
                  config_schema_version, created_at, updated_at
                ) values ($1,$2,$3,$4,$5,$6,$7,$7)
                "#,
            )
            .bind(id)
            .bind(user_id)
            .bind(state.crypto.encrypt_text(&name)?)
            .bind(state.crypto.encrypt_text(&municipality)?)
            .bind(state.crypto.encrypt_json(&config)?)
            .bind(CONFIG_SCHEMA_VERSION)
            .bind(now)
            .execute(&state.db)
            .await?;
            Ok(ProjectResponse {
                project_id: id.to_string(),
                name,
                municipality,
                config,
                storage_mode: "registered",
                file_count: 0,
                result_count: 0,
                created_at: now,
                updated_at: now,
            })
        }
        Actor::Guest { session_key } => {
            let id = Uuid::new_v4();
            let project = GuestProject {
                id,
                name: name.clone(),
                municipality: municipality.clone(),
                config: config.clone(),
                created_at: now,
                updated_at: now,
                files: Vec::new(),
                results: Vec::new(),
            };
            let mut guests = state.guests.lock().await;
            let session = guests.get_mut(session_key).ok_or(CkwError::Unauthorized)?;
            session.projects.insert(id, project);
            Ok(ProjectResponse {
                project_id: id.to_string(),
                name,
                municipality,
                config,
                storage_mode: "guest",
                file_count: 0,
                result_count: 0,
                created_at: now,
                updated_at: now,
            })
        }
    }
}

async fn create_project_handler(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Json(input): Json<CreateProjectInput>,
) -> Result<Json<ProjectResponse>, CkwError> {
    let actor = actor_from_headers(&state, &headers, true).await?;
    Ok(Json(
        create_project_for_actor(&state, &actor, &input.name, &input.municipality, default_config()).await?,
    ))
}

async fn registered_project_response(
    state: &CkwState,
    user_id: Uuid,
    project_id: Uuid,
) -> Result<ProjectResponse, CkwError> {
    let row = sqlx::query(
        r#"
        select p.id, p.name_ciphertext, p.municipality_ciphertext, p.config_ciphertext,
          p.created_at, p.updated_at,
          (select count(*) from ckw_project_files f where f.project_id = p.id) as file_count,
          (select count(*) from ckw_results r where r.project_id = p.id) as result_count
        from ckw_projects p
        where p.id = $1 and p.user_id = $2
        "#,
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(&state.db)
    .await?
    .ok_or(CkwError::NotFound)?;
    Ok(ProjectResponse {
        project_id: project_id.to_string(),
        name: state.crypto.decrypt_text(row.try_get("name_ciphertext").map_err(|_| CkwError::Internal)?)?,
        municipality: state
            .crypto
            .decrypt_text(row.try_get("municipality_ciphertext").map_err(|_| CkwError::Internal)?)?,
        config: state.crypto.decrypt_json(row.try_get("config_ciphertext").map_err(|_| CkwError::Internal)?)?,
        storage_mode: "registered",
        file_count: row.try_get("file_count").map_err(|_| CkwError::Internal)?,
        result_count: row.try_get("result_count").map_err(|_| CkwError::Internal)?,
        created_at: row.try_get("created_at").map_err(|_| CkwError::Internal)?,
        updated_at: row.try_get("updated_at").map_err(|_| CkwError::Internal)?,
    })
}

fn guest_project_response(project: &GuestProject) -> ProjectResponse {
    ProjectResponse {
        project_id: project.id.to_string(),
        name: project.name.clone(),
        municipality: project.municipality.clone(),
        config: project.config.clone(),
        storage_mode: "guest",
        file_count: project.files.len() as i64,
        result_count: project.results.len() as i64,
        created_at: project.created_at,
        updated_at: project.updated_at,
    }
}

async fn list_projects(
    State(state): State<CkwState>,
    headers: HeaderMap,
) -> Result<Json<Vec<ProjectResponse>>, CkwError> {
    let actor = actor_from_headers(&state, &headers, false).await?;
    let projects = match actor {
        Actor::Registered { user_id } => {
            let ids = sqlx::query_scalar::<_, Uuid>(
                "select id from ckw_projects where user_id = $1 order by updated_at desc",
            )
            .bind(user_id)
            .fetch_all(&state.db)
            .await?;
            let mut projects = Vec::with_capacity(ids.len());
            for id in ids {
                projects.push(registered_project_response(&state, user_id, id).await?);
            }
            projects
        }
        Actor::Guest { session_key } => {
            let guests = state.guests.lock().await;
            let session = guests.get(&session_key).ok_or(CkwError::Unauthorized)?;
            let mut projects = session.projects.values().map(guest_project_response).collect::<Vec<_>>();
            projects.sort_by_key(|project| std::cmp::Reverse(project.updated_at));
            projects
        }
    };
    Ok(Json(projects))
}

fn parse_uuid(value: &str) -> Result<Uuid, CkwError> {
    Uuid::parse_str(value).map_err(|_| CkwError::NotFound)
}

async fn get_project_handler(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
) -> Result<Json<ProjectResponse>, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, false).await?;
    let project = match actor {
        Actor::Registered { user_id } => registered_project_response(&state, user_id, project_id).await?,
        Actor::Guest { session_key } => {
            let guests = state.guests.lock().await;
            let session = guests.get(&session_key).ok_or(CkwError::Unauthorized)?;
            guest_project_response(session.projects.get(&project_id).ok_or(CkwError::NotFound)?)
        }
    };
    Ok(Json(project))
}

async fn patch_project_handler(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
    Json(input): Json<PatchProjectInput>,
) -> Result<Json<ProjectResponse>, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let existing = project_response_for_actor(&state, &actor, project_id).await?;
    let name = input.name.unwrap_or(existing.name);
    let municipality = input.municipality.unwrap_or(existing.municipality);
    let config = input.config.unwrap_or(existing.config);
    let (name, municipality) = validate_project_input(&name, &municipality)?;
    if serde_json::to_vec(&config).map_err(|_| CkwError::BadRequest("invalid config".to_string()))?.len() > 64 * 1024 {
        return Err(CkwError::PayloadTooLarge);
    }
    update_project_values(&state, &actor, project_id, &name, &municipality, &config).await?;
    Ok(Json(project_response_for_actor(&state, &actor, project_id).await?))
}

async fn project_response_for_actor(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
) -> Result<ProjectResponse, CkwError> {
    match actor {
        Actor::Registered { user_id } => registered_project_response(state, *user_id, project_id).await,
        Actor::Guest { session_key } => {
            let guests = state.guests.lock().await;
            let session = guests.get(session_key).ok_or(CkwError::Unauthorized)?;
            Ok(guest_project_response(
                session.projects.get(&project_id).ok_or(CkwError::NotFound)?,
            ))
        }
    }
}

async fn update_project_values(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
    name: &str,
    municipality: &str,
    config: &Value,
) -> Result<(), CkwError> {
    match actor {
        Actor::Registered { user_id } => {
            let result = sqlx::query(
                r#"
                update ckw_projects set name_ciphertext=$1, municipality_ciphertext=$2,
                  config_ciphertext=$3, config_schema_version=$4, updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
                where id=$5 and user_id=$6
                "#,
            )
            .bind(state.crypto.encrypt_text(name)?)
            .bind(state.crypto.encrypt_text(municipality)?)
            .bind(state.crypto.encrypt_json(config)?)
            .bind(CONFIG_SCHEMA_VERSION)
            .bind(project_id)
            .bind(user_id)
            .execute(&state.db)
            .await?;
            if result.rows_affected() == 0 {
                return Err(CkwError::NotFound);
            }
        }
        Actor::Guest { session_key } => {
            let mut guests = state.guests.lock().await;
            let session = guests.get_mut(session_key).ok_or(CkwError::Unauthorized)?;
            let project = session.projects.get_mut(&project_id).ok_or(CkwError::NotFound)?;
            project.name = name.to_string();
            project.municipality = municipality.to_string();
            project.config = config.clone();
            project.updated_at = Utc::now();
        }
    }
    Ok(())
}

async fn delete_project_handler(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
) -> Result<Json<Value>, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    delete_project_for_actor(&state, &actor, project_id).await?;
    Ok(Json(json!({ "ok": true, "project_id": project_id })))
}

async fn delete_project_for_actor(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
) -> Result<(), CkwError> {
    match actor {
        Actor::Registered { user_id } => {
            let mut transaction = state.db.begin().await?;
            let exists = sqlx::query_scalar::<_, bool>(
                "select exists(select 1 from ckw_projects where id=$1 and user_id=$2)",
            )
            .bind(project_id)
            .bind(user_id)
            .fetch_one(&mut *transaction)
            .await?;
            if !exists {
                return Err(CkwError::NotFound);
            }
            sqlx::query(
                r#"
                insert into storage_deletion_outbox (object_bucket, object_key)
                select object_bucket, object_key from ckw_project_files where project_id=$1
                union all
                select object_bucket, object_key from ckw_results where project_id=$1
                on conflict (object_bucket, object_key) do nothing
                "#,
            )
            .bind(project_id)
            .execute(&mut *transaction)
            .await?;
            sqlx::query("delete from ckw_projects where id=$1 and user_id=$2")
                .bind(project_id)
                .bind(user_id)
                .execute(&mut *transaction)
                .await?;
            transaction.commit().await?;
        }
        Actor::Guest { session_key } => {
            let project = {
                let mut guests = state.guests.lock().await;
                let session = guests.get_mut(session_key).ok_or(CkwError::Unauthorized)?;
                session.projects.remove(&project_id).ok_or(CkwError::NotFound)?
            };
            for item in project.files.iter().map(|item| &item.path).chain(project.results.iter().map(|item| &item.path)) {
                let _ = tokio::fs::remove_file(item).await;
            }
        }
    }
    Ok(())
}

async fn registered_usage(state: &CkwState, user_id: Uuid) -> Result<i64, CkwError> {
    sqlx::query_scalar::<_, i64>(
        r#"
        select coalesce((select sum(f.byte_size) from ckw_project_files f join ckw_projects p on p.id=f.project_id where p.user_id=$1),0)
             + coalesce((select sum(r.byte_size) from ckw_results r join ckw_projects p on p.id=r.project_id where p.user_id=$1),0)
        "#,
    )
    .bind(user_id)
    .fetch_one(&state.db)
    .await
    .map_err(Into::into)
}

fn guest_usage(session: &GuestSession) -> usize {
    session
        .projects
        .values()
        .map(|project| {
            project.files.iter().map(|file| file.byte_size).sum::<usize>()
                + project.results.iter().map(|result| result.byte_size).sum::<usize>()
        })
        .sum()
}

async fn upload_solar_files(
    state: State<CkwState>,
    headers: HeaderMap,
    path: Path<String>,
    multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    upload_compat_files(state, headers, path, "solar", multipart).await
}

async fn upload_consumption_files(
    state: State<CkwState>,
    headers: HeaderMap,
    path: Path<String>,
    multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    upload_compat_files(state, headers, path, "consumption", multipart).await
}

async fn upload_zev_files(
    state: State<CkwState>,
    headers: HeaderMap,
    path: Path<String>,
    multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    upload_compat_files(state, headers, path, "zev", multipart).await
}

async fn upload_compat_files(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
    upload_kind: &str,
    multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    upload_files_inner(&state, headers, parse_uuid(&project_id)?, upload_kind, multipart).await
}

async fn upload_files(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, upload_kind)): Path<(String, String)>,
    multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    upload_files_inner(&state, headers, parse_uuid(&project_id)?, &upload_kind, multipart).await
}

async fn upload_files_inner(
    state: &CkwState,
    headers: HeaderMap,
    project_id: Uuid,
    upload_kind: &str,
    mut multipart: Multipart,
) -> Result<Json<Vec<Value>>, CkwError> {
    if !matches!(upload_kind, "solar" | "consumption" | "zev" | "billing") {
        return Err(CkwError::BadRequest("unsupported upload kind".to_string()));
    }
    let actor = actor_from_headers(state, &headers, true).await?;
    let _ = project_response_for_actor(state, &actor, project_id).await?;
    let mut uploaded = Vec::new();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| CkwError::BadRequest("invalid multipart request".to_string()))?
    {
        if !matches!(field.name(), Some("files") | Some("file") | Some("csv_file")) {
            continue;
        }
        let filename = sanitize_filename(field.file_name().unwrap_or("upload.csv"));
        let media_type = field.content_type().unwrap_or("text/csv").to_string();
        let bytes = field
            .bytes()
            .await
            .map_err(|_| CkwError::BadRequest("could not read upload".to_string()))?;
        if bytes.is_empty() {
            return Err(CkwError::BadRequest("uploaded file is empty".to_string()));
        }
        if bytes.len() > state.max_file_bytes {
            return Err(CkwError::PayloadTooLarge);
        }
        let (file_id, duplicate) = store_project_file(
            state,
            &actor,
            project_id,
            &filename,
            upload_kind,
            &media_type,
            bytes.to_vec(),
        )
        .await?;
        uploaded.push(json!({
            "file_id": file_id,
            "original_filename": filename,
            "upload_kind": upload_kind,
            "flow_kind": upload_kind,
            "rows_inserted": 0,
            "total_kwh": 0.0,
            "warnings": [],
            "duplicate": duplicate
        }));
    }
    if uploaded.is_empty() {
        return Err(CkwError::BadRequest("missing file field".to_string()));
    }
    Ok(Json(uploaded))
}

fn sanitize_filename(value: &str) -> String {
    let normalized = value
        .chars()
        .map(|character| {
            if matches!(character, '/' | '\\' | '\0') {
                '_'
            } else {
                character
            }
        })
        .take(180)
        .collect::<String>()
        .trim()
        .to_string();
    if normalized.is_empty() {
        "upload.csv".to_string()
    } else {
        normalized
    }
}

async fn store_project_file(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
    filename: &str,
    upload_kind: &str,
    media_type: &str,
    bytes: Vec<u8>,
) -> Result<(Uuid, bool), CkwError> {
    let digest = content_digest(&bytes);
    match actor {
        Actor::Registered { user_id } => {
            let existing = sqlx::query_scalar::<_, Uuid>(
                r#"
                select f.id from ckw_project_files f
                join ckw_projects p on p.id=f.project_id
                where f.project_id=$1 and p.user_id=$2 and f.sha256=$3 and f.upload_kind=$4
                limit 1
                "#,
            )
            .bind(project_id)
            .bind(user_id)
            .bind(&digest)
            .bind(upload_kind)
            .fetch_optional(&state.db)
            .await?;
            if let Some(id) = existing {
                return Ok((id, true));
            }
            let usage = registered_usage(state, *user_id).await?;
            if usage + bytes.len() as i64 > state.registered_max_bytes {
                return Err(CkwError::QuotaExceeded(
                    "registered storage quota exceeded; delete files or results before uploading".to_string(),
                ));
            }
            let id = Uuid::new_v4();
            let object_key = format!("users/{user_id}/ckw/projects/{project_id}/uploads/{id}");
            let encrypted = state.crypto.encrypt(&bytes)?;
            state.storage.put(&object_key, &encrypted).await.map_err(|_| CkwError::Internal)?;
            let inserted = sqlx::query(
                r#"
                insert into ckw_project_files (
                  id, project_id, original_filename_ciphertext, upload_kind, media_type,
                  byte_size, sha256, object_bucket, object_key
                )
                select $1,$2,$3,$4,$5,$6,$7,$8,$9
                where exists(select 1 from ckw_projects where id=$2 and user_id=$10)
                "#,
            )
            .bind(id)
            .bind(project_id)
            .bind(state.crypto.encrypt_text(filename)?)
            .bind(upload_kind)
            .bind(media_type)
            .bind(bytes.len() as i64)
            .bind(&digest)
            .bind(&state.bucket)
            .bind(&object_key)
            .bind(user_id)
            .execute(&state.db)
            .await;
            match inserted {
                Ok(result) if result.rows_affected() == 1 => Ok((id, false)),
                _ => {
                    let _ = state.storage.delete(&object_key).await;
                    Err(CkwError::NotFound)
                }
            }
        }
        Actor::Guest { session_key } => {
            let mut guests = state.guests.lock().await;
            let session = guests.get_mut(session_key).ok_or(CkwError::Unauthorized)?;
            let existing = session
                .projects
                .get(&project_id)
                .ok_or(CkwError::NotFound)?
                .files
                .iter()
                .find(|file| file.sha256 == digest && file.upload_kind == upload_kind)
                .map(|file| file.id);
            if let Some(id) = existing {
                return Ok((id, true));
            }
            if guest_usage(session) + bytes.len() > state.guest_max_bytes {
                return Err(CkwError::QuotaExceeded(
                    "guest session storage quota exceeded; delete a project or register to continue".to_string(),
                ));
            }
            let id = Uuid::new_v4();
            let path = state.guest_tmp_dir.join(session_key).join(format!("file-{id}"));
            tokio::fs::write(&path, &bytes).await.map_err(|_| CkwError::Internal)?;
            let project = session.projects.get_mut(&project_id).ok_or(CkwError::NotFound)?;
            project.files.push(GuestFile {
                id,
                original_filename: filename.to_string(),
                upload_kind: upload_kind.to_string(),
                media_type: media_type.to_string(),
                byte_size: bytes.len(),
                sha256: digest,
                path,
            });
            project.updated_at = Utc::now();
            Ok((id, false))
        }
    }
}

async fn delete_file_handler(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, file_id)): Path<(String, String)>,
) -> Result<Json<Value>, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let file_id = parse_uuid(&file_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    match actor {
        Actor::Registered { user_id } => {
            let mut transaction = state.db.begin().await?;
            let row = sqlx::query(
                r#"
                select f.object_bucket, f.object_key from ckw_project_files f
                join ckw_projects p on p.id=f.project_id
                where f.id=$1 and f.project_id=$2 and p.user_id=$3
                "#,
            )
            .bind(file_id)
            .bind(project_id)
            .bind(user_id)
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or(CkwError::NotFound)?;
            sqlx::query(
                "insert into storage_deletion_outbox (object_bucket, object_key) values ($1,$2) on conflict do nothing",
            )
            .bind(row.try_get::<String, _>("object_bucket").map_err(|_| CkwError::Internal)?)
            .bind(row.try_get::<String, _>("object_key").map_err(|_| CkwError::Internal)?)
            .execute(&mut *transaction)
            .await?;
            sqlx::query("delete from ckw_project_files where id=$1 and project_id=$2")
                .bind(file_id)
                .bind(project_id)
                .execute(&mut *transaction)
                .await?;
            transaction.commit().await?;
        }
        Actor::Guest { session_key } => {
            let removed = {
                let mut guests = state.guests.lock().await;
                let session = guests.get_mut(&session_key).ok_or(CkwError::Unauthorized)?;
                let project = session.projects.get_mut(&project_id).ok_or(CkwError::NotFound)?;
                let index = project.files.iter().position(|file| file.id == file_id).ok_or(CkwError::NotFound)?;
                project.updated_at = Utc::now();
                project.files.remove(index)
            };
            let _ = tokio::fs::remove_file(removed.path).await;
        }
    }
    Ok(Json(json!({ "ok": true, "file_id": file_id })))
}

async fn load_engine_project(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
) -> Result<EngineProject, CkwError> {
    let project = project_response_for_actor(state, actor, project_id).await?;
    let files = match actor {
        Actor::Registered { user_id } => {
            let rows = sqlx::query(
                r#"
                select f.id, f.original_filename_ciphertext, f.upload_kind, f.media_type,
                  f.object_bucket, f.object_key
                from ckw_project_files f join ckw_projects p on p.id=f.project_id
                where f.project_id=$1 and p.user_id=$2 order by f.created_at, f.id
                "#,
            )
            .bind(project_id)
            .bind(user_id)
            .fetch_all(&state.db)
            .await?;
            let mut files = Vec::with_capacity(rows.len());
            for row in rows {
                    let key: String = row.try_get("object_key").map_err(|_| CkwError::Internal)?;
                let encrypted = state.storage.get(&key).await.map_err(|_| CkwError::Internal)?;
                files.push(EngineFile {
                    id: row.try_get("id").map_err(|_| CkwError::Internal)?,
                    original_filename: state.crypto.decrypt_text(
                        row.try_get("original_filename_ciphertext").map_err(|_| CkwError::Internal)?,
                    )?,
                    upload_kind: row.try_get("upload_kind").map_err(|_| CkwError::Internal)?,
                    media_type: row.try_get("media_type").map_err(|_| CkwError::Internal)?,
                    bytes: state.crypto.decrypt(&encrypted)?,
                });
            }
            files
        }
        Actor::Guest { session_key } => {
            let metadata = {
                let guests = state.guests.lock().await;
                let session = guests.get(session_key).ok_or(CkwError::Unauthorized)?;
                session.projects.get(&project_id).ok_or(CkwError::NotFound)?.files.clone()
            };
            let mut files = Vec::with_capacity(metadata.len());
            for file in metadata {
                files.push(EngineFile {
                    id: file.id,
                    original_filename: file.original_filename,
                    upload_kind: file.upload_kind,
                    media_type: file.media_type,
                    bytes: tokio::fs::read(file.path).await.map_err(|_| CkwError::Internal)?,
                });
            }
            files
        }
    };
    Ok(EngineProject {
        id: project_id,
        name: project.name,
        municipality: project.municipality,
        config: project.config,
        files,
    })
}

async fn execute_engine(
    state: &CkwState,
    project: &EngineProject,
    operation: &str,
    parameters: Value,
) -> Result<EngineResponse, CkwError> {
    let mut parameters = parameters.as_object().cloned().unwrap_or_default();
    if let Some(leg) = project.config.get("leg").and_then(Value::as_object) {
        if !parameters.contains_key("networkDiscount") {
            if let Some(value) = leg.get("networkDiscount") {
                parameters.insert("networkDiscount".to_string(), value.clone());
            }
        }
        if !parameters.contains_key("legEnergyPriceRpPerKwh") {
            if let Some(value) = leg.get("energyPriceRpPerKwh") {
                parameters.insert("legEnergyPriceRpPerKwh".to_string(), value.clone());
            }
        }
    }
    if let Some(vzev) = project.config.get("vzev").and_then(Value::as_object) {
        let fixed_mode = vzev.get("tariffMode").and_then(Value::as_str) == Some("fixed");
        if fixed_mode && !parameters.contains_key("fixedPvTariffRpPerKwh") {
            if let Some(value) = vzev.get("fixedPvTariffRpPerKwh") {
                parameters.insert("fixedPvTariffRpPerKwh".to_string(), value.clone());
            }
        }
    }
    let manifest = json!({
        "operation": operation,
        "project": {
            "id": project.id,
            "name": project.name,
            "municipality": project.municipality
        },
        "files": project.files.iter().map(|file| json!({
            "id": file.id,
            "original_filename": file.original_filename,
            "upload_kind": file.upload_kind
        })).collect::<Vec<_>>(),
        "config": project.config,
        "parameters": Value::Object(parameters)
    });
    let mut form = reqwest::multipart::Form::new().text("manifest", manifest.to_string());
    for file in &project.files {
        let part = reqwest::multipart::Part::bytes(file.bytes.clone())
            .file_name(file.original_filename.clone())
            .mime_str(&file.media_type)
            .map_err(|_| CkwError::BadRequest("invalid upload media type".to_string()))?;
        form = form.part("files", part);
    }
    let response = state
        .http
        .post(format!("{}/internal/v1/execute", state.engine_url))
        .bearer_auth(&state.engine_token)
        .multipart(form)
        .send()
        .await
        .map_err(|_| CkwError::Dependency("calculation engine unavailable".to_string()))?;
    let status = response.status();
    let media_type = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("application/octet-stream")
        .to_string();
    let filename = response
        .headers()
        .get("x-artifact-filename")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let invoice_count = response
        .headers()
        .get("x-invoice-count")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let period_count = response
        .headers()
        .get("x-period-count")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let body = response.bytes().await.map_err(|_| CkwError::Internal)?.to_vec();
    if !status.is_success() {
        let detail = serde_json::from_slice::<Value>(&body)
            .ok()
            .and_then(|value| value.get("detail").and_then(Value::as_str).map(ToOwned::to_owned));
        return if status.is_client_error() {
            Err(CkwError::BadRequest(detail.unwrap_or_else(|| "calculation request was rejected".to_string())))
        } else {
            Err(CkwError::Dependency("calculation failed".to_string()))
        };
    }
    Ok(EngineResponse {
        body,
        media_type,
        filename,
        invoice_count,
        period_count,
    })
}

fn query_parameters(pairs: Vec<(String, String)>) -> Value {
    let mut parameters = Map::new();
    for (key, value) in pairs {
        let key = match key.as_str() {
            "tariff_year" => "tariffYear",
            "network_discount" => "networkDiscount",
            "leg_energy_price_rp_per_kwh" => "legEnergyPriceRpPerKwh",
            "solar_file_id" => "solarFileIds",
            "consumption_file_id" => "consumptionFileId",
            "fixed_pv_tariff_rp_per_kwh" => "fixedPvTariffRpPerKwh",
            "load_shifting_year" => "loadShiftingYear",
            other => other,
        };
        if key == "solarFileIds" {
            parameters
                .entry(key.to_string())
                .or_insert_with(|| Value::Array(Vec::new()))
                .as_array_mut()
                .expect("array was inserted")
                .push(Value::String(value));
        } else {
            parameters.insert(key.to_string(), Value::String(value));
        }
    }
    Value::Object(parameters)
}

fn engine_operation_name(compatibility_name: &str) -> Option<&'static str> {
    match compatibility_name {
        "summary" => Some("summary"),
        "timeseries" => Some("timeseries"),
        "plants" => Some("plants"),
        "calculation" => Some("calculation"),
        "residual-solar" => Some("residual-solar"),
        "optimization" => Some("optimization"),
        "battery-placements" => Some("battery-placements"),
        "load-shifting" => Some("load-shifting"),
        "vzev-simulation" => Some("vzev-simulation"),
        "vzev-simulation.csv" => Some("vzev-simulation-csv"),
        "vzev-tariff-comparison.csv" => Some("vzev-tariff-comparison-csv"),
        "word-report" => Some("leg-word-report"),
        "vzev-word-report" => Some("vzev-word-report"),
        "excel-export" => Some("excel-export"),
        "billing-invoices" => Some("billing-invoices"),
        _ => None,
    }
}

fn operation_creates_artifact(operation: &str) -> bool {
    matches!(
        operation,
        "vzev-simulation-csv"
            | "vzev-tariff-comparison-csv"
            | "leg-word-report"
            | "vzev-word-report"
            | "excel-export"
            | "billing-invoices"
    )
}

async fn compatibility_operation(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, operation)): Path<(String, String)>,
    Query(pairs): Query<Vec<(String, String)>>,
) -> Result<Response, CkwError> {
    let operation = engine_operation_name(&operation).ok_or(CkwError::NotFound)?;
    run_operation(
        &state,
        headers,
        parse_uuid(&project_id)?,
        operation,
        query_parameters(pairs),
    )
    .await
}

async fn versioned_operation(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, operation)): Path<(String, String)>,
    Json(input): Json<OperationInput>,
) -> Result<Response, CkwError> {
    let operation = engine_operation_name(&operation).ok_or(CkwError::NotFound)?;
    run_operation(&state, headers, parse_uuid(&project_id)?, operation, input.parameters).await
}

async fn run_operation(
    state: &CkwState,
    headers: HeaderMap,
    project_id: Uuid,
    operation: &str,
    parameters: Value,
) -> Result<Response, CkwError> {
    let actor = actor_from_headers(state, &headers, operation_creates_artifact(operation)).await?;
    let project = load_engine_project(state, &actor, project_id).await?;
    let engine = execute_engine(state, &project, operation, parameters).await?;
    if engine.filename.is_none() {
        return Ok(json_bytes_response(engine.body));
    }
    let filename = engine.filename.clone().unwrap_or_else(|| "result.bin".to_string());
    let result_id = store_result(
        state,
        &actor,
        project_id,
        operation,
        &filename,
        &engine.media_type,
        &engine.body,
    )
    .await?;
    Ok(binary_download_response(engine, result_id))
}

fn json_bytes_response(bytes: Vec<u8>) -> Response {
    Response::builder()
        .status(StatusCode::OK)
        .header(CONTENT_TYPE, "application/json")
        .header(CACHE_CONTROL, "no-store")
        .body(Body::from(bytes))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

fn binary_download_response(engine: EngineResponse, result_id: Uuid) -> Response {
    let filename = engine.filename.unwrap_or_else(|| "result.bin".to_string());
    let mut builder = Response::builder()
        .status(StatusCode::OK)
        .header(CONTENT_TYPE, engine.media_type)
        .header(CACHE_CONTROL, "private, no-store")
        .header(CONTENT_DISPOSITION, format!("attachment; filename=\"{}\"", sanitize_filename(&filename)))
        .header("x-result-id", result_id.to_string());
    if let Some(value) = engine.invoice_count {
        builder = builder.header("x-invoice-count", value);
    }
    if let Some(value) = engine.period_count {
        builder = builder.header("x-period-count", value);
    }
    builder
        .body(Body::from(engine.body))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

async fn store_result(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
    operation: &str,
    filename: &str,
    media_type: &str,
    bytes: &[u8],
) -> Result<Uuid, CkwError> {
    let id = Uuid::new_v4();
    match actor {
        Actor::Registered { user_id } => {
            let digest = content_digest(bytes);
            if registered_usage(state, *user_id).await? + bytes.len() as i64 > state.registered_max_bytes {
                return Err(CkwError::QuotaExceeded(
                    "registered storage quota exceeded; delete files or results before generating another artifact"
                        .to_string(),
                ));
            }
            let object_key = format!("users/{user_id}/ckw/projects/{project_id}/results/{id}");
            state.storage.put(&object_key, &state.crypto.encrypt(bytes)?).await.map_err(|_| CkwError::Internal)?;
            let inserted = sqlx::query(
                r#"
                insert into ckw_results (
                  id, project_id, operation, filename_ciphertext, media_type, byte_size,
                  sha256, object_bucket, object_key
                )
                select $1,$2,$3,$4,$5,$6,$7,$8,$9
                where exists(select 1 from ckw_projects where id=$2 and user_id=$10)
                "#,
            )
            .bind(id)
            .bind(project_id)
            .bind(operation)
            .bind(state.crypto.encrypt_text(filename)?)
            .bind(media_type)
            .bind(bytes.len() as i64)
            .bind(digest)
            .bind(&state.bucket)
            .bind(&object_key)
            .bind(user_id)
            .execute(&state.db)
            .await;
            match inserted {
                Ok(result) if result.rows_affected() == 1 => Ok(id),
                _ => {
                    let _ = state.storage.delete(&object_key).await;
                    Err(CkwError::NotFound)
                }
            }
        }
        Actor::Guest { session_key } => {
            let mut guests = state.guests.lock().await;
            let session = guests.get_mut(session_key).ok_or(CkwError::Unauthorized)?;
            if guest_usage(session) + bytes.len() > state.guest_max_bytes {
                return Err(CkwError::QuotaExceeded(
                    "guest session storage quota exceeded; register or delete a project before generating another artifact"
                        .to_string(),
                ));
            }
            let path = state.guest_tmp_dir.join(session_key).join(format!("result-{id}"));
            tokio::fs::write(&path, bytes).await.map_err(|_| CkwError::Internal)?;
            let project = session.projects.get_mut(&project_id).ok_or(CkwError::NotFound)?;
            project.results.push(GuestResult {
                id,
                operation: operation.to_string(),
                filename: filename.to_string(),
                media_type: media_type.to_string(),
                byte_size: bytes.len(),
                path,
                created_at: Utc::now(),
            });
            project.updated_at = Utc::now();
            Ok(id)
        }
    }
}

async fn list_results(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
) -> Result<Json<Vec<ResultResponse>>, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, false).await?;
    let results = match actor {
        Actor::Registered { user_id } => {
            let rows = sqlx::query(
                r#"
                select r.id, r.operation, r.filename_ciphertext, r.media_type, r.byte_size, r.created_at
                from ckw_results r join ckw_projects p on p.id=r.project_id
                where r.project_id=$1 and p.user_id=$2 order by r.created_at desc
                "#,
            )
            .bind(project_id)
            .bind(user_id)
            .fetch_all(&state.db)
            .await?;
            rows.into_iter()
                .map(|row| {
                    let id: Uuid = row.try_get("id").map_err(|_| CkwError::Internal)?;
                    Ok(ResultResponse {
                        result_id: id.to_string(),
                        project_id: project_id.to_string(),
                        operation: row.try_get("operation").map_err(|_| CkwError::Internal)?,
                        filename: state.crypto.decrypt_text(
                            row.try_get("filename_ciphertext").map_err(|_| CkwError::Internal)?,
                        )?,
                        media_type: row.try_get("media_type").map_err(|_| CkwError::Internal)?,
                        byte_size: row.try_get("byte_size").map_err(|_| CkwError::Internal)?,
                        created_at: row.try_get("created_at").map_err(|_| CkwError::Internal)?,
                        download_url: format!("/v1/ckw/projects/{project_id}/results/{id}/download"),
                    })
                })
                .collect::<Result<Vec<_>, CkwError>>()?
        }
        Actor::Guest { session_key } => {
            let guests = state.guests.lock().await;
            let session = guests.get(&session_key).ok_or(CkwError::Unauthorized)?;
            let project = session.projects.get(&project_id).ok_or(CkwError::NotFound)?;
            let mut results = project
                .results
                .iter()
                .map(|result| ResultResponse {
                    result_id: result.id.to_string(),
                    project_id: project_id.to_string(),
                    operation: result.operation.clone(),
                    filename: result.filename.clone(),
                    media_type: result.media_type.clone(),
                    byte_size: result.byte_size as i64,
                    created_at: result.created_at,
                    download_url: format!("/v1/ckw/projects/{project_id}/results/{}/download", result.id),
                })
                .collect::<Vec<_>>();
            results.sort_by_key(|result| std::cmp::Reverse(result.created_at));
            results
        }
    };
    Ok(Json(results))
}

async fn download_result(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, result_id)): Path<(String, String)>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let result_id = parse_uuid(&result_id)?;
    let actor = actor_from_headers(&state, &headers, false).await?;
    let (filename, media_type, bytes) = match actor {
        Actor::Registered { user_id } => {
            let row = sqlx::query(
                r#"
                select r.filename_ciphertext, r.media_type, r.object_bucket, r.object_key
                from ckw_results r join ckw_projects p on p.id=r.project_id
                where r.id=$1 and r.project_id=$2 and p.user_id=$3
                "#,
            )
            .bind(result_id)
            .bind(project_id)
            .bind(user_id)
            .fetch_optional(&state.db)
            .await?
            .ok_or(CkwError::NotFound)?;
            let key: String = row.try_get("object_key").map_err(|_| CkwError::Internal)?;
            let encrypted = state.storage.get(&key).await.map_err(|_| CkwError::Internal)?;
            (
                state.crypto.decrypt_text(
                    row.try_get("filename_ciphertext").map_err(|_| CkwError::Internal)?,
                )?,
                row.try_get("media_type").map_err(|_| CkwError::Internal)?,
                state.crypto.decrypt(&encrypted)?,
            )
        }
        Actor::Guest { session_key } => {
            let result = {
                let guests = state.guests.lock().await;
                let session = guests.get(&session_key).ok_or(CkwError::Unauthorized)?;
                session
                    .projects
                    .get(&project_id)
                    .ok_or(CkwError::NotFound)?
                    .results
                    .iter()
                    .find(|result| result.id == result_id)
                    .cloned()
                    .ok_or(CkwError::NotFound)?
            };
            (
                result.filename,
                result.media_type,
                tokio::fs::read(result.path).await.map_err(|_| CkwError::Internal)?,
            )
        }
    };
    Ok(Response::builder()
        .status(StatusCode::OK)
        .header(CONTENT_TYPE, media_type)
        .header(CACHE_CONTROL, "private, no-store")
        .header(CONTENT_DISPOSITION, format!("attachment; filename=\"{}\"", sanitize_filename(&filename)))
        .body(Body::from(bytes))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()))
}

async fn mutate_config_and_get_plants(
    state: &CkwState,
    actor: &Actor,
    project_id: Uuid,
    config: Value,
) -> Result<Response, CkwError> {
    let current = project_response_for_actor(state, actor, project_id).await?;
    let candidate = EngineProject {
        config: config.clone(),
        ..load_engine_project(state, actor, project_id).await?
    };
    let engine = execute_engine(state, &candidate, "plants", json!({})).await?;
    update_project_values(state, actor, project_id, &current.name, &current.municipality, &config).await?;
    Ok(json_bytes_response(engine.body))
}

async fn adopt_automatic_plants(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let initial = execute_engine(&state, &project, "plants", json!({})).await?;
    let payload: Value = serde_json::from_slice(&initial.body).map_err(|_| CkwError::Internal)?;
    let plants = payload
        .get("automatic_matches")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|plant| {
            Some(json!({
                "id": Uuid::new_v4(),
                "source": "cache",
                "cacheId": plant.get("cache_id")?.as_str()?
            }))
        })
        .collect::<Vec<_>>();
    let mut config = project.config;
    config["plantSelection"] = json!({ "mode": "curated", "plants": plants });
    mutate_config_and_get_plants(&state, &actor, project_id, config).await
}

async fn add_cached_plant(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
    Json(input): Json<CachedPlantInput>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let mut config = project.config;
    let selection = config.as_object_mut().ok_or(CkwError::Internal)?
        .entry("plantSelection").or_insert_with(|| json!({"mode":"curated","plants":[]}));
    selection["mode"] = Value::String("curated".to_string());
    let plants = selection.as_object_mut().ok_or(CkwError::Internal)?
        .entry("plants").or_insert_with(|| Value::Array(Vec::new()));
    let array = plants.as_array_mut().ok_or(CkwError::Internal)?;
    if array.iter().any(|plant| plant.get("cacheId").and_then(Value::as_str) == Some(&input.cache_id)) {
        return Err(CkwError::BadRequest("PV plant is already selected".to_string()));
    }
    array.push(json!({ "id": Uuid::new_v4(), "source":"cache", "cacheId": input.cache_id }));
    mutate_config_and_get_plants(&state, &actor, project_id, config).await
}

async fn add_manual_plant(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
    Json(input): Json<ManualPlantInput>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let mut config = project.config;
    let selection = config.as_object_mut().ok_or(CkwError::Internal)?
        .entry("plantSelection").or_insert_with(|| json!({"mode":"curated","plants":[]}));
    selection["mode"] = Value::String("curated".to_string());
    selection.as_object_mut().ok_or(CkwError::Internal)?
        .entry("plants").or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut().ok_or(CkwError::Internal)?
        .push(json!({
            "id": Uuid::new_v4(),
            "source":"manual",
            "address": input.address,
            "installedKwp": input.installed_kwp
        }));
    mutate_config_and_get_plants(&state, &actor, project_id, config).await
}

async fn clear_plant_selection(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path(project_id): Path<String>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let mut config = project.config;
    config["plantSelection"] = json!({ "mode":"automatic", "plants":[] });
    mutate_config_and_get_plants(&state, &actor, project_id, config).await
}

async fn delete_selected_plant(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, plant_id)): Path<(String, String)>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let mut config = project.config;
    let plants = config
        .get_mut("plantSelection")
        .and_then(|value| value.get_mut("plants"))
        .and_then(Value::as_array_mut)
        .ok_or(CkwError::NotFound)?;
    let before = plants.len();
    plants.retain(|plant| plant.get("id").and_then(Value::as_str) != Some(&plant_id));
    if plants.len() == before {
        return Err(CkwError::NotFound);
    }
    mutate_config_and_get_plants(&state, &actor, project_id, config).await
}

async fn update_battery_consumers(
    State(state): State<CkwState>,
    headers: HeaderMap,
    Path((project_id, solar_file_id)): Path<(String, String)>,
    Json(input): Json<BatteryConsumersInput>,
) -> Result<Response, CkwError> {
    let project_id = parse_uuid(&project_id)?;
    let actor = actor_from_headers(&state, &headers, true).await?;
    let project = load_engine_project(&state, &actor, project_id).await?;
    let mut config = project.config;
    let consumers = config.as_object_mut().ok_or(CkwError::Internal)?
        .entry("batteryConsumers").or_insert_with(|| Value::Object(Map::new()));
    consumers.as_object_mut().ok_or(CkwError::Internal)?.insert(
        solar_file_id,
        Value::Array(input.consumer_file_ids.into_iter().map(Value::String).collect()),
    );
    let candidate = EngineProject {
        config: config.clone(),
        ..load_engine_project(&state, &actor, project_id).await?
    };
    let engine = execute_engine(&state, &candidate, "battery-placements", json!({})).await?;
    let current = project_response_for_actor(&state, &actor, project_id).await?;
    update_project_values(&state, &actor, project_id, &current.name, &current.municipality, &config).await?;
    Ok(json_bytes_response(engine.body))
}

async fn billing_invoices(
    State(state): State<CkwState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Response, CkwError> {
    let actor = actor_from_headers(&state, &headers, true).await?;
    let mut uploaded = Vec::<(String, String, Vec<u8>)>::new();
    let mut parameters = Map::new();
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|_| CkwError::BadRequest("invalid multipart request".to_string()))?
    {
        let field_name = field.name().unwrap_or_default().to_string();
        if field_name == "csv_file" {
            let filename = sanitize_filename(field.file_name().unwrap_or("vzev.csv"));
            let media_type = field.content_type().unwrap_or("text/csv").to_string();
            let bytes = field.bytes().await.map_err(|_| CkwError::BadRequest("could not read upload".to_string()))?;
            if bytes.is_empty() || bytes.len() > state.max_file_bytes {
                return Err(CkwError::PayloadTooLarge);
            }
            uploaded.push((filename, media_type, bytes.to_vec()));
        } else {
            let value = field.text().await.map_err(|_| CkwError::BadRequest("invalid form value".to_string()))?;
            let key = billing_parameter_name(&field_name);
            let parsed = match value.as_str() {
                "true" => Value::Bool(true),
                "false" => Value::Bool(false),
                _ => Value::String(value),
            };
            parameters.insert(key.to_string(), parsed);
        }
    }
    if uploaded.is_empty() {
        return Err(CkwError::BadRequest("at least one billing CSV is required".to_string()));
    }
    let invoice_date = parameters.get("invoiceDate").and_then(Value::as_str).unwrap_or("today");
    let project = create_project_for_actor(
        &state,
        &actor,
        &format!("vZEV-Abrechnung {invoice_date}"),
        "Sursee",
        json!({
            "schemaVersion": CONFIG_SCHEMA_VERSION,
            "leg": {}, "vzev": {}, "plantSelection": {"mode":"automatic","plants":[]},
            "batteryConsumers": {}, "billing": Value::Object(parameters.clone())
        }),
    )
    .await?;
    let project_id = parse_uuid(&project.project_id)?;
    let generated = async {
        for (filename, media_type, bytes) in uploaded {
            store_project_file(
                &state,
                &actor,
                project_id,
                &filename,
                "billing",
                &media_type,
                bytes,
            )
            .await?;
        }
        let engine_project = load_engine_project(&state, &actor, project_id).await?;
        let engine = execute_engine(
            &state,
            &engine_project,
            "billing-invoices",
            Value::Object(parameters),
        )
        .await?;
        let filename = engine.filename.clone().unwrap_or_else(|| "vzev-rechnungen.zip".to_string());
        let result_id = store_result(
            &state,
            &actor,
            project_id,
            "billing-invoices",
            &filename,
            &engine.media_type,
            &engine.body,
        )
        .await?;
        Ok::<_, CkwError>((engine, result_id))
    }
    .await;
    match generated {
        Ok((engine, result_id)) => Ok(binary_download_response(engine, result_id)),
        Err(error) => {
            if let Err(cleanup_error) = delete_project_for_actor(&state, &actor, project_id).await {
                tracing::error!(%cleanup_error, %project_id, "failed to roll back incomplete billing project");
            }
            Err(error)
        }
    }
}

fn billing_parameter_name(value: &str) -> &str {
    match value {
        "invoice_date" => "invoiceDate",
        "payment_terms" => "paymentTerms",
        "invoice_prefix" => "invoicePrefix",
        "tariff_tier" => "tariffTier",
        "local_energy_factor" => "localEnergyFactor",
        "pv_tariff_rp_per_kwh" => "pvTariffRpPerKwh",
        "pv_tariff_method" => "pvTariffMethod",
        "pv_tariff_note" => "pvTariffNote",
        "peak_allocation_mode" => "peakAllocationMode",
        "blindenergie_kvarh" => "blindenergieKvarh",
        "direct_meter_count" => "directMeterCount",
        "indirect_meter_count" => "indirectMeterCount",
        "virtual_meter_count" => "virtualMeterCount",
        "feed_in_energy_rp_per_kwh" => "feedInEnergyRpPerKwh",
        "feed_in_hkn_rp_per_kwh" => "feedInHknRpPerKwh",
        "vat_rate" => "vatRate",
        "no_vat" => "noVat",
        "exact_billing_basis" => "exactBillingBasis",
        other => other,
    }
}

async fn claim_guest_session(
    State(state): State<CkwState>,
    headers: HeaderMap,
) -> Result<Response, CkwError> {
    let user = state
        .auth
        .require_user(&headers)
        .await
        .map_err(|_| CkwError::Unauthorized)?;
    let user_id = upsert_user(&state.db, &user)
        .await
        .map_err(|_| CkwError::Internal)?;
    let token = cookie_value(&headers, &state.cookie_name).ok_or(CkwError::Unauthorized)?;
    let session_key = token_key(&token);
    let session = {
        let guests = state.guests.lock().await;
        let session = guests.get(&session_key).ok_or(CkwError::Unauthorized)?;
        let supplied = headers
            .get(CSRF_HEADER)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default();
        if supplied != session.csrf_token {
            return Err(CkwError::Unauthorized);
        }
        session.clone()
    };
    let total_bytes = guest_usage(&session) as i64;
    if registered_usage(&state, user_id).await? + total_bytes > state.registered_max_bytes {
        return Err(CkwError::QuotaExceeded(
            "registered storage quota is too small to claim this guest session".to_string(),
        ));
    }
    let registered_actor = Actor::Registered { user_id };
    let mut claimed = Vec::<Uuid>::new();
    let copied = async {
        for guest_project in session.projects.values() {
            let project = create_project_for_actor(
                &state,
                &registered_actor,
                &guest_project.name,
                &guest_project.municipality,
                guest_project.config.clone(),
            )
            .await?;
            let project_id = parse_uuid(&project.project_id)?;
            claimed.push(project_id);
            for file in &guest_project.files {
                let bytes = tokio::fs::read(&file.path).await.map_err(|_| CkwError::Internal)?;
                store_project_file(
                    &state,
                    &registered_actor,
                    project_id,
                    &file.original_filename,
                    &file.upload_kind,
                    &file.media_type,
                    bytes,
                )
                .await?;
            }
            for result in &guest_project.results {
                let bytes = tokio::fs::read(&result.path).await.map_err(|_| CkwError::Internal)?;
                store_result(
                    &state,
                    &registered_actor,
                    project_id,
                    &result.operation,
                    &result.filename,
                    &result.media_type,
                    &bytes,
                )
                .await?;
            }
        }
        Ok::<(), CkwError>(())
    }
    .await;
    if let Err(error) = copied {
        for project_id in &claimed {
            if let Err(cleanup_error) = delete_project_for_actor(&state, &registered_actor, *project_id).await {
                tracing::error!(%cleanup_error, %project_id, "failed to roll back incomplete guest claim");
            }
        }
        return Err(error);
    }
    state.guests.lock().await.remove(&session_key);
    let _ = tokio::fs::remove_dir_all(state.guest_tmp_dir.join(&session_key)).await;
    let mut response = Json(json!({
        "ok": true,
        "projectIds": claimed.into_iter().map(|id| id.to_string()).collect::<Vec<_>>()
    }))
    .into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_str(&guest_cookie(&state, "", 0)).map_err(|_| CkwError::Internal)?,
    );
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn persistent_test_state(root: &std::path::Path) -> CkwState {
        let db = sqlx::sqlite::SqlitePoolOptions::new().max_connections(1)
            .connect_with(sqlx::sqlite::SqliteConnectOptions::new()
                .filename(root.join("test.sqlite")).create_if_missing(true).foreign_keys(true)
                .journal_mode(sqlx::sqlite::SqliteJournalMode::Wal)).await.unwrap();
        sqlx::migrate!("./migrations").run(&db).await.unwrap();
        let config = crate::Config {
            database_url: String::new(), data_dir: root.into(), host: "127.0.0.1".into(), port: 0,
            cors_origins: vec![], max_csv_bytes: 1024,
            auth0_audience: "test".into(), auth0_issuer_base_url: "https://example.invalid/".into(),
        };
        let auth = AuthVerifier {
            config: Arc::new(config), http: reqwest::Client::new(),
            jwks: Arc::new(tokio::sync::RwLock::new(None)),
        };
        CkwState {
            db, storage: LocalStorage::new(root.join("objects")).unwrap(), auth,
            http: reqwest::Client::new(),
            crypto: DataCrypto::from_secret(&encryption_key(&root.join("secrets/key")).unwrap()).unwrap(),
            bucket: "local".into(), engine_url: "http://example.invalid".into(), engine_token: "test".into(),
            max_file_bytes: 1024, guest_max_bytes: 4096, registered_max_bytes: 4096,
            guest_idle: Duration::minutes(60), guest_max_age: Duration::hours(24),
            guest_tmp_dir: root.join("guests"), cookie_name: "test".into(), cookie_secure: true,
            guests: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    #[tokio::test]
    async fn sqlite_registered_storage_survives_restart_enforces_ownership_and_deletes() {
        let root = std::env::temp_dir().join(format!("communi-persistence-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let state = persistent_test_state(&root).await;
        let auth = crate::AuthUser { sub: "owner".into(), email: Some("owner@example.invalid".into()) };
        let owner = upsert_user(&state.db, &auth).await.unwrap();
        assert_eq!(owner, upsert_user(&state.db, &auth).await.unwrap());
        let other = upsert_user(&state.db, &crate::AuthUser { sub: "other".into(), email: None }).await.unwrap();
        let actor = Actor::Registered { user_id: owner };
        let project = create_project_for_actor(&state, &actor, "Private project", "Sursee", default_config()).await.unwrap();
        let id = Uuid::parse_str(&project.project_id).unwrap();
        let (file, duplicate) = store_project_file(&state, &actor, id, "private.csv", "solar", "text/csv", b"private CSV".to_vec()).await.unwrap();
        assert!(!duplicate);
        assert_eq!(store_project_file(&state, &actor, id, "private.csv", "solar", "text/csv", b"private CSV".to_vec()).await.unwrap(), (file, true));
        store_result(&state, &actor, id, "export", "private-result.csv", "text/csv", b"private result").await.unwrap();
        assert_eq!(registered_usage(&state, owner).await.unwrap(), 25);
        let key: String = sqlx::query_scalar("select object_key from ckw_project_files").fetch_one(&state.db).await.unwrap();
        assert_ne!(state.storage.get(&key).await.unwrap(), b"private CSV");
        assert!(matches!(load_engine_project(&state, &Actor::Registered { user_id: other }, id).await, Err(CkwError::NotFound)));
        assert!(matches!(delete_project_for_actor(&state, &Actor::Registered { user_id: other }, id).await, Err(CkwError::NotFound)));
        assert!(matches!(store_result(&state, &actor, id, "export", "oversize", "text/plain", &[0; 4096]).await, Err(CkwError::QuotaExceeded(_))));
        state.db.close().await;
        drop(state);
        let state = persistent_test_state(&root).await;
        let loaded = load_engine_project(&state, &actor, id).await.unwrap();
        assert_eq!(loaded.name, "Private project");
        assert_eq!(loaded.files[0].bytes, b"private CSV");
        let result_key: String = sqlx::query_scalar("select object_key from ckw_results").fetch_one(&state.db).await.unwrap();
        assert_eq!(state.crypto.decrypt(&state.storage.get(&result_key).await.unwrap()).unwrap(), b"private result");
        delete_project_for_actor(&state, &actor, id).await.unwrap();
        assert_eq!(state.process_deletion_outbox().await.unwrap(), 2);
        assert!(!root.join("objects").join(key).exists());
        assert!(!root.join("objects").join(result_key).exists());
        assert_eq!(registered_usage(&state, owner).await.unwrap(), 0);
        state.db.close().await;
        std::fs::remove_dir_all(root).unwrap();
    }

    #[tokio::test]
    async fn sqlite_deletion_outbox_retries_failed_local_deletions() {
        let root = std::env::temp_dir().join(format!("communi-outbox-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let state = persistent_test_state(&root).await;
        std::fs::create_dir(root.join("objects/blocked")).unwrap();
        sqlx::query("insert into storage_deletion_outbox (object_bucket,object_key) values ('local','blocked')").execute(&state.db).await.unwrap();
        assert_eq!(state.process_deletion_outbox().await.unwrap(), 0);
        let attempts: i32 = sqlx::query_scalar("select attempts from storage_deletion_outbox").fetch_one(&state.db).await.unwrap();
        assert_eq!(attempts, 1);
        std::fs::remove_dir(root.join("objects/blocked")).unwrap();
        sqlx::query("update storage_deletion_outbox set next_attempt_at='2000-01-01'").execute(&state.db).await.unwrap();
        assert_eq!(state.process_deletion_outbox().await.unwrap(), 1);
        state.db.close().await;
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn application_encryption_uses_random_nonces_and_round_trips() {
        let secret = general_purpose::STANDARD.encode([7_u8; 32]);
        let crypto = DataCrypto::from_secret(&secret).expect("valid test key");
        let first = crypto.encrypt(b"private project").expect("encryption succeeds");
        let second = crypto.encrypt(b"private project").expect("encryption succeeds");
        assert_ne!(first, second);
        assert_eq!(crypto.decrypt(&first).expect("decryption succeeds"), b"private project");
    }

    #[test]
    fn guest_tokens_are_stored_as_hashes() {
        let token = "opaque-browser-token";
        let key = token_key(token);
        assert_ne!(key, token);
        assert_eq!(key.len(), 64);
        assert!(is_guest_session_key(&key));
        assert!(!is_guest_session_key("not-a-session-directory"));
    }

    #[test]
    fn startup_removes_only_orphaned_guest_directories() {
        let root = std::env::temp_dir().join(format!("communi-guest-cleanup-{}", Uuid::new_v4()));
        let guest = root.join("a".repeat(64));
        let unrelated = root.join("keep-this-directory");
        std::fs::create_dir_all(&guest).expect("create guest directory");
        std::fs::create_dir_all(&unrelated).expect("create unrelated directory");

        clear_orphaned_guest_data(&root).expect("cleanup succeeds");

        assert!(!guest.exists());
        assert!(unrelated.exists());
        std::fs::remove_dir_all(root).expect("remove test root");
    }

    #[test]
    fn uploaded_filenames_cannot_escape_tmpfs() {
        assert_eq!(sanitize_filename("../../rechnung.csv"), ".._.._rechnung.csv");
        assert_eq!(sanitize_filename("a\\b.csv"), "a_b.csv");
    }

    #[test]
    fn default_vzev_tariff_remains_participant_cap_based() {
        assert_eq!(default_config()["vzev"]["tariffMode"], "participant_80_pct");
    }
}
