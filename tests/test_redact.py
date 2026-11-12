"""Secrets must never survive redaction.

Fake secrets are assembled at runtime so the repository never contains strings
that look like real credentials to secret scanners.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from agentlens import paths
from agentlens.redact import clean, fingerprint, redact, redact_obj

A = "A1b2C3d4E5f6G7h8J9k0" * 3  # 60 alnum chars
UP = "ABCDEFGHIJ0123456789"

SECRETS = {
    "anthropic": "sk-" + "ant-api03-" + A,
    "openai": "sk-" + "proj-" + A,
    "openai_legacy": "sk-" + A[:48],
    "ghp": "gh" + "p_" + A[:36],
    "gho": "gh" + "o_" + A[:36],
    "github_pat": "github" + "_pat_" + "11ABCDEFG0" + "_" + A[:50],
    "aws": "AK" + "IA" + UP[:16],
    "aws_sts": "AS" + "IA" + UP[:16],
    "slack_bot": "xo" + "xb-" + "1234567890-" + A[:24],
    "slack_user": "xo" + "xp-" + "1234567890-" + A[:24],
    "google": "AI" + "za" + A[:35],
    "jwt": "ey" + "JhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + A[:30],
}


@pytest.mark.parametrize("name", sorted(SECRETS))
@pytest.mark.parametrize(
    "template",
    [
        "{s}",
        "export TOKEN_X={s}",
        "curl -H 'X-Key: {s}' https://api.example.com",
        'echo "{s}" | pbcopy',
        '{{"key": "{s}"}}',
        "prefix_{s} suffix",
        "id={s}&next=1",
        "line1\nvalue={s}\nline3",
    ],
)
def test_known_token_shapes_never_survive(name, template):
    secret = SECRETS[name]
    text = template.format(s=secret)
    out = redact(text)
    assert secret not in out
    # no long fragment of the secret either
    assert secret[-12:] not in out
    assert "[REDACTED:" in out


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("curl -H 'Authorization: Bearer abc.DEF-123_xyz~987' x", "abc.DEF-123_xyz~987"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("authorization: token deadbeefcafe1234", "deadbeefcafe1234"),
        ("git clone https://alice:hunter2pass@github.com/x/y.git", "hunter2pass"),
        ("psql postgres://admin:s3cr3t%21@db.local:5432/app", "s3cr3t%21"),
        ("DATABASE_PASSWORD=correcthorse ./run", "correcthorse"),
        ("export STRIPE_SECRET_KEY='rk_live_zzzz9999'", "rk_live_zzzz9999"),
        ("API_KEY=plainvalue123 npm start", "plainvalue123"),
        ("MY_TOKEN=\"quoted value here\"", "quoted value here"),
        ('{"password": "pa55w0rd!"}', "pa55w0rd!"),
        ("password: yamlsecret99", "yamlsecret99"),
        ("mysql --password=dbpass77 -u root", "dbpass77"),
        ("tool --api-key abcd1234efgh", "abcd1234efgh"),
        ("gh auth --with-token 'tok en'", "tok en"),
        ("client_secret=xyzzy42", "xyzzy42"),
        ("SESSION_ID=abcdef0123", "abcdef0123"),
    ],
)
def test_contextual_secrets(text, leaked):
    out = redact(text)
    assert leaked not in out, out


def test_private_key_block_fully_removed():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + A
    for kind in ("RSA ", "EC ", "OPENSSH ", ""):
        block = f"-----BEGIN {kind}PRIVATE KEY-----\n{body}\n{body}\n-----END {kind}PRIVATE KEY-----"
        out = redact(f"cat > id <<EOF\n{block}\nEOF")
        assert body not in out
        assert "[REDACTED:private_key]" in out
        assert out.endswith("EOF")


def test_truncated_private_key_block_removed():
    out = redact("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0" + A)
    assert A not in out


def test_benign_text_preserved():
    for text in [
        "git status",
        "ls -la /tmp",
        "pytest -q tests/test_x.py",
        "cd $PWD && make",
        "git commit --author='Jane <j@x.io>' -m 'fix'",
        "open https://example.com/docs?page=2",
        "monkey=banana",
        "Exit code 1",
        "./run --name build-secret-santa now",
        "npm run task-runner-configuration",
        "python desk-organizer-application.py",
    ]:
        assert redact(text) == text, text


@pytest.mark.parametrize("name", ["ghp", "github_pat", "aws", "slack_bot", "google", "jwt"])
def test_distinctive_prefixes_caught_even_when_glued(name):
    secret = SECRETS[name]
    assert secret not in redact("prefix" + secret)


def test_redaction_is_idempotent():
    text = "API_KEY=" + SECRETS["anthropic"] + " Bearer " + A
    once = redact(text)
    assert redact(once) == once


def test_redact_obj_recurses_and_blanks_secret_keys():
    obj = {
        "command": "echo " + SECRETS["ghp"],
        "nested": [{"api_key": "plainvalue"}, {"note": SECRETS["aws"]}],
        "password": 12345,
        "count": 3,
        "ok": True,
    }
    out = redact_obj(obj)
    dumped = json.dumps(out)
    for s in (SECRETS["ghp"], "plainvalue", SECRETS["aws"], "12345"):
        assert s not in dumped
    assert out["count"] == 3 and out["ok"] is True


def test_clean_redacts_before_truncating():
    secret = SECRETS["openai"]
    out = clean("abc " + secret, limit=12)
    # a naive truncate-then-redact would keep a secret prefix that no longer matches
    assert secret[:10] not in out


def test_fingerprint_is_keyed_and_stable():
    a = fingerprint("yes")
    assert a == fingerprint("yes")
    assert a != fingerprint("no")
    import hashlib

    assert a != hashlib.sha256(b"yes").hexdigest()
    mode = stat.S_IMODE(os.stat(paths.salt_path()).st_mode)
    assert mode == 0o600
