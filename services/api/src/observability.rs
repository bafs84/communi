use std::{env, sync::Arc};
use axum::{extract::{MatchedPath, Request}, middleware::Next, response::Response};
use sentry::protocol::Event;
use serde_json::{json, Value};

// Reconstruct from an allowlist; tracing fields and error text can contain data.
fn sanitize(event: Event<'static>) -> Option<Event<'static>> {
    let source = serde_json::to_value(&event).ok()?;
    let mut clean = serde_json::Map::new();
    for key in ["event_id", "timestamp", "platform", "release", "environment", "level"] {
        if let Some(value) = source.get(key) { clean.insert(key.into(), value.clone()); }
    }
    let mut tags = serde_json::Map::new();
    tags.insert("service".into(), json!("api"));
    // Only our HTTP middleware sets these; route is an Axum template, never URI.
    for key in ["endpoint", "method", "status"] {
        if let Some(value) = source.get("tags").and_then(|v| v.get(key)) {
            tags.insert(key.into(), value.clone());
        }
    }
    clean.insert("user".into(), json!({"ip_address": "0.0.0.0"}));
    clean.insert("message".into(), json!("Communi API failure"));
    if tags.contains_key("status") {
        clean.insert("fingerprint".into(), json!(["api", tags.get("endpoint").and_then(Value::as_str).unwrap_or("/unknown"), tags.get("method").and_then(Value::as_str).unwrap_or("unknown"), tags.get("status").and_then(Value::as_str).unwrap_or("error")]));
    }
    clean.insert("tags".into(), Value::Object(tags));
    if let Some(exceptions) = source.get("exception").and_then(|v| v.get("values")).and_then(Value::as_array) {
        let values: Vec<Value> = exceptions.iter().map(|exception| {
            let frames: Vec<Value> = exception.get("stacktrace").and_then(|v| v.get("frames"))
                .and_then(Value::as_array).into_iter().flatten().map(|frame| {
                    let filename = frame.get("filename").and_then(Value::as_str).unwrap_or_default();
                    json!({"filename": std::path::Path::new(filename).file_name().and_then(|v| v.to_str()).unwrap_or("unknown"),
                        "lineno": frame.get("lineno"), "function": frame.get("function")})
                }).collect();
            json!({"type": "Error", "value": "[redacted]", "stacktrace": {"frames": frames}})
        }).collect();
        clean.insert("exception".into(), json!({"values": values}));
    }
    serde_json::from_value(Value::Object(clean)).ok()
}

pub fn initialize() -> sentry::ClientInitGuard {
    let mut options = sentry::ClientOptions::new();
    options.dsn = env::var("GLITCHTIP_DSN").ok().filter(|v| !v.is_empty()).and_then(|v| v.parse().ok());
    options.environment = Some(env::var("GLITCHTIP_ENVIRONMENT").unwrap_or_else(|_| "production".into()).into());
    options.release = Some(env::var("COMMUNI_RELEASE").unwrap_or_else(|_| "communi@0.2.0".into()).into());
    options.send_default_pii = false;
    options.max_breadcrumbs = 0;
    options.before_send = Some(Arc::new(sanitize));
    sentry::init(options)
}

pub async fn track_http(request: Request, next: Next) -> Response {
    let endpoint = request.extensions().get::<MatchedPath>().map(|v| v.as_str().to_owned()).unwrap_or_else(|| "/unmatched".into());
    let method = request.method().to_string();
    let response = next.run(request).await;
    let status = response.status();
    if status.is_client_error() || status.is_server_error() {
        // Isolated scope prevents cross-request attribution on async workers.
        let mut event = Event::default();
        event.level = if status.is_server_error() { sentry::Level::Error } else { sentry::Level::Warning };
        event.tags.insert("endpoint".into(), endpoint);
        event.tags.insert("method".into(), method);
        event.tags.insert("status".into(), status.as_u16().to_string());
        event.message = Some("HTTP request failed".into());
        sentry::capture_event(event);
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn drops_request_data_messages_and_tracing_fields() {
        let event: Event<'static> = serde_json::from_value(json!({
            "message": "private.csv secret@example.com", "extra": {"token": "private"},
            "user": {"email": "private"}, "request": {"data": "private"},
            "breadcrumbs": {"values": [{"message": "private"}]}, "tags": {"project_id": "private", "status": "401"},
            "exception": {"values": [{"type": "Secret", "value": "private", "stacktrace": {"frames": [{"filename": "/app/src/ckw.rs", "lineno": 12, "vars": {"upload": "private"}, "context_line": "private"}]}}]}
        })).unwrap();
        let value = serde_json::to_string(&sanitize(event).unwrap()).unwrap();
        assert!(!value.contains("private"));
        assert!(!value.contains("project_id"));
        assert!(value.contains("ckw.rs"));
        assert!(value.contains("401"));
    }
}
