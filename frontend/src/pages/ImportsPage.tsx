import { Card, Table, TableBody, TableCell, TableHead, TablePagination, TableRow } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import type { ImportBatch, Page } from '../types'

export function ImportsPage() {
  const [params, setParams] = useSearchParams()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const query = useQuery({ queryKey: ['imports', limit, offset], queryFn: () => api<Page<ImportBatch>>(`/imports?limit=${limit}&offset=${offset}`) })
  return <>
    <PageHeader title="Importazioni" subtitle="Batch idempotenti da spool live e archivi storici, con errori e duplicati tracciati." />
    <Card>{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessun batch di importazione." /> : <>
      <Table><TableHead><TableRow><TableCell>Avvio</TableCell><TableCell>Sorgente</TableCell><TableCell>Percorso</TableCell><TableCell>Stato</TableCell><TableCell align="right">Scoperti</TableCell><TableCell align="right">Importati</TableCell><TableCell align="right">Duplicati</TableCell><TableCell align="right">Errori</TableCell></TableRow></TableHead><TableBody>{query.data.items.map((batch) => <TableRow key={batch.id}><TableCell>{new Date(batch.started_at).toLocaleString('it-IT')}</TableCell><TableCell>{batch.source_type}</TableCell><TableCell sx={{ maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis' }}>{batch.source_root}</TableCell><TableCell><StatusChip value={batch.status} /></TableCell><TableCell align="right">{batch.discovered}</TableCell><TableCell align="right">{batch.imported}</TableCell><TableCell align="right">{batch.duplicates}</TableCell><TableCell align="right">{batch.failed}</TableCell></TableRow>)}</TableBody></Table>
      <TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, page) => setParams({ limit: String(limit), offset: String(page * limit) })} onRowsPerPageChange={(event) => setParams({ limit: event.target.value, offset: '0' })} labelRowsPerPage="Righe" />
    </>}</Card>
  </>
}
