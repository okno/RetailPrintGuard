from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows" / "Acquire-RchDgfeExport.ps1"
GUIDE = ROOT / "docs" / "RCH_DGFE_IMPORT.md"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
DETAIL_NAMESPACE = (
    "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/"
    "memoriadettaglio/export/v2.0"
)


def _synthetic_detail_xml(*, serial: str = "99LABDGFE01") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MemoriaDettaglio xmlns="{DETAIL_NAMESPACE}" versione="3.0">
  <MatricolaRT>{serial}</MatricolaRT>
  <Periodo>
    <LetturaDel>2042-03-01T10:00:00+01:00</LetturaDel>
    <LetturaAl>2042-03-01T10:01:00+01:00</LetturaAl>
  </Periodo>
  <DocumentiCommerciali>
    <DocumentoCommerciale>
      <Tipo>Vendita</Tipo>
      <NumeroDocumento>
        <NumeroAzzeramento>91</NumeroAzzeramento>
        <NumeroDocumento>7</NumeroDocumento>
      </NumeroDocumento>
      <DataOra>2042-03-01T10:00:30+01:00</DataOra>
      <Righe>
        <Riga>
          <Descrizione>ARTICOLO SINTETICO</Descrizione>
          <Quantita>1.00</Quantita>
          <PrezzoUnitario>1.50</PrezzoUnitario>
          <Importo>1.50</Importo>
          <IVA>10.00</IVA>
        </Riga>
      </Righe>
      <TotaleDocumento>1.50</TotaleDocumento>
      <Pagamenti>
        <Pagamento><Tipo>Contanti</Tipo><Importo>1.50</Importo></Pagamento>
      </Pagamenti>
    </DocumentoCommerciale>
  </DocumentiCommerciali>
</MemoriaDettaglio>
""".encode()


def _run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    executable_name = Path(POWERSHELL).name.lower()
    prefix = [POWERSHELL, "-NoProfile"]
    if executable_name == "powershell.exe":
        prefix.extend(["-ExecutionPolicy", "Bypass"])
    # The executable is resolved by shutil.which and arguments are test-created.
    return subprocess.run(  # noqa: S603
        [*prefix, "-File", str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def test_windows_dgfe_companion_has_a_fail_closed_static_contract() -> None:
    content = SCRIPT.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")

    assert "#requires -Version 5.1" in content
    assert "DtdProcessing]::Prohibit" in content
    assert "XmlResolver = $null" in content
    assert "Get-AuthenticodeSignature" in content
    assert "BYTE_IDENTICAL_COPY" in content
    assert "SHA256_COMPUTED" in content
    assert "NO_FISCAL_COMMAND_SENT" in content
    assert "Start-Process -FilePath $xtools.FullName" in content
    assert "Export V11 XML" in content
    assert "ExpectedRchSerial" in content
    assert "Reparse points are not accepted" in content
    for forbidden in (
        "System.Net.Sockets",
        "TcpClient",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "SendKeys",
        "AutomationElement",
        "telnet.exe",
        "nc.exe",
    ):
        assert forbidden not in content
    assert "Acquire-RchDgfeExport.ps1" in guide
    assert "non apre socket" in guide
    assert "non può rendere non presidiata" in guide
    assert "ExecutionPolicy Bypass" in guide


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="requires Windows PowerShell",
)
def test_windows_dgfe_companion_stages_byte_identical_content_idempotently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "xtools-export.xml"
    payload = _synthetic_detail_xml()
    source.write_bytes(payload)
    outbox = tmp_path / "private-outbox"

    first = _run_script(
        "-SourceFile",
        str(source),
        "-OutboxDirectory",
        str(outbox),
        "-ExpectedRchSerial",
        "99LABDGFE01",
        "-StableSeconds",
        "1",
        "-TimeoutSeconds",
        "10",
    )
    assert first.returncode == 0, first.stderr
    result = json.loads(first.stdout)
    assert result["status"] == "ok"
    assert result["artifact_count"] == 1
    artifact = outbox / result["artifacts"][0]
    assert artifact.read_bytes() == payload
    expected_hash = hashlib.sha256(payload).hexdigest()
    assert artifact.name == f"rch-dgfe-detail-{expected_hash}.xml"
    assert (outbox / f"{artifact.name}.sha256").read_text(encoding="utf-8") == (
        f"{expected_hash}  {artifact.name}\n"
    )
    manifest = json.loads((outbox / result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["rch_serial"] == "99LABDGFE01"
    assert manifest["artifacts"][0]["xml_signature_verification"] == (
        "NOT_PERFORMED_NO_TRUST_CHAIN"
    )

    second = _run_script(
        "-SourceFile",
        str(source),
        "-OutboxDirectory",
        str(outbox),
        "-ExpectedRchSerial",
        "99LABDGFE01",
        "-StableSeconds",
        "1",
        "-TimeoutSeconds",
        "10",
    )
    assert second.returncode == 0, second.stderr
    second_result = json.loads(second.stdout)
    assert second_result["artifacts"] == result["artifacts"]
    assert len(list(outbox.glob("rch-dgfe-*.xml"))) == 1
    assert len(list(outbox.glob("rch-dgfe-acquisition-*.json"))) == 2


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="requires Windows PowerShell",
)
def test_windows_dgfe_companion_watch_ignores_old_files_and_acquires_new_export(
    tmp_path: Path,
) -> None:
    export_directory = tmp_path / "xtools"
    export_directory.mkdir()
    (export_directory / "old.xml").write_bytes(
        _synthetic_detail_xml(serial="99OLDDEVICE1")
    )
    outbox = tmp_path / "private-outbox"
    executable_name = Path(POWERSHELL).name.lower()
    command = [POWERSHELL, "-NoProfile"]
    if executable_name == "powershell.exe":
        command.extend(["-ExecutionPolicy", "Bypass"])
    command.extend(
        [
            "-File",
            str(SCRIPT),
            "-ExportDirectory",
            str(export_directory),
            "-OutboxDirectory",
            str(outbox),
            "-ExpectedRchSerial",
            "99LABDGFE01",
            "-StableSeconds",
            "1",
            "-TimeoutSeconds",
            "10",
        ]
    )
    # The executable is resolved by shutil.which and arguments are test-created.
    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        time.sleep(1)
        (export_directory / "new.xml").write_bytes(_synthetic_detail_xml())
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["artifact_count"] == 1
    artifact = outbox / result["artifacts"][0]
    assert artifact.read_bytes() == _synthetic_detail_xml()
    assert not any(
        "99OLDDEVICE1" in path.read_text(errors="ignore")
        for path in outbox.glob("*.json")
    )


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="requires Windows PowerShell",
)
@pytest.mark.parametrize(
    ("payload", "expected_serial", "message"),
    [
        (
            (
                b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x "expanded">]>'
                + f'<MemoriaDettaglio xmlns="{DETAIL_NAMESPACE}" versione="3.0">'.encode()
                + b"<MatricolaRT>99LABDGFE01</MatricolaRT><X>&x;</X></MemoriaDettaglio>"
            ),
            "99LABDGFE01",
            "DTD",
        ),
        (_synthetic_detail_xml(), "99DIFFERENT1", "does not match"),
    ],
)
def test_windows_dgfe_companion_rejects_untrusted_or_wrong_device_xml(
    tmp_path: Path,
    payload: bytes,
    expected_serial: str,
    message: str,
) -> None:
    source = tmp_path / "untrusted.xml"
    source.write_bytes(payload)
    outbox = tmp_path / "private-outbox"

    completed = _run_script(
        "-SourceFile",
        str(source),
        "-OutboxDirectory",
        str(outbox),
        "-ExpectedRchSerial",
        expected_serial,
        "-StableSeconds",
        "1",
        "-TimeoutSeconds",
        "10",
    )

    assert completed.returncode != 0
    assert message.lower() in completed.stderr.lower()
    assert not list(outbox.glob("*.xml"))


@pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None,
    reason="requires Windows PowerShell",
)
def test_windows_dgfe_companion_refuses_unsigned_xtools_executable(
    tmp_path: Path,
) -> None:
    export_directory = tmp_path / "xtools"
    export_directory.mkdir()
    fake_xtools = tmp_path / "RchXTools.exe"
    fake_xtools.write_bytes(b"MZ synthetic unsigned executable")

    completed = _run_script(
        "-ExportDirectory",
        str(export_directory),
        "-OutboxDirectory",
        str(tmp_path / "private-outbox"),
        "-LaunchXTools",
        "-XToolsPath",
        str(fake_xtools),
        "-StableSeconds",
        "1",
        "-TimeoutSeconds",
        "2",
    )

    assert completed.returncode != 0
    assert "authenticode signature is not valid" in completed.stderr.lower()
