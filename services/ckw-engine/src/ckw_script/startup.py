"""Initialize the persistent internal token once, then start the engine."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import tempfile


def initialize_token(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        descriptor, name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(secrets.token_hex(48))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                pass
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(name)
    # Fail closed on corruption. Never replace a persistent token automatically.
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ValueError("persistent internal token is invalid")


def prepare_temp_directory() -> Path:
    # Nested libraries (pandas, DuckDB, invoice ZIP/DOCX) use tempfile without
    # an explicit dir. The container root is read-only; only this tmpfs is writable.
    directory = Path(os.environ.get("CKW_TMP_DIR", "/tmp/communi-ckw"))
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(directory)
    tempfile.tempdir = None
    return directory


def main() -> None:
    prepare_temp_directory()
    if not os.environ.get("CKW_INTERNAL_TOKEN"):
        path = Path(os.environ.setdefault("CKW_INTERNAL_TOKEN_FILE", "/run/communi/internal_token"))
        initialize_token(path)
    os.execvp("uvicorn", ["uvicorn", "ckw_script.engine_api:app", "--host", "0.0.0.0",
                          "--port", "8000", "--workers", "1", "--no-server-header"])


if __name__ == "__main__":
    main()
