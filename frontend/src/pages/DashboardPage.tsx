import { DescriptionOutlined, DevicesOutlined, EuroOutlined, ReceiptLongOutlined, ShieldOutlined, WarningAmberOutlined } from '@mui/icons-material'
import { Box, Card, CardContent, Grid, LinearProgress, List, ListItem, ListItemText, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import type { Dashboard, Device } from '../types'

const euros = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })
const bytes = new Intl.NumberFormat('it-IT', { notation: 'compact', style: 'unit', unit: 'byte' })

export function DashboardPage() {
  const dashboard = useQuery({ queryKey: scopedQueryKey('dashboard'), queryFn: () => api<Dashboard>('/dashboard') })
  const devices = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices') })
  if (dashboard.isLoading) return <LoadingState />
  if (dashboard.error || !dashboard.data) return <ErrorState error={dashboard.error} />
  const data = dashboard.data
  return (
    <>
      <PageHeader title="Quadro antifrode" subtitle="Evidenze, anomalie e stato dei flussi di stampa in un’unica vista." />
      <Grid container spacing={2.2}>
        <Grid size={{ xs: 12, sm: 6, lg: 3 }}><StatCard label="Documenti acquisiti" value={data.documents} helper={`${data.pre_bills} preconti`} icon={<DescriptionOutlined />} /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 3 }}><StatCard label="Alert aperti" value={data.open_alerts} helper={`${data.critical_alerts} critici`} icon={<ShieldOutlined />} tone="#b42318" /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 3 }}><StatCard label="Differenze economiche" value={euros.format(Number(data.economic_difference))} helper="alert attivi" icon={<EuroOutlined />} tone="#b54708" /></Grid>
        <Grid size={{ xs: 12, sm: 6, lg: 3 }}><StatCard label="Spool locale" value={bytes.format(data.spool_bytes)} helper={`${data.parse_errors} errori parsing`} icon={<ReceiptLongOutlined />} tone="#2f6c7e" /></Grid>
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
