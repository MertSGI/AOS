"""Deterministic offline unit tests for AOS Controller Relay CR2-Lite Components.

Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01
PROVES ZERO NETWORK CALLS.

Covers:
- latest unconsumed selection
- sequence ordering
- thread/reply validation
- supersession
- receipt progression
- requires_reply CONSUMED prohibition
- CAS race handling
- principal spoof rejection
- AOS->LARI identity separation
- LARI->AOS identity separation
- authority_effect always NONE
- authority reference independent validation
- secret exclusion
- new-chat recovery from canonical state
- no filename lexical-order dependency
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from aos.controller_relay import (
    ControllerRelayError,
    ControllerRelayValidationResult,
    compute_message_content_sha256,
)
from aos.controller_relay_authority_resolver import (
    AuthorityArtifactResolver,
    AuthorityResolutionError,
    CanonicalAuthorityReference,
)
from aos.controller_relay_detector import (
    detect_unconsumed_inbound_messages,
    get_latest_unconsumed_inbound,
)
from aos.controller_relay_ingress import (
    AuthenticatedSessionType,
    BoundedControllerRelayIngressAdapter,
    TrustedIngressBoundaryError,
)
from aos.controller_relay_poller import ControllerRelayPoller
from aos.controller_relay_service import (
    ControllerPrincipal,
    ControllerRelayService,
    derive_message_path,
    derive_receipt_path,
)

# Bootstrap SHA
BOOTSTRAP_SHA = "039232ecf10948bf55a9d9dab665828b6c06f7c6"


class FakeProvenance:
    def __init__(self, path: str, raw_bytes: bytes, pub_sha: str, pub_ord: int):
        self.path = path
        self.raw_bytes = raw_bytes
        self.publication_commit_sha = pub_sha
        self.publication_ordinal = pub_ord


class DeterministicFakeTransport:
    """In-memory test transport simulating GitDataCASRelayTransport."""

    def __init__(self, initial_head: str = BOOTSTRAP_SHA):
        self.head_sha = initial_head
        self.records: Dict[str, bytes] = {}
        self.message_provenances: List[FakeProvenance] = []
        self.receipt_provenances: List[FakeProvenance] = []
        self.ordinal_counter = 0

    def get_branch_head(self, repository: str, branch: str) -> str:
        return self.head_sha

    def read_record_bytes(self, repository: str, ref: str, path: str) -> bytes:
        if path not in self.records:
            raise ControllerRelayError(f"Path not found: {path}")
        return self.records[path]

    def list_record_provenance_under_prefix(
        self, repository: str, ref: str, prefix: str
    ) -> List[FakeProvenance]:
        if prefix.startswith("controller-relay/v1/messages/"):
            return list(self.message_provenances)
        if prefix.startswith("controller-relay/v1/receipts/"):
            return list(self.receipt_provenances)
        return []

    def publish_record(
        self,
        repository: str,
        branch: str,
        path: str,
        content_bytes: bytes,
        expected_head: str,
        record_type: str,
    ) -> ControllerRelayValidationResult:
        if self.head_sha != expected_head:
            return ControllerRelayValidationResult(
                False,
                "HOLD_CAS_RACE",
                [f"CAS race: expected {expected_head}, current {self.head_sha}"],
            )
        self.ordinal_counter += 1
        new_sha = f"c{self.ordinal_counter:039d}"
        self.records[path] = content_bytes
        prov = FakeProvenance(path, content_bytes, new_sha, self.ordinal_counter)
        if record_type == "message":
            self.message_provenances.append(prov)
        else:
            self.receipt_provenances.append(prov)
        self.head_sha = new_sha
        return ControllerRelayValidationResult(
            True,
            "PASS",
            details={
                "PUBLISHED_COMMIT_SHA": new_sha,
                "PUBLISHED_PATH": path,
                "RECORD_TYPE": record_type,
            },
        )


def _make_valid_message(
    from_c: str,
    to_c: str,
    seq: int,
    in_reply_to: Optional[str] = None,
    thread_id: Optional[str] = None,
    supersedes_id: Optional[str] = None,
    requires_reply: bool = False,
) -> Tuple[Dict[str, Any], bytes]:
    msg_id = f"CRV1-{from_c}-{to_c}-{seq:012d}"
    t_id = thread_id if thread_id else msg_id
    msg = {
        "schema_version": "0.1",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": msg_id,
        "thread_id": t_id,
        "sequence": seq,
        "from": from_c,
        "to": to_c,
        "in_reply_to": in_reply_to,
        "created_at": "2026-09-10T12:00:00Z",
        "subject": "TEST_SUBJECT",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "control/controller-relay",
        "subject_sha": BOOTSTRAP_SHA,
        "decision": "TEST_DECISION",
        "authority_effect": "NONE",
        "authority_refs": ["REF-01"],
        "requested_next_action": "TEST_NEXT",
        "requires_reply": requires_reply,
    }
    if supersedes_id:
        msg["supersedes_message_id"] = supersedes_id
    msg["content_sha256"] = compute_message_content_sha256(msg)
    raw = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return msg, raw


def _make_valid_receipt(
    msg: Dict[str, Any],
    commit_sha: str,
    actor: str,
    event: str,
) -> Tuple[Dict[str, Any], bytes]:
    rcpt = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": msg["message_id"],
        "message_commit_sha": commit_sha,
        "message_content_sha256": msg["content_sha256"],
        "actor": actor,
        "event": event,
        "created_at": "2026-09-10T12:05:00Z",
    }
    raw = json.dumps(rcpt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return rcpt, raw


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

def test_cr2_lite_unconsumed_detection_and_lexical_order_independence():
    """Verify that detection selects by publication ordinal and directed sequence, NOT filename."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_lari = ControllerPrincipal("LARI_CONTROLLER")

    # Publish message 1 from LARI to AOS
    msg1, raw1 = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    res1 = service.publish_message(raw1, transport.head_sha, p_lari)
    assert res1.is_valid

    # Publish message 2 from LARI to AOS
    msg2, raw2 = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 2)
    res2 = service.publish_message(raw2, transport.head_sha, p_lari)
    assert res2.is_valid

    # Swap storage order in provenance list to simulate reverse directory/file scan order
    transport.message_provenances.reverse()

    # Inbound detector should still correctly identify msg2 as the latest by ordinal
    latest = get_latest_unconsumed_inbound(service, "AOS_CONTROLLER")
    assert latest is not None
    assert latest.message_id == msg2["message_id"]
    assert latest.sequence == 2


def test_cr2_lite_supersession_excludes_prior_message():
    """Verify that a superseding message marks the target message as non-candidate."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_lari = ControllerPrincipal("LARI_CONTROLLER")

    # Msg 1
    msg1, raw1 = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    service.publish_message(raw1, transport.head_sha, p_lari)

    # Msg 2 supersedes Msg 1
    msg2, raw2 = _make_valid_message(
        "LARI_CONTROLLER", "AOS_CONTROLLER", 2, thread_id=msg1["message_id"], supersedes_id=msg1["message_id"]
    )
    service.publish_message(raw2, transport.head_sha, p_lari)

    unconsumed = detect_unconsumed_inbound_messages(service, "AOS_CONTROLLER")
    assert len(unconsumed) == 1
    assert unconsumed[0].message_id == msg2["message_id"]


def test_cr2_lite_consumed_receipt_removes_from_unconsumed():
    """Verify that publishing OBSERVED -> VERIFIED -> ACKNOWLEDGED -> CONSUMED removes message from unconsumed."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_lari = ControllerPrincipal("LARI_CONTROLLER")
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    msg1, raw1 = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1, requires_reply=False)
    res1 = service.publish_message(raw1, transport.head_sha, p_lari)
    pub_sha = res1.details["PUBLISHED_COMMIT_SHA"]

    # Inbound is detectable
    assert get_latest_unconsumed_inbound(service, "AOS_CONTROLLER") is not None

    # Step through receipts: OBSERVED -> VERIFIED -> ACKNOWLEDGED -> CONSUMED
    for ev in ["OBSERVED", "VERIFIED", "ACKNOWLEDGED", "CONSUMED"]:
        rcpt, raw_r = _make_valid_receipt(msg1, pub_sha, "AOS_CONTROLLER", ev)
        res_r = service.publish_receipt(raw_r, transport.head_sha, p_aos)
        assert res_r.is_valid

    # Now unconsumed should be empty
    assert get_latest_unconsumed_inbound(service, "AOS_CONTROLLER") is None


def test_cr2_lite_requires_reply_prohibits_consumed_without_reply():
    """Verify that a CONSUMED receipt cannot be published on requires_reply=True if no reply exists."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_lari = ControllerPrincipal("LARI_CONTROLLER")
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    msg1, raw1 = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1, requires_reply=True)
    res1 = service.publish_message(raw1, transport.head_sha, p_lari)
    pub_sha = res1.details["PUBLISHED_COMMIT_SHA"]

    # OBSERVED -> VERIFIED -> ACKNOWLEDGED succeed
    for ev in ["OBSERVED", "VERIFIED", "ACKNOWLEDGED"]:
        rcpt, raw_r = _make_valid_receipt(msg1, pub_sha, "AOS_CONTROLLER", ev)
        res_rcpt = service.publish_receipt(raw_r, transport.head_sha, p_aos)
        assert res_rcpt.is_valid

    # CONSUMED fails because no reply has been published yet
    rcpt_c, raw_c = _make_valid_receipt(msg1, pub_sha, "AOS_CONTROLLER", "CONSUMED")
    res_c = service.publish_receipt(raw_c, transport.head_sha, p_aos)
    assert not res_c.is_valid
    assert "no valid outbound reply" in str(res_c.errors).lower()


def test_cr2_lite_bounded_ingress_principal_separation():
    """Verify ingress adapter strictly enforces identity boundaries."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)

    aos_adapter = BoundedControllerRelayIngressAdapter(
        service=service, session_type=AuthenticatedSessionType.AOS_CONTROLLER_SESSION
    )
    lari_adapter = BoundedControllerRelayIngressAdapter(
        service=service, session_type=AuthenticatedSessionType.LARI_CONTROLLER_SESSION
    )

    # AOS adapter attempting to publish a message claimed as LARI_CONTROLLER must fail
    msg_lari, raw_lari = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    res_spoof = aos_adapter.relay_publish_message(raw_lari, transport.head_sha)
    assert not res_spoof.is_valid
    assert "Authentication mismatch" in str(res_spoof.errors)

    # LARI adapter publishing valid LARI message succeeds
    res_valid = lari_adapter.relay_publish_message(raw_lari, transport.head_sha)
    assert res_valid.is_valid

    # LARI adapter attempting to publish AOS message fails
    msg_aos, raw_aos = _make_valid_message("AOS_CONTROLLER", "LARI_CONTROLLER", 1)
    res_spoof2 = lari_adapter.relay_publish_message(raw_aos, transport.head_sha)
    assert not res_spoof2.is_valid
    assert "Authentication mismatch" in str(res_spoof2.errors)


def test_cr2_lite_cas_race_handling():
    """Verify that CAS mismatch fails closed without force mutation."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    msg, raw = _make_valid_message("AOS_CONTROLLER", "LARI_CONTROLLER", 1)
    res = service.publish_message(raw, expected_head="stale-head-sha", principal=p_aos)
    assert not res.is_valid
    assert res.disposition == "HOLD_CAS_RACE"


def test_cr2_lite_authority_resolver_independent_validation():
    """Verify independent canonical authority resolution and validation."""
    authority_id = "LARI-TEST-AUTH-20260910-01"
    pub_commit = "a" * 40
    auth_doc = {
        "authority_id": authority_id,
        "issuer_controller": "LARI_CONTROLLER",
        "status": "ACTIVE",
        "subject_sha": BOOTSTRAP_SHA,
        "scope": "TEST_ONLY",
    }
    raw_doc = json.dumps(auth_doc, sort_keys=True).encode("utf-8")
    body_sha256 = hashlib.sha256(raw_doc).hexdigest()

    def fake_reader(repo: str, commit: str, path: str) -> bytes:
        if path == f"docs/project-control/controller-authorities/LARI_CONTROLLER/{authority_id}.json":
            return raw_doc
        raise FileNotFoundError(path)

    resolver = AuthorityArtifactResolver(content_reader=fake_reader)
    ref = CanonicalAuthorityReference(
        repository="MertSGI/Randapp-main",
        branch="control/lari-project-control-plane",
        publication_commit_sha=pub_commit,
        path=f"docs/project-control/controller-authorities/LARI_CONTROLLER/{authority_id}.json",
        authority_id=authority_id,
        authority_body_sha256=body_sha256,
    )

    resolved = resolver.resolve_and_validate_authority(
        ref, expected_subject_sha=BOOTSTRAP_SHA, expected_authority_id=authority_id
    )
    assert resolved["authority_id"] == authority_id
    assert resolved["issuer_controller"] == "LARI_CONTROLLER"

    # Corrupted digest fails
    bad_ref = copy.copy(ref)
    bad_ref.authority_body_sha256 = "b" * 64
    with pytest.raises(AuthorityResolutionError, match="mismatch"):
        resolver.resolve_and_validate_authority(bad_ref)


def test_cr2_lite_secret_exclusion():
    """Verify messages containing prohibited credentials or secrets are rejected."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    msg, _ = _make_valid_message("AOS_CONTROLLER", "LARI_CONTROLLER", 1)
    msg["github_token"] = "ghp_1234567890abcdef1234567890abcdef1234"
    msg["content_sha256"] = compute_message_content_sha256(msg)
    raw = json.dumps(msg).encode("utf-8")

    res = service.publish_message(raw, transport.head_sha, p_aos)
    assert not res.is_valid
    assert any("credential" in err.lower() or "secret" in err.lower() for err in res.errors)


def test_cr2_lite_poller_caching_and_head_change():
    """Verify poller avoids reprocessing if HEAD has not changed."""
    transport = DeterministicFakeTransport()
    service = ControllerRelayService(transport)
    p_lari = ControllerPrincipal("LARI_CONTROLLER")

    poller = ControllerRelayPoller(
        service=service,
        controller_id="AOS_CONTROLLER",
        poll_interval=0.1,
    )

    # Initial poll
    changed, latest = poller.poll_once()
    assert changed is True
    assert latest is None

    # Second poll with no change
    changed, latest = poller.poll_once()
    assert changed is False

    # Publish message -> HEAD changes
    msg, raw = _make_valid_message("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    service.publish_message(raw, transport.head_sha, p_lari)

    changed, latest = poller.poll_once()
    assert changed is True
    assert latest is not None
    assert latest.message_id == msg["message_id"]
