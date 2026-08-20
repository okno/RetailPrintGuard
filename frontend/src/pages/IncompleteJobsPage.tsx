import {
  DownloadOutlined,
  FactCheckOutlined,
  ReplayOutlined,
  RemoveCircleOutline,
} from '@mui/icons-material'
import {
  Alert,
  Box,
  Button,
  Card,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Typography,
} from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import { formatDateTime } from '../format'
import type { JobRecord, Page } from '../types'

type ReviewAction = 'VERIFY_USABLE' | 'EXCLUDE_FROM_ANALYSIS' | 'REOPEN_REVIEW'

const actionLabels: Record<ReviewAction, string> = {
  VERIFY_USABLE: 'Verifica e usa',
  EXCLUDE_FROM_ANALYSIS: 'Escludi dall’analisi',
  REOPEN_REVIEW: 'Riapri revisione',
}

export function IncompleteJobsPage() {
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<JobRecord>()
  const [action, setAction] = useState<ReviewAction>('VERIFY_USABLE')
  const [reason, setReason] = useState('')
  const [password, setPassword] = useState('')
  const [reviewError, setReviewError] = useState<unknown>()
  const [downloadError, setDownloadError] = useState<unknown>()
  const reviewReset = useRef<() => void>(() => undefined)
  const queryClient = useQueryClient()
  const limit = Number(params.get('limit') ?? 25)
  const offset = Number(params.get('offset') ?? 0)
  const reviewState = params.get('review_state') ?? 'PENDING'
  const includeTechnical = params.get('technical') === 'all'
  const apiParams = new URLSearchParams(params)
  apiParams.delete('period')
  apiParams.delete('technical')
  apiParams.set('incomplete', 'true')
  if (includeTechnical) apiParams.set('include_technical', 'true')
  else apiParams.delete('include_technical')
  apiParams.set('review_state', reviewState)
  apiParams.set('limit', String(limit))
  apiParams.set('offset', String(offset))
  const roles = session().user?.roles ?? []
  const isAdmin = roles.includes('ADMIN')
  const canDownload = isAdmin || roles.includes('AUDITOR')

  const query = useQuery({
    queryKey: scopedQueryKey('jobs', 'incomplete', apiParams.toString()),
    queryFn: () => api<Page<JobRecord>>(`/jobs?${apiParams.toString()}`),
  })
  const review = useMutation({
    mutationFn: (body: { action: ReviewAction; reason: string; confirmation_password: string }) =>
      api<JobRecord>(`/jobs/${selected?.id}/review`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    onMutate: () => setReviewError(undefined),
    onSuccess: (job) => {
      setSelected(job)
      setReason('')
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
      queryClient.invalidateQueries({ queryKey: ['system', 'diagnostics'] })
    },
    onError: (error) => setReviewError(error),
    onSettled: () => {
      setPassword('')
      // TanStack Mutation retains its last variables object.  Reset it as well,
      // otherwise confirmation_password would remain reachable in client memory.
      reviewReset.current()
    },
  })
  reviewReset.current = review.reset

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params)
    value ? next.set(key, value) : next.delete(key)
    next.set('offset', '0')
    setParams(next)
  }

  function beginReview(job: JobRecord, requestedAction?: ReviewAction) {
    setSelected(job)
    setAction(requestedAction ?? (job.review_state === 'PENDING' ? 'VERIFY_USABLE' : 'REOPEN_REVIEW'))
    setReason('')
    setPassword('')
    setReviewError(undefined)
    review.reset()
  }

  async function download(job: JobRecord, direction: 'request' | 'response') {
    setDownloadError(undefined)
    try {
      await downloadApi(
        `/jobs/${encodeURIComponent(job.id)}/raw?direction=${direction}`,
        `${job.id}-${direction}.raw`,
      )
    } catch (error) {
      setDownloadError(error)
    }
  }

  const validConfirmation = reason.trim().length >= 10 && password.length >= 14
  return <>
    <PageHeader
      title="Job incompleti"
      subtitle="Revisione tecnica delle acquisizioni incomplete. Ogni decisione è auditata e non cancella mai RAW, manifest o documenti."
    />
    <Alert severity="info" sx={{ mb: 2 }}>
      La vista predefinita mostra solo incompleti con dati di vendita/importi o esiti
      parser ancora incerti. I frammenti tecnici privi di contenuto commerciale restano
      conservati e sono consultabili selezionando “Tutte le evidenze tecniche”.
    </Alert>
    {downloadError && <Box sx={{ mb: 2 }}><ErrorState error={downloadError} /></Box>}
    <Card sx={{ p: 2, mb: 2 }}>
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: 'repeat(4, minmax(150px, 1fr))' }, gap: 2 }}>
        <FormControl size="small">
          <InputLabel>Revisione</InputLabel>
          <Select label="Revisione" value={reviewState} onChange={(event) => setFilter('review_state', String(event.target.value))}>
            <MenuItem value="PENDING">Da revisionare</MenuItem>
            <MenuItem value="VERIFIED_USABLE">Verificati e utilizzabili</MenuItem>
            <MenuItem value="EXCLUDED">Esclusi dall’analisi</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small">
          <InputLabel>Contenuto</InputLabel>
          <Select label="Contenuto" value={includeTechnical ? 'all' : 'business'} onChange={(event) => setFilter('technical', String(event.target.value) === 'all' ? 'all' : '')}>
            <MenuItem value="business">Vendite e importi</MenuItem>
            <MenuItem value="all">Tutte le evidenze tecniche</MenuItem>
          </Select>
        </FormControl>
        <TextField size="small" label="Dispositivo" value={params.get('device_id') ?? ''} onChange={(event) => setFilter('device_id', event.target.value)} />
        <PeriodPicker params={params} onChange={setParams} />
      </Box>
    </Card>
    <Card>
      {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessun job incompleto per i filtri selezionati." /> : <>
        <Box sx={{ overflowX: 'auto' }}><Table><TableHead><TableRow>
          <TableCell>Acquisito</TableCell><TableCell>Dispositivo / job</TableCell><TableCell>Capture</TableCell><TableCell>Parser</TableCell><TableCell>Byte</TableCell><TableCell>Revisione</TableCell><TableCell align="right">Azioni</TableCell>
        </TableRow></TableHead><TableBody>{query.data.items.map((job) => <TableRow key={job.id} hover>
          <TableCell>{formatDateTime(job.captured_at)}</TableCell>
          <TableCell><Typography fontWeight={700}>{job.device_id}</Typography><Typography variant="caption" sx={{ fontFamily: 'monospace' }}>{job.external_job_id}</Typography></TableCell>
          <TableCell><StatusChip value={job.status} /></TableCell>
          <TableCell><StatusChip value={job.parser_status ?? 'NON_IMPORTATO'} /></TableCell>
          <TableCell>{new Intl.NumberFormat('it-IT').format(job.request_bytes + job.response_bytes)}</TableCell>
          <TableCell><StatusChip value={job.review_state ?? 'PENDING'} />{job.analysis_excluded && <Typography variant="caption" display="block">Fuori dall’analisi</Typography>}</TableCell>
          <TableCell align="right"><Box sx={{ display: 'flex', justifyContent: 'flex-end', flexWrap: 'wrap', gap: 1 }}>
            {canDownload && <Button size="small" startIcon={<DownloadOutlined />} onClick={() => download(job, 'request')}>RAW richiesta</Button>}
            {canDownload && job.response_bytes > 0 && <Button size="small" startIcon={<DownloadOutlined />} onClick={() => download(job, 'response')}>RAW risposta</Button>}
            {isAdmin && <Button size="small" variant="outlined" onClick={() => beginReview(job)}>Revisiona</Button>}
          </Box></TableCell>
        </TableRow>)}</TableBody></Table></Box>
        <TablePagination
          component="div"
          count={query.data.total}
          page={Math.floor(offset / limit)}
          rowsPerPage={limit}
          rowsPerPageOptions={[25, 50, 100]}
          onPageChange={(_, page) => { const next = new URLSearchParams(params); next.set('offset', String(page * limit)); next.set('limit', String(limit)); setParams(next) }}
          onRowsPerPageChange={(event) => { const next = new URLSearchParams(params); next.set('limit', event.target.value); next.set('offset', '0'); setParams(next) }}
          labelRowsPerPage="Righe"
        />
      </>}
    </Card>
    <Dialog open={Boolean(selected)} onClose={() => { if (!review.isPending) { setSelected(undefined); setPassword(''); setReviewError(undefined); review.reset() } }} fullWidth maxWidth="sm">
      <DialogTitle>Revisione job incompleto</DialogTitle>
      <DialogContent dividers>
        {selected && <>
          <Typography fontWeight={700}>{selected.device_id} · {selected.external_job_id}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{formatDateTime(selected.captured_at)} · SHA-256 {selected.manifest_sha256}</Typography>
          {selected.warnings.length > 0 && <Alert severity="warning" sx={{ mb: 2 }}>{selected.warnings.join(' · ')}</Alert>}
          {selected.review_reason && <Alert severity="info" sx={{ mb: 2 }}>Ultima motivazione: {selected.review_reason}</Alert>}
          {isAdmin ? <>
            <FormControl fullWidth sx={{ mt: 1 }}>
              <InputLabel>Azione</InputLabel>
              <Select label="Azione" value={action} onChange={(event) => setAction(event.target.value as ReviewAction)}>
                <MenuItem value="VERIFY_USABLE"><FactCheckOutlined fontSize="small" sx={{ mr: 1 }} />Verifica e usa</MenuItem>
                <MenuItem value="EXCLUDE_FROM_ANALYSIS"><RemoveCircleOutline fontSize="small" sx={{ mr: 1 }} />Escludi dall’analisi</MenuItem>
                <MenuItem value="REOPEN_REVIEW"><ReplayOutlined fontSize="small" sx={{ mr: 1 }} />Riapri revisione</MenuItem>
              </Select>
            </FormControl>
            <TextField fullWidth multiline minRows={3} label="Motivazione obbligatoria" value={reason} onChange={(event) => setReason(event.target.value)} helperText="Da 10 a 2000 caratteri; sarà conservata nell’audit log." inputProps={{ minLength: 10, maxLength: 2000 }} sx={{ mt: 2 }} />
            <TextField fullWidth type="password" name="job-review-confirmation-secret" autoComplete="new-password" label="Password di conferma amministratore" value={password} onChange={(event) => setPassword(event.target.value)} helperText="La password resta solo in memoria per questa richiesta e viene subito rimossa." inputProps={{ minLength: 14, maxLength: 1024 }} sx={{ mt: 2 }} />
            {action === 'EXCLUDE_FROM_ANALYSIS' && <Alert severity="warning" sx={{ mt: 2 }}>Il job sarà escluso dalle analisi future. Nessun dato RAW verrà eliminato.</Alert>}
            {reviewError && <Box sx={{ mt: 2 }}><ErrorState error={reviewError} /></Box>}
          </> : <Alert severity="info" sx={{ mt: 2 }}>Solo un amministratore può registrare la decisione. AUDITOR può consultare e scaricare le evidenze.</Alert>}
        </>}
      </DialogContent>
      <DialogActions>
        <Button disabled={review.isPending} onClick={() => { setSelected(undefined); setPassword(''); setReviewError(undefined); review.reset() }}>Chiudi</Button>
        {isAdmin && <Button
          variant="contained"
          color={action === 'EXCLUDE_FROM_ANALYSIS' ? 'warning' : 'primary'}
          disabled={!validConfirmation || review.isPending}
          onClick={() => review.mutate({ action, reason: reason.trim(), confirmation_password: password })}
        >{actionLabels[action]}</Button>}
      </DialogActions>
    </Dialog>
  </>
}
