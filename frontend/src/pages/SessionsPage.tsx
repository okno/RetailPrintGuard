import { DownloadOutlined } from '@mui/icons-material'
import {
  Alert,
  Box,
  Button,
  Card,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Typography,
} from '@mui/material'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { formatDateTime } from '../format'
import type { Page, ProxySession } from '../types'

const bytes = new Intl.NumberFormat('it-IT', { notation: 'compact', style: 'unit', unit: 'byte' })

function duration(item: ProxySession): string {
  const start = new Date(item.opened_at).getTime()
  const end = item.closed_at ? new Date(item.closed_at).getTime() : Date.now()
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '—'
  const seconds = Math.floor((end - start) / 1000)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  return hours ? `${hours}h ${minutes}m ${remainder}s` : `${minutes}m ${remainder}s`
}

export function SessionsPage() {
  const [params, setParams] = useSearchParams()
  const [downloadError, setDownloadError] = useState<unknown>()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const deviceId = params.get('device_id') ?? ''
  const user = session().user
  const canDownload = Boolean(user?.roles.some((role) => role === 'ADMIN' || role === 'AUDITOR'))
  const requestParams = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (deviceId) requestParams.set('device_id', deviceId)
  const query = useQuery({
    queryKey: scopedQueryKey('sessions', limit, offset, deviceId),
    queryFn: () => api<Page<ProxySession>>(`/sessions?${requestParams}`),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  })

  async function download(item: ProxySession, direction: 'request' | 'response') {
    setDownloadError(undefined)
    try {
      await downloadApi(
        `/sessions/${item.id}/raw?direction=${direction}`,
        `${item.device_id}_${item.id}_${direction}.raw`,
      )
    } catch (error) {
      setDownloadError(error)
    }
  }

  return <>
    <PageHeader title="Sessioni TCP" subtitle="Flussi applicativi ricostruiti per dispositivo, direzione e sessione proxy." />
    <Card sx={{ p: 2, mb: 2 }}>
      <TextField
        label="ID dispositivo"
        value={deviceId}
        onChange={(event) => setParams({ limit: String(limit), offset: '0', ...(event.target.value ? { device_id: event.target.value } : {}) })}
        size="small"
      />
    </Card>
    {downloadError && <Alert severity="error" sx={{ mb: 2 }}><ErrorState error={downloadError} /></Alert>}
    <Card>
      {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessuna sessione acquisita." /> : <>
        <Table>
          <TableHead><TableRow><TableCell>Apertura</TableCell><TableCell>Dispositivo</TableCell><TableCell>Sorgente → destinazione</TableCell><TableCell>Durata</TableCell><TableCell>Stato</TableCell><TableCell align="right">Request</TableCell><TableCell align="right">Response</TableCell><TableCell>Azioni</TableCell></TableRow></TableHead>
          <TableBody>{query.data.items.map((item) => <TableRow key={item.id} hover>
            <TableCell><Typography variant="body2">{formatDateTime(item.opened_at)}</Typography><Typography variant="caption" color="text.secondary">{item.id}</Typography></TableCell>
            <TableCell>{item.device_id}</TableCell>
            <TableCell>{item.source_endpoint}<br />{item.target_endpoint}</TableCell>
            <TableCell>{duration(item)}</TableCell>
            <TableCell><StatusChip value={item.complete ? 'COMPLETE' : (item.close_reason ?? 'INCOMPLETE')} /></TableCell>
            <TableCell align="right">{bytes.format(item.request_bytes)}</TableCell>
            <TableCell align="right">{bytes.format(item.response_bytes)}</TableCell>
            <TableCell>{canDownload ? <Box sx={{ display: 'flex', gap: 1 }}>
              <Button size="small" startIcon={<DownloadOutlined />} onClick={() => download(item, 'request')}>Request</Button>
              <Button size="small" startIcon={<DownloadOutlined />} onClick={() => download(item, 'response')}>Response</Button>
            </Box> : <Typography variant="caption" color="text.secondary">Download riservato agli auditor</Typography>}</TableCell>
          </TableRow>)}</TableBody>
        </Table>
        <TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, page) => setParams({ limit: String(limit), offset: String(page * limit), ...(deviceId ? { device_id: deviceId } : {}) })} onRowsPerPageChange={(event) => setParams({ limit: event.target.value, offset: '0', ...(deviceId ? { device_id: deviceId } : {}) })} labelRowsPerPage="Righe" />
      </>}
    </Card>
  </>
}
