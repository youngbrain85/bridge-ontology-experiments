"""Current-Windows-user DPAPI credential storage; no plaintext fallback.

The application serializes operations with its own lock. Each provider file is
replaced atomically after every supplied key has been validated and encrypted.
This is not a cross-process, multi-file transaction. Python strings necessarily
exist briefly in memory; status/errors never contain key fragments.

DPAPI reference:
https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from local_common import UserError, normalized_key

PROVIDERS = ("openai", "anthropic")
MAGIC = b"BRIDGE-USER-DPAPI\x01\x00"
MAX_FILE_BYTES = 65536
CRYPTPROTECT_UI_FORBIDDEN = 0x01
ERROR_PLATFORM = "Saved keys require Windows user-level encryption support."
ERROR_READ = "Cannot load the saved key. Save it again or delete it using the same Windows account."
ERROR_SAVE = "Could not encrypt and save the key. Check the storage folder and Windows user environment."
ERROR_DELETE = "Could not delete the saved key. Check the storage folder."
ERROR_PROVIDER = "Select a supported provider."


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows():
    return sys.platform == "win32"


def _load_dpapi():
    if not _windows():
        raise UserError(ERROR_PLATFORM, 409)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    common = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.argtypes = common
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = common
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return crypt, kernel


def _crypt(data, provider, encrypt):
    crypt, kernel = _load_dpapi()
    source = ctypes.create_string_buffer(data, len(data))
    entropy_bytes = ("bridge-ontology-key-store/v1/" + provider).encode("ascii")
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    try:
        operation = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
        # No LOCAL_MACHINE flag, UI prompt, description, or account discovery.
        ok = operation(ctypes.byref(blob), None, ctypes.byref(entropy), None,
                       None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output))
        if not ok or not output.pbData or not 0 < output.cbData <= MAX_FILE_BYTES:
            raise UserError(ERROR_SAVE if encrypt else ERROR_READ, 409)
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(source, 0, len(data))
        if output.pbData:
            if not encrypt:
                ctypes.memset(output.pbData, 0, output.cbData)
            kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _encrypt(provider, key):
    plaintext = json.dumps({"version": 1, "provider": provider, "key": key}, separators=(",", ":")).encode("utf-8")
    return MAGIC + _crypt(plaintext, provider, True)


def _decrypt(provider, encrypted):
    if not encrypted.startswith(MAGIC) or len(encrypted) <= len(MAGIC):
        raise UserError(ERROR_READ, 409)
    payload = json.loads(_crypt(encrypted[len(MAGIC):], provider, False).decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "provider", "key"}:
        raise UserError(ERROR_READ, 409)
    if type(payload["version"]) is not int or payload["version"] != 1 or payload["provider"] != provider:
        raise UserError(ERROR_READ, 409)
    key = normalized_key(payload["key"])
    if key != payload["key"]:
        raise UserError(ERROR_READ, 409)
    return key


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class CredentialStore:
    def __init__(self, root):
        # Do not resolve away a symlink before checking it. Construction itself
        # neither creates files nor reads/decrypts stored credentials.
        self.root = Path(os.path.abspath(os.fspath(root)))

    def _provider(self, provider):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise UserError(ERROR_PROVIDER)
        return provider

    def _guard_root(self, create=False):
        for path in reversed((self.root, *self.root.parents)):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise UserError(ERROR_READ, 409)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            self._guard_root()

    def _path(self, provider):
        self._provider(provider)
        self._guard_root()
        path = self.root / (provider + ".dpapi")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return path, False
        if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UserError(ERROR_READ, 409)
        return path, True

    def get(self, provider):
        self._provider(provider)
        try:
            path, exists = self._path(provider)
            if not exists:
                return None
            if not _windows():
                raise UserError(ERROR_PLATFORM, 409)
            with path.open("rb") as file:
                encrypted = file.read(MAX_FILE_BYTES + 1)
            if len(encrypted) > MAX_FILE_BYTES:
                raise UserError(ERROR_READ, 409)
            return _decrypt(provider, encrypted)
        except Exception:
            raise UserError(ERROR_READ, 409) from None

    def status(self):
        result = {}
        for provider in PROVIDERS:
            saved = False
            try:
                # lstat counts broken links as present, but _path/get reject
                # them. No filename/key content is included in the error.
                self._guard_root()
                try:
                    (self.root / (provider + ".dpapi")).lstat()
                    saved = True
                except FileNotFoundError:
                    pass
                if not _windows():
                    raise UserError(ERROR_PLATFORM, 409)
                available = self.get(provider) is not None
                result[provider] = {"saved": saved, "available": available, "error": None}
            except Exception:
                result[provider] = {"saved": saved, "available": False,
                                    "error": ERROR_READ if _windows() else ERROR_PLATFORM}
        return result

    def save_many(self, keys):
        if not isinstance(keys, dict) or not keys or any(key not in PROVIDERS for key in keys):
            raise UserError("Enter the API key for the provider you want to save.")
        normalized = {}
        staged = []
        try:
            if not _windows():
                raise UserError(ERROR_PLATFORM, 409)
            # Do all validation, path checks, and encryption before any write.
            for provider, value in keys.items():
                normalized[provider] = normalized_key(value)
                self._path(provider)
            encrypted = {provider: _encrypt(provider, key) for provider, key in normalized.items()}
            normalized.clear()
            self._guard_root(create=True)
            # Stage all encrypted files before publishing even the first one.
            for provider, data in encrypted.items():
                fd, name = tempfile.mkstemp(prefix="." + provider + "-", suffix=".tmp", dir=self.root)
                temporary = Path(name)
                staged.append((provider, temporary))
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
            for provider, temporary in staged:
                destination, _ = self._path(provider)
                os.replace(temporary, destination)
        except Exception:
            raise UserError(ERROR_SAVE, 409) from None
        finally:
            normalized.clear()
            for _, temporary in staged:
                try:
                    self._guard_root()
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                except UserError:
                    pass

    def delete(self, provider):
        self._provider(provider)
        try:
            if not _windows():
                raise UserError(ERROR_PLATFORM, 409)
            path, exists = self._path(provider)
            if exists:
                path.unlink()
        except Exception:
            raise UserError(ERROR_DELETE, 409) from None
