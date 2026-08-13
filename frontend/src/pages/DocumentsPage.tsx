import { Card, FormControl, InputLabel, MenuItem, Select, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, TextField, Box } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { StatusChip } from '../components/StatusChip'
import type { DocumentRecord, Page } from '../types'

const date = new Intl.DateTimeFormat('it-IT', { dateStyle: 'short', timeStyle: 'medium' })
const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

export function DocumentsPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const query = useQuery({ queryKey: ['documents', params.toString()], queryFn: () => api<Page<DocumentRecord>>(`/documents?${params.toString() || `limit=${limit}&offset=${offset}`}`) })
  function filter(key: string, value: string) { const next = new URLSearchParams(params); value ? next.set(key, value) : next.delete(key); next.set('offset', '0'); next.set('limit', String(limit)); setParams(next) }
  return <><PageHeader title="Documenti" subtitle="Archivio normalizzato con accesso controllato all’evidenza originale." />
    <Card sx={{ p: 2, mb: 2 }}><Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, 1fr)' }, gap: 2 }}><FormControl size="small"><InputLabel>Tipo</InputLabel><Select label="Tipo" value={params.get('type') ?? ''} onChange={(e) => filter('type', String(e.target.value))}><MenuItem value="">Tutti</MenuItem>{['ORDER','KITCHEN_ORDER','PRE_BILL','MANAGEMENT_DOCUMENT','COMMERCIAL_DOCUMENT','CONFORMING_COPY','CANCELLATION','REPRINT','UNKNOWN'].map((type) => <MenuItem key={type} value={type}>{type.replaceAll('_',' ')}</MenuItem>)}</Select></FormControl><TextField size="small" label="Dispositivo" value={params.get('device_id') ?? ''} onChange={(e) => filter('device_id', e.target.value)} /><TextField size="small" label="Codice ordine" value={params.get('order_code') ?? ''} onChange={(e) => filter('order_code', e.target.value)} /></Box></Card>
    <Card>{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState /> : <><Box sx={{ overflowX: 'auto' }}><Table><TableHead><TableRow><TableCell>Acquisito</TableCell><TableCell>Tipo</TableCell><TableCell>Riferimenti</TableCell><TableCell>Dispositivo</TableCell><TableCell align="right">Totale</TableCell><TableCell>Stato</TableCell><TableCell align="right">Confidenza</TableCell></TableRow></TableHead><TableBody>{query.data.items.map((doc) => <TableRow key={doc.id} hover sx={{ cursor: 'pointer' }} onClick={() => navigate(`/documenti/${doc.id}`)}><TableCell>{date.format(new Date(doc.captured_at))}</TableCell><TableCell><strong>{doc.subtype}</strong><br /><small>{doc.type}</small></TableCell><TableCell>{doc.order_code ?? doc.external_code ?? '—'}<br />{doc.table_code ? `Tavolo ${doc.table_code}` : ''}</TableCell><TableCell>{doc.device_id}</TableCell><TableCell align="right">{doc.gross_total ? money.format(Number(doc.gross_total)) : '—'}</TableCell><TableCell><StatusChip value={doc.complete ? 'COMPLETE' : 'INCOMPLETE'} /></TableCell><TableCell align="right">{doc.confidence}%</TableCell></TableRow>)}</TableBody></Table></Box><TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25,50,100]} onPageChange={(_, page) => { const next=new URLSearchParams(params); next.set('offset',String(page*limit)); next.set('limit',String(limit)); setParams(next)}} onRowsPerPageChange={(event) => {const next=new URLSearchParams(params);next.set('limit',event.target.value);next.set('offset','0');setParams(next)}} labelRowsPerPage="Righe" /></>}</Card>
  </>
}
