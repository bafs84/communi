//! Personal credentials only enter CKW handlers through a verified extension.
//! Auth0 remains the sole authority for issuing and revoking credentials.
use std::{convert::Infallible, ops::{Deref, DerefMut}};

use axum::{
    extract::{FromRequestParts, MatchedPath, Path, Request, State},
    http::{header::{AUTHORIZATION, CACHE_CONTROL}, request::Parts, HeaderMap, Method, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    routing::{delete, get},
    Json, Router,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use chrono::{DateTime, Duration, Utc};
use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool};
use uuid::Uuid;

use crate::{ckw::CkwError, upsert_user, AuthVerifier};

const PREFIX: &str = "communi_api_";
const SCOPES: &[&str] = &["read", "write", "calculate", "delete"];

#[derive(Clone)]
pub(crate) struct KeyState {
    pub db: SqlitePool,
    pub auth: AuthVerifier,
}

#[derive(Clone)]
pub(crate) struct KeyIdentity {
    pub user_id: Uuid,
    pub project_id: Option<Uuid>,
}

#[derive(Clone, Default)]
pub(crate) struct AccessHeaders {
    headers: HeaderMap,
    pub identity: Option<KeyIdentity>,
}

impl From<HeaderMap> for AccessHeaders {
    fn from(headers: HeaderMap) -> Self { Self { headers, identity: None } }
}
impl Deref for AccessHeaders {
    type Target = HeaderMap;
    fn deref(&self) -> &HeaderMap { &self.headers }
}
impl DerefMut for AccessHeaders {
    fn deref_mut(&mut self) -> &mut HeaderMap { &mut self.headers }
}

#[axum::async_trait]
impl<S: Send + Sync> FromRequestParts<S> for AccessHeaders {
    type Rejection = Infallible;
    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        Ok(Self {
            headers: parts.headers.clone(),
            identity: parts.extensions.get::<KeyIdentity>().cloned(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CreateKey {
    name: String,
    scopes: Vec<String>,
    #[serde(default = "default_days")]
    expires_in_days: i64,
    project_id: Option<Uuid>,
}
fn default_days() -> i64 { 30 }

#[derive(Serialize)]
pub(crate) struct KeyInfo {
    id: Uuid,
    name: String,
    token_prefix: String,
    scopes: Vec<String>,
    project_id: Option<Uuid>,
    created_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
    revoked_at: Option<DateTime<Utc>>,
    last_used_at: Option<DateTime<Utc>>,
}

#[derive(Serialize)]
pub(crate) struct IssuedKey {
    #[serde(flatten)]
    info: KeyInfo,
    token: String,
}

pub(crate) fn router(state: KeyState) -> Router {
    Router::new()
        .route("/v1/ckw/api-keys", get(list_keys).post(create_key))
        .route("/v1/ckw/api-keys/:key_id", delete(revoke_key))
        .with_state(state)
}

async fn owner(state: &KeyState, headers: &HeaderMap) -> Result<Uuid, CkwError> {
    let user = state.auth.require_user(headers).await.map_err(|_| CkwError::Unauthorized)?;
    upsert_user(&state.db, &user).await.map_err(|_| CkwError::Internal)
}

fn info(row: sqlx::sqlite::SqliteRow) -> Result<KeyInfo, CkwError> {
    let scopes: String = row.try_get("scopes")?;
    Ok(KeyInfo {
        id: row.try_get("id")?, name: row.try_get("name")?,
        token_prefix: row.try_get("token_prefix")?,
        scopes: serde_json::from_str(&scopes).map_err(|_| CkwError::Internal)?,
        project_id: row.try_get("project_id")?, created_at: row.try_get("created_at")?,
        expires_at: row.try_get("expires_at")?, revoked_at: row.try_get("revoked_at")?,
        last_used_at: row.try_get("last_used_at")?,
    })
}

async fn list_keys(State(state): State<KeyState>, headers: HeaderMap) -> Result<Json<Vec<KeyInfo>>, CkwError> {
    let user_id = owner(&state, &headers).await?;
    let rows = sqlx::query("select * from ckw_api_keys where user_id=$1 order by created_at desc limit 100")
        .bind(user_id).fetch_all(&state.db).await?;
    Ok(Json(rows.into_iter().map(info).collect::<Result<_, _>>()?))
}

async fn create_key(
    State(state): State<KeyState>, headers: HeaderMap, Json(input): Json<CreateKey>,
) -> Result<(StatusCode, Json<IssuedKey>), CkwError> {
    let user_id = owner(&state, &headers).await?;
    Ok((StatusCode::CREATED, Json(issue(&state.db, user_id, input).await?)))
}

pub(crate) async fn issue(db: &SqlitePool, user_id: Uuid, mut input: CreateKey) -> Result<IssuedKey, CkwError> {
    let name = input.name.trim();
    if name.is_empty() || name.chars().count() > 80 || name.chars().any(char::is_control) {
        return Err(CkwError::BadRequest("key name must contain 1 to 80 printable characters".into()));
    }
    input.scopes.sort(); input.scopes.dedup();
    if input.scopes.is_empty() || input.scopes.iter().any(|s| !SCOPES.contains(&s.as_str())) {
        return Err(CkwError::BadRequest("supported scopes: read, write, calculate, delete".into()));
    }
    if !(1..=365).contains(&input.expires_in_days) {
        return Err(CkwError::BadRequest("expiry must be between 1 and 365 days".into()));
    }
    let mut random = [0u8; 32];
    OsRng.fill_bytes(&mut random);
    let token = format!("{PREFIX}{}", URL_SAFE_NO_PAD.encode(random));
    let now = Utc::now();
    let key = KeyInfo {
        id: Uuid::new_v4(), name: name.into(), token_prefix: token[..PREFIX.len() + 8].into(),
        scopes: input.scopes, project_id: input.project_id, created_at: now,
        expires_at: now + Duration::days(input.expires_in_days), revoked_at: None, last_used_at: None,
    };
    // Take the SQLite write lock before checking ownership and the active-key limit.
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    if let Some(project_id) = key.project_id {
        let owned: i64 = sqlx::query_scalar("select count(*) from ckw_projects where id=$1 and user_id=$2")
            .bind(project_id).bind(user_id).fetch_one(&mut *tx).await?;
        if owned != 1 { return Err(CkwError::NotFound); }
    }
    let count: i64 = sqlx::query_scalar("select count(*) from ckw_api_keys where user_id=$1 and revoked_at is null and julianday(expires_at)>julianday('now')")
        .bind(user_id).fetch_one(&mut *tx).await?;
    if count >= 20 { return Err(CkwError::BadRequest("revoke an existing key before creating another (maximum 20 active keys)".into())); }
    sqlx::query("insert into ckw_api_keys (id,user_id,name,token_prefix,token_hash,scopes,project_id,created_at,expires_at) values ($1,$2,$3,$4,$5,$6,$7,$8,$9)")
        .bind(key.id).bind(user_id).bind(&key.name).bind(&key.token_prefix).bind(hash(&token))
        .bind(serde_json::to_string(&key.scopes).map_err(|_| CkwError::Internal)?)
        .bind(key.project_id).bind(key.created_at).bind(key.expires_at).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(IssuedKey { info: key, token })
}

async fn revoke_key(
    State(state): State<KeyState>, headers: HeaderMap, Path(key_id): Path<Uuid>,
) -> Result<StatusCode, CkwError> {
    let user_id = owner(&state, &headers).await?;
    let changed = sqlx::query("update ckw_api_keys set revoked_at=coalesce(revoked_at,$1) where id=$2 and user_id=$3")
        .bind(Utc::now()).bind(key_id).bind(user_id).execute(&state.db).await?;
    if changed.rows_affected() == 0 { return Err(CkwError::NotFound); }
    Ok(StatusCode::NO_CONTENT)
}

fn hash(token: &str) -> String { format!("{:x}", Sha256::digest(token.as_bytes())) }

fn permission(method: &Method, route: &str) -> Option<&'static str> {
    match (method.as_str(), route) {
        ("GET" | "HEAD", "/v1/ckw/projects" | "/v1/ckw/projects/:project_id" |
            "/v1/ckw/projects/:project_id/files" | "/v1/ckw/projects/:project_id/files/:file_id/download" |
            "/v1/ckw/projects/:project_id/results" | "/v1/ckw/projects/:project_id/results/:result_id/download" |
            "/v1/ckw/municipalities" | "/v1/ckw/capabilities") => Some("read"),
        ("POST", "/v1/ckw/projects" | "/v1/ckw/projects/:project_id/uploads/:upload_kind") |
        ("PATCH", "/v1/ckw/projects/:project_id") => Some("write"),
        ("POST", "/v1/ckw/projects/:project_id/operations/:operation") => Some("calculate"),
        ("DELETE", "/v1/ckw/projects/:project_id" | "/v1/ckw/projects/:project_id/files/:file_id") => Some("delete"),
        _ => None,
    }
}

async fn authorize(state: &KeyState, token: &str, method: &Method, route: &str, path: &str) -> Result<KeyIdentity, CkwError> {
    if token.len() != PREFIX.len() + 43 || !token[PREFIX.len()..].bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') {
        return Err(CkwError::Unauthorized);
    }
    let row = sqlx::query("select k.* from ckw_api_keys k join app_users u on u.id=k.user_id where k.token_hash=$1 and k.revoked_at is null and julianday(k.expires_at)>julianday('now')")
        .bind(hash(token)).fetch_optional(&state.db).await?.ok_or(CkwError::Unauthorized)?;
    let user_id: Uuid = row.try_get("user_id")?;
    let key = info(row)?;
    let required = permission(method, route).ok_or(CkwError::Forbidden)?;
    if !key.scopes.iter().any(|s| s == required) { return Err(CkwError::Forbidden); }
    if let Some(project_id) = key.project_id {
        if route.contains(":project_id") {
            let requested = path.split('/').nth(4).and_then(|s| Uuid::parse_str(s).ok());
            if requested != Some(project_id) { return Err(CkwError::Forbidden); }
        } else if method == Method::POST { return Err(CkwError::Forbidden); }
    }
    sqlx::query("update ckw_api_keys set last_used_at=$1 where id=$2 and (last_used_at is null or julianday(last_used_at)<julianday('now','-1 minute'))")
        .bind(Utc::now()).bind(key.id).execute(&state.db).await?;
    Ok(KeyIdentity { user_id, project_id: key.project_id })
}

pub(crate) async fn authenticate(State(state): State<KeyState>, mut request: Request, next: Next) -> Response {
    let token = request.headers().get(AUTHORIZATION).and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer ")).filter(|t| t.starts_with(PREFIX)).map(str::to_owned);
    let mut response = if let Some(token) = token {
        let route = request.extensions().get::<MatchedPath>().map(|p| p.as_str()).unwrap_or("");
        match authorize(&state, &token, request.method(), route, request.uri().path()).await {
            Ok(identity) => { request.extensions_mut().insert(identity); next.run(request).await }
            Err(error) => error.into_response(),
        }
    } else { next.run(request).await };
    response.headers_mut().insert(CACHE_CONTROL, "private, no-store".parse().unwrap());
    response
}
