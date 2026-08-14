import { DnsOutlined, PrintOutlined, StorageOutlined } from '@mui/icons-material'
import { Alert, Box, Card, CardContent, Grid, LinearProgress, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import type { Device } from '../types'
import { formatDateTime } from '../format'

const bytes = new Intl.NumberFormat('it-IT', { notation: 'compact', style: 'unit', unit: 'byte' })

export function DevicesPage() {
  const query = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices'), refetchInterval: 30_000 })
  return (
    <>
      <PageHeader title="Stato dispositivi" subtitle="Connettività, code locali e ultime attività delle tre POS e della RCH." />
      {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : (
        <Grid container spacing={2.2}>
          {query.data?.map((device) => (
            <Grid key={device.id} size={{ xs: 12, md: 6 }}>
              <Card sx={{ height: '100%' }}>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, mb: 2 }}>
                    <Box sx={{ p: 1, borderRadius: 2, bgcolor: 'primary.main', color: 'white' }}>
                      {device.type === 'rch' ? <DnsOutlined /> : <PrintOutlined />}
                    </Box>
                    <Box sx={{ flex: 1 }}>
                      <Typography variant="h2">{device.name}</Typography>
                      <Typography variant="body2" color="text.secondary">{device.id} · {device.type.toUpperCase()}</Typography>
                      {(device.department || device.role) && <Typography variant="body2" color="text.secondary">
                        {[device.department, device.role].filter(Boolean).join(' · ')}
                      </Typography>}
                    </Box>
                    <StatusChip value={device.online ? 'ONLINE' : 'OFFLINE'} />
                  </Box>
                  {device.last_error && <Alert severity="warning" sx={{ mb: 2 }}>{device.last_error}</Alert>}
                  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5 }}>
                    <Box><Typography variant="caption" color="text.secondary">Listener virtuale</Typography><Typography>{device.listen_endpoint}</Typography></Box>
                    <Box><Typography variant="caption" color="text.secondary">Stampante fisica</Typography><Typography>{device.target_endpoint}</Typography></Box>
                    <Box><Typography variant="caption" color="text.secondary">Ultima connessione</Typography><Typography>{formatDateTime(device.last_connection_at)}</Typography></Box>
                    <Box><Typography variant="caption" color="text.secondary">Ultima risposta</Typography><Typography>{formatDateTime(device.last_response_at)}</Typography></Box>
                    {device.mac_address && <Box><Typography variant="caption" color="text.secondary">MAC dispositivo</Typography><Typography sx={{ fontFamily: 'monospace' }}>{device.mac_address}</Typography></Box>}
                  </Box>
                  <Box sx={{ mt: 2.5, display: 'flex', justifyContent: 'space-between' }}>
                    <Typography variant="body2"><StorageOutlined fontSize="inherit" /> {bytes.format(device.spool_bytes)} nello spool</Typography>
                    <Typography variant="body2" fontWeight={700}>{device.pending_jobs} job pendenti</Typography>
                  </Box>
                  <LinearProgress variant="determinate" value={Math.min(100, device.pending_jobs * 5)} color={device.pending_jobs > 10 ? 'warning' : 'primary'} sx={{ mt: 1, height: 6, borderRadius: 4 }} />
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>Servizio {device.service_version ?? 'versione non disponibile'} · configurazione sensibile omessa</Typography>
                </CardContent>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}
    </>
  )
}
