import { DownloadOutlined } from '@mui/icons-material'
import { Box, Button, Card, Dialog, DialogActions, DialogContent, DialogTitle, FormControl, InputLabel, MenuItem, Select, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, TextField, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { alertApiSearchParams, DEFAULT_ALERT_VIEW } from '../alertFilters'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { StatusChip } from '../components/StatusChip'
import { formatDateTime } from '../format'
import type { AlertRecord, Page } from '../types'

export function AlertsPage() {
  const [params, setParams] = useSearchParams()
  const [selectedId, setSelectedId] = useState<string>()
  const [note, setNote] = useState('')
  const [exportError, setExportError] = useState<unknown>()
  const queryClient = useQueryClient()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const view = params.get('view') ?? DEFAULT_ALERT_VIEW
  const requestParams = alertApiSearchParams(params, limit, offset)
  const roles = session().user?.roles ?? []
  const canReview = roles.some((role) => ['ADMIN', 'AUDITOR', 'OPERATOR'].includes(role))
  const canExport = roles.some((role) => ['ADMIN', 'AUDITOR'].includes(role))
  const query = useQuery({
    queryKey: scopedQueryKey('alerts', requestParams.toString()),
    queryFn: () => api<Page<AlertRecord>>(`/alerts?${requestParams.toString()}`),
  })
  const detail = useQuery({
    queryKey: scopedQueryKey('alert', selectedId),
    queryFn: () => api<AlertRecord>(`/alerts/${selectedId}`),
    enabled: Boolean(selectedId),
  })
  const update = useMutation({
    mutationFn: (body: object) => api<AlertRecord>(`/alerts/${selectedId}`, { method: 'PATCH', body: JSON.stringify(body) }),
    onSuccess: (value) => {
      queryClient.setQueryData(scopedQueryKey('alert', selectedId), value)
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
  })

  function filter(key: string, value: string) {
    const next = new URLSearchParams(params)
    value ? next.set(key, value) : next.delete(key)
    next.set('offset', '0')
    next.set('limit', String(limit))
    setParams(next)
  }

  async function exportAlerts() {
    setExportError(undefined)
    const exported = new URLSearchParams(requestParams)
    exported.delete('limit')
    exported.delete('offset')
    try {
      await downloadApi(`/alerts/export.csv?${exported.toString()}`, 'alert-antifrode.csv')
    } catch (error) {
      setExportError(error)
    }
  }

  const selected = detail.data
  return <>
    <PageHeader title="Alert antifrode" subtitle="Workbench investigativo con evidenze, presa in carico e storico completo." actions={canExport ? <Button onClick={exportAlerts} startIcon={<DownloadOutlined />} variant="outlined">Esporta CSV</Button> : undefined} />
    {exportError && <Box sx={{ mb: 2 }}><ErrorState error={exportError} /></Box>}
    <Card sx={{ p: 2, mb: 2 }}><Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(6,1fr)' }, gap: 2 }}>
      <FormControl size="small"><InputLabel>Vista</InputLabel><Select label="Vista" value={view} onChange={(event) => filter('view', String(event.target.value))}><MenuItem value="operational">Operativi</MenuItem><MenuItem value="archive">Archivio</MenuItem><MenuItem value="all">Tutti</MenuItem></Select></FormControl>
      <FormControl size="small"><InputLabel>Severità</InputLabel><Select label="Severità" value={params.get('severity') ?? ''} onChange={(event) => filter('severity', String(event.target.value))}><MenuItem value="">Tutte</MenuItem>{['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((value) => <MenuItem key={value} value={value}>{value}</MenuItem>)}</Select></FormControl>
      <FormControl size="small"><InputLabel>Stato</InputLabel><Select label="Stato" value={params.get('status') ?? ''} onChange={(event) => filter('status', String(event.target.value))}><MenuItem value="">Tutti</MenuItem>{['OPEN', 'UNDER_REVIEW', 'CONFIRMED', 'FALSE_POSITIVE', 'JUSTIFIED', 'CLOSED'].map((value) => <MenuItem key={value} value={value}>{value.replaceAll('_', ' ')}</MenuItem>)}</Select></FormControl>
      <TextField size="small" label="Regola" value={params.get('rule') ?? ''} onChange={(event) => filter('rule', event.target.value)} />
      <TextField size="small" label="Dispositivo" value={params.get('device_id') ?? ''} onChange={(event) => filter('device_id', event.target.value)} />
      <TextField size="small" label="Operatore" value={params.get('operator_code') ?? ''} onChange={(event) => filter('operator_code', event.target.value)} />
    </Box></Card>
    <Card>{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState /> : <>
      <Box sx={{ overflowX: 'auto' }}><Table><TableHead><TableRow><TableCell>Aperto</TableCell><TableCell>Regola</TableCell><TableCell>Severità</TableCell><TableCell>Punteggio</TableCell><TableCell>Descrizione</TableCell><TableCell>Stato</TableCell><TableCell>Confidenza</TableCell></TableRow></TableHead><TableBody>{query.data.items.map((alert) => <TableRow key={alert.id} hover sx={{ cursor: 'pointer' }} onClick={() => { setSelectedId(alert.id); setNote('') }}><TableCell>{formatDateTime(alert.opened_at)}</TableCell><TableCell>{alert.rule_code}</TableCell><TableCell><StatusChip value={alert.severity} /></TableCell><TableCell>{alert.score}/100</TableCell><TableCell>{alert.description}</TableCell><TableCell><StatusChip value={alert.status} /></TableCell><TableCell>{alert.confidence}%</TableCell></TableRow>)}</TableBody></Table></Box>
      <TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, page) => { const next = new URLSearchParams(params); next.set('offset', String(page * limit)); next.set('limit', String(limit)); setParams(next) }} onRowsPerPageChange={(event) => { const next = new URLSearchParams(params); next.set('limit', event.target.value); next.set('offset', '0'); setParams(next) }} labelRowsPerPage="Righe" />
    </>}</Card>
    <Dialog open={Boolean(selectedId)} onClose={() => setSelectedId(undefined)} fullWidth maxWidth="md">
      <DialogTitle>Analisi alert</DialogTitle>
      <DialogContent dividers>
        {detail.isLoading ? <LoadingState /> : detail.error ? <ErrorState error={detail.error} /> : selected && <>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 2 }}><StatusChip value={selected.severity} /><StatusChip value={selected.status} /></Box>
          <Typography variant="h2">{selected.description}</Typography>
          <Typography sx={{ mt: 1, mb: 3 }} color="text.secondary">{selected.explanation}</Typography>
          <Typography variant="h2" sx={{ mb: 1 }}>Evidenze</Typography>
          <Box component="pre" sx={{ p: 2, bgcolor: 'action.hover', color: 'text.primary', border: '1px solid', borderColor: 'divider', borderRadius: 1, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflowX: 'auto', fontSize: 12 }}>{JSON.stringify(selected.evidence, null, 2)}</Box>
          <Typography variant="h2" sx={{ mt: 2, mb: 1 }}>Storico</Typography>
          <Box component="pre" sx={{ p: 2, bgcolor: 'action.hover', color: 'text.primary', border: '1px solid', borderColor: 'divider', borderRadius: 1, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflowX: 'auto', fontSize: 12 }}>{JSON.stringify(selected.history, null, 2)}</Box>
          {canReview && <TextField fullWidth multiline minRows={3} label="Nota dell’auditor" value={note} onChange={(event) => setNote(event.target.value)} sx={{ mt: 2 }} />}
          {update.error && <Box sx={{ mt: 2 }}><ErrorState error={update.error} /></Box>}
        </>}
      </DialogContent>
      <DialogActions><Button onClick={() => setSelectedId(undefined)}>Chiudi</Button>{canReview && <><Button onClick={() => update.mutate({ assigned_to_me: true, status: 'UNDER_REVIEW', note })} disabled={update.isPending}>Prendi in carico</Button><Button color="success" variant="contained" onClick={() => update.mutate({ status: 'JUSTIFIED', note, resolution_reason: note })} disabled={!note || update.isPending}>Giustifica</Button></>}</DialogActions>
    </Dialog>
  </>
}
