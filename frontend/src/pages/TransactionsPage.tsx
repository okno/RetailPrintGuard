import { Box, Card, FormControl, InputLabel, MenuItem, Select, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, TextField } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { StatusChip } from '../components/StatusChip'
import type { Page, Transaction } from '../types'
import { shortDateTime as date } from '../format'

const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

export function TransactionsPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const query = useQuery({
    queryKey: scopedQueryKey('transactions', params.toString()),
    queryFn: () => api<Page<Transaction>>(`/transactions?${params.toString() || `limit=${limit}&offset=${offset}`}`),
  })
  function update(name: string, value: string) { const next = new URLSearchParams(params); value ? next.set(name, value) : next.delete(name); next.set('offset', '0'); next.set('limit', String(limit)); setParams(next) }
  return <>
    <PageHeader title="Transazioni" subtitle="Correlazione unificata di comande, modifiche, preconti, documenti fiscali e pagamenti." />
    <Card sx={{ p: 2, mb: 2 }}><Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, 1fr)' }, gap: 2 }}>
      <TextField label="Tavolo" size="small" value={params.get('table_code') ?? ''} onChange={(e) => update('table_code', e.target.value)} />
      <TextField label="Codice ordine" size="small" value={params.get('order_code') ?? ''} onChange={(e) => update('order_code', e.target.value)} />
      <TextField label="Operatore" size="small" value={params.get('operator_code') ?? ''} onChange={(e) => update('operator_code', e.target.value)} />
      <FormControl size="small"><InputLabel>Differenza</InputLabel><Select label="Differenza" value={params.get('minimum_difference') ?? ''} onChange={(e) => update('minimum_difference', String(e.target.value))}><MenuItem value="">Tutte</MenuItem><MenuItem value="0.01">Con differenza</MenuItem><MenuItem value="20">Almeno 20 €</MenuItem><MenuItem value="50">Almeno 50 €</MenuItem></Select></FormControl>
    </Box></Card>
    <Card>{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessuna transazione corrisponde ai filtri." /> : <>
      <Box sx={{ overflowX: 'auto' }}><Table><TableHead><TableRow><TableCell>Data/ora</TableCell><TableCell>Tavolo / ordine</TableCell><TableCell>Operatore</TableCell><TableCell align="right">Preconto</TableCell><TableCell align="right">Fiscale</TableCell><TableCell align="right">Differenza</TableCell><TableCell>Stato</TableCell><TableCell align="right">Alert</TableCell><TableCell align="right">Confidenza</TableCell></TableRow></TableHead><TableBody>{query.data.items.map((item) => <TableRow key={item.id} hover tabIndex={0} onClick={() => navigate(`/transazioni/${item.id}`)} sx={{ cursor: 'pointer' }}><TableCell>{date.format(new Date(item.occurred_at))}</TableCell><TableCell><strong>{item.table_code ?? '—'}</strong><br />{item.order_code ?? 'senza riferimento'}</TableCell><TableCell>{item.operator_code ?? '—'}</TableCell><TableCell align="right">{item.pre_bill_total ? money.format(Number(item.pre_bill_total)) : '—'}</TableCell><TableCell align="right">{item.fiscal_total ? money.format(Number(item.fiscal_total)) : '—'}</TableCell><TableCell align="right" sx={{ color: Number(item.difference) > 0 ? 'error.main' : 'text.primary', fontWeight: 700 }}>{item.difference ? money.format(Number(item.difference)) : '—'}</TableCell><TableCell><StatusChip value={item.status} /></TableCell><TableCell align="right">{item.alert_count}</TableCell><TableCell align="right">{item.correlation_confidence}%</TableCell></TableRow>)}</TableBody></Table></Box>
      <TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, page) => { const next = new URLSearchParams(params); next.set('offset', String(page * limit)); next.set('limit', String(limit)); setParams(next) }} onRowsPerPageChange={(event) => { const next = new URLSearchParams(params); next.set('limit', event.target.value); next.set('offset', '0'); setParams(next) }} labelRowsPerPage="Righe" />
    </>}</Card>
  </>
}
