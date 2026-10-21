"""Public data-leak gate for released benchmark data.

Scans plain text and the contents of OOXML (xlsx/docx/pptx), zip, tar/tar.gz and
SQLite files.  The rules are format based (no original values or hashes of them),
so this module can be published.  Unparseable containers are reported as errors
instead of being skipped silently.  Findings carry rule, location and count only.
"""
from __future__ import annotations

import io
import re
import sqlite3
import tarfile
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

OOXML = ('.xlsx', '.docx', '.pptx', '.zip')
TARS = ('.tar.gz', '.tgz', '.tar')
DATABASES = ('.db', '.sqlite', '.sqlite3')
SKIP = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.pyc')
MAX_DEPTH = 3
MAX_MEMBER = 64 * 1024 * 1024

ALLOWED_HOSTS = {'vip.' + 'kingdee.com'}  # public community article links (see NOTICE)
ALLOWED_EMAIL_DOMAINS = {'example.com', 'example.org', 'example.net'}
PLACEHOLDER_USCC = '91000000MK'

HOST = re.compile(r'\b(?:[a-z0-9-]+\.)+kingdee\.com\b', re.I)
PRIVATE_IP = re.compile(r'(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org|edu|gov|io|co|hk|sg|me|top))\b')
MOBILE = re.compile(r'(?<![\d.])1[3-9]\d{9}(?![\d.])')
ID_CARD = re.compile(r'(?<![\dA-Za-z])[1-6]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dA-Za-z])')
USCC = re.compile(r'(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Z])')
USCC_CHARS = '0123456789ABCDEFGHJKLMNPQRTUWXY'


def _id_card_valid(value: str) -> bool:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    total = sum(int(c) * w for c, w in zip(value[:17], weights))
    return '10X98765432'[total % 11] == value[17].upper()


def _uscc_valid(value: str) -> bool:
    weights = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]
    total = sum(USCC_CHARS.index(c) * w for c, w in zip(value[:17], weights))
    return USCC_CHARS[(31 - total % 31) % 31] == value[17]


def scan_text(text: str) -> Counter:
    found: Counter = Counter()
    for m in HOST.finditer(text):
        if m[0].lower() not in ALLOWED_HOSTS:
            found['internal_host'] += 1
    found['private_ip'] += len(PRIVATE_IP.findall(text))
    found['email'] += sum(1 for m in EMAIL.finditer(text) if m[1].lower() not in ALLOWED_EMAIL_DOMAINS)
    found['mobile'] += len(MOBILE.findall(text))
    found['id_card'] += sum(1 for m in ID_CARD.finditer(text) if _id_card_valid(m[0]))
    found['uscc'] += sum(1 for m in USCC.finditer(text)
                         if not m[0].startswith(PLACEHOLDER_USCC) and re.search('[A-Z]', m[0]) and _uscc_valid(m[0]))
    return +found


def _members(name: str, data: bytes, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ValueError(f'nesting too deep: {name}')
    low = name.lower()
    if low.endswith(SKIP):
        return
    if low.endswith(OOXML):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MAX_MEMBER or '..' in Path(info.filename).parts:
                    raise ValueError(f'unsafe archive member: {name}!{info.filename}')
                yield from _members(f'{name}!{info.filename}', archive.read(info), depth + 1)
    elif low.endswith(TARS):
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                if member.issym() or member.islnk() or member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise ValueError(f'unsafe archive member: {name}!{member.name}')
                if member.isfile():
                    if member.size > MAX_MEMBER:
                        raise ValueError(f'archive member too large: {name}!{member.name}')
                    yield from _members(f'{name}!{member.name}', archive.extractfile(member).read(), depth + 1)
    elif low.endswith(DATABASES):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'scan.db'
            path.write_bytes(data)
            con = sqlite3.connect(path)
            try:
                for (table,) in con.execute("select name from sqlite_master where type='table'"):
                    rows = con.execute(f'select * from "{table}"')
                    yield f'{name}!{table}', '\n'.join(' '.join(map(str, row)) for row in rows)
            finally:
                con.close()
    elif b'\0' not in data[:4096]:
        yield name, data.decode('utf-8', 'replace')


def scan_paths(paths, root: Path) -> list[str]:
    """Return error strings like ``data leak <rule>: <location> (<count>)``."""
    errors = []
    for path in sorted(paths):
        rel = path.relative_to(root).as_posix()
        try:
            for location, text in _members(rel, path.read_bytes()):
                for rule, count in sorted(scan_text(text).items()):
                    errors.append(f'data leak {rule}: {location} ({count})')
        except (zipfile.BadZipFile, tarfile.TarError, sqlite3.DatabaseError, ValueError, OSError) as exc:
            errors.append(f'data scan failed: {rel} ({type(exc).__name__})')
    return errors
