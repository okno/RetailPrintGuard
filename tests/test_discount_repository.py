from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_db_repository import NOW, _seed_api
from test_export_selection import _add_document, _selection_corpus

from retailprintguard.api.auth import TokenService
from retailprintguard.api.main import create_app
from retailprintguard.api.schemas import RoleName, UserPrincipal
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import Document, DocumentLine, DocumentVersion
from retailprintguard.db.repository import SqlAlchemyApiRepository


@pytest.fixture
def discount_repo(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'discounts.db'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    ids = _seed_api(factory)
    repository = SqlAlchemyApiRepository(factory)
    yield repository, factory, ids
    engine.dispose()


def _discount(factory, ids, *, label="Sconto dipendenti 30%", complete=True,
              kind="MANAGEMENT_DOCUMENT", total="7.00"):
    document_id = _add_document(
        factory, ids, kind, f"Articolo 10,00\n{label} -3,00\nTOT {total}",
        complete=complete,
    )
    with factory.begin() as session:
        version = session.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == document_id
        ))
        version.gross_total = Decimal(total)
        for sequence, description, amount in (
            (1, "Articolo", "10.00"), (2, label, "-3.00"), (3, "TOT", total),
        ):
            session.add(DocumentLine(
                document_version_id=version.id, sequence=sequence,
                description=description, raw_text=description, line_total=Decimal(amount),
            ))
    return document_id


def test_observed_totals_match_dashboard_and_paginated_drilldown(discount_repo):
    repo, factory, ids = discount_repo
    first = _discount(factory, ids)
    second = _discount(factory, ids)
    generic = _discount(factory, ids, label="Sconto 30%")
    partial = _discount(factory, ids, complete=False)
    _discount(factory, ids, kind="SHIFT_END_REPORT")
    _discount(factory, ids, kind="DEVICE_RESPONSE")
    result = repo.list_discounts(limit=1, offset=0, filters={})
    assert result.total == 4 and len(result.items) == 1
    assert result.summary.observed_employee_discount_total == 9
    assert result.summary.observed_employee_discount_count == 3
    assert result.summary.arithmetic_reconciled_employee_discount_total == 6
    assert result.summary.arithmetic_reconciled_employee_discount_count == 2
    assert result.summary.observed_generic_30_discount_total == 3
    assert result.summary.observed_generic_30_discount_count == 1
    assert result.items[0].device_name == "POS uno"
    all_items = repo.list_discounts(limit=20, offset=0, filters={}).items
    assert {item.document_id for item in all_items} == {first, second, generic, partial}
    assert len({item.document_version_id for item in all_items}) == 4
    dashboard = repo.dashboard()
    for key, value in result.summary.model_dump().items():
        assert getattr(dashboard, key) == value
    # Same amount and time are not sufficient grounds for collapsing receipts.
    assert dashboard.observed_employee_discount_count == 3
    assert repo.list_discounts(limit=20, offset=100, filters={}).total == 4


def test_discount_filters_use_capture_time_and_full_document_quadrature(discount_repo):
    repo, factory, ids = discount_repo
    verified = _discount(factory, ids)
    mismatch = _discount(factory, ids, total="8.00")
    generic = _discount(factory, ids, label="Sconto 30%")
    with factory.begin() as session:
        session.get(Document, verified).captured_at = NOW - timedelta(days=1)
    result = repo.list_discounts(limit=20, offset=0, filters={
        "from": NOW, "to": NOW + timedelta(days=1), "kind": "employee_30",
        "reconciliation": "review", "device_id": "pos_1",
    })
    assert [item.document_id for item in result.items] == [mismatch]
    assert result.summary.observed_employee_discount_total == 3
    assert result.summary.arithmetic_reconciled_employee_discount_total == 0
    assert result.items[0].discounts.observations[0].arithmetic_verified
    assert not result.items[0].discounts.arithmetic_reconciled
    assert repo.list_discounts(limit=20, offset=0, filters={"to": NOW}).total == 1
    assert repo.list_discounts(limit=20, offset=0, filters={"device_id": "missing"}).total == 0
    assert [item.document_id for item in repo.list_discounts(
        limit=20, offset=0, filters={"kind": "generic_30"}
    ).items] == [generic]


def test_dashboard_classification_matches_documents_and_keeps_global_coverage(discount_repo):
    repo, factory, ids = discount_repo
    complete, partial, excluded = _selection_corpus(factory, ids)
    dashboard = repo.dashboard()
    assert dashboard.complete_documents == len(complete)
    assert dashboard.partial_documents == len(partial)
    assert dashboard.excluded_evidence == len(excluded)
    assert dashboard.total_evidence == len(complete | partial | excluded)
    assert sum(dashboard.complete_document_type_counts.values()) == len(complete)
    for kind, count in dashboard.complete_document_type_counts.items():
        assert repo.list_documents(limit=200, offset=0, filters={
            "document_scope": "complete", "type": kind,
        })[1] == count
    empty = repo.dashboard(filters={"from": NOW + timedelta(days=1)})
    assert empty.total_evidence == 0
    assert empty.available_data_from == NOW and empty.available_data_to == NOW


def test_dashboard_capture_bounds_follow_selected_period_and_all_history(discount_repo):
    repo, factory, ids = discount_repo
    technical = _add_document(factory, ids, "DEVICE_RESPONSE", "ON00000000")
    first = _add_document(factory, ids, "COMMERCIAL_DOCUMENT", "Articolo 1,00")
    last_partial = _discount(factory, ids, complete=False)
    with factory.begin() as session:
        session.get(Document, technical).captured_at = NOW - timedelta(days=4)
        session.get(Document, first).captured_at = NOW - timedelta(days=3)
        session.get(Document, last_partial).captured_at = NOW + timedelta(days=2)

    all_history = repo.dashboard()
    assert all_history.first_document_captured_at == NOW - timedelta(days=3)
    assert all_history.last_document_captured_at == NOW + timedelta(days=2)
    assert all_history.available_data_from == NOW - timedelta(days=4)

    selected = repo.dashboard(filters={"from": NOW, "to": NOW + timedelta(days=2)})
    assert selected.first_document_captured_at == NOW
    assert selected.last_document_captured_at == NOW

    empty = repo.dashboard(filters={"from": NOW + timedelta(days=3)})
    assert empty.first_document_captured_at is None
    assert empty.last_document_captured_at is None
    assert empty.available_data_to == NOW + timedelta(days=2)


def test_discount_route_validates_period_roles_and_pagination(discount_repo):
    repo, factory, ids = discount_repo
    _discount(factory, ids)
    secret = b"discount-route-test-signing-key-only"
    app = create_app(repository=repo, jwt_secret=secret)
    user = UserPrincipal(id=ids["user"], username="reader", roles=(RoleName.READ_ONLY,))
    token = TokenService(secret, lifetime_minutes=30).issue(user)
    with TestClient(app) as client:
        assert client.get("/api/v1/discounts").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        response = client.get("/api/v1/discounts", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["summary"]["observed_employee_discount_total"] == "3.0000"
        assert response.json()["date_basis"] == "captured_at"
        for parameters in ({"from": "2042-05-06"}, {"limit": 201}, {"offset": -1},
                           {"kind": "authorized"}, {"reconciliation": "yes"}):
            assert client.get("/api/v1/discounts", headers=headers,
                              params=parameters).status_code == 422
