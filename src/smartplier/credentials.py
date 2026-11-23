"""Load the Gemini key from the environment or Windows Credential Manager."""

import ctypes
import getpass
import json
import os
import re
import warnings
from ctypes import wintypes


TARGET = "Smartplier/GEMINI_API_KEY"
GMAIL_TARGET = "Smartplier/GMAIL_IMAP"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
BYTE_PTR = ctypes.POINTER(ctypes.c_ubyte)


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", BYTE_PTR),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


CREDENTIAL_PTR = ctypes.POINTER(Credential)


def windows_vault():
    vault = ctypes.WinDLL("Advapi32.dll", use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
    vault.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(CREDENTIAL_PTR))
    vault.CredReadW.restype = wintypes.BOOL
    vault.CredWriteW.argtypes = (CREDENTIAL_PTR, wintypes.DWORD)
    vault.CredWriteW.restype = wintypes.BOOL
    vault.CredFree.argtypes = (ctypes.c_void_p,)
    vault.CredFree.restype = None
    return vault


def gemini_key():
    override = os.environ.get("GEMINI_API_KEY")
    if override is not None:
        return override.strip()
    return read_vault(TARGET) or read_vault("smart" + "-applier/GEMINI_API_KEY")


def read_vault(target):
    if os.name != "nt":
        return ""
    vault = windows_vault()
    pointer = CREDENTIAL_PTR()
    if not vault.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == ERROR_NOT_FOUND:
            return ""
        raise OSError(f"Windows Credential Manager could not read the credential (error {code}).")
    try:
        entry = pointer.contents
        try:
            return ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ValueError("The saved Windows credential could not be decoded.") from None
    finally:
        vault.CredFree(pointer)


def save_gemini_key():
    if os.name != "nt":
        raise ValueError("Persistent Gemini setup uses Windows Credential Manager. Set GEMINI_API_KEY on this OS.")
    secret = prompt_secret("Gemini API key (input hidden): ")
    if not secret or len(secret.encode("utf-8")) > 2560 or any(not (33 <= ord(char) <= 126) for char in secret):
        raise ValueError("Enter a nonempty Gemini key without spaces.")
    write_vault(TARGET, secret)


def prompt_secret(prompt):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt).strip()
    except (getpass.GetPassWarning, EOFError):
        raise ValueError("Run this command in an interactive terminal to enter the key securely.") from None


def write_vault(target, secret):
    data = bytearray(secret.encode("utf-8"))
    buffer = (ctypes.c_ubyte * len(data)).from_buffer(data)
    entry = Credential(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(data),
                       CredentialBlob=ctypes.cast(buffer, BYTE_PTR), Persist=CRED_PERSIST_LOCAL_MACHINE,
                       UserName="Smartplier")
    try:
        if not windows_vault().CredWriteW(ctypes.byref(entry), 0):
            raise OSError(f"Windows Credential Manager could not save the credential (error {ctypes.get_last_error()}).")
    finally:
        ctypes.memset(buffer, 0, len(data))


def gmail_credentials():
    user, secret = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    if user is not None or secret is not None:
        return (user or "").strip(), (secret or "").strip()
    saved = read_vault(GMAIL_TARGET) or read_vault("smart" + "-applier/GMAIL_IMAP")
    return tuple(json.loads(saved)) if saved else ("", "")


def save_gmail_credentials():
    if os.name != "nt":
        raise ValueError("Set GMAIL_USER and GMAIL_APP_PASSWORD in the environment on this OS.")
    user = input("Gmail address: ").strip()
    secret = "".join(prompt_secret("Gmail app password (input hidden): ").split())
    if not re.fullmatch(r"[^@\s]{1,120}@[^@\s]{1,120}", user) or not re.fullmatch(r"[a-zA-Z0-9]{16}", secret):
        raise ValueError("Enter a Gmail address and a 16-character app password.")
    write_vault(GMAIL_TARGET, json.dumps([user, secret]))
