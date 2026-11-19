import { ArrowBackOutlined, DownloadOutlined, ManageSearchOutlined, RefreshOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, Chip, LinearProgress, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, Typography } from '@mui/material'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { analysisCreateBody, analysisPending, analysisPollingInterval, analysisProgress, analysisReportFilename, analysisStageLabel, analysisStatusLabel, canAnalyze, type AnalysisRun } from '../analyses'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { formatDateTime } from '../format'
import { analysisDetailPath } from '../routes'
import type { Page } from '../types'

const colors = { queued: 'default', running: 'info', completed: 'success', failed: 'error' } as const

function AnalysisProgressPanel({ run }: { run: AnalysisRun }) {
  const progress = analysisProgress(run)
  return <Stack spacing={1}>
    <Box><Chip size="small" color={colors[run.status] ?? 'default'} label={analysisStatusLabel(run.status)} /></Box>
    <Typography fontWeight={650}>{analysisStageLabel(run.stage)}</Typography>
    {analysisPending(run) && <LinearProgress aria-label="Avanzamento della fase" variant={progress?.percent === undefined ? 'indeterminate' : 'determinate'} value={progress?.percent} />}
    {progress && <Typography variant="body2">{progress.completed} / {progress.total} {progress.unit} nella fase corrente</Typography>}
    <Typography variant="body2">Job elaborati: {run.completed_jobs} / {run.total_jobs} · Documenti ed evidenze analizzati: {run.completed_documents} / {run.total_documents}</Typography>
    {run.failed_jobs > 0 && <Alert severity="warning">{run.failed_jobs} job richiedono verifica. Consultare l’esito prima di usare i risultati.</Alert>}
    {run.error && <ErrorState error={new Error(run.error)} />}
  </Stack>
}

function ReportDownloads({ run }: { run: AnalysisRun }) {
  const [format, setFormat] = useState<'json' | 'csv'>()
  const [error, setError] = useState<unknown>()
  async function download(next: 'json' | 'csv') {
    setFormat(next)
    setError(undefined)
    try {
      await downloadApi(`/analyses/${encodeURIComponent(run.id)}/download?format=${next}`, analysisReportFilename(run.id, next))
    } catch (failure) {
      setError(failure)
    } finally {
      setFormat(undefined)
    }
  }
  return <Stack spacing={1}>
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {(['json', 'csv'] as const).map((value) => <Button key={value} startIcon={<DownloadOutlined />} disabled={run.status !== 'completed' || Boolean(format)} onClick={() => { void download(value) }}>
        {format === value ? 'Download…' : value === 'json' ? 'Report JSON completo' : 'CSV sconti'}
      </Button>)}
    </Box>
    {error != null && <ErrorState error={error} />}
  </Stack>
}

export function ReanalysisPage() {
  if (!canAnalyze(session().user?.roles ?? [])) return <Alert severity="warning">Le rianalisi e i relativi report sono riservati ad amministratori e revisori.</Alert>
  return <ReanalysisWorkspace />
}

function ReanalysisWorkspace() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(0)
  const [limit, setLimit] = useState(25)
  const latest = useQuery({
    queryKey: scopedQueryKey('analyses', 'latest'),
    queryFn: () => api<Page<AnalysisRun>>('/analyses?limit=1&offset=0'),
    refetchInterval: (query) => analysisPollingInterval(query.state.data?.items),
  })
  const history = useQuery({
    queryKey: scopedQueryKey('analyses', 'history', limit, page),
    queryFn: () => api<Page<AnalysisRun>>(`/analyses?limit=${limit}&offset=${page * limit}`),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => analysisPollingInterval(query.state.data?.items),
  })
  const active = latest.data?.items.find(analysisPending)
  const create = useMutation({
    mutationFn: () => api<AnalysisRun>('/analyses', { method: 'POST', body: JSON.stringify(analysisCreateBody()) }),
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ['analyses'] })
      navigate(analysisDetailPath(run.id))
    },
  })
  return <>
    <PageHeader title="Rianalisi" subtitle="Rielaborazione dei RAW dell’intero storico, con avanzamento e richieste conservate." />
    <Card sx={{ p: 2.5, mb: 3 }}>
      <Typography variant="h2" sx={{ mb: 1 }}>Rianalizza l’intero storico</Typography>
      <Typography color="text.secondary" sx={{ mb: 2 }}>La richiesta rielabora i RAW, aggiorna le correlazioni e verifica regole e sconti. Le nuove interpretazioni vengono versionate; RAW originali, interpretazioni precedenti e archivi già prodotti restano conservati.</Typography>
      <Alert severity="info" sx={{ mb: 2 }}>L’attivazione della nuova interpretazione riguarda tutto lo storico. I filtri per date e dispositivi sono disponibili nella consultazione dei risultati.</Alert>
      <Button variant="contained" startIcon={<ManageSearchOutlined />} disabled={create.isPending || latest.isLoading || Boolean(latest.error) || Boolean(active)} onClick={() => create.mutate()}>
        {create.isPending ? 'Registrazione richiesta…' : 'Avvia rianalisi dell’intero storico'}
      </Button>
      {active && <Alert severity="info" sx={{ mt: 2 }} action={<Button component={Link} to={analysisDetailPath(active.id)}>Segui avanzamento</Button>}>È già presente una rianalisi in corso.</Alert>}
      {latest.error && <Box sx={{ mt: 2 }}><ErrorState error={latest.error} /></Box>}
      {create.error && <Box sx={{ mt: 2 }}><ErrorState error={create.error} /></Box>}
    </Card>
    <PageHeader title="Storico rianalisi" subtitle="Ogni richiesta mantiene il proprio esito e i risultati del momento in cui è stata eseguita." actions={<Button startIcon={<RefreshOutlined />} disabled={history.isFetching} onClick={() => { void history.refetch(); void latest.refetch() }}>Aggiorna</Button>} />
    <Card>
      {history.isLoading ? <LoadingState /> : history.error ? <ErrorState error={history.error} /> : !history.data?.items.length ? <EmptyState label="Nessuna rianalisi richiesta." /> : <>
        <TableContainer><Table aria-label="Storico rianalisi">
          <TableHead><TableRow><TableCell>Richiesta</TableCell><TableCell>Stato e avanzamento</TableCell><TableCell>Risultati</TableCell></TableRow></TableHead>
          <TableBody>{history.data.items.map((run) => <TableRow key={run.id}>
            <TableCell sx={{ minWidth: 210, maxWidth: 310 }}><Typography fontWeight={650}>{formatDateTime(run.created_at)}</Typography><Typography variant="body2">{run.requested_by} · Intero storico</Typography><Typography variant="caption" sx={{ overflowWrap: 'anywhere' }}>ID {run.id}</Typography></TableCell>
            <TableCell sx={{ minWidth: 290 }}><AnalysisProgressPanel run={run} /></TableCell>
            <TableCell sx={{ minWidth: 180 }}><Button component={Link} to={analysisDetailPath(run.id)}>Apri rianalisi</Button>{run.status === 'completed' && <Typography variant="body2">{run.summary?.documents_with_discounts ?? '—'} documenti con sconti osservati</Typography>}</TableCell>
          </TableRow>)}</TableBody>
        </Table></TableContainer>
        <TablePagination component="div" count={history.data.total} page={page} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, next) => setPage(next)} onRowsPerPageChange={(event) => { setLimit(Number(event.target.value)); setPage(0) }} labelRowsPerPage="Richieste per pagina" />
      </>}
    </Card>
  </>
}

export function AnalysisDetailPage() {
  const { analysisId } = useParams()
  if (!canAnalyze(session().user?.roles ?? [])) return <Alert severity="warning">Le rianalisi e i relativi report sono riservati ad amministratori e revisori.</Alert>
  if (!analysisId) return <ErrorState error={new Error('Identificativo della rianalisi assente.')} />
  return <AnalysisDetailWorkspace id={analysisId} />
}

function AnalysisDetailWorkspace({ id }: { id: string }) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const query = useQuery({
    queryKey: scopedQueryKey('analyses', 'detail', id),
    queryFn: () => api<AnalysisRun>(`/analyses/${encodeURIComponent(id)}`),
    refetchInterval: (state) => analysisPending(state.state.data) ? 3_000 : false,
  })
  const retry = useMutation({
    mutationFn: () => api<AnalysisRun>(`/analyses/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ['analyses'] })
      navigate(analysisDetailPath(run.id))
    },
  })
  if (query.isLoading) return <LoadingState label="Caricamento rianalisi…" />
  if (query.error || !query.data) return <ErrorState error={query.error} />
  const run = query.data
  return <>
    <PageHeader title="Dettaglio rianalisi" subtitle="Avanzamento dell’intero storico e risultati conservati per questa richiesta." actions={<Button component={Link} to="/rianalisi" startIcon={<ArrowBackOutlined />}>Storico</Button>} />
    <Card sx={{ p: 2.5, mb: 2 }}>
      <Typography fontWeight={650}>{formatDateTime(run.created_at)} · {run.requested_by}</Typography>
      <Typography variant="caption" display="block" sx={{ overflowWrap: 'anywhere', mb: 2 }}>ID {run.id}</Typography>
      <AnalysisProgressPanel run={run} />
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>Avvio: {run.started_at ? formatDateTime(run.started_at) : 'In attesa'} · Fine: {run.completed_at ? formatDateTime(run.completed_at) : 'Non terminata'}</Typography>
      {run.status === 'failed' && <Box sx={{ mt: 2 }}>
        <Button variant="outlined" startIcon={<RefreshOutlined />} disabled={retry.isPending} onClick={() => retry.mutate()}>{retry.isPending ? 'Registrazione richiesta…' : 'Riprova rianalisi dell’intero storico'}</Button>
        {retry.error && <Box sx={{ mt: 1 }}><ErrorState error={retry.error} /></Box>}
      </Box>}
    </Card>
    {run.status === 'completed' && <Card sx={{ p: 2.5, mb: 2 }}>
      <Typography variant="h2" sx={{ mb: 1 }}>Risultati della richiesta</Typography>
      <Typography sx={{ mb: 2 }}>{run.summary?.documents_with_discounts ?? '—'} documenti con sconti osservati · {run.summary?.observed_rows ?? '—'} righe di sconto.</Typography>
      {Boolean(run.summary?.jobs_without_request_raw || run.summary?.documents_without_raw_reprocessing) && <Alert severity="warning" sx={{ mb: 2 }}>{run.summary?.jobs_without_request_raw ?? 0} job privi di RAW richiesta · {run.summary?.documents_without_raw_reprocessing ?? 0} documenti conservati dalla precedente interpretazione. Questi documenti sono inclusi nei risultati, ma non sono stati rielaborati dal RAW.</Alert>}
      <Alert severity="info" sx={{ mb: 2 }}>Uno sconto osservato o verificato aritmeticamente non è automaticamente autorizzato e non comporta la chiusura degli alert antifrode.</Alert>
      <Button variant="contained" component={Link} to={`/sconti?analysis_id=${encodeURIComponent(run.id)}`}>Consulta risultati e documenti</Button>
      <ReportDownloads run={run} />
      <Typography variant="caption" color="text.secondary">Il JSON conserva tutti gli input e i risultati della rianalisi; il CSV contiene soltanto le righe di sconto. Entrambi sono congelati: i filtri di consultazione non modificano i report.</Typography>
    </Card>}
    <Card component="details" sx={{ p: 2.5 }}>
      <Typography component="summary" sx={{ cursor: 'pointer', fontWeight: 650 }}>Versioni degli strumenti usati</Typography>
      <Typography variant="body2" sx={{ mt: 1, overflowWrap: 'anywhere' }}>Analisi sconti: {run.policy_version || 'Non disponibile'}</Typography>
      {Object.entries(run.parser_versions).map(([name, version]) => <Typography key={name} variant="body2" sx={{ overflowWrap: 'anywhere' }}>{name}: {typeof version === 'string' ? version : JSON.stringify(version)}</Typography>)}
    </Card>
  </>
}
