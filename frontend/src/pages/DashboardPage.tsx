import {
  AssignmentLateOutlined,
  DescriptionOutlined,
  DevicesOutlined,
  EuroOutlined,
  HistoryOutlined,
  PriceChangeOutlined,
  ShieldOutlined,
  WarningAmberOutlined,
} from '@mui/icons-material'
import { Box, Card, CardContent, Grid, LinearProgress, List, ListItem, ListItemText, Typography } from '@mui/material'
import { useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { StatCard } from '../components/StatCard'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { operationalReductionTransactionsPath } from '../dashboardDrilldown'
import { apiPeriodParams } from '../period'
import type { Dashboard, Device, Diagnostics } from '../types'

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
  const devices = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices') })
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
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Alert operativi attivi" value={operationalAlerts} helper={`${data.critical_alerts} critici; escluso lo storico chiuso`} icon={<ShieldOutlined />} tone={theme.palette.error.main} to={routeWithPeriod('/alert', period, { view: 'operational' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Episodi con riduzione" value={reductionEpisodes} helper="una vendita conta una sola volta" icon={<PriceChangeOutlined />} tone={theme.palette.warning.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Differenza economica unica" value={euros.format(Number(economicDifference))} helper="aggregata per episodio, non per alert" icon={<EuroOutlined />} tone={theme.palette.warning.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Incompleti da revisionare" value={incompleteJobs} helper="il RAW originale non viene eliminato" icon={<AssignmentLateOutlined />} tone={theme.palette.secondary.main} to={routeWithPeriod('/incompleti', period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Storico non operativo" value={archivedKnown ? archived : '—'} helper="falsi positivi e giustificati" icon={<HistoryOutlined />} tone={theme.palette.text.secondary} to={routeWithPeriod('/alert', period, { view: 'archive' })} /></Grid>
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
