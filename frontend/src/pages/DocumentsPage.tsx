import {
  Alert,
  Box,
  Card,
  FormControl,
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
} from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import {
  ALL_EVIDENCE_FILTER,
  documentApiSearchParams,
  presentedDocuments,
} from '../documentPresentation'
import { mediumDateTime as date } from '../format'
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
              {type === 'DEVICE_RESPONSE' ? 'RISPOSTE TECNICHE RCH' : type.replaceAll('_', ' ')}
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
        <Box sx={{ overflowX: 'auto' }}>
          <Table>
            <TableHead><TableRow>
              <TableCell>Acquisito</TableCell><TableCell>Tipo</TableCell>
              <TableCell>Riferimenti</TableCell><TableCell>Dispositivo</TableCell>
              <TableCell align="right">Totale</TableCell><TableCell>Stato</TableCell>
              <TableCell align="right">Confidenza</TableCell>
            </TableRow></TableHead>
            <TableBody>{visibleDocuments.map((doc) => <TableRow
              key={doc.id}
              hover
              sx={{ cursor: 'pointer' }}
              onClick={() => navigate(documentDetailPath(doc.id))}
            >
              <TableCell>{date.format(new Date(doc.captured_at))}</TableCell>
              <TableCell><strong>{doc.subtype}</strong><br /><small>{doc.type}</small></TableCell>
              <TableCell>{doc.order_code ?? doc.external_code ?? '—'}<br />{doc.table_code ? `Tavolo ${doc.table_code}` : ''}</TableCell>
              <TableCell>{doc.device_id}</TableCell>
              <TableCell align="right">{doc.gross_total ? money.format(Number(doc.gross_total)) : '—'}</TableCell>
              <TableCell><StatusChip value={doc.complete ? 'COMPLETE' : 'INCOMPLETE'} /></TableCell>
              <TableCell align="right">{doc.confidence}%</TableCell>
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
