import json
from ckw_script.observability import sanitize_event


def test_error_events_exclude_all_private_fields_and_exception_text():
    event = sanitize_event({
        "message": "private.csv", "request": {"headers": {"Authorization": "private"}, "data": "private"},
        "user": {"email": "private"}, "extra": {"manifest": "private"}, "breadcrumbs": [{"message": "private"}],
        "tags": {"endpoint": "/internal/v1/execute", "status": 500, "project_id": "private"},
        "exception": {"values": [{"type": "ValueError", "value": "private", "stacktrace": {"frames": [{
            "filename": "/app/engine_api.py", "lineno": 12, "function": "execute", "vars": {"upload": "private"}, "context_line": "private",
        }]}}]},
    })
    assert "private" not in json.dumps(event)
    assert event["tags"]["status"] == "500"
    assert event["exception"]["values"][0]["stacktrace"]["frames"][0]["lineno"] == 12
