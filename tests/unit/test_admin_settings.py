import pytest
from cryptography.fernet import Fernet

from tender_system.persistence.admin_settings import decrypt_secret, encrypt_secret


def test_secret_round_trip_and_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_secret("private credential")
    assert "private credential" not in encrypted
    assert decrypt_secret(encrypted) == "private credential"
    monkeypatch.setenv("QUARRY_SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="Cannot decrypt"):
        decrypt_secret(encrypted)


def test_missing_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUARRY_SETTINGS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="persistent QUARRY_SETTINGS_ENCRYPTION_KEY"):
        encrypt_secret("private credential")
