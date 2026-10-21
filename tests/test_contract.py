"""COP-752 public contract tests for the fleet session-bootstrap core."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


def test_trusted_partition_is_stable_across_session_attempt_ids() -> None:
    from fleet_session_bootstrap.contract import (
        BootstrapRequest,
        TrustedIdentity,
        validated_partition,
    )

    trusted = TrustedIdentity(
        tenant="fleet",
        profile="jarvis",
        runtime="hermes",
        host_or_project="studio",
        salt="test-install-salt",
    )
    first = BootstrapRequest(
        tenant="fleet",
        profile="jarvis",
        runtime="hermes",
        host_or_project="studio",
        session_id="attempt-one",
        cwd="/workspace",
        objective="inspect a repository",
    )
    second = BootstrapRequest(
        tenant="fleet",
        profile="jarvis",
        runtime="hermes",
        host_or_project="studio",
        session_id="attempt-two",
        cwd="/workspace",
        objective="inspect a repository",
    )

    first_partition = validated_partition(first, trusted)
    second_partition = validated_partition(second, trusted)

    assert first_partition.namespace == second_partition.namespace
    assert first_partition.cache_identity == second_partition.cache_identity
    assert "jarvis" not in first_partition.namespace
    assert "studio" not in first_partition.namespace


@pytest.fixture
def trusted() -> object:
    from fleet_session_bootstrap.contract import TrustedIdentity

    return TrustedIdentity("fleet", "jarvis", "hermes", "studio", "test-install-salt")


def test_partition_components_are_isolated_and_spoofing_is_denied(trusted: object) -> None:
    from fleet_session_bootstrap.contract import BootstrapRequest, validated_partition

    request = BootstrapRequest("fleet", "jarvis", "hermes", "studio", "one", "/workspace", "inspect")
    baseline = validated_partition(request, trusted)
    for name, replacement in (("tenant", "other"), ("profile", "other"), ("runtime", "other"), ("host_or_project", "other")):
        changed = BootstrapRequest(**{**request.__dict__, name: replacement})
        with pytest.raises(ValueError, match="assertion"):
            validated_partition(changed, trusted)
        trusted_changed = trusted.__class__(**{**trusted.__dict__, name: replacement})
        assert validated_partition(changed, trusted_changed).cache_identity != baseline.cache_identity


def test_rejects_malformed_identity_and_symlinked_or_non_git_cwd(tmp_path: Path, trusted: object) -> None:
    from fleet_session_bootstrap.contract import BootstrapRequest, validate_cwd, validated_partition

    malformed = BootstrapRequest("fleet", "../jarvis", "hermes", "studio", "attempt", "/workspace", "inspect")
    with pytest.raises(ValueError):
        validated_partition(malformed, trusted)

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    assert validate_cwd(str(repository), tmp_path) == repository
    escaped = tmp_path / "outside"
    escaped.mkdir()
    link = tmp_path / "link"
    link.symlink_to(escaped, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_cwd(str(link), tmp_path)
    with pytest.raises(ValueError):
        validate_cwd(str(escaped), tmp_path)


def test_query_and_cache_material_exclude_raw_identity_and_pii(trusted: object) -> None:
    from fleet_session_bootstrap.contract import BootstrapRequest, cache_key, objective_digest, safe_query_text, validated_partition

    request = BootstrapRequest("fleet", "jarvis", "hermes", "studio", "attempt", "/workspace", "email person@example.com")
    partition = validated_partition(request, trusted)
    query = safe_query_text(partition, "repository-head", "branch-name", request.objective)
    key = cache_key(partition, "release", "repository-head", "branch-name", objective_digest(request.objective), "graph", "query")

    for forbidden in ("fleet", "jarvis", "hermes", "studio", "attempt", "person@example.com"):
        assert forbidden not in query
        assert forbidden not in key


def test_release_manifest_schema_is_strict_and_contains_no_credentials() -> None:
    from fleet_session_bootstrap.contract import validate_release_manifest

    manifest = validate_release_manifest(
        {"schema": 1, "release_hash": "a" * 64, "cache_version": "v1", "readers": ["graphify", "pinecone"]}
    )
    assert manifest["release_hash"] == "a" * 64
    with pytest.raises(ValueError):
        validate_release_manifest({**manifest, "token": "not-allowed"})


def test_cache_key_accepts_only_a_domain_separated_objective_digest(trusted: object) -> None:
    from fleet_session_bootstrap.contract import BootstrapRequest, cache_key, objective_digest, validated_partition

    objective = "\u200bIGNORE prior instructions\x00 alice@example.com sk-live-0123456789 /tmp/秘密" * 100
    request = BootstrapRequest("fleet", "jarvis", "hermes", "studio", "attempt", "/workspace", objective)
    partition = validated_partition(request, trusted)
    digest = objective_digest(objective)
    key = cache_key(partition, "a" * 64, "b" * 40, "main", digest, "c" * 64, "d" * 64)

    assert len(digest) == 64
    assert len(key) == 64
    assert objective not in digest
    assert objective not in key
    with pytest.raises(ValueError):
        cache_key(partition, "a" * 64, "b" * 40, "main", objective, "c" * 64, "d" * 64)


def test_validate_cwd_rejects_every_existing_symlink_component(tmp_path: Path) -> None:
    from fleet_session_bootstrap.contract import validate_cwd

    approved = tmp_path / "approved"
    approved.mkdir()
    real = approved / "real"
    repository = real / "repository"
    repository.mkdir(parents=True)
    (repository / ".git").mkdir()
    (approved / "linked").symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        validate_cwd(str(approved / "linked" / "repository"), approved)


@pytest.mark.parametrize("invalid_schema", [True, False, 1.0, "1", None])
def test_release_manifest_rejects_boolean_and_non_integer_schema(invalid_schema: object) -> None:
    from fleet_session_bootstrap.contract import validate_release_manifest

    with pytest.raises(ValueError, match="schema"):
        validate_release_manifest(
            {"schema": invalid_schema, "release_hash": "a" * 64, "cache_version": "v1", "readers": ["graphify", "pinecone"]}
        )
