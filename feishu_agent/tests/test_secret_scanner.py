"""Unit tests for ``feishu_agent.tools.secret_scanner``.

Goals:
- Every rule actually trips on a real-looking positive example.
- Common non-secret code (normal source, test fixtures with obviously
  fake values) does NOT trip.
- ``ensure_clean`` raises a ``SecretDetectedError`` carrying the findings
  and its string message includes rule id + line number so the agent
  (and humans) can debug without us dumping the secret preview here.
"""

from __future__ import annotations

import pytest

from feishu_agent.tools import secret_scanner

# ---------------------------------------------------------------------------
# Positive cases — each rule should trip
# ---------------------------------------------------------------------------


def _assert_hits(content: str, rule_id: str) -> None:
    findings = secret_scanner.scan(content)
    assert any(f.rule_id == rule_id for f in findings), (
        f"expected rule {rule_id!r} to trip on: {content!r}, got: {findings}"
    )


def test_private_key_block_pem_rsa() -> None:
    body = (
        "const KEY = '-----BEGIN RSA PRIVATE KEY-----\\n"
        "MIIEowIBAAKCAQEAz...=\\n"
        "-----END RSA PRIVATE KEY-----'"
    )
    _assert_hits(body, "private_key_block")


def test_private_key_block_openssh() -> None:
    _assert_hits("-----BEGIN OPENSSH PRIVATE KEY-----\nxxx\n", "private_key_block")


def test_private_key_block_plain() -> None:
    _assert_hits("-----BEGIN PRIVATE KEY-----\nxxx\n", "private_key_block")


def test_ssh_authorized_key_material() -> None:
    body = (
        "ssh-ed25519 "
        + "A" * 120
        + " deploy@laptop"
    )
    _assert_hits(body, "ssh_public_key_line")


def test_aws_access_key_id() -> None:
    _assert_hits("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "aws_access_key_id")


def test_aws_secret_access_key_assignment() -> None:
    body = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    _assert_hits(body, "aws_secret_access_key")


def test_github_token_pat() -> None:
    _assert_hits("TOKEN = ghp_" + "a" * 40, "github_token")


def test_github_fine_grained_pat() -> None:
    _assert_hits("github_pat_" + "A" * 82, "github_fine_grained_pat")


def test_openai_style_api_key() -> None:
    _assert_hits("OPENAI_API_KEY=sk-" + "A" * 40, "openai_api_key")


def test_openai_project_key() -> None:
    _assert_hits("key='sk-proj-" + "A" * 40 + "'", "openai_api_key")


def test_anthropic_api_key() -> None:
    _assert_hits("key='sk-ant-" + "A" * 40 + "'", "anthropic_api_key")


def test_gcp_api_key() -> None:
    _assert_hits("const k = 'AIza" + "A" * 35 + "'", "gcp_api_key")


def test_gcp_service_account_json_fragment() -> None:
    body = '{"type":"service_account","private_key":"-----BEGIN PRIVATE KEY-----\\nM..."}'
    findings = secret_scanner.scan(body)
    rules = {f.rule_id for f in findings}
    assert "gcp_service_account_json" in rules or "private_key_block" in rules


def test_slack_token() -> None:
    _assert_hits("SLACK=xoxb-" + "1" * 30, "slack_token")


def test_feishu_app_secret_assignment() -> None:
    _assert_hits('APP_SECRET = "' + "a" * 24 + '"', "feishu_app_secret_assignment")


def test_generic_quoted_credential_assignment() -> None:
    # Enough entropy in quotes tied to a credential-looking name.
    _assert_hits(
        'const API_KEY = "' + "A" * 40 + '";',
        "generic_quoted_credential",
    )


# ---------------------------------------------------------------------------
# Negative cases — must NOT trip on normal code / obviously-fake values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "",
        "def foo(x):\n    return x + 1\n",
        "# read your API key from os.environ['OPENAI_API_KEY']\n",
        # documentation prose describing how to *not* hardcode
        "See README on how to set api_key via env vars.\n",
        # obvious placeholder tokens
        'api_key = "<REPLACE_ME>"\n',
        'api_key = "YOUR_TOKEN_HERE"\n',
        'api_key = "xxx"\n',
        # test fixtures that are plainly fake
        'api_key = "test-key"\n',
        'api_key = "fake-secret"\n',
        # short base64-ish blob that is NOT a credential
        'hash = "abc123"\n',
        # structural mentions without credential values
        "password field is required by the schema.\n",
    ],
)
def test_clean_content_passes(body: str) -> None:
    assert secret_scanner.scan(body) == []
    secret_scanner.ensure_clean(body)  # no raise


# ---------------------------------------------------------------------------
# ensure_clean
# ---------------------------------------------------------------------------


def test_ensure_clean_raises_with_findings_and_path() -> None:
    body = "line1\nAKIAIOSFODNN7EXAMPLE\nline3\n"
    with pytest.raises(secret_scanner.SecretDetectedError) as ei:
        secret_scanner.ensure_clean(body, path="example_app/lib/secret.dart")
    assert ei.value.findings, "findings must be non-empty"
    assert any(f.rule_id == "aws_access_key_id" for f in ei.value.findings)
    assert "example_app/lib/secret.dart" in str(ei.value)
    assert "SECRET_DETECTED" == ei.value.code


def test_line_numbers_accurate() -> None:
    body = (
        "line 1 clean\n"
        "line 2 clean\n"
        "line 3 ghp_" + "a" * 40 + "\n"
        "line 4 clean\n"
    )
    findings = secret_scanner.scan(body)
    assert findings
    assert findings[0].line_number == 3


def test_preview_is_redacted() -> None:
    token = "sk-" + "A" * 48
    findings = secret_scanner.scan(f"key = '{token}'")
    assert findings
    preview = findings[0].matched_preview
    assert token not in preview
    assert "***" in preview
