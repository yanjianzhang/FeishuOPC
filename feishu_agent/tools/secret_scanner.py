"""Pre-write secret scanner.

Hard rule: whenever the agent is about to **write** content to a project
source tree or a workflow artifact, the content passes through this
scanner first. If we detect patterns that are **never legitimately** in
source-controlled code (PEM/OpenSSH private keys, AWS access keys,
common provider tokens, GitHub PATs, etc.), we refuse the write.

This is a defense-in-depth measure *inside the agent process*. It does
not replace a pre-commit hook or CI secret-scan in the target project.
It addresses one specific threat: the LLM — or the caller — smuggling a
credential through the tool-calling layer into a file we would then
commit on the user's behalf.

Design principles:

- **Fail-closed**: any match → refuse. No "warn and continue."
- **Very low false-positive rate**: patterns chosen so that a real
  production codebase would virtually never trip them. Generic things
  like "password =" or "api_key =" are NOT flagged (too noisy — those
  are legitimate in config/test code). We only flag literal key
  *material* that has no business being in source.
- **No regex over-engineering**: the patterns are the classic set
  maintained by ``trufflehog`` / ``detect-secrets`` / GitHub push
  protection, trimmed to the items that are both (a) high-confidence
  signals and (b) relevant to our stack (SSH / AWS / OpenAI-compatible
  vendors / Feishu / GitHub).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


@dataclass(frozen=True)
class SecretFinding:
    rule_id: str
    description: str
    line_number: int
    matched_preview: str  # never the full match — just a short redacted preview


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    description: str
    pattern: Pattern[str]


# ---------------------------------------------------------------------------
# Rules (ordered roughly by how deterministic the pattern is)
# ---------------------------------------------------------------------------


def _compile(pattern: str) -> Pattern[str]:
    return re.compile(pattern)


_RULES: tuple[_Rule, ...] = (
    # ---- Private key blocks: impossible to justify in source code ----
    _Rule(
        "private_key_block",
        "PEM/OpenSSH private key block",
        _compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    # ---- SSH public key lines (authorized_keys-style). These are
    # public material, not secret, but we refuse them in committed
    # artifacts because (a) they let an attacker target the owner and
    # (b) where there's a public key there's often a private key nearby,
    # and the private-key rule above doesn't catch raw base64 keyfile
    # bodies that happen to be outside a PEM block. Name the rule
    # accurately so the SecretDetectedError message doesn't mislead
    # operators triaging false positives.
    _Rule(
        "ssh_public_key_line",
        "SSH public key line (authorized_keys-style)",
        _compile(r"(?:ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/]{100,}"),
    ),
    # ---- AWS access keys ----
    _Rule(
        "aws_access_key_id",
        "AWS access key id",
        _compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    # AWS secret access key in `aws_secret_access_key = <40 char base64>`
    _Rule(
        "aws_secret_access_key",
        "AWS secret access key assignment",
        _compile(
            r"""aws_secret_access_key\s*[:=]\s*["']?[A-Za-z0-9/+=]{40}["']?"""
        ),
    ),
    # ---- GitHub tokens (PAT / OAuth / app / fine-grained / refresh) ----
    _Rule(
        "github_token",
        "GitHub token",
        _compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}\b"),
    ),
    _Rule(
        "github_fine_grained_pat",
        "GitHub fine-grained PAT",
        _compile(r"\bgithub_pat_[A-Za-z0-9_]{80,}\b"),
    ),
    # ---- OpenAI & OpenAI-compatible providers ----
    _Rule(
        "openai_api_key",
        "OpenAI-style API key",
        _compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    _Rule(
        "anthropic_api_key",
        "Anthropic API key",
        _compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b"),
    ),
    # ---- Google Cloud ----
    _Rule(
        "gcp_service_account_json",
        "GCP service-account private_key field",
        _compile(r'"private_key"\s*:\s*"-----BEGIN'),
    ),
    _Rule(
        "gcp_api_key",
        "Google API key",
        _compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    # ---- Slack / Discord / other common bots ----
    _Rule(
        "slack_token",
        "Slack token",
        _compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    _Rule(
        "discord_bot_token",
        "Discord bot token",
        _compile(
            r"\b[MN][A-Za-z0-9_-]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}\b"
        ),
    ),
    # ---- Generic high-entropy credential-style assignments ----
    # Only flag when the token is >= 24 chars of [A-Za-z0-9_-] **inside a
    # string literal** tied to a name like secret/token/password — keeps
    # the false-positive rate sane.
    _Rule(
        "generic_quoted_credential",
        "High-entropy credential assignment",
        _compile(
            r"""(?ix)
            \b(?:api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|
               bearer[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?token|
               app[_-]?secret)
            \s*[:=]\s*
            ["']([A-Za-z0-9_\-]{24,})["']
            """
        ),
    ),
    # ---- Feishu / Lark ----
    _Rule(
        "feishu_app_secret_assignment",
        "Feishu/Lark app_secret assignment",
        _compile(
            r"""(?i)\b(?:app[_-]?secret|lark[_-]?secret|feishu[_-]?secret)\s*[:=]\s*["'][A-Za-z0-9]{20,}["']"""
        ),
    ),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SecretDetectedError(Exception):
    """Raised when a write is refused because its content contains secrets."""

    code = "SECRET_DETECTED"

    def __init__(self, findings: list[SecretFinding], *, path: str | None = None) -> None:
        self.findings = findings
        self.path = path
        summary = ", ".join(f"{f.rule_id}@L{f.line_number}" for f in findings)
        msg = (
            f"refused: content appears to contain secret(s) [{summary}]."
            f" If this is a false positive, sanitize the value or load it from an env var."
        )
        if path:
            msg = f"{path}: {msg}"
        super().__init__(msg)


def _redact_preview(s: str, keep: int = 4) -> str:
    s = s.strip()
    if len(s) <= keep * 2:
        return s[:keep] + "***"
    return s[:keep] + "***" + s[-keep:]


def scan(content: str) -> list[SecretFinding]:
    """Scan ``content``; return a list of findings (empty = clean)."""
    findings: list[SecretFinding] = []
    if not content:
        return findings
    # Compute line starts once for offset→line conversion.
    line_starts: list[int] = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            line_starts.append(i + 1)

    def offset_to_line(off: int) -> int:
        # Binary search would be faster for huge files; linear is fine at
        # our scales.
        lineno = 1
        for i, start in enumerate(line_starts):
            if start > off:
                break
            lineno = i + 1
        return lineno

    for rule in _RULES:
        for m in rule.pattern.finditer(content):
            findings.append(
                SecretFinding(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    line_number=offset_to_line(m.start()),
                    matched_preview=_redact_preview(m.group(0)),
                )
            )
    return findings


def ensure_clean(content: str, *, path: str | None = None) -> None:
    """Scan ``content``; raise ``SecretDetectedError`` on any hit."""
    findings = scan(content)
    if findings:
        raise SecretDetectedError(findings, path=path)
