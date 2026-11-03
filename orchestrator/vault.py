"""Encrypted store for credential values entered through the dashboard (Sprint 4, phase 8).

The credential registry stays names-only. Values a person types into the Connections panel
are written here, Fernet-encrypted, under ``var/vault/secrets.enc`` (or
``ORCHESTRATOR_VAULT_PATH``). The key comes from ``ORCHESTRATOR_VAULT_KEY`` or is generated
once into ``var/vault/.key`` with mode 0600. ``export_env`` copies every stored value into
the service process environment, so ``credentials.is_present`` and every provider see it
without a restart, and registers each value with the redaction layer so it can never reach
a log, a report, or a response. Only ``load`` (used by ``export_env``) returns values; the
API lists names.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app import config

from . import credentials, redaction

FILE_NAME = "secrets.enc"
KEY_FILE_NAME = ".key"
MAX_VALUE_CHARS = 8192
_LOCK = threading.RLock()


class VaultError(ValueError):
    """A caller asked for something the vault refuses (unknown name, bad value, bad key)."""


def vault_dir() -> Path:
    configured = os.getenv("ORCHESTRATOR_VAULT_PATH", "").strip()
    return Path(configured) if configured else config.PROJECT_DIR / "var" / "vault"


def _file() -> Path:
    return vault_dir() / FILE_NAME


def _key_file() -> Path:
    return vault_dir() / KEY_FILE_NAME


def _ensure_dir() -> None:
    directory = vault_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _key() -> bytes:
    configured = os.getenv("ORCHESTRATOR_VAULT_KEY", "").strip()
    if configured:
        try:
            Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise VaultError("ORCHESTRATOR_VAULT_KEY is not a valid Fernet key") from exc
        return configured.encode("ascii")
    _ensure_dir()
    key_file = _key_file()
    if key_file.is_file():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    tmp = key_file.with_suffix(".tmp")
    tmp.write_bytes(key + b"\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, key_file)
    return key


def _fernet() -> Fernet:
    return Fernet(_key())


def load() -> dict[str, str]:
    """Decrypt the store. Internal: callers outside this module list names only."""
    with _LOCK:
        path = _file()
        if not path.is_file():
            return {}
        fernet = _fernet()  # a bad ORCHESTRATOR_VAULT_KEY raises its own VaultError
        try:
            raw = fernet.decrypt(path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, OSError) as exc:
            raise VaultError(f"the vault file cannot be read: {type(exc).__name__}") from exc
        return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _save(data: Mapping[str, str]) -> None:
    with _LOCK:
        _ensure_dir()
        path = _file()
        token = _fernet().encrypt(json.dumps(dict(data), sort_keys=True).encode("utf-8"))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def known_names() -> set[str]:
    return set(credentials.all_names())


def names() -> list[str]:
    """Names stored in the vault, sorted. Never values."""
    return sorted(load())


def set_many(values: Mapping[str, str]) -> list[str]:
    """Store values by registry name, export them to the process, and return the names."""
    known = known_names()
    cleaned: dict[str, str] = {}
    for name, value in values.items():
        key = str(name).strip()
        if key not in known:
            raise VaultError(f"{key or '(empty)'} is not a name in the credential registry")
        if not isinstance(value, str) or not value.strip():
            raise VaultError(f"{key} needs a non-empty value")
        if len(value) > MAX_VALUE_CHARS:
            raise VaultError(f"{key} is longer than {MAX_VALUE_CHARS} characters")
        cleaned[key] = value.strip() if "\n" not in value else value
    if not cleaned:
        raise VaultError("no values were given")
    with _LOCK:
        data = load()
        data.update(cleaned)
        _save(data)
    export_env(cleaned)
    return sorted(cleaned)


def delete(name: str) -> bool:
    """Remove one name from the vault and from the process environment."""
    with _LOCK:
        data = load()
        if name not in data:
            return False
        del data[name]
        _save(data)
    os.environ.pop(name, None)
    return True


def clear() -> None:
    with _LOCK:
        path = _file()
        if path.is_file():
            path.unlink()


def export_env(values: Mapping[str, str] | None = None, target_env: dict[str, str] | None = None) -> list[str]:
    """Copy stored values into the process variables and register them for redaction.

    A vault value overrides an existing variable: a value typed in the dashboard is the
    newest instruction. Returns the exported names.
    """
    target = target_env if target_env is not None else os.environ
    data = dict(values) if values is not None else load()
    for name, value in data.items():
        target[name] = value
    redaction.register_values(data.values())
    return sorted(data)


def generate_key() -> str:
    """A fresh Fernet key for ORCHESTRATOR_VAULT_KEY (shown once by a person, never by the app)."""
    return Fernet.generate_key().decode("ascii")


def token() -> str:
    """A random dashboard token for ORCHESTRATOR_UI_TOKEN."""
    return secrets.token_hex(16)
