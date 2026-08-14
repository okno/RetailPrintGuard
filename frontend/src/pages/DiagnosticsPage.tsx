import { BugReportOutlined, DataObjectOutlined, StorageOutlined } from '@mui/icons-material'
import { Alert, Box, Card, CardContent, Grid, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { formatDateTime } from '../format'
import type { Diagnostics } from '../types'

export function DiagnosticsPage() {
  const query = useQuery({
    queryKey: scopedQueryKey('system', 'diagnostics'),
    queryFn: () => api<Diagnostics>('/system/diagnostics'),
    refetchInterval: 30_000,
  })
  if (query.isLoading) return <LoadingState />
  if (query.error) return <ErrorState error={query.error} />
  const data = query.data
  if (!data) return <EmptyState label="Diagnostica non disponibile." />
  return <>
    <PageHeader title="Diagnostica" subtitle={`Stato tecnico aggiornato ${formatDateTime(data.generated_at)}. Usa l'ID di correlazione per ricercare i log.`} />
    {(data.database !== 'ok' || data.spool !== 'ok') && <Alert severity="warning" sx={{ mb: 2 }}>Uno o più componenti non risultano operativi. Verificare i servizi e lo spazio disco.</Alert>}
    <Grid container spacing={2} sx={{ mb: 2 }}>
      <Grid size={{ xs: 12, md: 4 }}><Card><CardContent><StorageOutlined color="primary" /><Typography variant="overline">Database / spool</Typography><Box sx={{ display: 'flex', gap: 1, mt: 1 }}><StatusChip value={data.database.toUpperCase()} /><StatusChip value={data.spool.toUpperCase()} /></Box></CardContent></Card></Grid>
      <Grid size={{ xs: 12, md: 4 }}><Card><CardContent><DataObjectOutlined color="primary" /><Typography variant="overline">Errori parser</Typography><Typography variant="h2">{data.parser_errors}</Typography></CardContent></Card></Grid>
      <Grid size={{ xs: 12, md: 4 }}><Card><CardContent><BugReportOutlined color="primary" /><Typography variant="overline">Job incompleti</Typography><Typography variant="h2">{data.incomplete_jobs}</Typography></CardContent></Card></Grid>
    </Grid>
    <Card>
      {!data.recent_events.length ? <EmptyState label="Nessun evento tecnico recente." /> : <Table>
        <TableHead><TableRow><TableCell>Data</TableCell><TableCell>Severità</TableCell><TableCell>Servizio / evento</TableCell><TableCell>Messaggio</TableCell><TableCell>ID correlazione</TableCell></TableRow></TableHead>
        <TableBody>{data.recent_events.map((event) => <TableRow key={event.id} hover>
          <TableCell>{formatDateTime(event.occurred_at)}</TableCell>
          <TableCell><StatusChip value={event.severity} /></TableCell>
          <TableCell><Typography fontWeight={700}>{event.service}</Typography><Typography variant="caption">{event.event_type}</Typography></TableCell>
          <TableCell>{event.message}{event.error && <Typography variant="caption" color="error" display="block">{event.error}</Typography>}</TableCell>
          <TableCell sx={{ fontFamily: 'monospace', fontSize: 12 }}>{event.correlation_id ?? '—'}</TableCell>
        </TableRow>)}</TableBody>
      </Table>}
    </Card>
  </>
}
