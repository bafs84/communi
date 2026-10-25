#!/usr/bin/env python3
"""Use the live Communi CKW API without a browser session (standard library only)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a credential or replay an upload elsewhere.


def origin(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("API URL must be an HTTPS origin (HTTP is allowed only on loopback)")
    return value.rstrip("/")


def read_token(path: Path) -> str:
    token = os.environ.get("COMMUNI_API_TOKEN", "").strip()
    if not token:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("token file must be private: chmod 600 <token-file>")
        token = path.read_text(encoding="utf-8").strip()
    if not token.startswith("communi_api_") or len(token) != 55 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in token[12:]):
        # Prefix is 12 characters; do not include the supplied value in errors.
        raise ValueError("invalid API key format")
    return token


def multipart(paths: list[Path]) -> tuple[bytes, str]:
    boundary = f"communi-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    total = 0
    for path in paths:
        size = path.stat().st_size
        if size < 1 or size > 10 * 1024 * 1024:
            raise ValueError("each upload must contain 1 byte to 10 MiB")
        total += size
        if total > 50 * 1024 * 1024:
            raise ValueError("upload batch exceeds 50 MiB; upload in smaller batches")
        # Quoted header fields must not contain caller-controlled control characters.
        filename = path.name.replace('"', "_").replace("\\", "_")
        if any(ord(c) < 32 or ord(c) == 127 for c in filename):
            raise ValueError("filename contains control characters")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n').encode("utf-8"))
        parts.extend([path.read_bytes(), b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def request(base: str, token: str, method: str, path: str, *, data=None, content_type=None, output: Path | None = None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, application/octet-stream"}
    if data is not None and not isinstance(data, bytes):
        data = json.dumps(data, allow_nan=False).encode("utf-8")
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(base + path, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=200) as response:
            if output:
                # Exclusive creation avoids accidentally replacing an input or prior result.
                with output.open("xb") as target:
                    try:
                        while chunk := response.read(1024 * 1024):
                            target.write(chunk)
                    except BaseException:
                        target.close()
                        output.unlink(missing_ok=True)
                        raise
                print(json.dumps({"saved": str(output), "result_id": response.headers.get("X-Result-Id")}))
            elif response.status != 204:
                if "application/json" not in response.headers.get("Content-Type", ""):
                    raise ValueError("binary response requires --output <path>; the export remains in project results")
                print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except HTTPError as error:
        hints = {401: "key missing, expired or revoked", 403: "key lacks the required scope or project access", 404: "resource or operation not found", 413: "upload too large", 507: "storage quota exceeded"}
        error.close()
        raise ValueError(f"HTTP {error.code}: {hints.get(error.code, 'API request failed')}") from None
    except URLError:
        raise ValueError("API unavailable; mutation requests are never retried automatically") from None


def identifier(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("expected a UUID") from None


def parameters(value: str):
    parsed = json.loads(Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value)
    if not isinstance(parsed, dict):
        raise ValueError("parameters/configuration must be a JSON object")
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMMUNI_API_URL", "https://ckw.communi.ch"))
    parser.add_argument("--token-file", type=Path, default=Path(__file__).resolve().parents[1] / "runtime/communi-api-token")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="store a key using hidden input, with private file permissions")
    sub.add_parser("info", help="show supported operations and upload limits")
    sub.add_parser("projects")
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--municipality", required=True)
    for command in ["get", "update", "files", "upload", "run", "results", "download", "delete-project", "delete-file"]:
        cmd = sub.add_parser(command)
        cmd.add_argument("project_id", type=identifier)
        if command == "update":
            cmd.add_argument("--name")
            cmd.add_argument("--municipality")
            cmd.add_argument("--config", help="JSON object or @file; replaces the entire project configuration")
        elif command == "upload":
            cmd.add_argument("--kind", choices=["solar", "consumption", "zev", "billing"], required=True)
            cmd.add_argument("files", type=Path, nargs="+")
        elif command == "run":
            cmd.add_argument("operation")
            cmd.add_argument("--parameters", default="{}", help="JSON object or @file")
            cmd.add_argument("--output", type=Path)
        elif command == "download":
            group = cmd.add_mutually_exclusive_group(required=True)
            group.add_argument("--file", type=identifier)
            group.add_argument("--result", type=identifier)
            cmd.add_argument("--output", type=Path, required=True)
        elif command == "delete-file":
            cmd.add_argument("file_id", type=identifier)
    args = parser.parse_args()
    base = origin(args.url)
    if args.command == "configure":
        secret = getpass.getpass("API key (input hidden): ").strip()
        if not secret.startswith("communi_api_") or len(secret) != 55:
            raise ValueError("invalid API key format")
        args.token_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic private publication also supports safely replacing a rotated key.
        fd, temporary = tempfile.mkstemp(prefix=".communi-key-", dir=args.token_file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                target.write(secret + "\n")
            os.replace(temporary, args.token_file)
        finally:
            Path(temporary).unlink(missing_ok=True)
        print("API key saved in the private token file.")
        return
    token = read_token(args.token_file)
    prefix = "/v1/ckw"
    project = f"{prefix}/projects/{args.project_id}" if hasattr(args, "project_id") else ""
    call = lambda method, path, **kw: request(base, token, method, path, **kw)
    if args.command == "info": call("GET", f"{prefix}/capabilities")
    elif args.command == "projects": call("GET", f"{prefix}/projects")
    elif args.command == "create": call("POST", f"{prefix}/projects", data={"name": args.name, "municipality": args.municipality})
    elif args.command == "get": call("GET", project)
    elif args.command == "update":
        data = {key: value for key in ["name", "municipality"] if (value := getattr(args, key)) is not None}
        if args.config is not None: data["config"] = parameters(args.config)
        if not data: raise ValueError("provide --name, --municipality or --config")
        call("PATCH", project, data=data)
    elif args.command == "files": call("GET", project + "/files")
    elif args.command == "upload":
        body, content_type = multipart(args.files)
        call("POST", project + f"/uploads/{args.kind}", data=body, content_type=content_type)
    elif args.command == "run": call("POST", project + "/operations/" + quote(args.operation, safe=""), data={"parameters": parameters(args.parameters)}, output=args.output)
    elif args.command == "results": call("GET", project + "/results")
    elif args.command == "download":
        kind, resource = ("files", args.file) if args.file else ("results", args.result)
        call("GET", project + f"/{kind}/{resource}/download", output=args.output)
    elif args.command == "delete-project": call("DELETE", project)
    elif args.command == "delete-file": call("DELETE", project + f"/files/{args.file_id}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        # Never print HTTP bodies, credentials, or uploaded file contents.
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
