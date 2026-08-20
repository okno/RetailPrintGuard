import { ArrowBack, CodeOutlined, DownloadOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Chip, Grid, Paper, Tab, Table, TableBody, TableCell, TableHead, TableRow, Tabs, Typography } from '@mui/material'
import { alpha } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, downloadApi, rawDocument, scopedQueryKey, session } from '../api/client'
import { ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { StatusChip } from '../components/StatusChip'
import { formatDateTime, formatDocumentDateTime } from '../format'
import { DOCUMENT_DETAIL_PARAM } from '../routes'
import {
  NOT_OBSERVED_IN_FLOW,
  rchClockOffsetLabel,
  rchSerialEvidenceLabel,
  rchTimestampEvidenceLabel,
} from '../rchIdentityPresentation'
import {
  confidencePercent,
  deviceLabel,
  documentTypeLabel,
} from '../documentPresentation'
import type { DocumentRecord } from '../types'

const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

function progressiveObservationLabel(value: string | undefined) {
  if (value === 'FULL_CODE_OBSERVED_IN_CAPTURE') return 'Progressivo completo osservato nel flusso'
  if (value === 'SUFFIX_ONLY_OBSERVED_IN_CAPTURE') return 'Solo suffisso osservato; non è un codice completo'
  if (value === 'NOT_OBSERVED_IN_CAPTURE') return 'Progressivo proprio generato dalla RCH, non presente nel flusso osservato'
  return 'Non applicabile'
}

function ProvenanceValue({ label, value, provenance }: { label: string, value: ReactNode, provenance?: string }) {
  return <Box sx={{ mt: 2 }}>
    <Typography variant="caption" color="text.secondary">{label}</Typography>
    <Typography sx={{ wordBreak: 'break-word' }}>{value}</Typography>
    {provenance && <Typography variant="caption" color="text.secondary">Provenienza: {provenance}</Typography>}
  </Box>
}

function LinePrice({ line }: { line: DocumentRecord['lines'][number] }) {
  const value = line.unit_price ?? line.derived_unit_price
  if (value === undefined && line.derived_price_source === 'CONFLICTING_SOURCES') {
    const candidates = [...new Set(
      (line.price_attributions ?? [])
        .filter((item) => item.observed_unit_price !== undefined)
        .map((item) => `${item.source_kind}: ${money.format(Number(item.observed_unit_price))}`),
    )]
    return <Chip
      size="small"
      variant="outlined"
      color="warning"
      label="Prezzi in conflitto"
      title={candidates.join(' · ') || 'Le fonti correlate riportano prezzi differenti.'}
    />
  }
  if (value === undefined) return <>—</>
  const derived = line.unit_price === undefined
  const confidences = (line.price_attributions ?? [])
    .filter((item) => item.source_kind === line.derived_price_source && item.observed_unit_price === line.derived_unit_price)
    .map((item) => Number(item.confidence))
    .filter(Number.isFinite)
  const confidence = confidencePercent(confidences.length ? Math.max(...confidences) : undefined)
  return <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: .5 }}>
    <span>{money.format(Number(value))}</span>
    {derived && <Chip
      size="small"
      variant="outlined"
      color="info"
      label={`Derivato ${line.derived_price_source ?? ''}${confidence === undefined ? '' : ` · ${confidence}%`}`.trim()}
      title="Valore attribuito da un documento correlato; il prezzo originale della comanda non è stato modificato."
    />}
  </Box>
}

function hex(data: Uint8Array) {
  return Array.from(data.slice(0, 65_536))
    .map((byte, index) => `${index % 16 === 0 ? `\n${index.toString(16).padStart(8, '0')}  ` : ''}${byte.toString(16).padStart(2, '0')} `)
    .join('')
    .trim()
}

export function DocumentDetailPage() {
  const params = useParams()
  const documentId = params[DOCUMENT_DETAIL_PARAM]
  const navigate = useNavigate()
  const [tab, setTab] = useState(0)
  const [raw, setRaw] = useState<string>()
  const [artifactError, setArtifactError] = useState<unknown>()
  const query = useQuery({
    queryKey: scopedQueryKey('document', documentId),
    queryFn: () => api<DocumentRecord>(`/documents/${encodeURIComponent(documentId ?? '')}`),
    enabled: Boolean(documentId),
  })
  if (!documentId) {
    return <ErrorState error={new Error('Identificativo documento mancante o URL non valida.')} />
  }
  if (query.isLoading) return <LoadingState />
  if (query.error || !query.data) return <ErrorState error={query.error} />
  const doc = query.data
  const roles = session().user?.roles ?? []
  const canDownloadEvidence = roles.includes('ADMIN') || roles.includes('AUDITOR')
  const hasRchIdentity = doc.device_id.toLowerCase().startsWith('rch')
    || doc.parser_name.toLowerCase().includes('rch')
    || doc.application_timestamp != null
    || doc.rch_footer_timestamp != null
    || doc.rch_serial_number != null

  async function runArtifact(action: () => Promise<unknown>) {
    setArtifactError(undefined)
    try {
      await action()
    } catch (error) {
      setArtifactError(error)
    }
  }

  async function loadRaw() {
    await runArtifact(async () => {
      setRaw(hex(await rawDocument(doc.id)))
      setTab(3)
    })
  }

  const actions = <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
    <Button startIcon={<ArrowBack />} onClick={() => navigate('/documenti')}>Indietro</Button>
    {canDownloadEvidence && <>
      <Button variant="outlined" startIcon={<CodeOutlined />} onClick={loadRaw}>Anteprima RAW</Button>
      <Button startIcon={<DownloadOutlined />} onClick={() => runArtifact(() => downloadApi(`/documents/${doc.id}/txt`, `${doc.id}.txt`))}>TXT</Button>
      <Button startIcon={<DownloadOutlined />} onClick={() => runArtifact(() => downloadApi(`/documents/${doc.id}/json`, `${doc.id}.json`))}>JSON</Button>
      <Button startIcon={<DownloadOutlined />} onClick={() => runArtifact(() => downloadApi(`/documents/${doc.id}/pdf`, `${doc.id}.pdf`))}>PDF</Button>
      <Button startIcon={<DownloadOutlined />} onClick={() => runArtifact(() => downloadApi(`/documents/${doc.id}/raw?direction=request`, `${doc.id}-request.raw`))}>RAW richiesta</Button>
      <Button startIcon={<DownloadOutlined />} onClick={() => runArtifact(() => downloadApi(`/documents/${doc.id}/raw?direction=response`, `${doc.id}-response.raw`))}>RAW risposta</Button>
    </>}
  </Box>

  return <>
    <PageHeader title={documentTypeLabel(doc.type)} subtitle={`${doc.subtype.replaceAll('_', ' ')} · ${deviceLabel(doc.device_id)} (${doc.device_id}) · parser ${doc.parser_name} ${doc.parser_version}`} actions={actions} />
    {artifactError && <Box sx={{ mb: 2 }}><ErrorState error={artifactError} /></Box>}
    {!canDownloadEvidence && <Alert severity="info" sx={{ mb: 2 }}>Il download delle evidenze RAW e derivate richiede il ruolo AUDITOR o ADMIN.</Alert>}
    <Grid container spacing={2.5}>
      <Grid size={{ xs: 12, lg: 8 }}>
        <Card>
          <Tabs value={tab} onChange={(_, value) => setTab(value)} aria-label="Viste documento">
            <Tab label="Scontrino" />
            <Tab label="Righe strutturate" />
            <Tab label="Testo parser" />
            <Tab label="RAW tecnico" disabled={!canDownloadEvidence} />
          </Tabs>
          <CardContent>
            {tab === 0 && <Paper variant="outlined" sx={(theme) => ({ mx: 'auto', maxWidth: 520, p: 3, bgcolor: theme.appChrome.receiptPaper, color: theme.appChrome.receiptInk, fontFamily: 'ui-monospace,Consolas,monospace', whiteSpace: 'pre-wrap', lineHeight: 1.55 })}>{doc.receipt_text || doc.normalized_text || 'Nessun testo documento disponibile.'}</Paper>}
            {tab === 1 && <Box sx={{ overflowX: 'auto' }}><Table size="small"><TableHead><TableRow><TableCell>#</TableCell><TableCell>Portata</TableCell><TableCell>Descrizione</TableCell><TableCell align="right">Q.tà</TableCell><TableCell align="right">Prezzo</TableCell><TableCell align="right">Totale</TableCell><TableCell>Stato</TableCell></TableRow></TableHead><TableBody>{doc.lines.map((line) => <TableRow key={line.id ?? line.sequence} sx={(theme) => ({ textDecoration: line.removed ? 'line-through' : 'none', bgcolor: line.removed ? alpha(theme.palette.error.main, theme.palette.mode === 'dark' ? 0.2 : 0.08) : 'transparent' })}><TableCell>{line.sequence}</TableCell><TableCell>{line.course_code ?? '—'}</TableCell><TableCell>{line.description ?? line.raw_text ?? '—'}</TableCell><TableCell align="right">{line.quantity ?? '—'}</TableCell><TableCell align="right"><LinePrice line={line} /></TableCell><TableCell align="right">{line.line_total ? money.format(Number(line.line_total)) : '—'}</TableCell><TableCell>{line.removed ? 'Rimosso' : line.cancelled ? 'Annullato' : line.state ?? 'Attivo'}</TableCell></TableRow>)}</TableBody></Table></Box>}
            {tab === 2 && <Paper component="pre" variant="outlined" sx={{ p: 2, maxHeight: 520, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>{doc.normalized_text || 'Nessun testo normalizzato.'}</Paper>}
            {tab === 3 && <Box><Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Anteprima esadecimale limitata a 64 KiB; il download completo è separato e auditato.</Typography>{raw ? <Paper component="pre" variant="outlined" sx={{ p: 2, maxHeight: 520, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>{raw}</Paper> : <Button startIcon={<DownloadOutlined />} onClick={loadRaw}>Richiedi anteprima originale</Button>}</Box>}
          </CardContent>
        </Card>
      </Grid>
      <Grid size={{ xs: 12, lg: 4 }}>
        <Card><CardContent>
          <Typography variant="h2" sx={{ mb: 2 }}>Provenienza</Typography>
          <StatusChip value={doc.complete ? 'COMPLETE' : 'INCOMPLETE'} />
          <ProvenanceValue
            label="Ora documento"
            value={doc.document_timestamp ? formatDocumentDateTime(doc.document_timestamp, doc.document_timestamp_precision) : NOT_OBSERVED_IN_FLOW}
            provenance={doc.document_timestamp ? rchTimestampEvidenceLabel(doc.document_timestamp_evidence) : NOT_OBSERVED_IN_FLOW}
          />
          {hasRchIdentity && <ProvenanceValue
            label="Ora applicativa RCH"
            value={doc.application_timestamp ? formatDocumentDateTime(doc.application_timestamp, doc.application_timestamp_precision ?? undefined) : NOT_OBSERVED_IN_FLOW}
            provenance={doc.application_timestamp ? rchTimestampEvidenceLabel(doc.application_timestamp_evidence) : NOT_OBSERVED_IN_FLOW}
          />}
          <ProvenanceValue
            label="Acquisizione server"
            value={formatDateTime(doc.captured_at)}
            provenance="Timestamp registrato dal server; non è un orario stampato dalla RCH"
          />
          {hasRchIdentity && <ProvenanceValue
            label="Ora footer RCH"
            value={doc.rch_footer_timestamp ? formatDocumentDateTime(doc.rch_footer_timestamp, doc.rch_footer_timestamp_precision ?? undefined) : NOT_OBSERVED_IN_FLOW}
            provenance={doc.rch_footer_timestamp ? rchTimestampEvidenceLabel(doc.rch_footer_timestamp_evidence) : NOT_OBSERVED_IN_FLOW}
          />}
          {hasRchIdentity && <ProvenanceValue label="Scarto orologio (footer − applicativa)" value={rchClockOffsetLabel(doc.rch_clock_offset_seconds)} />}
          {hasRchIdentity && <ProvenanceValue
            label="Seriale RCH"
            value={doc.rch_serial_number ?? NOT_OBSERVED_IN_FLOW}
            provenance={doc.rch_serial_number ? rchSerialEvidenceLabel(doc.rch_serial_number_evidence) : NOT_OBSERVED_IN_FLOW}
          />}
          {[
            ['Progressivo documento', doc.external_document_code ?? doc.external_code ?? (doc.resolved_external_document_code ? `${doc.resolved_external_document_code} (da riferimento gestionale correlato)` : undefined) ?? (doc.progressive_observation_status === 'NOT_OBSERVED_IN_CAPTURE' ? 'Non osservato nel flusso catturato' : undefined)],
            ['Suffisso progressivo RCH', doc.external_document_code_suffix],
            ['Osservabilità progressivo', progressiveObservationLabel(doc.progressive_observation_status)],
            ['Provenienza risoluzione', doc.resolved_external_document_code_provenance === 'CORRELATED_MANAGEMENT_REFERENCE' ? 'Riferimento commerciale in documento gestionale correlato' : undefined],
            ['Riferimento commerciale', doc.commercial_reference_code],
            ['Codice ordine', doc.order_code],
            ['Tavolo', doc.table_code],
            ['Coperti', doc.covers],
            ['Operatore', doc.operator_code],
            ['Hash SHA-256', doc.sha256],
            ['Confidenza', `${doc.confidence}%`],
          ].map(([label, value]) => <ProvenanceValue key={label} label={String(label)} value={value ?? '—'} />)}
        </CardContent></Card>
        {doc.warnings.length > 0 && <Alert severity="warning" sx={{ mt: 2 }}>{doc.warnings.join(' · ')}</Alert>}
      </Grid>
    </Grid>
  </>
}
