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
import { StatusChip } from '../components/StatusChip'
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
  documentTimestampEvidenceLabel,
  documentTypeLabel,
  presentedDocuments,
} from '../documentPresentation'
import { formatDocumentDateTime, mediumDateTime as date } from '../format'
import { documentDetailPath } from '../routes'
import type { DocumentRecord, Page } from '../types'

const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })
const documentTypes = [
  'ORDER',
  'ORDER_CHANGE',
  'KITCHEN_ORDER',
  'PRE_BILL',
  'MANAGEMENT_DOCUMENT',
  'COMMERCIAL_DOCUMENT',
  'CONFORMING_COPY',
  'CANCELLATION',
  'REFUND',
  'REPRINT',
  'PAYMENT',
  'DEVICE_RESPONSE',
  'UNKNOWN',
] as const

const columnLabels: Record<DocumentColumnId, string> = {
  document_time: 'Ora cassa',
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
  const selectedType = params.get('type') ?? ''
  const apiParams = documentApiSearchParams(params)
  if (!apiParams.has('limit')) apiParams.set('limit', String(limit))
  if (!apiParams.has('offset')) apiParams.set('offset', String(offset))
  const query = useQuery({
    queryKey: scopedQueryKey('documents', apiParams.toString()),
    queryFn: () => api<Page<DocumentRecord>>(`/documents?${apiParams.toString()}`),
  })
  const visibleDocuments = presentedDocuments(query.data?.items ?? [], selectedType)
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
    if (column === 'document_time') return doc.document_timestamp ? <>
      <strong>{formatDocumentDateTime(doc.document_timestamp, doc.document_timestamp_precision)}</strong>
      <br /><small>{documentTimestampEvidenceLabel(doc.document_timestamp_evidence)}</small>
    </> : '—'
    if (column === 'captured_at') return date.format(new Date(doc.captured_at))
    if (column === 'type') return <>
      <strong>{documentTypeLabel(doc.type)}</strong>
      <br /><small>{doc.subtype.replaceAll('_', ' ')}</small>
    </>
    if (column === 'references') return <>
      {(doc.external_document_code ?? doc.external_code)
        ? <strong>Codice documento {doc.external_document_code ?? doc.external_code}</strong>
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
    if (column === 'device') return <>
      <strong>{deviceLabel(doc.device_id)}</strong>
      {deviceLabel(doc.device_id) !== doc.device_id && <><br /><small>{doc.device_id}</small></>}
    </>
    if (column === 'total') {
      return doc.gross_total !== undefined && doc.gross_total !== null
        ? money.format(Number(doc.gross_total))
        : '—'
    }
    if (column === 'status') return <StatusChip value={doc.complete ? 'COMPLETE' : 'INCOMPLETE'} />
    return `${doc.confidence}%`
  }

  function filter(key: string, value: string) {
    const next = new URLSearchParams(params)
    value ? next.set(key, value) : next.delete(key)
    next.set('offset', '0')
    next.set('limit', String(limit))
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
          <InputLabel>Tipo</InputLabel>
          <Select label="Tipo" value={selectedType} onChange={(event) => filter('type', String(event.target.value))}>
            <MenuItem value="">Documenti operativi</MenuItem>
            <MenuItem value={ALL_EVIDENCE_FILTER}>Tutte le evidenze</MenuItem>
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
    </Card>
    {!selectedType && <Alert severity="info" sx={{ mb: 2 }}>
      Le risposte tecniche della stampante RCH sono conservate ma separate dalla vista
      documentale primaria. Seleziona “Risposte tecniche RCH” o “Tutte le evidenze” per
      consultarle.
    </Alert>}
    <Card>
      {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !visibleDocuments.length ? <EmptyState /> : <>
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
