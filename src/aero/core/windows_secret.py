"""Windows current-user DPAPI envelope for local credential files."""

from __future__ import annotations

import base64
import ctypes
import os

PREFIX = "AERO-DPAPI1\n"


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.c_void_p)]


def _crypt(value: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI 只在 Windows 上可用")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source = ctypes.create_string_buffer(value)
    input_blob = _Blob(len(value), ctypes.cast(source, ctypes.c_void_p))
    output_blob = _Blob()
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.restype = ctypes.c_int
    function.argtypes = (
        [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_Blob),
        ]
        if decrypt
        else [
            ctypes.POINTER(_Blob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_Blob),
        ]
    )
    description = None if decrypt else "Aerolytica credentials"
    if not function(
        ctypes.byref(input_blob),
        description,
        None,
        None,
        None,
        1,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.data)


def protect_text(text: str) -> str:
    return PREFIX + base64.b64encode(_crypt(text.encode("utf-8"), decrypt=False)).decode()


def unprotect_text(text: str) -> str:
    if not text.startswith(PREFIX):
        return text
    encrypted = base64.b64decode(text[len(PREFIX) :], validate=True)
    return _crypt(encrypted, decrypt=True).decode("utf-8")
