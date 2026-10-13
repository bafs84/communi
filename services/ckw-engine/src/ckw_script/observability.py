"""Error-only telemetry. Never transmit uploads, manifests or exception text."""
import os
from pathlib import PurePath

import sentry_sdk
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.threading import ThreadingIntegration


def sanitize_event(event, hint=None):
    clean = {key: event[key] for key in (
        "event_id", "timestamp", "platform", "level", "release", "environment"
    ) if key in event}
    source = event.get("tags", {})
    tags = {"service": "ckw-engine"}
    if source.get("endpoint") in ("/internal/health", "/internal/v1/execute", "/internal/v1/municipalities"):
        tags["endpoint"] = source["endpoint"]
    if source.get("method") in ("GET", "POST"):
        tags["method"] = source["method"]
    if str(source.get("status", "")).isdigit() and 400 <= int(source["status"]) <= 599:
        tags["status"] = str(source["status"])
    clean["user"] = {"ip_address": "0.0.0.0"}
    clean["tags"] = tags
    clean["message"] = "Communi calculation engine failure"
    values = []
    for value in event.get("exception", {}).get("values", []):
        frames = []
        for frame in value.get("stacktrace", {}).get("frames", []):
            filename = PurePath(frame.get("filename", "")).name
            if filename.endswith(".py"):
                frames.append({"filename": filename, "lineno": frame.get("lineno"),
                               "function": frame.get("function"), "in_app": frame.get("in_app")})
        exception_type = value.get("type")
        if exception_type not in ("ValueError", "KeyError", "TypeError", "RuntimeError", "OSError", "IndexError", "ZeroDivisionError", "AttributeError", "ValidationError"):
            exception_type = "Error"
        values.append({"type": exception_type, "value": "[redacted]", "stacktrace": {"frames": frames}})
    if values:
        clean["exception"] = {"values": values}
    else:
        clean["fingerprint"] = ["ckw-engine", tags.get("endpoint", "unknown"), tags.get("status", "error")]
    return clean


def initialize_observability():
    sentry_sdk.init(
        dsn=os.environ.get("GLITCHTIP_DSN") or None,
        environment=os.environ.get("GLITCHTIP_ENVIRONMENT", "production"),
        release=os.environ.get("COMMUNI_RELEASE", "communi@0.2.0"),
        default_integrations=False,
        integrations=[ExcepthookIntegration(), ThreadingIntegration(propagate_hub=False)],
        data_collection={
            "user_info": False, "cookies": {"mode": "off"},
            "http_headers": {"request": {"mode": "off"}}, "http_bodies": [],
            "url_query_params": {"mode": "off"}, "stack_frame_variables": False,
            "frame_context_lines": 0, "database_query_data": False,
        },
        include_local_variables=False,
        max_request_body_size="never", max_breadcrumbs=0, send_client_reports=False,
        traces_sample_rate=0, sample_rate=1, before_send=sanitize_event,
    )


def report_exception(error, request=None):
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("service", "ckw-engine")
        if request is not None:
            scope.set_tag("endpoint", request.url.path)
            scope.set_tag("method", request.method)
            request.state.error_reported = True
        sentry_sdk.capture_exception(error)
