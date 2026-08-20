import {
  AssignmentLateOutlined,
  ArrowForwardOutlined,
  DescriptionOutlined,
  DevicesOutlined,
  EuroOutlined,
  HistoryOutlined,
  PriceChangeOutlined,
  ShieldOutlined,
  WarningAmberOutlined,
} from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Grid, LinearProgress, List, ListItem, ListItemText, Paper, Stack, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { StatCard } from '../components/StatCard'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { operationalReductionQuery, operationalReductionTransactionsPath } from '../dashboardDrilldown'
import { shortDateTime } from '../format'
import { apiPeriodParams } from '../period'
import { transactionDetailPath } from '../routes'
import type { Dashboard, Device, Diagnostics, Page, Transaction } from '../types'

const euros = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

function routeWithPeriod(path: string, period: URLSearchParams, extras?: Record<string, string>) {
  const query = new URLSearchParams()
  for (const key of ['from', 'to']) {
    const value = period.get(key)
    if (value) query.set(key, value)
  }
  for (const [key, value] of Object.entries(extras ?? {})) query.set(key, value)
  return query.size ? `${path}?${query.toString()}` : path
}

export function DashboardPage() {
  const theme = useTheme()
  const [params, setParams] = useSearchParams()
  // A seven-day default keeps the latest completed service visible after
  // midnight while the operator can still switch explicitly to Oggi.
  const period = apiPeriodParams(params, 'week')
  const dashboard = useQuery({
    queryKey: scopedQueryKey('dashboard', period.toString()),
    queryFn: () => api<Dashboard>(`/dashboard?${period.toString()}`),
  })
  const episodeParams = operationalReductionQuery(period, { limit: 8, offset: 0 })
  const episodes = useQuery({
    queryKey: scopedQueryKey('dashboard', 'economic-episodes', episodeParams.toString()),
    queryFn: () => api<Page<Transaction>>(`/transactions?${episodeParams.toString()}`),
  })
  const devices = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices'), refetchInterval: 10_000 })
  const diagnostics = useQuery({
    queryKey: scopedQueryKey('system', 'diagnostics'),
    queryFn: () => api<Diagnostics>('/system/diagnostics'),
    staleTime: 30_000,
  })
  if (dashboard.isLoading) return <LoadingState />
  if (dashboard.error || !dashboard.data) return <ErrorState error={dashboard.error} />
  const data = dashboard.data
  const operationalAlerts = data.operational_alerts ?? data.open_alerts
  const economicDifference = data.operational_economic_difference ?? data.economic_difference
  const reductionEpisodes = data.economic_reduction_episodes ?? (Number(economicDifference) > 0 ? 1 : 0)
  const incompleteJobs = data.incomplete_jobs ?? diagnostics.data?.incomplete_jobs ?? 0
  const archivedKnown = data.false_positive_alerts !== undefined || data.justified_alerts !== undefined
  const archived = (data.false_positive_alerts ?? 0) + (data.justified_alerts ?? 0)
  return (
    <>
      <PageHeader
        title="Quadro antifrode"
        subtitle="Rischi operativi del periodo: gli alert giustificati o falsi positivi restano nello storico ma non aumentano il rischio."
        actions={<Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, minmax(150px, 1fr))' }, gap: 1 }}>
          <PeriodPicker params={params} onChange={setParams} defaultPreset="week" />
        </Box>}
      />
      <Grid container spacing={2.2}>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Documenti acquisiti" value={data.documents} helper={`${data.pre_bills} preconti nel periodo`} icon={<DescriptionOutlined />} to={routeWithPeriod('/documenti', period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Segnalazioni operative" value={operationalAlerts} helper={`${data.critical_alerts} critiche; consultazione di supporto`} icon={<ShieldOutlined />} tone={theme.palette.text.secondary} to={routeWithPeriod('/alert', period, { view: 'operational' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Episodi con possibile ammanco" value={reductionEpisodes} helper="una vendita conta una sola volta" icon={<PriceChangeOutlined />} tone={theme.palette.error.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Ammanco potenziale totale" value={euros.format(Number(economicDifference))} helper="somma unica degli episodi del periodo" icon={<EuroOutlined />} tone={theme.palette.error.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Incompleti da revisionare" value={incompleteJobs} helper="il RAW originale non viene eliminato" icon={<AssignmentLateOutlined />} tone={theme.palette.secondary.main} to={routeWithPeriod('/incompleti', period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Storico non operativo" value={archivedKnown ? archived : '—'} helper="falsi positivi e giustificati" icon={<HistoryOutlined />} tone={theme.palette.text.secondary} to={routeWithPeriod('/alert', period, { view: 'archive' })} /></Grid>
        <Grid size={{ xs: 12 }}>
          <Card
            sx={{
              border: '1px solid',
              borderColor: episodes.data?.total ? 'error.main' : 'divider',
              boxShadow: episodes.data?.total
                ? `0 14px 36px ${alpha(theme.palette.error.main, theme.palette.mode === 'dark' ? 0.22 : 0.14)}`
                : undefined,
            }}
          >
            <CardContent>
              <Stack
                direction={{ xs: 'column', md: 'row' }}
                alignItems={{ xs: 'flex-start', md: 'center' }}
                justifyContent="space-between"
                gap={1.5}
                sx={{ mb: 2 }}
              >
                <Box>
                  <Typography variant="h2">Eventi economici prioritari</Typography>
                  <Typography color="text.secondary" sx={{ mt: 0.5 }}>
                    Riduzioni tra preconto e documento finale ancora da verificare. Ogni vendita è conteggiata una sola volta.
                  </Typography>
                </Box>
                <Button
                  component={Link}
                  to={operationalReductionTransactionsPath(period)}
                  endIcon={<ArrowForwardOutlined />}
                  color={episodes.data?.total ? 'error' : 'primary'}
                  variant={episodes.data?.total ? 'contained' : 'outlined'}
                >
                  Vedi tutti gli eventi{episodes.data ? ` (${episodes.data.total})` : ''}
                </Button>
              </Stack>
              {episodes.isLoading ? <LoadingState label="Analisi episodi economici…" />
                : episodes.error ? <Alert severity="warning">Impossibile caricare il dettaglio degli episodi economici.</Alert>
                  : !episodes.data?.items.length ? <Alert severity="success">Nessuna riduzione economica operativa nel periodo selezionato.</Alert>
                    : <Stack spacing={1.25}>
                      {episodes.data.items.map((episode) => <Paper
                        key={episode.id}
                        variant="outlined"
                        sx={{
                          p: 2,
                          borderColor: 'error.main',
                          bgcolor: alpha(theme.palette.error.main, theme.palette.mode === 'dark' ? 0.13 : 0.045),
                        }}
                      >
                        <Stack direction={{ xs: 'column', lg: 'row' }} alignItems={{ xs: 'stretch', lg: 'center' }} gap={2}>
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography color="error.main" fontWeight={800}>
                              Possibile ammanco {euros.format(Number(episode.difference ?? 0))}
                            </Typography>
                            <Typography fontWeight={700} sx={{ mt: 0.4 }}>
                              Tavolo {episode.table_code ?? 'non identificato'}
                              {episode.order_code ? ` · riferimento ${episode.order_code}` : ''}
                            </Typography>
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.4 }}>
                              {shortDateTime.format(new Date(episode.occurred_at))} · preconto {euros.format(Number(episode.pre_bill_total ?? 0))} → documento finale {euros.format(Number(episode.fiscal_total ?? 0))} · {episode.document_count} documenti correlati
                            </Typography>
                          </Box>
                          <Box sx={{ textAlign: { xs: 'left', lg: 'right' } }}>
                            <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 0.7 }}>
                              Confidenza correlazione {episode.correlation_confidence}%
                            </Typography>
                            <Button
                              component={Link}
                              to={transactionDetailPath(episode.id)}
                              color="error"
                              variant="outlined"
                              endIcon={<ArrowForwardOutlined />}
                            >
                              Apri evento e documenti
                            </Button>
                          </Box>
                        </Stack>
                      </Paper>)}
                    </Stack>}
            </CardContent>
          </Card>
        </Grid>
        <Grid size={{ xs: 12, lg: 7 }}>
          <Card><CardContent><Typography variant="h2" sx={{ mb: 2 }}>Copertura documentale</Typography>
            {[
              ['Comande e ordini', data.orders, data.documents],
              ['Documenti gestionali', data.management_documents, data.documents],
              ['Documenti commerciali', data.commercial_documents, data.documents],
            ].map(([label, value, total]) => <Box key={String(label)} sx={{ mb: 2 }}><Box sx={{ display: 'flex', justifyContent: 'space-between', mb: .75 }}><Typography>{label}</Typography><Typography fontWeight={700}>{value}</Typography></Box><LinearProgress variant="determinate" value={Number(total) ? Math.min(100, Number(value) / Number(total) * 100) : 0} sx={{ height: 8, borderRadius: 4 }} /></Box>)}
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 3, color: data.parse_errors ? 'warning.main' : 'success.main' }}><WarningAmberOutlined fontSize="small" /><Typography variant="body2">{data.parse_errors ? `${data.parse_errors} job richiedono verifica tecnica` : 'Nessun errore di parsing aperto'}</Typography></Box>
          </CardContent></Card>
        </Grid>
        <Grid size={{ xs: 12, lg: 5 }}>
          <Card><CardContent><Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 1 }}><Typography variant="h2">Dispositivi</Typography><DevicesOutlined color="action" /></Box>
            {devices.isLoading ? <LoadingState label="Stato dispositivi…" /> : devices.error ? <ErrorState error={devices.error} /> : <List disablePadding>{devices.data?.map((device) => <ListItem key={device.id} disableGutters secondaryAction={<StatusChip value={device.online ? 'ONLINE' : 'OFFLINE'} />}><ListItemText primary={device.name} secondary={`${device.type.toUpperCase()} · ${device.pending_jobs} job in attesa`} /></ListItem>)}</List>}
          </CardContent></Card>
        </Grid>
      </Grid>
    </>
  )
}
