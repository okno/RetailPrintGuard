import { AddCircleOutline, ArrowBack, DeleteOutline, EuroOutlined, ReceiptLongOutlined } from '@mui/icons-material'
import { Box, Button, Card, CardContent, Divider, Grid, List, ListItem, ListItemIcon, ListItemText, Paper, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { TRANSACTION_DETAIL_PARAM } from '../routes'
import type { Transaction } from '../types'

const money = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

export function TransactionDetailPage() {
  const params = useParams()
  const transactionId = params[TRANSACTION_DETAIL_PARAM]
  const navigate = useNavigate()
  const query = useQuery({ queryKey: scopedQueryKey('transaction', transactionId), queryFn: () => api<Transaction>(`/transactions/${encodeURIComponent(transactionId ?? '')}`), enabled: Boolean(transactionId) })
  if (!transactionId) return <ErrorState error={new Error('Identificativo transazione mancante o URL non valida.')} />
  if (query.isLoading) return <LoadingState />
  if (query.error || !query.data) return <ErrorState error={query.error} />
  const item = query.data
  const diffEntries = Object.entries(item.diff)
  return <>
    <PageHeader title={`Transazione ${item.order_code ?? item.id.slice(0, 8)}`} subtitle={`Tavolo ${item.table_code ?? 'non disponibile'} · correlazione ${item.correlation_confidence}%`} actions={<Button startIcon={<ArrowBack />} onClick={() => navigate('/transazioni')}>Torna all’elenco</Button>} />
    <Grid container spacing={2.5}>
      <Grid size={{ xs: 12, lg: 7 }}><Card><CardContent><Typography variant="h2" sx={{ mb: 2 }}>Timeline unificata</Typography><List>{item.timeline.map((event, index) => <Box key={String(event.id ?? index)}><ListItem alignItems="flex-start"><ListItemIcon>{String(event.type).includes('REMOVED') ? <DeleteOutline color="error" /> : String(event.type).includes('PRICE') ? <EuroOutlined color="warning" /> : String(event.type).includes('DOCUMENT') ? <ReceiptLongOutlined color="primary" /> : <AddCircleOutline color="success" />}</ListItemIcon><ListItemText primary={String(event.label ?? event.type ?? 'Evento')} secondary={<><span>{String(event.occurred_at ?? '')}</span><br /><span>{String(event.description ?? '')}</span></>} /></ListItem>{index < item.timeline.length - 1 && <Divider variant="inset" component="li" />}</Box>)}</List></CardContent></Card></Grid>
      <Grid size={{ xs: 12, lg: 5 }}><Card><CardContent><Typography variant="h2" sx={{ mb: 2 }}>Confronto economico</Typography><Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 2 }}><Paper variant="outlined" sx={{ p: 2 }}><Typography color="text.secondary">Preconto</Typography><Typography variant="h4" fontWeight={750}>{money.format(Number(item.pre_bill_total ?? 0))}</Typography></Paper><Paper variant="outlined" sx={{ p: 2 }}><Typography color="text.secondary">Totale fiscale</Typography><Typography variant="h4" fontWeight={750}>{money.format(Number(item.fiscal_total ?? 0))}</Typography></Paper></Box><Box sx={{ mt: 2, p: 2, borderRadius: 2, bgcolor: Number(item.difference) > 0 ? 'error.50' : 'success.50' }}><Typography color="text.secondary">Differenza assoluta</Typography><Typography variant="h4" color={Number(item.difference) > 0 ? 'error.main' : 'success.main'} fontWeight={760}>{money.format(Number(item.difference ?? 0))}</Typography></Box></CardContent></Card>
      <Card sx={{ mt: 2 }}><CardContent><Typography variant="h2" sx={{ mb: 2 }}>Diff righe</Typography>{diffEntries.length ? diffEntries.map(([key, value]) => <Box key={key} sx={{ py: 1.2, borderBottom: '1px solid #edf1f4' }}><Typography fontWeight={680}>{key.replaceAll('_', ' ')}</Typography><Typography component="pre" variant="body2" sx={{ whiteSpace: 'pre-wrap', m: 0, color: 'text.secondary' }}>{JSON.stringify(value, null, 2)}</Typography></Box>) : <Typography color="text.secondary">Nessuna differenza strutturata disponibile.</Typography>}</CardContent></Card></Grid>
    </Grid>
  </>
}
