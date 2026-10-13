import pytest

from ckw_script.startup import initialize_token


def test_token_is_private_and_survives_restarts(tmp_path):
    path = tmp_path / "secrets" / "internal_token"
    initialize_token(path)
    token = path.read_bytes()
    assert len(token) == 96
    assert path.stat().st_mode & 0o777 == 0o600
    initialize_token(path)
    assert path.read_bytes() == token


def test_invalid_existing_token_is_preserved_and_rejected(tmp_path):
    path = tmp_path / "internal_token"
    path.write_text("broken")
    with pytest.raises(ValueError, match="invalid"):
        initialize_token(path)
    assert path.read_text() == "broken"
