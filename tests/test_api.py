from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from retailprintguard.api.auth import LoginThrottle
from retailprintguard.api.main import create_app
from retailprintguard.api.repository import EmptyRepository, RawArtifact
from retailprintguard.api.schemas import (
    AlertUpdate,
    AlertView,
    AuditEntry,
    DashboardView,
    DeviceView,
    DocumentView,
    RoleName,
    RuleView,
    UserPrincipal,
)


class FakeRepository(EmptyRepository):
    def __init__(self) -> None:
        self.user_id = uuid4()
        self.document_id = uuid4()
        self.job_id = uuid4()
        self.session_id = uuid4()
        self.alert_id = uuid4()
        self.audits: list[AuditEntry] = []
        self.rule_enabled = True
        self.spool_state = "ok"
        self.last_alert_filters: dict[str, object] = {}
        self.last_document_filters: dict[str, object] = {}

    def authenticate(self, username: str, password: str) -> UserPrincipal | None:
        if username == "auditor" and password == "correct-password":
            return UserPrincipal(
                id=self.user_id,
                username=username,
                roles=(RoleName.AUDITOR,),
            )
        if username == "admin" and password == "correct-password":
            return UserPrincipal(
                id=self.user_id,
                username=username,
                roles=(RoleName.ADMIN,),
            )
        if username == "reader" and password == "correct-password":
            return UserPrincipal(
                id=self.user_id,
                username=username,
                roles=(RoleName.READ_ONLY,),
            )
        return None

    def dashboard(self) -> DashboardView:
        return DashboardView(
            documents=7,
            pre_bills=2,
            commercial_documents=3,
            open_alerts=1,
            economic_difference=Decimal("50.00"),
        )

    def list_devices(self) -> list[DeviceView]:
        return [
            DeviceView(
                id="pos_1",
                name="POS sintetica",
                type="pos",
                enabled=True,
                online=True,
                listen_endpoint="192.0.2.10:9100",
                target_endpoint="192.0.2.20:9100",
            )
        ]

    def get_document(self, document_id: UUID) -> DocumentView | None:
        if document_id != self.document_id:
            return None
        return DocumentView(
            id=self.document_id,
            device_id="pos_1",
            job_id=self.job_id,
            type="PRE_BILL",
            subtype="PRECONTO",
            captured_at=datetime.now(UTC),
            gross_total=Decimal("100.00"),
            status="COMPLETE",
            normalized_text="PRECONTO SINTETICO",
            parser_name="synthetic",
            parser_version="1",
            confidence=100,
            sha256="a" * 64,
            complete=True,
        )

    def list_documents(
        self, *, limit: int, offset: int, filters: dict[str, object]
    ) -> tuple[list[DocumentView], int]:
        self.last_document_filters = filters
        return [], 0

    def get_document_raw(
        self, document_id: UUID, *, direction: str = "request"
    ) -> RawArtifact | None:
        if document_id != self.document_id:
            return None
        payload = b"\x1bSYNTHETIC" if direction == "request" else b"ACK"
        return RawArtifact(
            payload,
            f"synthetic-{direction}.raw",
            hashlib.sha256(payload).hexdigest(),
        )

    def get_job_raw(self, job_id: UUID, *, direction: str) -> RawArtifact | None:
        return (
            self.get_document_raw(self.document_id, direction=direction)
            if job_id == self.job_id
            else None
        )

    def get_session_raw(self, session_id: UUID, *, direction: str) -> RawArtifact | None:
        return (
            self.get_document_raw(self.document_id, direction=direction)
            if session_id == self.session_id
            else None
        )

    def list_alerts(
        self, *, limit: int, offset: int, filters: dict[str, object]
    ) -> tuple[list[AlertView], int]:
        del limit, offset
        self.last_alert_filters = filters
        return [self._alert()], 1

    def get_alert(self, alert_id: UUID) -> AlertView | None:
        return self._alert() if alert_id == self.alert_id else None

    def update_alert(
        self, alert_id: UUID, update: AlertUpdate, actor: UserPrincipal
    ) -> AlertView | None:
        del update, actor
        return self._alert(status="UNDER_REVIEW") if alert_id == self.alert_id else None

    def _alert(self, *, status: str = "OPEN") -> AlertView:
        return AlertView(
            id=self.alert_id,
            rule_code="PREBILL_FISCAL_AMOUNT_DROP",
            severity="HIGH",
            score=90,
            status=status,
            opened_at=datetime.now(UTC),
            description="=RIDUZIONE_SINTETICA()",
            explanation="100.00 -> 50.00",
            confidence=100,
        )

    def list_rules(self) -> list[RuleView]:
        return [
            RuleView(
                code="PREBILL_FISCAL_AMOUNT_DROP",
                name="Riduzione preconto",
                enabled=self.rule_enabled,
                version=1,
                severity="HIGH",
                weight=90,
                threshold=Decimal("20"),
            )
        ]

    def set_rule_enabled(self, code: str, enabled: bool, actor: UserPrincipal) -> RuleView | None:
        del actor
        if code != "PREBILL_FISCAL_AMOUNT_DROP":
            return None
        self.rule_enabled = enabled
        return self.list_rules()[0]

    def append_audit(self, entry: AuditEntry) -> None:
        self.audits.append(entry)

    def database_health(self) -> str:
        return "ok"

    def spool_health(self) -> str:
        return self.spool_state


def _client() -> tuple[TestClient, FakeRepository]:
    repository = FakeRepository()
    app = create_app(repository=repository, jwt_secret=b"x" * 64)
    return TestClient(app), repository


def _login(client: TestClient, username: str = "auditor") -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_health_is_public_and_security_headers_are_present() -> None:
    client, _ = _client()
    response = client.get(
        "/api/v1/system/health", headers={"X-Correlation-ID": "test-correlation-123"}
    )

    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.json()["spool"] == "ok"
    assert response.json()["status"] == "ok"
    assert response.headers["X-Correlation-ID"] == "test-correlation-123"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_login_dashboard_and_bad_password() -> None:
    client, repository = _client()
    bad = client.post(
        "/api/v1/auth/login",
        json={"username": "auditor", "password": "wrong-password"},
    )
    assert bad.status_code == 401

    headers = _login(client)
    response = client.get("/api/v1/dashboard", headers=headers)
    assert response.status_code == 200
    assert response.json()["economic_difference"] == "50.00"
    assert any(entry.action == "AUTH_LOGIN" for entry in repository.audits)

    diagnostics = client.get("/api/v1/system/diagnostics", headers=headers)
    assert diagnostics.status_code == 200
    assert diagnostics.json()["database"] == "ok"
    assert diagnostics.json()["spool"] == "ok"
    assert diagnostics.json()["recent_events"] == []


def test_health_degrades_when_spool_is_not_healthy() -> None:
    repository = FakeRepository()
    repository.spool_state = "degraded"
    client = TestClient(create_app(repository=repository, jwt_secret=b"x" * 64))

    response = client.get("/api/v1/system/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"
    assert response.json()["spool"] == "degraded"
    assert response.json()["status"] == "degraded"


def test_protected_routes_reject_missing_token() -> None:
    client, _ = _client()
    assert client.get("/api/v1/devices").status_code == 401
    assert client.get("/api/v1/documents").status_code == 401
    assert client.get("/api/v1/alerts").status_code == 401


def test_documents_route_forwards_server_side_technical_exclusion() -> None:
    client, repository = _client()
    response = client.get(
        "/api/v1/documents?exclude_type=DEVICE_RESPONSE",
        headers=_login(client),
    )

    assert response.status_code == 200
    assert repository.last_document_filters["exclude_type"] == "DEVICE_RESPONSE"


def test_raw_download_requires_auditor_and_is_audited() -> None:
    client, repository = _client()
    headers = _login(client)
    response = client.get(f"/api/v1/documents/{repository.document_id}/raw", headers=headers)

    assert response.status_code == 200
    assert response.content == b"\x1bSYNTHETIC"
    encoded = base64.b64encode(hashlib.sha256(response.content).digest()).decode("ascii")
    assert response.headers["Content-Digest"] == f"sha-256=:{encoded}:"
    assert response.headers["X-Checksum-SHA256"] == hashlib.sha256(response.content).hexdigest()
    assert repository.audits[-1].action == "RAW_DOWNLOAD"

    reader = _login(client, "reader")
    assert (
        client.get(f"/api/v1/documents/{repository.document_id}/raw", headers=reader).status_code
        == 403
    )


def test_document_derivatives_and_bidirectional_evidence_downloads() -> None:
    client, repository = _client()
    headers = _login(client)
    assert (
        client.get(f"/api/v1/documents/{repository.document_id}/txt", headers=headers).text
        == "PRECONTO SINTETICO"
    )
    json_response = client.get(
        f"/api/v1/documents/{repository.document_id}/json", headers=headers
    )
    assert json_response.status_code == 200
    assert json_response.json()["id"] == str(repository.document_id)
    pdf_response = client.get(
        f"/api/v1/documents/{repository.document_id}/pdf", headers=headers
    )
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF-")
    assert pdf_response.content.endswith(b"%%EOF\n")
    assert pdf_response.headers["X-RetailPrintGuard-Renderer"]
    assert repository.audits[-1].action == "PDF_DOWNLOAD"
    assert (
        client.get(
            f"/api/v1/jobs/{repository.job_id}/raw?direction=response", headers=headers
        ).content
        == b"ACK"
    )
    assert (
        client.get(
            f"/api/v1/sessions/{repository.session_id}/raw?direction=request", headers=headers
        ).content
        == b"\x1bSYNTHETIC"
    )


def test_alert_workflow_and_csv_export() -> None:
    client, repository = _client()
    headers = _login(client)
    updated = client.patch(
        f"/api/v1/alerts/{repository.alert_id}",
        headers=headers,
        json={"status": "UNDER_REVIEW", "note": "verifica sintetica"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "UNDER_REVIEW"

    exported = client.get(
        "/api/v1/alerts/export.csv?severity=HIGH&rule=DROP&status=OPEN"
        "&device_id=pos_1&operator_code=op_1",
        headers=headers,
    )
    assert exported.status_code == 200
    assert "PREBILL_FISCAL_AMOUNT_DROP" in exported.content.decode("utf-8-sig")
    assert "'=RIDUZIONE_SINTETICA()" in exported.content.decode("utf-8-sig")
    assert repository.last_alert_filters == {
        "severity": "HIGH",
        "rule": "DROP",
        "status": "OPEN",
        "device_id": "pos_1",
        "operator_code": "op_1",
    }


def test_invalid_alert_transition_is_a_validation_error() -> None:
    client, repository = _client()
    response = client.patch(
        f"/api/v1/alerts/{repository.alert_id}",
        headers=_login(client),
        json={"status": "NOT_A_STATE"},
    )
    assert response.status_code == 422
    assert response.headers["X-Correlation-ID"]


def test_only_admin_can_toggle_rule() -> None:
    client, _ = _client()
    auditor = _login(client)
    assert (
        client.patch(
            "/api/v1/rules/PREBILL_FISCAL_AMOUNT_DROP?enabled=false", headers=auditor
        ).status_code
        == 403
    )
    admin = _login(client, "admin")
    response = client.patch("/api/v1/rules/PREBILL_FISCAL_AMOUNT_DROP?enabled=false", headers=admin)
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_login_throttle_is_bounded_and_rotating_usernames_cannot_bypass_ip_limit() -> None:
    throttle = LoginThrottle(limit=3, window_seconds=300, maximum_buckets=6)
    for index in range(3):
        throttle.failure("192.0.2.50", f"random-user-{index}")
    assert throttle.retry_after("192.0.2.50", "another-random-user") > 0
    for index in range(50):
        throttle.failure(f"192.0.2.{index + 60}", f"user-{index}")
    assert len(throttle._events) <= 6  # noqa: SLF001 - security-bound regression
