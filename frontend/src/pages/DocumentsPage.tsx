import {
  ChevronLeft,
  ChevronRight,
  DragIndicatorOutlined,
  RestartAltOutlined,
} from '@mui/icons-material'
import {
  Alert,
  Box,
  Button,
  Card,
  Chip,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import {
  DEFAULT_DOCUMENT_COLUMNS,
  DOCUMENT_COLUMN_STORAGE_KEY,
  isDocumentColumnId,
  moveDocumentColumn,
  parseDocumentColumnOrder,
  type DocumentColumnId,
} from '../documentColumns'
import {
  ALL_EVIDENCE_FILTER,
  deviceLabel,
  documentApiSearchParams,
  documentTypeLabel,
  isEpsonDailyReport,
  selectedDocumentScope,
} from '../documentPresentation'
import { DOCUMENT_SCOPE_LABELS, documentPdfLabel, documentReasonLabel, documentsMatchScope } from '../documentEligibility'
import { formatDocumentDateTime, mediumDateTime as date } from '../format'
import {
  NOT_OBSERVED_IN_FLOW,
  rchClockOffsetLabel,
  rchSerialEvidenceLabel,
  rchTimestampEvidenceLabel,
} from '../rchIdentityPresentation'
import {
  receiptHeaderEvidenceLabel,
  receiptHeaderSummary,
} from '../receiptHeaderPresentation'
import { documentDetailPath } from '../routes'
import type { Device, DocumentRecord, DocumentScope, Page } from '../types'

const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })
const documentTypes = [
  'ORDER',
  'ORDER_CHANGE',
  'KITCHEN_ORDER',
  'PRE_BILL',
  'MANAGEMENT_DOCUMENT',
  'COMMERCIAL_DOCUMENT',
  'SHIFT_END_REPORT',
  'INVOICE',
  'CONFORMING_COPY',
  'CANCELLATION',
  'REFUND',
  'REPRINT',
  'PAYMENT',
  'DEVICE_RESPONSE',
  'UNKNOWN',
] as const

const columnLabels: Record<DocumentColumnId, string> = {
  document_time: 'Orari documento',
  captured_at: 'Acquisito',
  type: 'Tipo',
  references: 'Riferimenti',
  device: 'Reparto / dispositivo',
  total: 'Totale',
  status: 'Stato',
  confidence: 'Confidenza',
}

function initialColumnOrder(): DocumentColumnId[] {
  if (typeof window === 'undefined') return [...DEFAULT_DOCUMENT_COLUMNS]
  try {
    return parseDocumentColumnOrder(window.localStorage.getItem(DOCUMENT_COLUMN_STORAGE_KEY))
  } catch {
    return [...DEFAULT_DOCUMENT_COLUMNS]
  }
}

export function DocumentsPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const selectedType = params.get('type') === ALL_EVIDENCE_FILTER ? '' : params.get('type') ?? ''
  const documentScope = selectedDocumentScope(params)
  const apiParams = documentApiSearchParams(params)
  if (!apiParams.has('limit')) apiParams.set('limit', String(limit))
  if (!apiParams.has('offset')) apiParams.set('offset', String(offset))
  const query = useQuery({
    queryKey: scopedQueryKey('documents', apiParams.toString()),
    queryFn: () => api<Page<DocumentRecord>>(`/documents?${apiParams.toString()}`),
  })
  const devices = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices'), staleTime: 30_000 })
  const visibleDocuments = query.data?.items ?? []
  const scopeMatches = documentsMatchScope(visibleDocuments, documentScope)
  const [columnOrder, setColumnOrder] = useState<DocumentColumnId[]>(initialColumnOrder)

  function saveColumnOrder(next: DocumentColumnId[]) {
    setColumnOrder(next)
    try {
      window.localStorage.setItem(DOCUMENT_COLUMN_STORAGE_KEY, JSON.stringify(next))
    } catch {
      // Browser privacy policies may make persistent preferences unavailable.
    }
  }

  function nudgeColumn(column: DocumentColumnId, direction: -1 | 1) {
    const sourceIndex = columnOrder.indexOf(column)
    const targetIndex = sourceIndex + direction
    if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= columnOrder.length) return
    const next = [...columnOrder]
    const source = next[sourceIndex]
    const target = next[targetIndex]
    if (!source || !target) return
    next[targetIndex] = source
    next[sourceIndex] = target
    saveColumnOrder(next)
  }

  function renderColumn(column: DocumentColumnId, doc: DocumentRecord): ReactNode {
    if (column === 'document_time') {
      const isRch = doc.device_id.toLowerCase().startsWith('rch')
        || doc.parser_name.toLowerCase().includes('rch')
        || doc.application_timestamp != null
        || doc.rch_footer_timestamp != null
      if (!isRch) return doc.document_timestamp ? <>
        <strong>{formatDocumentDateTime(doc.document_timestamp, doc.document_timestamp_precision)}</strong>
        <br /><small>{rchTimestampEvidenceLabel(doc.document_timestamp_evidence)}</small>
      </> : 'Data documento non osservata'
      return <>
        {doc.application_timestamp ? <>
          <strong>Applicativa RCH {formatDocumentDateTime(doc.application_timestamp, doc.application_timestamp_precision ?? undefined)}</strong>
          <br /><small>{rchTimestampEvidenceLabel(doc.application_timestamp_evidence)}</small>
        </> : doc.document_timestamp ? <>
          <strong>Ora documento {formatDocumentDateTime(doc.document_timestamp, doc.document_timestamp_precision)}</strong>
          <br /><small>{rchTimestampEvidenceLabel(doc.document_timestamp_evidence)}</small>
        </> : <strong>Applicativa RCH: {NOT_OBSERVED_IN_FLOW}</strong>}
        <br />
        {doc.rch_footer_timestamp
          ? <>Footer RCH {formatDocumentDateTime(doc.rch_footer_timestamp, doc.rch_footer_timestamp_precision ?? undefined)}<br /><small>{rchTimestampEvidenceLabel(doc.rch_footer_timestamp_evidence)}</small></>
          : <small>Footer RCH: {NOT_OBSERVED_IN_FLOW}</small>}
        {doc.rch_clock_offset_seconds != null && <><br /><small>{rchClockOffsetLabel(doc.rch_clock_offset_seconds)}</small></>}
      </>
    }
    if (column === 'captured_at') return date.format(new Date(doc.captured_at))
    if (column === 'type') return <>
      <strong>{documentTypeLabel(doc.type)}</strong>
      <br /><small>{doc.subtype.replaceAll('_', ' ')}</small>
    </>
    if (column === 'references') return <>
      {(doc.external_document_code ?? doc.external_code)
        ? <strong>{isEpsonDailyReport(doc) ? 'Progressivo Z' : 'Codice documento'} {doc.external_document_code ?? doc.external_code}</strong>
        : doc.resolved_external_document_code
          ? <strong>Codice scontrino {doc.resolved_external_document_code} (da riferimento correlato)</strong>
          : doc.progressive_observation_status === 'NOT_OBSERVED_IN_CAPTURE'
            ? <strong>Progressivo proprio non osservato nel flusso</strong>
            : '—'}
      {doc.external_document_code_suffix && <><br />Suffisso scontrino RCH {doc.external_document_code_suffix} (non completo)</>}
      {doc.commercial_reference_code && <><br />Riferimento scontrino {doc.commercial_reference_code}</>}
      {doc.order_code && <><br />Ordine {doc.order_code}</>}
      {doc.table_code && <><br />Tavolo {doc.table_code}</>}
    </>
    if (column === 'device') {
      const isRch = doc.device_id.toLowerCase().startsWith('rch')
        || doc.parser_name.toLowerCase().includes('rch')
        || doc.receipt_header != null
      return <>
        <strong>{deviceLabel(doc.device_id, devices.data)}</strong>
        {deviceLabel(doc.device_id, devices.data) !== doc.device_id && <><br /><small>{doc.device_id}</small></>}
        {isRch && <>
          <br />Seriale RCH: {doc.rch_serial_number ?? NOT_OBSERVED_IN_FLOW}
          {doc.rch_serial_number && <><br /><small>{rchSerialEvidenceLabel(doc.rch_serial_number_evidence)}</small></>}
          <br /><small>Intestazione: {receiptHeaderSummary(doc.receipt_header)}</small>
          {doc.receipt_header && receiptHeaderEvidenceLabel(doc.receipt_header.evidence) && <><br /><small>{receiptHeaderEvidenceLabel(doc.receipt_header.evidence)}</small></>}
        </>}
      </>
    }
    if (column === 'total') {
      return doc.gross_total !== undefined && doc.gross_total !== null
        ? <>{isEpsonDailyReport(doc) && <small>Totale giornaliero<br /></small>}{money.format(Number(doc.gross_total))}</>
        : '—'
    }
    if (column === 'status') return <Box>
      <Chip size="small" variant="outlined" label={documentPdfLabel(doc)} color={doc.pdf_category === 'partial' ? 'warning' : doc.pdf_category === 'complete' ? 'success' : 'default'} title={documentReasonLabel(doc.pdf_reason_code)} />
      <Typography variant="caption" display="block" sx={{ mt: .5 }}>{doc.complete ? 'Acquisizione completa' : 'Acquisizione incompleta'}</Typography>
    </Box>
    return `${doc.confidence}%`
  }

  function filter(key: string, value: string) {
    const next = new URLSearchParams(params)
    value ? next.set(key, value) : next.delete(key)
    next.set('offset', '0')
    next.set('limit', String(limit))
    setParams(next)
  }

  function setDocumentScope(value: DocumentScope) {
    const next = new URLSearchParams(params)
    next.set('document_scope', value)
    next.delete('include_technical')
    if (next.get('type') === ALL_EVIDENCE_FILTER || (value !== 'all' && next.get('type') === 'DEVICE_RESPONSE')) next.delete('type')
    next.set('offset', '0')
    setParams(next)
  }

  return <>
    <PageHeader
      title="Documenti"
      subtitle="Archivio normalizzato con accesso controllato all’evidenza originale."
    />
    <Card sx={{ p: 2, mb: 2 }}>
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 2 }}>
        <FormControl size="small">
          <InputLabel id="document-content-label">Contenuto</InputLabel>
          <Select labelId="document-content-label" label="Contenuto" value={documentScope} onChange={(event) => setDocumentScope(event.target.value as DocumentScope)}>
            {Object.entries(DOCUMENT_SCOPE_LABELS).map(([value, label]) => <MenuItem key={value} value={value}>{label}</MenuItem>)}
          </Select>
        </FormControl>
        <FormControl size="small">
          <InputLabel id="document-type-label" shrink>Tipo</InputLabel>
          <Select labelId="document-type-label" label="Tipo" displayEmpty value={selectedType} onChange={(event) => filter('type', String(event.target.value))}>
            <MenuItem value="">Tutti i tipi</MenuItem>
            {documentTypes.map((type) => <MenuItem key={type} value={type}>
              {documentTypeLabel(type)}
            </MenuItem>)}
          </Select>
        </FormControl>
        <TextField
          size="small"
          label="Dispositivo"
          value={params.get('device_id') ?? ''}
          onChange={(event) => filter('device_id', event.target.value)}
        />
        <TextField
          size="small"
          label="Codice ordine"
          value={params.get('order_code') ?? ''}
          onChange={(event) => filter('order_code', event.target.value)}
        />
        <TextField
          size="small"
          label="Progressivo documento"
          value={params.get('external_document_code') ?? ''}
          onChange={(event) => filter('external_document_code', event.target.value)}
        />
        <TextField
          size="small"
          label="Suffisso progressivo RCH"
          value={params.get('external_document_code_suffix') ?? ''}
          onChange={(event) => filter('external_document_code_suffix', event.target.value)}
        />
        <TextField
          size="small"
          label="Riferimento commerciale"
          value={params.get('commercial_reference_code') ?? ''}
          onChange={(event) => filter('commercial_reference_code', event.target.value)}
        />
        <PeriodPicker params={params} onChange={setParams} />
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ mt: 1.5, display: 'block' }}>
        Il periodo filtra la data di acquisizione del server, in ora italiana. Gli orari osservati nel documento sono indicati separatamente.
      </Typography>
    </Card>
    <Alert severity={documentScope === 'partial' ? 'warning' : 'info'} sx={{ mb: 2 }}>
      {documentScope === 'complete' ? 'La vista principale include documenti e comande completi. I parziali da verificare sono consultabili separatamente; tutte le evidenze originali restano conservate.'
        : documentScope === 'partial' ? 'Questi documenti contengono dati parziali e richiedono verifica. I relativi PDF vengono raccolti in un archivio separato dai documenti completi.'
          : 'Questa vista include anche tracce tecniche e contenuti esclusi dai PDF. Le evidenze RAW restano consultabili secondo il ruolo di accesso.'}
    </Alert>
    <Card>
      {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !scopeMatches ? <ErrorState error={new Error('La classificazione del server non corrisponde al filtro selezionato. Aggiornare la pagina prima di consultare questo archivio.')} /> : !visibleDocuments.length ? <EmptyState label={`Nessun risultato: ${DOCUMENT_SCOPE_LABELS[documentScope].toLowerCase()}.`} /> : <>
        <Box sx={{ px: 2, pt: 1.5, display: 'flex', gap: 1, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <Typography variant="caption" color="text.secondary">
            Trascina le intestazioni o usa le frecce per cambiare l’ordine delle colonne.
          </Typography>
          <Button
            size="small"
            startIcon={<RestartAltOutlined />}
            disabled={columnOrder.every((column, index) => column === DEFAULT_DOCUMENT_COLUMNS[index])}
            onClick={() => saveColumnOrder([...DEFAULT_DOCUMENT_COLUMNS])}
          >
            Ripristina colonne
          </Button>
        </Box>
        <Box sx={{ overflowX: 'auto' }}>
          <Table>
            <TableHead><TableRow>
              {columnOrder.map((column, index) => <TableCell
                key={column}
                draggable
                align={column === 'total' || column === 'confidence' ? 'right' : 'left'}
                onDragStart={(event) => {
                  event.dataTransfer.effectAllowed = 'move'
                  event.dataTransfer.setData('text/plain', column)
                }}
                onDragOver={(event) => {
                  event.preventDefault()
                  event.dataTransfer.dropEffect = 'move'
                }}
                onDrop={(event) => {
                  event.preventDefault()
                  const source = event.dataTransfer.getData('text/plain')
                  if (isDocumentColumnId(source)) {
                    saveColumnOrder(moveDocumentColumn(columnOrder, source, column))
                  }
                }}
                sx={{ whiteSpace: 'nowrap' }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: column === 'total' || column === 'confidence' ? 'flex-end' : 'flex-start' }}>
                  <DragIndicatorOutlined fontSize="small" color="disabled" sx={{ mr: 0.5 }} />
                  <span>{columnLabels[column]}</span>
                  <Tooltip title="Sposta a sinistra">
                    <span><IconButton size="small" aria-label={`Sposta ${columnLabels[column]} a sinistra`} disabled={index === 0} onClick={() => nudgeColumn(column, -1)}><ChevronLeft fontSize="small" /></IconButton></span>
                  </Tooltip>
                  <Tooltip title="Sposta a destra">
                    <span><IconButton size="small" aria-label={`Sposta ${columnLabels[column]} a destra`} disabled={index === columnOrder.length - 1} onClick={() => nudgeColumn(column, 1)}><ChevronRight fontSize="small" /></IconButton></span>
                  </Tooltip>
                </Box>
              </TableCell>)}
            </TableRow></TableHead>
            <TableBody>{visibleDocuments.map((doc) => <TableRow
              key={doc.id}
              hover
              sx={{ cursor: 'pointer' }}
              onClick={() => navigate(documentDetailPath(doc.id))}
            >
              {columnOrder.map((column) => <TableCell
                key={column}
                align={column === 'total' || column === 'confidence' ? 'right' : 'left'}
              >{renderColumn(column, doc)}</TableCell>)}
            </TableRow>)}</TableBody>
          </Table>
        </Box>
        <TablePagination
          component="div"
          count={query.data?.total ?? 0}
          page={Math.floor(offset / limit)}
          rowsPerPage={limit}
          rowsPerPageOptions={[25, 50, 100]}
          onPageChange={(_, page) => {
            const next = new URLSearchParams(params)
            next.set('offset', String(page * limit))
            next.set('limit', String(limit))
            setParams(next)
          }}
          onRowsPerPageChange={(event) => {
            const next = new URLSearchParams(params)
            next.set('limit', event.target.value)
            next.set('offset', '0')
            setParams(next)
          }}
          labelRowsPerPage="Righe"
        />
      </>}
    </Card>
  </>
}
