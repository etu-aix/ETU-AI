"""Secrets fournisseur chiffrés avec DPAPI sur Windows, coffre système ailleurs."""

import ctypes
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from uuid import uuid4

SERVICE_NAME = "ETU-AI"
_LOGGER = logging.getLogger(__name__)
SECRETS_PATH = Path(__file__).resolve().parent / "api_secrets.dpapi"
_LOCK = threading.RLock()
_ENTROPY = b"ETU-AI provider credentials v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI est disponible uniquement sous Windows.")

    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function_name = "CryptUnprotectData" if decrypt else "CryptProtectData"
    function = getattr(crypt32, function_name)
    blob_pointer = ctypes.POINTER(_DataBlob)
    function.argtypes = (
        [blob_pointer, ctypes.POINTER(wintypes.LPWSTR), blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
        if decrypt
        else [blob_pointer, wintypes.LPCWSTR, blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    )
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def make_blob(value: bytes) -> tuple[_DataBlob, Any]:
        buffer = ctypes.create_string_buffer(value)
        return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    input_blob, input_buffer = make_blob(data)
    entropy_blob, entropy_buffer = make_blob(_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    try:
        if decrypt:
            ok = function(ctypes.byref(input_blob), ctypes.byref(description), ctypes.byref(entropy_blob), None, None, 0x1, ctypes.byref(output_blob))
        else:
            ok = function(ctypes.byref(input_blob), SERVICE_NAME, ctypes.byref(entropy_blob), None, None, 0x1, ctypes.byref(output_blob))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        if decrypt and description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        # Keep ctypes input buffers alive until the native call has completed.
        _ = input_buffer, entropy_buffer


def _load_windows_secrets() -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    encrypted = SECRETS_PATH.read_bytes()
    decoded = _dpapi(encrypted, decrypt=True)
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in payload.items()):
        raise ValueError("Le coffre de clés ETU-AI a un format invalide.")
    return payload


def _save_windows_secrets(secrets: dict[str, str]) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not secrets:
        SECRETS_PATH.unlink(missing_ok=True)
        return
    encrypted = _dpapi(json.dumps(secrets, separators=(",", ":")).encode("utf-8"), decrypt=False)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=SECRETS_PATH.parent, prefix=".api_secrets-", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(encrypted)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, SECRETS_PATH)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def _backup_unreadable_windows_vault() -> Path:
    """Keep the original encrypted vault before replacing one this account cannot read."""
    backup_path = SECRETS_PATH.with_name(f"api_secrets.unreadable-{uuid4().hex}.dpapi")
    encrypted = SECRETS_PATH.read_bytes()
    try:
        with backup_path.open("xb") as backup:
            backup.write(encrypted)
            backup.flush()
            os.fsync(backup.fileno())
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _username(provider: str) -> str:
    return f"llm-api-key:{provider}"


def _fallback_username(provider: str) -> str:
    """A DPAPI override kept in Windows Credential Manager if an old vault is unreadable."""
    return f"llm-api-key:dpapi-fallback:{provider}"


def _get_keyring_password(provider: str) -> str | None:
    import keyring

    value = keyring.get_password(SERVICE_NAME, _username(provider))
    if value is None and provider == "openai-compatible":
        value = keyring.get_password(SERVICE_NAME, "llm-api-key")
    return value


def _get_windows_fallback(provider: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(SERVICE_NAME, _fallback_username(provider))
    except Exception:
        return None


def _set_windows_fallback(value: str, provider: str) -> None:
    import keyring

    keyring.set_password(SERVICE_NAME, _fallback_username(provider), value)


def _delete_windows_fallback(provider: str) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, _fallback_username(provider))
    except Exception:
        pass


def get_api_key(provider: str = "openai-compatible") -> str | None:
    if os.name == "nt":
        # A fallback entry takes precedence so a later recovery of an older DPAPI
        # file cannot silently replace a key the user has just updated.
        fallback = _get_windows_fallback(provider)
        if fallback is not None:
            return fallback
        with _LOCK:
            try:
                value = _load_windows_secrets().get(provider)
            except Exception as exc:
                _LOGGER.warning("DPAPI vault unavailable; checking Windows Credential Manager (provider=%s, error=%s)", provider, type(exc).__name__)
                try:
                    return _get_keyring_password(provider)
                except Exception:
                    return None
        if value is not None:
            return value
        # One-time migration for keys saved by the earlier keyring-only release.
        try:
            value = _get_keyring_password(provider)
        except Exception:
            return None
        if value:
            try:
                set_api_key(value, provider)
            except Exception:
                return None
        return value
    return _get_keyring_password(provider)


def set_api_key(value: str, provider: str = "openai-compatible") -> None:
    if os.name == "nt":
        try:
            with _LOCK:
                try:
                    secrets = _load_windows_secrets()
                except (OSError, ValueError) as vault_error:
                    if not SECRETS_PATH.exists():
                        raise
                    backup_path = _backup_unreadable_windows_vault()
                    secrets = {}
                    _LOGGER.warning(
                        "Archived unreadable DPAPI vault as %s before creating a new vault (error=%s)",
                        backup_path.name, type(vault_error).__name__,
                    )
                secrets[provider] = value
                _save_windows_secrets(secrets)
                # A fallback can remain from an earlier save made while the
                # DPAPI vault was unavailable. Since reads prefer that entry,
                # remove it after the new DPAPI value has been committed.
                _delete_windows_fallback(provider)
            _LOGGER.info("Saved API credential for provider=%s with DPAPI (credential value redacted)", provider)
        except Exception as dpapi_error:
            try:
                _set_windows_fallback(value, provider)
            except Exception as keyring_error:
                raise OSError("Ni DPAPI ni le Gestionnaire d’identifiants Windows ne sont disponibles.") from keyring_error
            _LOGGER.warning("Saved provider=%s credential in Windows Credential Manager because the existing DPAPI vault is unavailable (credential value redacted, error=%s)", provider, type(dpapi_error).__name__)
        return
    import keyring

    keyring.set_password(SERVICE_NAME, _username(provider), value)
    _LOGGER.info("Saved API credential for provider=%s in the system keyring (credential value redacted)", provider)


def delete_api_key(provider: str = "openai-compatible") -> None:
    if os.name == "nt":
        _delete_windows_fallback(provider)
        with _LOCK:
            try:
                secrets = _load_windows_secrets()
                secrets.pop(provider, None)
                _save_windows_secrets(secrets)
            except Exception as exc:
                _LOGGER.warning("Could not update DPAPI vault while deleting provider=%s (error=%s)", provider, type(exc).__name__)
        _LOGGER.info("Deleted API credential for provider=%s (credential value redacted)", provider)
        try:
            import keyring

            keyring.delete_password(SERVICE_NAME, _username(provider))
        except Exception:
            pass
        return
    import keyring

    try:
        keyring.delete_password(SERVICE_NAME, _username(provider))
    except keyring.errors.PasswordDeleteError:
        pass
