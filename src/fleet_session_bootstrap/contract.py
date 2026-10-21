"""Trusted identity and partition contracts for session bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

_COMPONENTS = ("tenant", "profile", "runtime", "host_or_project")
_SAFE_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class TrustedIdentity:
    """Partition authority supplied by the active runtime, never adapter stdin."""

    tenant: str
    profile: str
    runtime: str
    host_or_project: str
    salt: str


@dataclass(frozen=True)
class BootstrapRequest:
    """Untrusted adapter assertions and opaque attempt identity."""

    tenant: str
    profile: str
    runtime: str
    host_or_project: str
    session_id: str
    cwd: str
    objective: str


@dataclass(frozen=True)
class Partition:
    """Redacted cache/query partition; it deliberately excludes session identity."""

    namespace: str
    cache_identity: str


def _component_values(identity: TrustedIdentity) -> tuple[str, ...]:
    return tuple(getattr(identity, name) for name in _COMPONENTS)


def _require_safe_components(identity: TrustedIdentity) -> None:
    if not identity.salt or any(not _SAFE_COMPONENT.fullmatch(value) for value in _component_values(identity)):
        raise ValueError("trusted partition is incomplete or malformed")


def _digest(salt: str, value: str) -> str:
    return sha256(f"{salt}\0{value}".encode()).hexdigest()


def validated_partition(request: BootstrapRequest, trusted: TrustedIdentity) -> Partition:
    """Return a salted partition only when all adapter assertions match authority."""
    _require_safe_components(trusted)
    for name in _COMPONENTS:
        asserted = getattr(request, name)
        if not _SAFE_COMPONENT.fullmatch(asserted):
            raise ValueError("untrusted identity assertion is malformed")
        if asserted != getattr(trusted, name):
            raise ValueError("untrusted identity assertion does not match trusted identity")
    components = "/".join(_digest(trusted.salt, value) for value in _component_values(trusted))
    namespace = f"v1/{components}"
    return Partition(namespace=namespace, cache_identity=sha256(namespace.encode()).hexdigest())


def validate_cwd(cwd: str, approved_root: Path) -> Path:
    """Accept only a direct, non-symlinked Git worktree within an approved root."""
    candidate = Path(cwd)
    if not cwd or not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("cwd is invalid")
    root = approved_root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if root not in (resolved, *resolved.parents) or not (resolved / ".git").exists():
        raise ValueError("cwd is outside the approved Git worktree root")
    return resolved


def cache_key(
    partition: Partition,
    release_hash: str,
    repository_head: str,
    branch: str,
    objective: str,
    graph_digest: str,
    query_digest: str,
) -> str:
    """Derive a deterministic shared-cache identity without retaining raw inputs."""
    material = "\0".join(
        ("v1", partition.namespace, release_hash, repository_head, branch, objective, graph_digest, query_digest)
    )
    return sha256(material.encode()).hexdigest()


def validate_release_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Validate the immutable, credential-free release metadata contract."""
    if set(manifest) != {"schema", "release_hash", "cache_version", "readers"}:
        raise ValueError("release manifest schema is invalid")
    if manifest["schema"] != 1 or manifest["cache_version"] != "v1":
        raise ValueError("release manifest version is invalid")
    release_hash = manifest["release_hash"]
    readers = manifest["readers"]
    if not isinstance(release_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", release_hash):
        raise ValueError("release hash is invalid")
    if not isinstance(readers, list) or readers != ["graphify", "pinecone"]:
        raise ValueError("release readers are invalid")
    return manifest


def safe_query_text(partition: Partition, repository_head: str, branch: str, objective: str) -> str:
    """Supply only generic task terms and digests to later read-only readers."""
    del partition, objective
    return "task=session-bootstrap repo=" + sha256(repository_head.encode()).hexdigest() + " branch=" + sha256(branch.encode()).hexdigest()
