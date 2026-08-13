"""Versioned API routes with role checks and server-side pagination."""

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from retailprintguard import __version__
from retailprintguard.api.auth import LoginThrottle, TokenService, constant_time_dummy_verify
from retailprintguard.api.repository import ApiRepository
from retailprintguard.api.schemas import (
    AlertUpdate,
    AlertView,
    AuditEntry,
    DashboardView,
    DeviceView,
    DocumentView,
    HealthView,
    ImportBatchView,
    JobView,
    LoginRequest,
    OrderView,
    Page,
    RoleName,
    RuleView,
    SearchHit,
    SessionView,
    TokenResponse,
    TransactionView,
    UserPrincipal,
)

bearer = HTTPBearer(auto_error=False)


class ApiContext:
    def __init__(
        self,
        repository: ApiRepository,
        tokens: TokenService,
        throttle: LoginThrottle,
        *,
        spool_health: str = "unknown",
    ) -> None:
        self.repository = repository
        self.tokens = tokens
        self.throttle = throttle
        self.spool_health = spool_health


def create_router(context: ApiContext) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def repository() -> ApiRepository:
        return context.repository

    def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> UserPrincipal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Autenticazione richiesta")
        try:
            principal = context.tokens.decode(credentials.credentials)
        except (jwt.InvalidTokenError, ValueError):
            raise HTTPException(status_code=401, detail="Token non valido o scaduto") from None
        if not principal.active:
            raise HTTPException(status_code=403, detail="Utente disabilitato")
        return principal

    def roles(*allowed: RoleName) -> Any:
        def dependency(user: Annotated[UserPrincipal, Depends(current_user)]) -> UserPrincipal:
            if not set(user.roles).intersection(allowed):
                raise HTTPException(status_code=403, detail="Permessi insufficienti")
            return user

        return dependency

    AnyUser = Annotated[UserPrincipal, Depends(current_user)]
    Reviewer = Annotated[
        UserPrincipal,
        Depends(roles(RoleName.ADMIN, RoleName.AUDITOR, RoleName.OPERATOR)),
    ]
    Auditor = Annotated[UserPrincipal, Depends(roles(RoleName.ADMIN, RoleName.AUDITOR))]
    Admin = Annotated[UserPrincipal, Depends(roles(RoleName.ADMIN))]

    def audit(
        request: Request,
        repo: ApiRepository,
        actor: UserPrincipal | None,
        action: str,
        entity_type: str,
        entity_id: str | None = None,
        **metadata: Any,
    ) -> None:
        repo.append_audit(
            AuditEntry(
                actor_id=actor.id if actor else None,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                correlation_id=request.state.correlation_id,
                occurred_at=datetime.now(UTC),
                metadata=metadata,
            )
        )

    @router.post("/auth/login", response_model=TokenResponse, tags=["auth"])
    def login(
        request: Request, body: LoginRequest, repo: Annotated[ApiRepository, Depends(repository)]
    ) -> TokenResponse:
        client_ip = request.client.host if request.client else "unknown"
        retry_after = context.throttle.retry_after(client_ip, body.username)
        if retry_after > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Troppi tentativi; riprovare più tardi",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
        user = repo.authenticate(body.username, body.password)
        if user is None or not user.active:
            constant_time_dummy_verify(body.password)
            context.throttle.failure(client_ip, body.username)
            raise HTTPException(status_code=401, detail="Credenziali non valide")
        context.throttle.success(client_ip, body.username)
        audit(request, repo, user, "AUTH_LOGIN", "user", str(user.id))
        return TokenResponse(
            access_token=context.tokens.issue(user),
            expires_in=context.tokens.expires_in,
            user=user,
        )

    @router.get("/auth/me", response_model=UserPrincipal, tags=["auth"])
    def me(user: AnyUser) -> UserPrincipal:
        return user

    @router.get("/dashboard", response_model=DashboardView, tags=["dashboard"])
    def dashboard(_: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]) -> DashboardView:
        return repo.dashboard()

    @router.get("/devices", response_model=list[DeviceView], tags=["devices"])
    def devices(
        _: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]
    ) -> list[DeviceView]:
        return list(repo.list_devices())

    @router.get("/sessions", response_model=Page[SessionView], tags=["sessions"])
    def sessions(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        device_id: str | None = None,
    ) -> Page[SessionView]:
        items, total = repo.list_sessions(
            limit=limit, offset=offset, filters={"device_id": device_id}
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/jobs", response_model=Page[JobView], tags=["jobs"])
    def jobs(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        device_id: str | None = None,
        job_status: str | None = Query(default=None, alias="status"),
    ) -> Page[JobView]:
        items, total = repo.list_jobs(
            limit=limit,
            offset=offset,
            filters={"device_id": device_id, "status": job_status},
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/documents", response_model=Page[DocumentView], tags=["documents"])
    def documents(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        document_type: str | None = Query(default=None, alias="type"),
        device_id: str | None = None,
        order_code: str | None = None,
    ) -> Page[DocumentView]:
        items, total = repo.list_documents(
            limit=limit,
            offset=offset,
            filters={"type": document_type, "device_id": device_id, "order_code": order_code},
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/documents/{document_id}", response_model=DocumentView, tags=["documents"])
    def document(
        document_id: UUID, _: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]
    ) -> DocumentView:
        result = repo.get_document(document_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Documento non trovato")
        return result

    @router.get("/documents/{document_id}/raw", tags=["documents"])
    def document_raw(
        document_id: UUID,
        request: Request,
        user: Auditor,
        repo: Annotated[ApiRepository, Depends(repository)],
    ) -> Response:
        artifact = repo.get_document_raw(document_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Payload originale non trovato")
        audit(
            request,
            repo,
            user,
            "RAW_DOWNLOAD",
            "document",
            str(document_id),
            sha256=artifact.sha256,
        )
        return Response(
            content=artifact.content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "Digest": f"sha-256={artifact.sha256}",
            },
        )

    @router.get("/orders", response_model=Page[OrderView], tags=["orders"])
    def orders(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        table_code: str | None = None,
        order_code: str | None = None,
    ) -> Page[OrderView]:
        items, total = repo.list_orders(
            limit=limit, offset=offset, filters={"table_code": table_code, "order_code": order_code}
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/transactions", response_model=Page[TransactionView], tags=["transactions"])
    def transactions(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        table_code: str | None = None,
        operator_code: str | None = None,
        minimum_difference: float | None = None,
    ) -> Page[TransactionView]:
        items, total = repo.list_transactions(
            limit=limit,
            offset=offset,
            filters={
                "table_code": table_code,
                "operator_code": operator_code,
                "minimum_difference": minimum_difference,
            },
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get(
        "/transactions/{transaction_id}", response_model=TransactionView, tags=["transactions"]
    )
    def transaction(
        transaction_id: UUID, _: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]
    ) -> TransactionView:
        result = repo.get_transaction(transaction_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Transazione non trovata")
        return result

    @router.get("/alerts/export.csv", tags=["alerts"])
    def export_alerts(
        request: Request,
        user: Auditor,
        repo: Annotated[ApiRepository, Depends(repository)],
        severity: str | None = None,
        alert_status: str | None = Query(default=None, alias="status"),
    ) -> StreamingResponse:
        items, _ = repo.list_alerts(
            limit=10_000, offset=0, filters={"severity": severity, "status": alert_status}
        )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            ["id", "regola", "severita", "punteggio", "stato", "aperto_il", "descrizione"]
        )
        for item in items:
            writer.writerow(
                [
                    item.id,
                    item.rule_code,
                    item.severity,
                    item.score,
                    item.status,
                    item.opened_at.isoformat(),
                    item.description,
                ]
            )
        audit(request, repo, user, "ALERT_EXPORT", "fraud_alert", count=len(items))
        return StreamingResponse(
            iter([buffer.getvalue().encode("utf-8-sig")]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="alert-antifrode.csv"'},
        )

    @router.get("/alerts", response_model=Page[AlertView], tags=["alerts"])
    def alerts(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        severity: str | None = None,
        rule: str | None = None,
        alert_status: str | None = Query(default=None, alias="status"),
        device_id: str | None = None,
        operator_code: str | None = None,
    ) -> Page[AlertView]:
        items, total = repo.list_alerts(
            limit=limit,
            offset=offset,
            filters={
                "severity": severity,
                "rule": rule,
                "status": alert_status,
                "device_id": device_id,
                "operator_code": operator_code,
            },
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/alerts/{alert_id}", response_model=AlertView, tags=["alerts"])
    def alert(
        alert_id: UUID, _: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]
    ) -> AlertView:
        result = repo.get_alert(alert_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Alert non trovato")
        return result

    @router.patch("/alerts/{alert_id}", response_model=AlertView, tags=["alerts"])
    def update_alert(
        alert_id: UUID,
        body: AlertUpdate,
        request: Request,
        user: Reviewer,
        repo: Annotated[ApiRepository, Depends(repository)],
    ) -> AlertView:
        result = repo.update_alert(alert_id, body, user)
        if result is None:
            raise HTTPException(status_code=404, detail="Alert non trovato")
        audit(request, repo, user, "ALERT_UPDATE", "fraud_alert", str(alert_id), status=body.status)
        return result

    @router.get("/rules", response_model=list[RuleView], tags=["rules"])
    def rules(_: AnyUser, repo: Annotated[ApiRepository, Depends(repository)]) -> list[RuleView]:
        return list(repo.list_rules())

    @router.patch("/rules/{code}", response_model=RuleView, tags=["rules"])
    def toggle_rule(
        code: str,
        enabled: bool,
        request: Request,
        user: Admin,
        repo: Annotated[ApiRepository, Depends(repository)],
    ) -> RuleView:
        result = repo.set_rule_enabled(code, enabled, user)
        if result is None:
            raise HTTPException(status_code=404, detail="Regola non trovata")
        audit(request, repo, user, "RULE_TOGGLE", "fraud_rule", code, enabled=enabled)
        return result

    @router.get("/search", response_model=Page[SearchHit], tags=["search"])
    def search(
        _: AnyUser,
        repo: Annotated[ApiRepository, Depends(repository)],
        q: Annotated[str, Query(min_length=2, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[SearchHit]:
        items, total = repo.search(query=q, limit=limit, offset=offset)
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/imports", response_model=Page[ImportBatchView], tags=["imports"])
    def imports(
        _: Reviewer,
        repo: Annotated[ApiRepository, Depends(repository)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Page[ImportBatchView]:
        items, total = repo.list_imports(limit=limit, offset=offset)
        return Page(items=items, total=total, limit=limit, offset=offset)

    @router.get("/system/health", response_model=HealthView, tags=["system"])
    def health(repo: Annotated[ApiRepository, Depends(repository)]) -> HealthView:
        database = repo.database_health()
        overall = "ok" if database == "ok" else "degraded"
        return HealthView(
            status=overall,
            version=__version__,
            database=database,
            spool=context.spool_health,
            timestamp=datetime.now(UTC),
        )

    return router
