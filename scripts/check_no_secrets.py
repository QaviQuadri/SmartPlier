"""Check publication files. Output contains paths, line numbers and categories only."""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
from urllib.parse import unquote
import zlib

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
LOCAL_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "dist", "__pycache__", ".test-runtime",
                     ".pytest_cache", ".ruff_cache"}
DB_EXTENSIONS = {".sqlite", ".sqlite3", ".db", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm"}
TRUSTED_EMAIL_SUFFIX = re.compile(r"^(?:[\w-]+\.)*(?:example\.(?:com|org|net|test)|example|test|invalid)$", re.I)
EMAIL = re.compile(r"[\w.%+\-]+@([\w.\-]+\.[a-z][a-z0-9.-]+)", re.I)
PHONE = re.compile(r"(?<![\w-])\+?\d{1,3}[ .-]*(?:\(\d{2,4}\)|\d{2,4})[ .-]+\d{3,4}[ .-]+\d{4}(?!\d)")
SOCIAL = re.compile(r"linkedin\.com/in/[\w-]{3,}|github\.com/[\w-]{3,}(?![\w/-])", re.I)
APPLICATION_PATH = re.compile(r"https?://([a-z0-9.-]+)(?::\d+)?/[^\s'\"<>]*?"
                              r"(?:apply(?:[/?#]|(?=[\s'\"<>]|$))|(?:jobs|careers)/[^\s'\"<>/]+)", re.I)
APPLICATION = re.compile(r"(?:jobs(?:\.eu)?\.lever\.co/[\w-]+/[\w-]+|"
                         r"(?:boards|job-boards)\.greenhouse\.io/[\w-]+/jobs/\d+|"
                         r"apply\.workable\.com/(?:[\w-]+/)?j/[\w-]+|jobs\.ashbyhq\.com/[\w-]+/[\w-]+|"
                         r"(?:linkedin\.com/(?:comm/)?jobs/view|indeed\.[\w.]+/(?:viewjob|rc/clk)|"
                         r"naukri\.com/job-listings|(?:bayt|gulftalent)\.com/[\w/.-]+/jobs|"
                         r"naukrigulf\.com/[\w-]+-jobs)/?[\w./?=-]+)", re.I)
PATTERNS = {
    "API key": re.compile(r"\b(?:AIza[0-9A-Za-z_-]{35}|sk-(?:proj-)?[0-9A-Za-z_-]{20,}|"
                          r"gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})\b"),
    "Gmail app password": re.compile(r"""\b(?:GMAIL_APP_PASSWORD|app_password)\b['"]?\s*[:=]\s*['"]?([a-z]{16}|(?:[a-z]{4} ){3}[a-z]{4})\b""", re.I),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Local path": re.compile(r"\b[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\s/\\\x22']+", re.I),
    "Confirmation email": re.compile(r"^Authentication-Results:.*|^Received:.*\b(?:smtp|esmtp)\b|"
                                     r"^Subject:.*(?:application received|thank you for applying|application confirmation)", re.I),
}


def reference_patterns(path):
    if path is None:
        return {}
    path = path.resolve(strict=True)
    profile = json.loads(path.read_text(encoding="utf-8-sig"))
    basics = profile["basics"]
    groups = {
        "Original profile identity": [basics.get(k, "") for k in ("name", "first_name", "last_name", "email", "phone",
                                                                  "linkedin", "github", "portfolio")],
        "Original resume text": [basics.get("summary", ""), *(s.get("text", "") for s in profile.get("stories", []))],
        "Windows username or local folder": [Path.home().name, path.parent.name, path.parent.parent.name],
    }
    for entry in profile.get("education", []):
        groups["Original resume text"].append(entry.get("school", ""))
    for section in ("projects", "experience"):
        for entry in profile.get(section, []):
            groups["Original resume text"].extend([entry.get("name", ""), entry.get("company", ""),
                                                    *(b.get("text", "") for b in entry.get("bullets", []))])
    patterns = {}
    for category, values in groups.items():
        expanded = set()
        for raw in values:
            if not isinstance(raw, str) or len(raw.strip()) < 4:
                continue
            value = raw.strip().casefold()
            expanded.add(value)
            if "/" in value:
                expanded.add(value.removeprefix("https://").removeprefix("http://").removeprefix("www."))
            if category == "Original resume text":
                for sentence in re.split(r"(?<=[.!?])\s+", value):
                    # Also catch excerpts which appear on a single OCR line.
                    words = sentence.split()
                    expanded.update(" ".join(words[pos:pos + 8]) for pos in range(0, max(0, len(words) - 7), 3))
        if expanded:
            patterns[category] = re.compile(r"(?<![\w-])(?:" + "|".join(
                re.escape(value) for value in sorted(expanded, key=len, reverse=True)) + r")(?![\w-])", re.I)
    phone = re.sub(r"\D", "", basics.get("phone", ""))
    if len(phone) > 8:
        patterns["Original phone"] = re.compile(r"(?<!\d)" + r"[ .()+-]*".join(phone[-10:]) + r"(?!\d)")
    return patterns


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def findings(line, patterns):
    visible = html.unescape(unquote(line))
    values = set()
    for category, regex in {**PATTERNS, **patterns}.items():
        if regex.search(visible):
            values.add(category)
    if any(not TRUSTED_EMAIL_SUFFIX.fullmatch(email.rstrip(".")) for email in EMAIL.findall(visible)):
        values.add("Non-example email")
    if any("-555-01" not in phone.replace(" ", "-") for phone in PHONE.findall(visible)):
        values.add("Non-fictional phone")
    if SOCIAL.search(visible):
        values.add("Public profile URL")
    if APPLICATION.search(visible) or any(
        not TRUSTED_EMAIL_SUFFIX.fullmatch(host) and host not in {"localhost", "127.0.0.1"}
        for host in APPLICATION_PATH.findall(visible)
    ):
        values.add("Employer application URL")
    for key in ("GEMINI_API_KEY", "GMAIL_APP_PASSWORD", "ADZUNA_APP_KEY"):
        secret = os.environ.get(key, "").strip()
        if len(secret) >= 8 and secret in visible:
            values.add("Environment credential")
    return sorted(values)


def image_text(path):
    executable = shutil.which("tesseract")
    environment = os.environ.copy()
    if executable:
        command = [executable, str(path), "stdout", "-l", "eng"]
    elif sys.platform == "win32":
        source = Path(__file__).with_name("read_image_text.ps1").read_text(encoding="utf-8")
        code = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
        environment["SMARTPLIER_OCR_IMAGE"] = str(path)
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", code]
    else:
        raise ValueError("OCR required")
    result = subprocess.run(command, env=environment, capture_output=True, check=True, timeout=40)
    return result.stdout.decode("utf-8", errors="replace")


def png_text(path):
    if path.suffix.lower() != ".png":
        return ""
    found = []
    with path.open("rb") as file:
        if file.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("Invalid PNG")
        while len(header := file.read(8)) == 8:
            size, kind = struct.unpack(">I4s", header)
            if kind in {b"tEXt", b"zTXt", b"iTXt"}:
                if size > 200_000:
                    raise ValueError("Oversized metadata")
                part = file.read(size)
                if kind == b"zTXt":
                    key, payload = part.split(b"\0", 1)
                    part = key + b" " + zlib.decompressobj().decompress(payload[1:], 200_000)
                elif kind == b"iTXt":
                    key, payload = part.split(b"\0", 1)
                    flags, body = payload[:2], payload[2:]
                    *_, text = body.split(b"\0", 2)
                    if flags[:1] == b"\x01":
                        text = zlib.decompressobj().decompress(text, 200_000)
                    part = key + b" " + text
                found.append(part.decode("utf-8", errors="replace"))
                file.seek(4, os.SEEK_CUR)
            else:
                file.seek(size + 4, os.SEEK_CUR)
            if kind == b"IEND":
                break
    return "\n".join(found)


def scan(root, reference=None, include_generated=False):
    root = root.resolve()
    private = reference_patterns(reference)
    allow = root / "scripts" / "scanner-fixtures.json"
    exceptions = set(map(tuple, json.loads(allow.read_text(encoding="utf-8")))) if allow.is_file() else set()
    for folder, names, files in os.walk(root, followlinks=False):
        for name in sorted(names):
            directory = Path(folder) / name
            if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                yield directory.relative_to(root).as_posix(), 0, "Unreviewed link"
                names.remove(name)
        names[:] = sorted(n for n in names if n != ".git" and (include_generated or (
            n not in LOCAL_DIRECTORIES and not n.endswith(".egg-info")
        )))
        for name in sorted(files):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                yield rel, 0, "Unreviewed link"
                continue
            if path.name in {"profile.json", "discovery.json"} or path.name.startswith(".env") and path.name != ".env.example":
                yield rel, 0, "Local configuration"
            try:
                with path.open("rb") as file:
                    header = file.read(16)
                if header.startswith(b"SQLite format 3") or path.suffix.lower() in DB_EXTENSIONS:
                    yield rel, 0, "Local database"
                    continue
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    content = image_text(path) + "\n" + png_text(path)
                else:
                    content = path.read_text(encoding="utf-8-sig")
                    if "\x00" in content:
                        raise UnicodeError
            except (UnicodeError, ValueError, OSError, zlib.error, subprocess.SubprocessError):
                yield rel, 0, "Image OCR unavailable" if path.suffix.lower() in IMAGE_EXTENSIONS else "Unreviewed binary or unreadable file"
                continue
            for number, line in enumerate(content.splitlines(), 1):
                for category in findings(line, private):
                    if category.startswith(("Original", "Windows")) or (rel, category, digest(line.strip())) not in exceptions:
                        yield rel, number, category


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--reference", type=Path, help="Path to the private original profile, read without printing its values.")
    parser.add_argument("--include-generated", action="store_true", help="Also inspect installed dependencies and build artifacts.")
    args = parser.parse_args()
    try:
        result = list(scan(args.root.resolve(strict=True), args.reference, args.include_generated))
    except (OSError, ValueError, KeyError, TypeError):
        print("Scanner could not read its root, fixture exceptions or private reference.", file=sys.stderr)
        return 2
    print("File | Line | Category")
    for entry in result:
        print(*entry, sep=" | ")
    if not result:
        print("Clean")
    return int(bool(result))


if __name__ == "__main__":
    raise SystemExit(main())
