from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from retailprintguard.common.domain import DocumentType
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import Device, Document, DocumentVersion, PrintJob, RawPayload
from retailprintguard.db.repository import (
    SqlAlchemyApiRepository,
    SqlAlchemyIngestionRepository,
)
from retailprintguard.importer.main import cli as importer_cli
from retailprintguard.ingestion.dgfe import (
    DETAIL_NAMESPACE,
    SUMMARY_NAMESPACE,
    RCHFiscalMemoryXMLAdapter,
)
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    ArtifactSnapshot,
    Endpoint,
    NormalizedDocument,
    NormalizedEnvelope,
    SourceKind,
)
from retailprintguard.ingestion.errors import SourceValidationError
from retailprintguard.ingestion.repository import ImportDisposition, MemoryIngestionRepository
from retailprintguard.ingestion.worker import IngestionWorker

SERIAL = "99XX0000001"
TARGET = Endpoint("192.0.2.251", 23)


def _detail_xml(
    *,
    read_at: str = "2042-05-06T21:00:00",
    document_at: str = "2042-05-06T20:00:00",
    description: str = "Espresso",
    part: int | None = None,
    total_parts: int | None = None,
    chain_key: str | None = None,
    version: str = "3.0",
) -> bytes:
    multipart = ""
    if part is not None:
        multipart += f"<Parte>{part}</Parte>"
    if chain_key is not None:
        multipart += f"<ChiaveConcatenazione>{chain_key}</ChiaveConcatenazione>"
    if total_parts is not None:
        multipart += f"<NumeroTotaleParti>{total_parts}</NumeroTotaleParti>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MemoriaDettaglio xmlns="{DETAIL_NAMESPACE}" versione="{version}">
  <Periodo integrale="false">
    <Dal>2042-05-06T00:00:00</Dal>
    <Al>2042-05-06T23:59:59</Al>
    <MatricolaRT>{SERIAL}</MatricolaRT>
    <LetturaDel>{read_at}</LetturaDel>
    {multipart}
  </Periodo>
  <RecordIntestazioni>
    <Intestazione>
      <DataOra>2042-05-06T19:00:00</DataOra>
      <Denominazione>ESERCENTE SINTETICO</Denominazione>
      <DatiUtente>Fixture test</DatiUtente>
      <Indirizzo>
        <Comune>Testville</Comune><Provincia>TS</Provincia>
        <Indirizzo>Via Esempio 1</Indirizzo><Cap>21000</Cap>
      </Indirizzo>
      <PartitaIva>00000000000</PartitaIva>
    </Intestazione>
  </RecordIntestazioni>
  <SessioniGiornaliere><SessioneGiornaliera><DocumentiEmessi>
    <DocumentoCommerciale><Vendita>
      <DataOra>{document_at}</DataOra><PuntoCassa>ABCD0001</PuntoCassa>
      <NumeroDocumento>2</NumeroDocumento><NumeroAzzeramento>1</NumeroAzzeramento>
      <Dettaglio><RigaGenerica><Riga>
        <Elemento Bene="true">
          <Importo>3.00</Importo><Descrizione>{description}</Descrizione>
          <Quantita>1</Quantita><PrezzoUnitario>3.00</PrezzoUnitario>
          <IVA><AliquotaIVA>10.00</AliquotaIVA><Imposta>0.27</Imposta></IVA>
        </Elemento>
        <ModificatoreElemento>
          <Importo>0.30</Importo><Segno>-</Segno><Descrizione>Sconto</Descrizione>
          <IVA><AliquotaIVA>10.00</AliquotaIVA></IVA>
        </ModificatoreElemento>
      </Riga></RigaGenerica></Dettaglio>
      <ImportoTotale>2.70</ImportoTotale>
      <Pagamenti><Pagamento><Importo>2.70</Importo><Tipo>PC</Tipo></Pagamento>
      <Resto>0.00</Resto></Pagamenti>
    </Vendita></DocumentoCommerciale>
  </DocumentiEmessi></SessioneGiornaliera></SessioniGiornaliere>
</MemoriaDettaglio>
""".encode()


def _summary_xml() -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MemoriaRiepilogo xmlns="{SUMMARY_NAMESPACE}" versione="3.0">
  <Periodo integrale="false">
    <DaChiusuraNumero>1</DaChiusuraNumero><AChiusuraNumero>1</AChiusuraNumero>
    <MatricolaRT>{SERIAL}</MatricolaRT><LetturaDel>2042-05-07T09:00:00</LetturaDel>
  </Periodo>
  <RecordIntestazioni />
  <RecordDati><RecordAzzeramento>
    <DataOra>2042-05-06T23:30:00</DataOra><Numero>1</Numero><Modalita>RT</Modalita>
    <Corrispettivo>123.45</Corrispettivo><TotaleAnnullo>2.00</TotaleAnnullo>
    <TotaleReso>1.00</TotaleReso><NumeroDocumentiCommerciali>42</NumeroDocumentiCommerciali>
  </RecordAzzeramento></RecordDati>
</MemoriaRiepilogo>
""".encode()


def _adapter(root: Path) -> RCHFiscalMemoryXMLAdapter:
    return RCHFiscalMemoryXMLAdapter(
        root,
        source_instance_id="xtools-synthetic",
        device_id="rch_1",
        device_endpoint=TARGET,
        timezone="Europe/Rome",
    )


def test_dgfe_adapter_preserves_xml_and_normalizes_fiscal_identity(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    path = source / "LetturaMemoriaDettaglio_synthetic.xml"
    original = _detail_xml()
    path.write_bytes(original)

    adapter = _adapter(source)
    envelope = adapter.load(adapter.discover(maximum=10)[0])

    assert envelope.source_kind is SourceKind.RCH_XTOOLS_DGFE_V1
    assert envelope.device_endpoint == TARGET
    assert envelope.chunks == ()
    assert envelope.complete is True
    assert len(envelope.documents) == 1
    artifact = envelope.artifacts[0]
    assert artifact.role is ArtifactRole.DGFE_XML
    assert artifact.content == original
    assert path.read_bytes() == original

    document = envelope.documents[0]
    assert document.document_type is DocumentType.COMMERCIAL_DOCUMENT
    assert document.subtype == "RCH_DGFE_VENDITA"
    assert document.capture_time == datetime(2042, 5, 6, 18, 0, tzinfo=UTC)
    assert document.semantic["external_document_code"] == "0001-0002"
    assert document.semantic["rch_serial_number"] == SERIAL
    assert document.semantic["provenance"] == "RCH_DGFE_EXPORT"
    assert document.semantic["document_timestamp_precision"] == "SECOND"
    assert document.semantic["document_timestamp_evidence"] == "RCH_DGFE_EXPORT"
    assert document.semantic["receipt_header"]["evidence"] == "RCH_DGFE_EXPORT"
    assert document.semantic["receipt_header"]["merchant_name"] == "ESERCENTE SINTETICO"
    assert document.semantic["lines"][0]["discount"] == "0.30"
    assert document.semantic["payments"][0]["method"] == "PC"
    assert document.semantic.get("rch_footer_timestamp") is None


def test_fiscal_summary_export_becomes_shift_end_report(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    (source / "LetturaMemoriaRiepilogo_synthetic.xml").write_bytes(_summary_xml())

    envelope = _adapter(source).load(_adapter(source).discover(maximum=10)[0])

    assert envelope.source_kind is SourceKind.RCH_XTOOLS_FISCAL_SUMMARY_V1
    assert envelope.artifacts[0].role is ArtifactRole.FISCAL_SUMMARY_XML
    assert len(envelope.documents) == 1
    report = envelope.documents[0]
    assert report.document_type is DocumentType.SHIFT_END_REPORT
    assert report.semantic["gross_total"] == "123.45"
    assert report.semantic["total_cancellations"] == "2.00"
    assert report.semantic["total_refunds"] == "1.00"
    assert report.semantic["document_timestamp_evidence"] == (
        "RCH_FISCAL_SUMMARY_EXPORT"
    )


def test_dgfe_adapter_rejects_dtd_entities_before_parsing(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    malicious = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x "expanded">]>'
        + f'<MemoriaDettaglio xmlns="{DETAIL_NAMESPACE}" versione="3.0">'.encode()
        + b"<Periodo integrale=\"false\"><MatricolaRT>99XX0000001</MatricolaRT>"
        + b"<LetturaDel>&x;</LetturaDel></Periodo></MemoriaDettaglio>"
    )
    (source / "LetturaMemoriaDettaglio_malicious.xml").write_bytes(malicious)

    with pytest.raises(SourceValidationError, match="DTD|entity"):
        _adapter(source).discover(maximum=10)


def test_dgfe_adapter_fails_closed_on_unknown_payload_version(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    (source / "LetturaMemoriaDettaglio_future.xml").write_bytes(
        _detail_xml(version="9.9")
    )
    adapter = _adapter(source)

    with pytest.raises(SourceValidationError, match="payload version"):
        adapter.load(adapter.discover(maximum=10)[0])


def test_dgfe_adapter_marks_independent_multipart_part_incomplete(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    read_at = "2042-05-06T21:00:00"
    chain = hashlib.sha256(f"{read_at}0{SERIAL}".encode()).hexdigest()
    (source / "LetturaMemoriaDettaglio_part1.xml").write_bytes(
        _detail_xml(part=1, total_parts=2, chain_key=chain)
    )
    adapter = _adapter(source)

    envelope = adapter.load(adapter.discover(maximum=10)[0])

    assert envelope.complete is False
    assert envelope.status == "PARTIAL_MULTIPART"
    assert envelope.artifacts[0].complete is True
    assert envelope.metadata["period"]["chiave_concatenazione_verificata"] is True
    assert "requires reconciliation" in envelope.warnings[0]


def test_dgfe_adapter_rejects_ambiguous_naive_fiscal_time(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    (source / "LetturaMemoriaDettaglio_dst.xml").write_bytes(
        _detail_xml(document_at="2042-10-26T02:30:00")
    )
    adapter = RCHFiscalMemoryXMLAdapter(
        source,
        source_instance_id="xtools-synthetic",
        device_id="rch_1",
        device_endpoint=TARGET,
        timezone="Europe/Rome",
    )

    with pytest.raises(SourceValidationError, match="ambiguous"):
        adapter.load(adapter.discover(maximum=10)[0])


def test_dgfe_worker_is_idempotent_for_same_export(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    (source / "LetturaMemoriaDettaglio_synthetic.xml").write_bytes(_detail_xml())
    repository = MemoryIngestionRepository()
    worker = IngestionWorker(repository, [_adapter(source)], sleeper=lambda _delay: None)

    first = worker.run_once(maximum_per_adapter=10)
    repeated = worker.run_once(maximum_per_adapter=10)

    assert first.imported == 1
    assert repeated.duplicates == 1
    assert len(repository.imports) == 1


def test_existing_import_cli_accepts_dgfe_directory(tmp_path: Path, capsys: object) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    (source / "LetturaMemoriaDettaglio_synthetic.xml").write_bytes(_detail_xml())
    config = Path(__file__).parents[1] / "config" / "retailprintguard.example.yaml"

    exit_code = importer_cli(
        [
            "--config",
            str(config),
            "--dgfe-root",
            str(source),
            "--dgfe-device",
            "rch_1",
            "--validate-only",
            "--json",
        ]
    )

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert exit_code == 0
    assert '"imported": 1' in output
    assert '"quarantined": 0' in output


def _proxy_envelope_same_identity() -> NormalizedEnvelope:
    raw = b"synthetic observed stream"
    digest = hashlib.sha256(raw).hexdigest()
    return NormalizedEnvelope(
        source_key="synthetic-proxy:same-fiscal-identity",
        source_kind=SourceKind.COMMERCIAL_RCH_PARSED_V1,
        source_instance_id="proxy-synthetic",
        device_id="rch_1",
        source_job_id="proxy-job",
        source_session_id="proxy-session",
        connection_id=None,
        opened_at=datetime(2042, 5, 6, 18, 0, tzinfo=UTC),
        closed_at=datetime(2042, 5, 6, 18, 0, tzinfo=UTC),
        source_endpoint=None,
        proxy_endpoint=None,
        device_endpoint=TARGET,
        status="COMPLETE",
        complete=True,
        boundary_source="SYNTHETIC",
        boundary_confidence=1.0,
        delivery_evidence="SYNTHETIC",
        manifest_sha256=digest,
        parser_version="synthetic-1",
        artifacts=(
            ArtifactSnapshot(
                role=ArtifactRole.REQUEST_RAW,
                original_path=Path("/synthetic/proxy.raw"),
                sha256=digest,
                size=len(raw),
                content=raw,
            ),
        ),
        chunks=(),
        documents=(
            NormalizedDocument(
                external_id="proxy-doc",
                document_type=DocumentType.COMMERCIAL_DOCUMENT,
                subtype="RCH_STREAM_VENDITA",
                complete=True,
                evidence="CONFIRMED",
                capture_time=datetime(2042, 5, 6, 18, 0, tzinfo=UTC),
                timezone="Europe/Rome",
                source_start_offset=0,
                source_end_offset=len(raw),
                source_frame_ids=(),
                semantic={
                    "external_document_code": "0001-0002",
                    "rch_serial_number": SERIAL,
                    "gross_total": "2.70",
                    "normalized_text": "documento sintetico dal flusso",
                },
            ),
        ),
        metadata={},
    )


def test_overlapping_dgfe_exports_append_versions_without_merging_proxy_document(
    tmp_path: Path,
) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    first_path = source / "LetturaMemoriaDettaglio_first.xml"
    first_path.write_bytes(_detail_xml(description="Espresso"))
    adapter = _adapter(source)
    first_envelope = adapter.load(adapter.discover(maximum=10)[0])
    first_path.rename(source / "first.imported")
    (source / "LetturaMemoriaDettaglio_second.xml").write_bytes(
        _detail_xml(read_at="2042-05-06T21:05:00", description="Espresso verificato")
    )
    second_envelope = adapter.load(adapter.discover(maximum=10)[0])

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH sintetica",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.231",
                listen_port=23,
                target_ip=TARGET.ip,
                target_port=TARGET.port,
            )
        )
    repository = SqlAlchemyIngestionRepository(factory)

    assert (
        repository.store_import(_proxy_envelope_same_identity()).disposition
        is ImportDisposition.IMPORTED
    )
    assert repository.store_import(first_envelope).disposition is ImportDisposition.IMPORTED
    assert repository.store_import(second_envelope).disposition is ImportDisposition.IMPORTED

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PrintJob)) == 3
        assert all(
            job.timeline_complete
            for job in session.scalars(
                select(PrintJob).where(
                    PrintJob.source_system == SourceKind.RCH_XTOOLS_DGFE_V1.value
                )
            )
        )
        assert session.scalar(select(func.count()).select_from(RawPayload)) == 3
        assert session.scalar(select(func.count()).select_from(Document)) == 2
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 3
        dgfe_document = session.scalar(
            select(Document)
            .join(PrintJob, PrintJob.id == Document.job_id)
            .where(PrintJob.source_system == SourceKind.RCH_XTOOLS_DGFE_V1.value)
        )
        assert dgfe_document is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(DocumentVersion)
                .where(DocumentVersion.document_id == dgfe_document.id)
            )
            == 2
        )
        assert all(
            raw.artifact_role == ArtifactRole.DGFE_XML.value
            for raw in session.scalars(
                select(RawPayload)
                .join(PrintJob, PrintJob.id == RawPayload.job_id)
                .where(PrintJob.source_system == SourceKind.RCH_XTOOLS_DGFE_V1.value)
            )
        )
    api_repository = SqlAlchemyApiRepository(factory)
    projected = api_repository.get_document(dgfe_document.id)
    assert projected is not None
    assert projected.receipt_header is not None
    assert projected.receipt_header.evidence == "RCH_DGFE_EXPORT"
    assert projected.document_timestamp_evidence == "RCH_DGFE_EXPORT"
    source_artifact = api_repository.get_document_raw(
        dgfe_document.id, direction="source"
    )
    assert source_artifact is not None
    assert source_artifact.content == second_envelope.artifacts[0].content
    assert source_artifact.media_type == "application/xml"
    assert source_artifact.filename.endswith(".xml")
    engine.dispose()


def test_overlapping_fiscal_summary_exports_append_versions(tmp_path: Path) -> None:
    source = tmp_path / "exports"
    source.mkdir()
    first_path = source / "LetturaMemoriaRiepilogo_first.xml"
    first_path.write_bytes(_summary_xml())
    adapter = _adapter(source)
    first = adapter.load(adapter.discover(maximum=10)[0])
    first_path.rename(source / "first.imported")
    (source / "LetturaMemoriaRiepilogo_second.xml").write_bytes(
        _summary_xml().replace(b"2042-05-07T09:00:00", b"2042-05-07T09:05:00")
    )
    second = adapter.load(adapter.discover(maximum=10)[0])

    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    with factory.begin() as session:
        session.add(
            Device(
                external_id="rch_1",
                name="RCH sintetica",
                device_type="rch",
                parser_kind="rch_observed",
                listen_ip="192.0.2.231",
                listen_port=23,
                target_ip=TARGET.ip,
                target_port=TARGET.port,
            )
        )
    repository = SqlAlchemyIngestionRepository(factory)
    assert repository.store_import(first).disposition is ImportDisposition.IMPORTED
    assert repository.store_import(second).disposition is ImportDisposition.IMPORTED
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PrintJob)) == 2
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 2
    engine.dispose()
