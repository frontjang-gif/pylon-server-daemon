"""capability: cap:system.secrets.dpapi  (Windows, via crypt32 DPAPI).

Endpoint-bound because DPAPI binds ciphertext to the CURRENT WINDOWS USER on
THIS machine (CryptProtectData/CryptUnprotectData) — there is no portable key, so
the seal/unseal can only happen where the user's profile lives. That is exactly
why it is a capability, not app logic: a framework hosted elsewhere calls
cap:system.secrets.dpapi and the wrapping happens on the endpoint.

Generic byte wrapping (NOT store-specific), so it is reusable for any secret an
app wants user-bound on this endpoint (credential store today; SSH key material
in system/auth_ssh later). The app keeps the ciphertext; the key never leaves
Windows.

Methods (args carry base64 so JSON/WS transport stays text-clean):
  encrypt(data_b64) -> {ok, data_b64}   # protect bytes for the current user
  decrypt(data_b64) -> {ok, data_b64}   # unprotect bytes (same user only)

ctx.user is informational: DPAPI scopes to the OS user running the daemon.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_in(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _crypt_protect(data: bytes) -> bytes:
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(_blob_in(data)), None, None, None, None, 0,
            ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _crypt_unprotect(data: bytes) -> bytes:
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(_blob_in(data)), None, None, None, None, 0,
            ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def encrypt(args, ctx):
    b64 = args.get("data_b64")
    if not b64:
        return {"ok": False, "error": "data_b64 required", "error_category": "application"}
    try:
        raw = base64.b64decode(b64)
        sealed = _crypt_protect(raw)
        return {"ok": True, "data_b64": base64.b64encode(sealed).decode("ascii")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"dpapi encrypt failed: {exc}",
                "error_category": "application"}


def decrypt(args, ctx):
    b64 = args.get("data_b64")
    if not b64:
        return {"ok": False, "error": "data_b64 required", "error_category": "application"}
    try:
        sealed = base64.b64decode(b64)
        raw = _crypt_unprotect(sealed)
        return {"ok": True, "data_b64": base64.b64encode(raw).decode("ascii")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"dpapi decrypt failed: {exc}",
                "error_category": "application"}


METHODS = {"encrypt": encrypt, "decrypt": decrypt}
CAPABILITY = "cap:system.secrets.dpapi"
VARIANT = None
ATTRS = {"user_bound": True}
