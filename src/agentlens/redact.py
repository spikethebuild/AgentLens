"""Secret redaction and privacy-preserving fingerprints.

Everything AgentLens writes to disk passes through ``redact`` first. Patterns
err on the side of over-redaction: losing a harmless string is acceptable,
leaking a credential is not.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from typing import Any, Callable

from agentlens import paths


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


_SECRET_KEY_NAME = re.compile(
    r"(secret|token|passw(or)?d|passphrase|api[_.-]?key|access[_.-]?key|private[_.-]?key"
    r"|client[_.-]?secret|credential|session[_.-]?id|cookie|(^|[_.-])key$|^key$|^pass$|^pwd$)",
    re.IGNORECASE,
)

# (kind, pattern, replacement). Order matters: specific token shapes first.
_PATTERNS: list[tuple[str, re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
            re.DOTALL,
        ),
        _tag("private_key"),
    ),
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{8,}"), _tag("anthropic_key")),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"), _tag("openai_key")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), _tag("github_token")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), _tag("github_token")),
    ("aws_key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"), _tag("aws_key")),
    ("slack_token", re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"), _tag("slack_token")),
    ("google_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), _tag("google_key")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
        _tag("jwt"),
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}"),
        lambda m: f"{m.group(1)} {_tag('bearer')}",
    ),
    (
        "auth_header",
        re.compile(r"(?i)\b(authorization\s*:\s*)(?!bearer \[REDACTED)([A-Za-z]+\s+)?[^\s'\"]+"),
        lambda m: f"{m.group(1)}{m.group(2) or ''}{_tag('auth')}",
    ),
    (
        "url_userinfo",
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@'\"]+:[^/\s@'\"]+@"),
        lambda m: f"{m.group(1)}{_tag('userinfo')}@",
    ),
    (
        "secret_flag",
        re.compile(
            r"(?i)(?<!\S)(--?[a-z0-9-]*(?:password|passwd|passphrase|token|secret|api-?key|access-?key)[a-z0-9-]*)"
            r"(\s+|=)(\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
        ),
        lambda m: f"{m.group(1)}{m.group(2)}{_tag('flag')}",
    ),
]

# KEY=value / "key": "value" / key: value, redacted only when the key looks secret.
_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P<sep>[\"']?\s*[=:]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s'\",;&|)}\]]+)"
)


def _assignment_sub(m: re.Match[str]) -> str:
    key = m.group("key")
    if not _SECRET_KEY_NAME.search(key):
        return m.group(0)
    val = m.group("val")
    if val.startswith("[REDACTED") or val.strip("\"'").startswith("[REDACTED"):
        return m.group(0)
    if val.startswith("//"):  # "http://..." style false positive
        return m.group(0)
    quote = val[0] if val[:1] in ("'", '"') else ""
    return f"{key}{m.group('sep')}{quote}{_tag('value')}{quote}"


def redact(text: str) -> str:
    """Return ``text`` with every recognised secret replaced by a [REDACTED:kind] tag."""
    if not text:
        return text
    for _kind, pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return _ASSIGNMENT.sub(_assignment_sub, text)


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings in a JSON-like structure; also blank secret-named keys."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_NAME.search(k) and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out[k] = _tag("value")
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def clean(text: Any, limit: int) -> str | None:
    """Redact first, then truncate (so a truncated secret is never half-kept)."""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    return truncate(redact(text), limit)


# ---------------------------------------------------------------- fingerprints


def _load_salt() -> bytes:
    path = paths.salt_path()
    try:
        data = path.read_bytes()
        if len(data) >= 16:
            return data
    except FileNotFoundError:
        pass
    paths.ensure_dir(path.parent)
    tmp = path.with_name(f".salt-{os.getpid()}-{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secrets.token_bytes(32))
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)  # atomic; fails if another process won the race
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)
    return path.read_bytes()


def fingerprint(text: str) -> str:
    """Keyed hash of ``text``. A bare sha256 of a short prompt is trivially reversible."""
    return hmac.new(_load_salt(), text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()
