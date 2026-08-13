from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from ingestion_fixtures import (
    POS_TARGET,
    RCH_TARGET,
    TEST_HMAC_KEY,
    tree_snapshot,
    write_printproxy_job,
    write_rch_job,
)

from retailprintguard.common.domain import DocumentType
from retailprintguard.ingestion.dto import ArtifactRole, SourceKind, StreamDirection
from retailprintguard.ingestion.errors import SourceValidationError
from retailprintguard.ingestion.printproxy import PrintProxyV3Adapter, canonical_json
from retailprintguard.ingestion.rch import RCHCaptureV1Adapter, RCHParsedV1Adapter


def test_rch_capture_v1_validates_ready_hashes_and_bidirectional_timeline(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rch"
    write_rch_job(source)
    before = tree_snapshot(source)
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    candidates = adapter.discover(maximum=10)
    assert len(candidates) == 1
    envelope = adapter.load(candidates[0])

    assert envelope.source_kind is SourceKind.COMMERCIAL_RCH_CAPTURE_V1
    assert envelope.device_id == "rch_1"
    assert envelope.complete is True
    assert [chunk.direction for chunk in envelope.chunks] == [
        StreamDirection.CLIENT_TO_DEVICE,
        StreamDirection.DEVICE_TO_CLIENT,
    ]
    assert {artifact.role for artifact in envelope.artifacts} == {
        ArtifactRole.REQUEST_RAW,
        ArtifactRole.RESPONSE_RAW,
        ArtifactRole.RECEIVE_TIMELINE,
        ArtifactRole.CAPTURE_MANIFEST,
        ArtifactRole.CAPTURE_READY_MARKER,
    }
    assert tree_snapshot(source) == before


def test_rch_capture_accepts_offline_replay_opaque_ids_and_unknown_source_port(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rch"
    write_rch_job(source, offline=True)
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-offline",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    envelope = adapter.load(adapter.discover(maximum=10)[0])

    assert envelope.source_session_id is not None
    assert envelope.source_session_id.startswith("offline-")
    assert envelope.source_endpoint is None


def test_rch_parsed_v1_normalizes_versioned_document_without_replacing_raw(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rch"
    write_rch_job(source, parsed=True)
    before = tree_snapshot(source)
    adapter = RCHParsedV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    envelope = adapter.load(adapter.discover(maximum=10)[0])

    assert envelope.source_kind is SourceKind.COMMERCIAL_RCH_PARSED_V1
    assert envelope.parser_version == "0.3.0"
    assert envelope.documents[0].document_type is DocumentType.COMMERCIAL_DOCUMENT
    assert envelope.documents[0].source_start_offset == 0
    assert ArtifactRole.NORMALIZED_TEXT in {item.role for item in envelope.artifacts}
    assert tree_snapshot(source) == before


def test_rch_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    job = write_rch_job(source)
    manifest_path = job / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["request_raw"] = "../outside.raw"
    manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_raw)
    ready_path = job / ".ready"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    # Keep the completion marker bound to the malicious manifest so the
    # traversal validator, rather than an earlier digest check, is exercised.
    import hashlib

    ready["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
    ready_path.write_text(json.dumps(ready, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    with pytest.raises(SourceValidationError, match="unsafe artifact|outside|escapes"):
        adapter.load(adapter.discover(maximum=10)[0])


def test_rch_rejects_raw_changed_after_ready_commit(tmp_path: Path) -> None:
    source = tmp_path / "rch"
    job = write_rch_job(source)
    raw = next(job.glob("file_*.raw"))
    raw.write_bytes(raw.read_bytes() + b"tampered")
    adapter = RCHCaptureV1Adapter(
        source,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    with pytest.raises(SourceValidationError, match="SHA-256 mismatch"):
        adapter.load(adapter.discover(maximum=10)[0])


def test_discovery_does_not_follow_symlinked_source_tree(tmp_path: Path) -> None:
    root = tmp_path / "rch"
    root.mkdir()
    external = tmp_path / "external"
    write_rch_job(external)
    link = root / "linked"
    try:
        os.symlink(external, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this test environment")
    adapter = RCHCaptureV1Adapter(
        root,
        source_instance_id="rch-synthetic",
        devices_by_target={RCH_TARGET: "rch_1"},
    )

    assert adapter.discover(maximum=10) == ()


def test_printproxy_v3_validates_authenticated_ledger_head_and_raw(tmp_path: Path) -> None:
    source = tmp_path / "printproxy"
    write_printproxy_job(source)
    before = tree_snapshot(source)
    adapter = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
        hmac_key=TEST_HMAC_KEY,
    )

    envelope = adapter.load(adapter.discover(maximum=10)[0])

    assert envelope.source_kind is SourceKind.PRINTPROXY_V3
    assert envelope.device_id == "pos_1"
    assert envelope.metadata["source_contract"]["integrity"] == (
        "HMAC_SHA256_AND_HASH_CHAIN_VALIDATED"
    )
    assert envelope.chunks[0].direction is StreamDirection.CLIENT_TO_DEVICE
    assert envelope.delivery_evidence == "LOCAL_SOCKET_PROGRESS_ONLY_PHYSICAL_PRINT_UNCONFIRMED"
    assert tree_snapshot(source) == before


def test_printproxy_v3_requires_hmac_unless_source_explicitly_has_none(tmp_path: Path) -> None:
    source = tmp_path / "printproxy"
    write_printproxy_job(source, hmac_key=None)

    strict = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
    )
    invalid = strict.discover(maximum=10)
    with pytest.raises(SourceValidationError, match="HMAC key"):
        strict.load(invalid[0])

    explicit = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
        require_hmac=False,
    )
    envelope = explicit.load(explicit.discover(maximum=10)[0])
    assert envelope.metadata["source_contract"]["integrity"].endswith("NO_HMAC_CONFIGURED")


def test_printproxy_unknown_state_schema_becomes_invalid_candidate(tmp_path: Path) -> None:
    source = tmp_path / "printproxy"
    write_printproxy_job(source, state_schema_version=99)
    adapter = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
        hmac_key=TEST_HMAC_KEY,
    )

    candidate = adapter.discover(maximum=10)[0]
    with pytest.raises(SourceValidationError, match="state schema"):
        adapter.load(candidate)


def test_printproxy_noncanonical_or_tampered_head_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "printproxy"
    route = write_printproxy_job(source)
    head_path = route / "manifest.head.json"
    head = json.loads(head_path.read_text(encoding="utf-8"))
    head["hmac_sha256"] = "0" * 64
    head_path.write_bytes(canonical_json(head) + b"\n")
    adapter = PrintProxyV3Adapter(
        source,
        source_instance_id="pos-synthetic",
        devices_by_target={POS_TARGET: "pos_1"},
        hmac_key=TEST_HMAC_KEY,
    )

    invalid = adapter.discover(maximum=10)
    with pytest.raises(SourceValidationError, match="manifest head"):
        adapter.load(invalid[0])
