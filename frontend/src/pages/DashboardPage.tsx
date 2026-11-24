import {
  AssignmentLateOutlined,
  AccessTimeOutlined,
  ArrowForwardOutlined,
  DescriptionOutlined,
  DevicesOutlined,
  EuroOutlined,
  HistoryOutlined,
  GroupsOutlined,
  LocalCafeOutlined,
  LocalPizzaOutlined,
  PercentOutlined,
  PriceChangeOutlined,
  RestaurantOutlined,
  ShieldOutlined,
  WarningAmberOutlined,
  UpdateOutlined,
} from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Chip, Grid, LinearProgress, List, ListItem, ListItemText, Paper, Stack, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import type { ReactNode } from 'react'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { StatCard } from '../components/StatCard'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { operationalReductionQuery, operationalReductionTransactionsPath } from '../dashboardDrilldown'
import { deviceLabel } from '../documentPresentation'
import { discountDrilldownPath, discountMoney } from '../discountPresentation'
import { shortDateTime } from '../format'
import { apiPeriodParams } from '../period'
import { transactionDetailPath } from '../routes'
import type { Dashboard, Device, Diagnostics, Page, Transaction } from '../types'

const euros = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })
const quantities = new Intl.NumberFormat('it-IT', { maximumFractionDigits: 4 })

export function DepartmentQuantityCard({
  label,
  total,
  dineIn,
  takeaway,
  undetermined,
  icon,
}: {
  label: string
  total?: string
  dineIn?: string
  takeaway?: string
  undetermined?: string
  icon: ReactNode
}) {
  const formatted = (value?: string) => quantities.format(Number(value ?? 0))
  return <Card sx={{ height: '100%' }}>
    <CardContent>
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
        <Box>
          <Typography variant="body2" color="text.secondary" fontWeight={650}>{label} · totale netto</Typography>
          <Typography variant="h4" sx={{ mt: 0.75, fontWeight: 760 }}>{formatted(total)}</Typography>
        </Box>
        <Box sx={{ color: 'primary.main' }}>{icon}</Box>
      </Stack>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 1, mt: 2 }}>
        <Box sx={{ bgcolor: 'action.hover', borderRadius: 2, p: 1.25 }}>
          <Typography variant="caption" color="text.secondary">In sede (Tavolo)</Typography>
          <Typography variant="h6">{formatted(dineIn)}</Typography>
        </Box>
        <Box sx={{ bgcolor: 'action.hover', borderRadius: 2, p: 1.25 }}>
          <Typography variant="caption" color="text.secondary">Asporto (Ordine)</Typography>
          <Typography variant="h6">{formatted(takeaway)}</Typography>
        </Box>
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.25 }}>
        Non determinato: {formatted(undetermined)} · quantità nette con rettifiche
      </Typography>
    </CardContent>
  </Card>
}

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
  const documentCount = data.complete_documents ?? data.documents
  const types = data.complete_document_type_counts
  const preBills = types ? types.PRE_BILL ?? 0 : data.pre_bills
  const orders = types ? (types.ORDER ?? 0) + (types.ORDER_CHANGE ?? 0) + (types.KITCHEN_ORDER ?? 0) : data.orders
  const operationalAlerts = data.operational_alerts ?? data.open_alerts
  const economicDifference = data.operational_economic_difference ?? data.economic_difference
  const reductionEpisodes = data.economic_reduction_episodes ?? (Number(economicDifference) > 0 ? 1 : 0)
  const incompleteJobs = data.incomplete_jobs ?? diagnostics.data?.incomplete_jobs ?? 0
  const archivedKnown = data.false_positive_alerts !== undefined || data.justified_alerts !== undefined
  const archived = (data.false_positive_alerts ?? 0) + (data.justified_alerts ?? 0)
  const coversHelper = data.observed_covers === undefined
    ? 'Conteggio non ancora disponibile'
    : [
      `${data.covers_counted_episodes ?? 0} episodi contati una volta`,
      data.covers_ambiguous_episodes ? `${data.covers_ambiguous_episodes} episodi ambigui esclusi` : null,
      data.covers_source_disagreements ? `${data.covers_source_disagreements} discrepanze POS/RCH da verificare` : null,
      data.covers_unlinked_documents ? `${data.covers_unlinked_documents} documenti non correlati esclusi (somma indicata ${data.covers_unlinked_observations ?? 0})` : null,
    ].filter(Boolean).join(' · ')
  return (
    <>
      <PageHeader
        title="Quadro antifrode"
        subtitle="Rischi operativi del periodo: gli alert giustificati o falsi positivi restano nello storico ma non aumentano il rischio."
        actions={<Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(3, minmax(150px, 1fr))' }, gap: 1 }}>
          <PeriodPicker params={params} onChange={setParams} defaultPreset="week" />
        </Box>}
      />
      {documentCount === 0 && (period.has('from') || period.has('to')) && <Alert severity="info" sx={{ mb: 2 }} action={<Button onClick={() => {
        const next = new URLSearchParams(params)
        next.set('period', 'all')
        next.delete('from')
        next.delete('to')
        next.delete('offset')
        setParams(next)
      }}>Mostra intero storico</Button>}>
        Il periodo selezionato non contiene documenti completi. I dati precedenti restano consultabili nello storico.
      </Alert>}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 2 }}>
        {data.partial_documents !== undefined && <Chip component={Link} clickable to={routeWithPeriod('/documenti', period, { document_scope: 'partial' })} variant="outlined" color="warning" label={`${data.partial_documents} parziali da verificare`} />}
        {data.total_evidence !== undefined && <Chip component={Link} clickable to={routeWithPeriod('/documenti', period, { document_scope: 'all' })} variant="outlined" label={`${data.total_evidence} evidenze totali`} />}
        {data.excluded_evidence !== undefined && <Typography variant="caption" color="text.secondary" sx={{ alignSelf: 'center' }}>{data.excluded_evidence} evidenze escluse dai PDF</Typography>}
      </Box>
      <Grid container spacing={2.2}>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Documenti completi" value={documentCount} helper={`${preBills} preconti completi nel periodo`} icon={<DescriptionOutlined />} to={routeWithPeriod('/documenti', period, { document_scope: 'complete' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Primo documento acquisito" value={data.first_document_captured_at ? shortDateTime.format(new Date(data.first_document_captured_at)) : 'Nessun documento'} helper="Data e ora di acquisizione · ora italiana" icon={<AccessTimeOutlined />} to={routeWithPeriod('/documenti', period, { document_scope: 'all' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Ultimo documento acquisito" value={data.last_document_captured_at ? shortDateTime.format(new Date(data.last_document_captured_at)) : 'Nessun documento'} helper="Data e ora di acquisizione · ora italiana" icon={<UpdateOutlined />} to={routeWithPeriod('/documenti', period, { document_scope: 'all' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Coperti osservati" value={data.observed_covers ?? '—'} helper={coversHelper} icon={<GroupsOutlined />} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Segnalazioni operative" value={operationalAlerts} helper={`${data.critical_alerts} critiche; consultazione di supporto`} icon={<ShieldOutlined />} tone={theme.palette.text.secondary} to={routeWithPeriod('/alert', period, { view: 'operational' })} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Episodi con possibile ammanco" value={reductionEpisodes} helper="una vendita conta una sola volta" icon={<PriceChangeOutlined />} tone={theme.palette.error.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Ammanco potenziale totale" value={euros.format(Number(economicDifference))} helper="somma unica degli episodi del periodo" icon={<EuroOutlined />} tone={theme.palette.error.main} to={operationalReductionTransactionsPath(period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Incompleti da revisionare" value={incompleteJobs} helper="il RAW originale non viene eliminato" icon={<AssignmentLateOutlined />} tone={theme.palette.secondary.main} to={routeWithPeriod('/incompleti', period)} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><StatCard label="Storico non operativo" value={archivedKnown ? archived : '—'} helper="falsi positivi e giustificati" icon={<HistoryOutlined />} tone={theme.palette.text.secondary} to={routeWithPeriod('/alert', period, { view: 'archive' })} /></Grid>
        <Grid size={{ xs: 12 }} sx={{ mt: 1 }}>
          <Box>
            <Typography variant="h2">Quantità nelle comande per reparto</Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              Quantità nette delle righe prodotto osservate nelle comande, suddivise per il riferimento stampato Tavolo o Ordine. Le rettifiche possono rendere negativo un sotto-totale. Le comande senza riferimento univoco restano in “Non determinato”. Non rappresentano una riconciliazione delle vendite fiscali.
            </Typography>
          </Box>
        </Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><DepartmentQuantityCard label="Prodotti BAR" total={data.sold_product_quantity_bar} dineIn={data.sold_product_quantity_bar_dine_in} takeaway={data.sold_product_quantity_bar_takeaway} undetermined={data.sold_product_quantity_bar_undetermined} icon={<LocalCafeOutlined />} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><DepartmentQuantityCard label="Prodotti CUCINA" total={data.sold_product_quantity_kitchen} dineIn={data.sold_product_quantity_kitchen_dine_in} takeaway={data.sold_product_quantity_kitchen_takeaway} undetermined={data.sold_product_quantity_kitchen_undetermined} icon={<RestaurantOutlined />} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 4 }}><DepartmentQuantityCard label="Prodotti PIZZERIA" total={data.sold_product_quantity_pizzeria} dineIn={data.sold_product_quantity_pizzeria_dine_in} takeaway={data.sold_product_quantity_pizzeria_takeaway} undetermined={data.sold_product_quantity_pizzeria_undetermined} icon={<LocalPizzaOutlined />} /></Grid>
        <Grid size={{ xs: 12 }} sx={{ mt: 1 }}>
          <Typography variant="h2">Sconti osservati nei documenti</Typography>
          <Typography color="text.secondary" sx={{ mt: .5 }}>Importi presenti nelle stampe selezionate. La verifica aritmetica non prova l’autorizzazione dello sconto e non chiude gli alert antifrode. Copie diverse possono riferirsi alla stessa vendita.</Typography>
        </Grid>
        <Grid size={{ xs: 12, md: 4 }}><StatCard label="Dipendenti 30% esplicito · osservati" value={discountMoney(data.observed_employee_discount_total)} helper={data.observed_employee_discount_count === undefined ? 'Conteggio documenti non disponibile' : `${data.observed_employee_discount_count} documenti · apri le evidenze`} icon={<PercentOutlined />} to={discountDrilldownPath(period, 'employee_30')} /></Grid>
        <Grid size={{ xs: 12, md: 4 }}><StatCard label="Dipendenti 30% · documento riconciliato" value={discountMoney(data.arithmetic_reconciled_employee_discount_total)} helper={data.arithmetic_reconciled_employee_discount_count === undefined ? 'Conteggio documenti non disponibile' : `${data.arithmetic_reconciled_employee_discount_count} documenti · autorizzazione da verificare`} icon={<PercentOutlined />} to={discountDrilldownPath(period, 'employee_30', 'verified')} /></Grid>
        <Grid size={{ xs: 12, md: 4 }}><StatCard label="30% generico · osservati" value={discountMoney(data.observed_generic_30_discount_total)} helper={data.observed_generic_30_discount_count === undefined ? 'Conteggio documenti non disponibile' : `${data.observed_generic_30_discount_count} documenti · nessuna attribuzione ai dipendenti`} icon={<PercentOutlined />} to={discountDrilldownPath(period, 'generic_30')} /></Grid>
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
              ['Comande e ordini completi', orders, documentCount],
              ['Documenti gestionali completi', types ? types.MANAGEMENT_DOCUMENT ?? 0 : data.management_documents, documentCount],
              ['Documenti commerciali completi', types ? types.COMMERCIAL_DOCUMENT ?? 0 : data.commercial_documents, documentCount],
              ['Report di fine turno completi', types ? types.SHIFT_END_REPORT ?? 0 : data.shift_end_reports ?? 0, documentCount],
              ['Fatture complete', types ? types.INVOICE ?? 0 : data.invoices ?? 0, documentCount],
            ].map(([label, value, total]) => <Box key={String(label)} sx={{ mb: 2 }}><Box sx={{ display: 'flex', justifyContent: 'space-between', mb: .75 }}><Typography>{label}</Typography><Typography fontWeight={700}>{value}</Typography></Box><LinearProgress variant="determinate" value={Number(total) ? Math.min(100, Number(value) / Number(total) * 100) : 0} sx={{ height: 8, borderRadius: 4 }} /></Box>)}
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 3, color: data.parse_errors ? 'warning.main' : 'success.main' }}><WarningAmberOutlined fontSize="small" /><Typography variant="body2">{data.parse_errors ? `${data.parse_errors} job richiedono verifica tecnica` : 'Nessun errore di parsing aperto'}</Typography></Box>
          </CardContent></Card>
        </Grid>
        <Grid size={{ xs: 12, lg: 5 }}>
          <Card><CardContent><Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 1 }}><Typography variant="h2">Dispositivi</Typography><DevicesOutlined color="action" /></Box>
            {devices.isLoading ? <LoadingState label="Stato dispositivi…" /> : devices.error ? <ErrorState error={devices.error} /> : <List disablePadding>{devices.data?.map((device) => <ListItem key={device.id} disableGutters secondaryAction={<StatusChip value={device.online ? 'ONLINE' : 'OFFLINE'} />}><ListItemText primary={deviceLabel(device.id, devices.data)} secondary={`${device.type.toUpperCase()} · ${device.pending_jobs} job in attesa`} /></ListItem>)}</List>}
          </CardContent></Card>
        </Grid>
      </Grid>
    </>
  )
}
