from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.tools.code_write_service import (
    CodeWriteBatchError,
    CodeWriteConfirmationRequired,
    CodeWritePathError,
    CodeWritePolicy,
    CodeWriteProjectError,
    CodeWriteSecretError,
    CodeWriteService,
    CodeWriteSizeError,
    PolicyFileError,
    load_policy_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def policy() -> CodeWritePolicy:
    return CodeWritePolicy(
        allowed_write_roots=(
            "example_app/lib/",
            "example_app/test/",
            "tools/",
            "docs/",
        ),
        allowed_read_roots=(
            "example_app/",
            "tools/",
            "docs/",
            "specs/",
        ),
        hard_max_bytes_per_file=4 * 1024,  # 4KB for tests
        require_confirmation_above_bytes=512,  # 512B to trigger quickly
        max_files_per_write_batch=5,
    )


@pytest.fixture
def svc(tmp_path: Path, policy: CodeWritePolicy) -> CodeWriteService:
    project_repo = tmp_path / "exampleapp"
    (project_repo / "example_app/lib/core").mkdir(parents=True)
    (project_repo / "example_app/test").mkdir(parents=True)
    (project_repo / "tools").mkdir()
    (project_repo / "docs").mkdir()
    (project_repo / ".larkagent/secrets").mkdir(parents=True)
    (project_repo / "specs/003-x").mkdir(parents=True)
    (project_repo / "example_app/lib/core/existing.dart").write_text(
        "// original\n", encoding="utf-8"
    )
    (project_repo / "specs/003-x/spec.md").write_text(
        "# spec", encoding="utf-8"
    )
    (project_repo / ".larkagent/secrets/keys.env").write_text(
        "TOKEN=1", encoding="utf-8"
    )
    audit = tmp_path / "audit"
    return CodeWriteService(
        project_roots={"exampleapp": project_repo},
        policies={"exampleapp": policy},
        audit_root=audit,
        trace_id="trace-test",
    )


# ---------------------------------------------------------------------------
# describe_policy / unknown project
# ---------------------------------------------------------------------------


def test_describe_policy(svc: CodeWriteService):
    out = svc.describe_policy("exampleapp")
    assert "example_app/lib/" in out["allowed_write_roots"]
    assert out["hard_max_bytes_per_file"] == 4 * 1024
    assert out["require_confirmation_above_bytes"] == 512


def test_unknown_project(svc: CodeWriteService):
    with pytest.raises(CodeWriteProjectError):
        svc.write_source(
            project_id="nope",
            relative_path="foo.txt",
            content="x",
            reason="r",
        )


# ---------------------------------------------------------------------------
# Path guards
# ---------------------------------------------------------------------------


def test_write_outside_allowed_roots_refused(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="server/foo.py",  # not in allowed_write_roots
            content="x",
            reason="r",
        )


def test_write_escape_refused(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="../../etc/passwd",
            content="x",
            reason="r",
        )


def test_write_denied_segment_refused(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="tools/secrets/token.env",
            content="x",
            reason="r",
        )


def test_write_absolute_path_refused(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="/etc/hosts",
            content="x",
            reason="r",
        )


def test_symlink_refused(tmp_path: Path, svc: CodeWriteService):
    project_repo = tmp_path / "exampleapp"
    link = project_repo / "example_app/lib/linked.dart"
    target_outside = tmp_path / "outside.dart"
    target_outside.write_text("pwn", encoding="utf-8")
    link.symlink_to(target_outside)
    with pytest.raises(CodeWritePathError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/linked.dart",
            content="x",
            reason="r",
        )


# ---------------------------------------------------------------------------
# Size gates
# ---------------------------------------------------------------------------


def test_write_small_file_succeeds(svc: CodeWriteService, tmp_path: Path):
    res = svc.write_source(
        project_id="exampleapp",
        relative_path="example_app/lib/new_dao.dart",
        content="class NewDao {}\n",
        reason="story 3-1",
    )
    assert res["path"] == "example_app/lib/new_dao.dart"
    assert res["is_new_file"] is True
    assert res["bytes_written"] == len("class NewDao {}\n".encode("utf-8"))
    assert (tmp_path / "exampleapp/example_app/lib/new_dao.dart").read_text(
        encoding="utf-8"
    ) == "class NewDao {}\n"


def test_write_above_confirm_threshold_refused_without_confirmed(svc: CodeWriteService):
    big = "x" * (600)  # > 512 threshold
    with pytest.raises(CodeWriteConfirmationRequired):
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/big.dart",
            content=big,
            reason="r",
        )


def test_write_above_confirm_threshold_allowed_with_confirmed(svc: CodeWriteService):
    big = "x" * (600)
    res = svc.write_source(
        project_id="exampleapp",
        relative_path="example_app/lib/big.dart",
        content=big,
        reason="r",
        confirmed=True,
    )
    assert res["bytes_written"] == 600


def test_write_above_hard_ceiling_refused_even_with_confirmed(svc: CodeWriteService):
    huge = "x" * (5 * 1024)  # > 4KB hard cap
    with pytest.raises(CodeWriteSizeError):
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/huge.dart",
            content=huge,
            reason="r",
            confirmed=True,
        )


def test_overwrite_delta_triggers_confirm(svc: CodeWriteService, tmp_path: Path):
    # existing.dart is tiny. Overwrite with a 600-byte payload → delta > 512.
    big = "y" * 600
    with pytest.raises(CodeWriteConfirmationRequired):
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/core/existing.dart",
            content=big,
            reason="r",
        )


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_batch_happy(svc: CodeWriteService, tmp_path: Path):
    res = svc.write_sources_batch(
        project_id="exampleapp",
        files=[
            {
                "relative_path": "example_app/lib/a.dart",
                "content": "class A {}\n",
            },
            {
                "relative_path": "example_app/test/a_test.dart",
                "content": "void main() {}\n",
                "reason": "test for A",
            },
        ],
        reason="story 3-2",
    )
    assert res["count"] == 2
    project_repo = tmp_path / "exampleapp"
    assert (project_repo / "example_app/lib/a.dart").is_file()
    assert (project_repo / "example_app/test/a_test.dart").is_file()


def test_batch_all_or_nothing(svc: CodeWriteService, tmp_path: Path):
    project_repo = tmp_path / "exampleapp"
    with pytest.raises(CodeWritePathError):
        svc.write_sources_batch(
            project_id="exampleapp",
            files=[
                {"relative_path": "example_app/lib/ok.dart", "content": "// ok\n"},
                # Denied segment in second file — whole batch must abort before any write.
                {"relative_path": "tools/secrets/bad.env", "content": "K=V"},
            ],
            reason="r",
        )
    assert not (project_repo / "example_app/lib/ok.dart").exists()


def test_batch_exceeds_max_files(svc: CodeWriteService):
    files = [
        {"relative_path": f"example_app/lib/f{i}.dart", "content": "//\n"}
        for i in range(6)  # policy max = 5
    ]
    with pytest.raises(CodeWriteBatchError):
        svc.write_sources_batch(
            project_id="exampleapp", files=files, reason="r"
        )


def test_batch_empty_refused(svc: CodeWriteService):
    with pytest.raises(CodeWriteBatchError):
        svc.write_sources_batch(project_id="exampleapp", files=[], reason="r")


# ---------------------------------------------------------------------------
# Read / list
# ---------------------------------------------------------------------------


def test_read_source_happy(svc: CodeWriteService):
    res = svc.read_source(
        project_id="exampleapp",
        relative_path="example_app/lib/core/existing.dart",
    )
    assert "original" in res["content"]
    assert res["truncated"] is False


def test_read_source_denied_outside_read_roots(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.read_source(
            project_id="exampleapp", relative_path="server/app.py"  # not in read roots
        )


def test_read_source_denied_on_secrets(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.read_source(
            project_id="exampleapp",
            relative_path=".larkagent/secrets/keys.env",
        )


def test_list_paths_root_shows_read_roots(svc: CodeWriteService):
    res = svc.list_paths(project_id="exampleapp", sub_path="")
    names = {e["name"] for e in res["entries"]}
    assert "example_app" in names
    assert "docs" in names


def test_list_paths_sub_dir(svc: CodeWriteService):
    res = svc.list_paths(
        project_id="exampleapp", sub_path="example_app/lib"
    )
    assert res["exists"] is True
    names = {e["name"] for e in res["entries"]}
    assert "core" in names


def test_list_paths_escape_refused(svc: CodeWriteService):
    with pytest.raises(CodeWritePathError):
        svc.list_paths(project_id="exampleapp", sub_path="../")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Policy JSONL loader
# ---------------------------------------------------------------------------


@pytest.fixture
def default_pol() -> CodeWritePolicy:
    return CodeWritePolicy(
        allowed_write_roots=("fallback/",),
        allowed_read_roots=("fallback/",),
        hard_max_bytes_per_file=1024,
        require_confirmation_above_bytes=256,
        max_files_per_write_batch=10,
    )


def test_load_policy_file_missing_returns_empty(tmp_path: Path, default_pol: CodeWritePolicy):
    out = load_policy_file(
        tmp_path / "nope.jsonl", default_policy=default_pol
    )
    assert out == {}


def test_load_policy_file_happy(tmp_path: Path, default_pol: CodeWritePolicy):
    gv_root = tmp_path / "gv"
    gv_root.mkdir()
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        "# comment\n"
        "\n"
        '{"project_id":"exampleapp","project_repo_root":"' + str(gv_root) + '",'
        '"allowed_write_roots":["lib/","test/"],'
        '"hard_max_bytes_per_file":2048}\n',
        encoding="utf-8",
    )
    out = load_policy_file(pfile, default_policy=default_pol)
    assert "exampleapp" in out
    entry = out["exampleapp"]
    assert entry.project_repo_root == gv_root
    assert entry.policy.allowed_write_roots == ("lib/", "test/")
    assert entry.policy.hard_max_bytes_per_file == 2048
    # require_confirmation_above_bytes inherited from default:
    assert entry.policy.require_confirmation_above_bytes == 256


def test_load_policy_file_denied_union_with_hardcoded(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp","allowed_write_roots":["lib/"],'
        '"denied_path_segments":["custom_secret"]}\n',
        encoding="utf-8",
    )
    out = load_policy_file(pfile, default_policy=default_pol)
    denied = out["exampleapp"].policy.denied_path_segments
    # Hardcoded baseline always present:
    for needle in (".env", ".git", "secrets", ".pem", ".key", "id_ed25519", "id_rsa"):
        assert needle in denied
    # Custom one is merged in:
    assert "custom_secret" in denied


def test_load_policy_file_fallback_root(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    gv = tmp_path / "gv"
    gv.mkdir()
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp","allowed_write_roots":["lib/"]}\n',
        encoding="utf-8",
    )
    out = load_policy_file(
        pfile,
        default_policy=default_pol,
        fallback_project_roots={"exampleapp": gv},
    )
    assert out["exampleapp"].project_repo_root == gv


def test_load_policy_file_bad_json_aborts(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(PolicyFileError):
        load_policy_file(pfile, default_policy=default_pol)


def test_load_policy_file_missing_project_id_aborts(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text('{"allowed_write_roots":["lib/"]}\n', encoding="utf-8")
    with pytest.raises(PolicyFileError):
        load_policy_file(pfile, default_policy=default_pol)


def test_load_policy_file_empty_write_roots_inherits_default(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp"}\n', encoding="utf-8"
    )
    out = load_policy_file(pfile, default_policy=default_pol)
    assert out["exampleapp"].policy.allowed_write_roots == default_pol.allowed_write_roots


def test_load_policy_file_negative_int_refused(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp","allowed_write_roots":["lib/"],'
        '"hard_max_bytes_per_file":-1}\n',
        encoding="utf-8",
    )
    with pytest.raises(PolicyFileError):
        load_policy_file(pfile, default_policy=default_pol)


def test_load_policy_file_bad_list_refused(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp","allowed_write_roots":"lib/"}\n',
        encoding="utf-8",
    )
    with pytest.raises(PolicyFileError):
        load_policy_file(pfile, default_policy=default_pol)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_audit_log_written(svc: CodeWriteService, tmp_path: Path):
    svc.write_source(
        project_id="exampleapp",
        relative_path="example_app/lib/audited.dart",
        content="class Audited {}\n",
        reason="story-X: audit check",
    )
    audit_file = tmp_path / "audit" / "trace-test.jsonl"
    assert audit_file.is_file()
    content = audit_file.read_text(encoding="utf-8")
    assert "example_app/lib/audited.dart" in content
    assert "story-X: audit check" in content
    assert "sha256_after" in content


# ---------------------------------------------------------------------------
# Secret-scanner integration — writes with credential material are refused.
# These tests close the "LLM hallucinates / is prompt-injected to embed a
# key in source" threat vector. Scanner has its own unit coverage in
# test_secret_scanner.py; here we verify it's wired into the write path.
# ---------------------------------------------------------------------------


def test_write_source_refused_when_content_contains_private_key(
    svc: CodeWriteService, tmp_path: Path
):
    body = (
        "// auto-generated\n"
        "const KEY = `-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAz\n"
        "-----END RSA PRIVATE KEY-----`;\n"
    )
    with pytest.raises(CodeWriteSecretError) as ei:
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/leak.dart",
            content=body,
            reason="should be refused",
        )
    assert ei.value.code == "CODE_WRITE_SECRET_DETECTED"
    assert any(f.rule_id == "private_key_block" for f in ei.value.findings)
    # Ensure nothing landed on disk — write refused, file absent.
    assert not (tmp_path / "exampleapp" / "example_app/lib/leak.dart").exists()


def test_write_source_refused_for_openai_key(svc: CodeWriteService):
    body = 'const apiKey = "sk-' + "A" * 48 + '";\n'
    with pytest.raises(CodeWriteSecretError) as ei:
        svc.write_source(
            project_id="exampleapp",
            relative_path="example_app/lib/client.dart",
            content=body,
            reason="should be refused",
        )
    ids = {f.rule_id for f in ei.value.findings}
    assert ids & {"openai_api_key", "generic_quoted_credential"}


def test_batch_refused_when_any_file_contains_secret(svc: CodeWriteService, tmp_path: Path):
    """All-or-nothing: a secret in file B blocks the clean file A too."""
    files = [
        {
            "relative_path": "example_app/lib/a.dart",
            "content": "// clean\nclass A {}\n",
        },
        {
            "relative_path": "example_app/lib/b.dart",
            "content": 'const TOKEN = "ghp_' + "a" * 40 + '";\n',
        },
    ]
    with pytest.raises(CodeWriteSecretError):
        svc.write_sources_batch(
            project_id="exampleapp",
            files=files,
            reason="batch should be refused atomically",
        )
    # Neither file should have been written.
    assert not (tmp_path / "exampleapp" / "example_app/lib/a.dart").exists()
    assert not (tmp_path / "exampleapp" / "example_app/lib/b.dart").exists()


def test_write_source_still_allows_env_var_reference(svc: CodeWriteService):
    """The recommended pattern — loading from env vars, not hardcoding —
    must not be flagged."""
    body = (
        "import 'dart:io';\n"
        "final token = Platform.environment['OPENAI_API_KEY'] ?? '';\n"
        "// note: api_key is read from env, never committed.\n"
    )
    out = svc.write_source(
        project_id="exampleapp",
        relative_path="example_app/lib/env_ref.dart",
        content=body,
        reason="clean: env-var reference",
    )
    assert out["bytes_written"] == len(body.encode("utf-8"))


def test_write_source_allows_placeholder_in_example(svc: CodeWriteService):
    body = 'api_key = "<REPLACE_ME>"\n'
    out = svc.write_source(
        project_id="exampleapp",
        relative_path="docs/example.txt",
        content=body,
        reason="placeholder",
    )
    assert out["is_new_file"] is True


# ---------------------------------------------------------------------------
# Protected-branches policy field
# ---------------------------------------------------------------------------


def test_default_policy_protects_main_and_master():
    pol = CodeWritePolicy(allowed_write_roots=("lib/",))
    assert pol.is_protected_branch("main")
    assert pol.is_protected_branch("master")
    assert pol.is_protected_branch("feature/foo") is False


def test_empty_branch_treated_as_protected():
    """Defensive: refuse to operate when the branch lookup somehow
    returned empty."""
    pol = CodeWritePolicy(allowed_write_roots=("lib/",))
    assert pol.is_protected_branch("") is True


def test_policy_file_merges_protected_branches(
    tmp_path: Path, default_pol: CodeWritePolicy
):
    gv = tmp_path / "gv"
    gv.mkdir()
    pfile = tmp_path / "policies.jsonl"
    pfile.write_text(
        '{"project_id":"exampleapp","allowed_write_roots":["lib/"],'
        '"protected_branches":["release","prod"]}\n',
        encoding="utf-8",
    )
    out = load_policy_file(
        pfile,
        default_policy=default_pol,
        fallback_project_roots={"exampleapp": gv},
    )
    pol = out["exampleapp"].policy
    # main + master are ALWAYS included even if the user overrides.
    assert "main" in pol.protected_branches
    assert "master" in pol.protected_branches
    # plus the user-supplied values.
    assert "release" in pol.protected_branches
    assert "prod" in pol.protected_branches


def test_describe_policy_exposes_protected_branches(svc: CodeWriteService):
    out = svc.describe_policy("exampleapp")
    assert "protected_branches" in out
    assert "main" in out["protected_branches"]
