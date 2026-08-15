"""Repository boundary used by HTTP handlers and tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from retailprintguard.api.schemas import (
    AlertUpdate,
    AlertView,
    AuditEntry,
    DashboardView,
    DeviceView,
    DiagnosticsView,
    DocumentView,
    ImportBatchView,
    JobReviewRequest,
    JobView,
    OrderView,
    RuleView,
    SearchHit,
    SessionView,
    TransactionView,
    UserPrincipal,
)


class RawArtifact:
    def __init__(
        self,
        content: bytes,
        filename: str,
        sha256: str,
        *,
        media_type: str = "application/octet-stream",
    ) -> None:
        self.content = content
        self.filename = filename
        self.sha256 = sha256
        self.media_type = media_type


class ApiRepository(Protocol):
    def authenticate(self, username: str, password: str) -> UserPrincipal | None: ...

    def dashboard(self, *, filters: dict[str, Any] | None = None) -> DashboardView: ...

    def diagnostics(self) -> DiagnosticsView: ...

    def list_devices(self) -> Sequence[DeviceView]: ...

    def list_sessions(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[SessionView], int]: ...

    def list_jobs(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[JobView], int]: ...

    def review_job(
        self,
        job_id: UUID,
        review: JobReviewRequest,
        actor: UserPrincipal,
        *,
        correlation_id: str,
    ) -> JobView | None: ...

    def list_documents(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[DocumentView], int]: ...

    def get_document(self, document_id: UUID) -> DocumentView | None: ...

    def get_document_raw(
        self, document_id: UUID, *, direction: str = "request"
    ) -> RawArtifact | None: ...

    def get_job_raw(self, job_id: UUID, *, direction: str) -> RawArtifact | None: ...

    def get_session_raw(self, session_id: UUID, *, direction: str) -> RawArtifact | None: ...

    def list_orders(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[OrderView], int]: ...

    def list_transactions(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[TransactionView], int]: ...

    def get_transaction(self, transaction_id: UUID) -> TransactionView | None: ...

    def list_alerts(
        self, *, limit: int, offset: int, filters: dict[str, Any]
    ) -> tuple[list[AlertView], int]: ...

    def get_alert(self, alert_id: UUID) -> AlertView | None: ...

    def update_alert(
        self, alert_id: UUID, update: AlertUpdate, actor: UserPrincipal
    ) -> AlertView | None: ...

    def list_rules(self) -> Sequence[RuleView]: ...

    def set_rule_enabled(
        self, code: str, enabled: bool, actor: UserPrincipal
    ) -> RuleView | None: ...

    def list_imports(self, *, limit: int, offset: int) -> tuple[list[ImportBatchView], int]: ...

    def search(
        self,
        *,
        query: str,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[SearchHit], int]: ...

    def append_audit(self, entry: AuditEntry) -> None: ...

    def database_health(self) -> str: ...

    def spool_health(self) -> str: ...


class RepositoryUnavailable(RuntimeError):
    """Raised when the control-plane database cannot satisfy a request."""


class EmptyRepository:
    """Safe startup repository: health works, protected data is empty."""

    def authenticate(self, username: str, password: str) -> UserPrincipal | None:
        del username, password
        return None

    def dashboard(self, *, filters: dict[str, Any] | None = None) -> DashboardView:
        del filters
        return DashboardView()

    def diagnostics(self) -> DiagnosticsView:
        from datetime import UTC, datetime

        return DiagnosticsView(
            generated_at=datetime.now(UTC),
            database="unconfigured",
            spool=self.spool_health(),
        )

    def list_devices(self) -> Sequence[DeviceView]:
        return ()

    @staticmethod
    def _empty(*_args: Any, **_kwargs: Any) -> tuple[list[Any], int]:
        return [], 0

    list_sessions = _empty
    list_jobs = _empty
    list_documents = _empty
    list_orders = _empty
    list_transactions = _empty
    list_alerts = _empty
    list_imports = _empty
    search = _empty

    def review_job(
        self,
        job_id: UUID,
        review: JobReviewRequest,
        actor: UserPrincipal,
        *,
        correlation_id: str,
    ) -> JobView | None:
        del job_id, review, actor, correlation_id
        return None

    def get_document(self, document_id: UUID) -> DocumentView | None:
        del document_id
        return None

    def get_document_raw(
        self, document_id: UUID, *, direction: str = "request"
    ) -> RawArtifact | None:
        del document_id, direction
        return None

    def get_job_raw(self, job_id: UUID, *, direction: str) -> RawArtifact | None:
        del job_id, direction
        return None

    def get_session_raw(self, session_id: UUID, *, direction: str) -> RawArtifact | None:
        del session_id, direction
        return None

    def get_transaction(self, transaction_id: UUID) -> TransactionView | None:
        del transaction_id
        return None

    def get_alert(self, alert_id: UUID) -> AlertView | None:
        del alert_id
        return None

    def update_alert(
        self, alert_id: UUID, update: AlertUpdate, actor: UserPrincipal
    ) -> AlertView | None:
        del alert_id, update, actor
        return None

    def list_rules(self) -> Sequence[RuleView]:
        return ()

    def set_rule_enabled(self, code: str, enabled: bool, actor: UserPrincipal) -> RuleView | None:
        del code, enabled, actor
        return None

    def append_audit(self, entry: AuditEntry) -> None:
        del entry

    def database_health(self) -> str:
        return "unconfigured"

    def spool_health(self) -> str:
        return "unknown"
