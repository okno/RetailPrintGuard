import { DownloadOutlined, FolderOpenOutlined, PictureAsPdfOutlined, RefreshOutlined } from '@mui/icons-material'
import {
  Alert, Box, Button, Card, Checkbox, Chip, FormControl, FormHelperText, InputLabel,
  LinearProgress, ListItemText, MenuItem, Select, Stack, Table, TableBody, TableCell,
  TableContainer, TableHead, TablePagination, TableRow, TextField, Tooltip, Typography,
} from '@mui/material'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { deviceLabel } from '../documentPresentation'
import { documentReasonLabel } from '../documentEligibility'
import { canExport, exportArchiveFilename, exportCoverageLabel, exportDateError, exportDateLabel, exportDocumentScopeLabel, exportPollingInterval, exportProgress, exportRequestBody, exportScopeLabel, exportStatusLabel, isLegacyExport } from '../exports'
import { DISPLAY_TIME_ZONE, formatDateTime } from '../format'
import { romeInputFromUtc } from '../period'
import { exportDetailPath } from '../routes'
import type { Device, ExportDocumentScope, ExportRequest, ExportScope, Page } from '../types'

const bytes = new Intl.NumberFormat('it-IT', { notation: 'compact', style: 'unit', unit: 'byte' })
const statusColors = { queued: 'default', running: 'info', completed: 'success', failed: 'error' } as const

export function ExportsPage() {
  if (!canExport(session().user?.roles ?? [])) {
    return <Alert severity="warning">Le esportazioni sono riservate agli utenti amministratori e revisori.</Alert>
  }
  return <ExportWorkspace />
}

function ExportSelection({ job }: { job: ExportRequest }) {
  const metadata = job.selection_metadata
  if (isLegacyExport(job) || !metadata) return <>
    <Chip size="small" color="warning" variant="outlined" label="Archivio precedente non filtrato" />
    <Typography variant="body2" sx={{ mt: 1 }}>Può includere evidenze tecniche e documenti parziali.</Typography>
    <Typography variant="caption" display="block" color="text.secondary">Copertura effettiva non registrata.</Typography>
    <Box component="details" sx={{ mt: 1 }}>
      <Typography component="summary" variant="caption" sx={{ cursor: 'pointer' }}>Filtro richiesto originariamente</Typography>
      <Typography variant="caption" display="block">{exportDateLabel(job.date_from)} – {exportDateLabel(job.date_to)} · {job.timezone}</Typography>
      <Typography variant="caption" display="block">Questi estremi sono il filtro della richiesta, non le date dei documenti.</Typography>
    </Box>
  </>
  return <>
    <Chip size="small" color={metadata.document_scope === 'partial' ? 'warning' : 'success'} variant="outlined" label={exportDocumentScopeLabel(metadata.document_scope)} />
    <Typography variant="body2" fontWeight={700} sx={{ mt: 1 }}>{exportScopeLabel(metadata.scope)}</Typography>
    {metadata.scope === 'latest_10' && <Typography variant="caption" display="block">Selezionati per acquisizione più recente; fino a 10 documenti completi.</Typography>}
    <Typography variant="body2">Copertura effettiva: {exportCoverageLabel(metadata)}</Typography>
    <Typography variant="caption" display="block" color="text.secondary">{job.timezone} · giorni inclusi</Typography>
    {metadata.scope === 'date_range' && <Typography variant="caption" display="block">Filtro richiesto: {exportDateLabel(job.date_from)} – {exportDateLabel(job.date_to)}</Typography>}
    <Typography variant="caption" display="block" sx={{ mt: .5 }}>Data documento: {metadata.document_date_count} · Data acquisizione: {metadata.capture_date_count}</Typography>
    <Box component="details" sx={{ mt: 1 }}>
      <Typography component="summary" variant="caption" sx={{ cursor: 'pointer' }}>Selezione e motivi di esclusione</Typography>
      <Typography variant="caption" display="block">Esaminati: {metadata.scanned_documents} · Selezionati: {metadata.selected_documents} · Esclusi da questo archivio: {metadata.excluded_documents}</Typography>
      {metadata.scope === 'latest_10' && metadata.unscanned_documents !== undefined && <Typography variant="caption" display="block">Documenti precedenti non esaminati dopo il raggiungimento del limite: {metadata.unscanned_documents}</Typography>}
      <Typography variant="caption" display="block">Completi: {metadata.category_counts.complete} · Parziali: {metadata.category_counts.partial} · Evidenze escluse dai PDF: {metadata.category_counts.excluded}</Typography>
      {Object.entries(metadata.exclusion_counts).filter(([, count]) => count > 0).map(([reason, count]) => <Typography key={reason} variant="caption" display="block" sx={{ mt: .5 }}>
        {count} — {reason === 'OTHER_DOCUMENT_SCOPE'
          ? metadata.document_scope === 'complete' ? 'Parziali da verificare, disponibili nell’archivio separato.' : 'Documenti completi, disponibili nell’archivio principale.'
          : documentReasonLabel(reason)}
      </Typography>)}
    </Box>
  </>
}

function ExportWorkspace() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [scope, setScope] = useState<ExportScope>('all_history')
  const [documentScope, setDocumentScope] = useState<ExportDocumentScope>('complete')
  const [dateFrom, setDateFrom] = useState(() => romeInputFromUtc(new Date().toISOString()).slice(0, 10))
  const [dateTo, setDateTo] = useState(dateFrom)
  const [deviceIds, setDeviceIds] = useState<string[]>([])
  const [page, setPage] = useState(0)
  const [limit, setLimit] = useState(25)
  const [downloadingId, setDownloadingId] = useState<string>()
  const [downloadError, setDownloadError] = useState<unknown>()
  const devices = useQuery({
    queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices'), staleTime: 30_000,
  })
  const history = useQuery({
    queryKey: scopedQueryKey('exports', limit, page),
    queryFn: () => api<Page<ExportRequest>>(`/exports?limit=${limit}&offset=${page * limit}`),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => exportPollingInterval(query.state.data?.items),
  })
  const create = useMutation({
    mutationFn: () => api<ExportRequest>('/exports', {
      method: 'POST',
      body: JSON.stringify(exportRequestBody({ scope, documentScope, dateFrom, dateTo, deviceIds })),
    }),
    onSuccess: async (job) => {
      setPage(0)
      await queryClient.invalidateQueries({ queryKey: ['exports'] })
      if (job.scope === 'latest_10') navigate(exportDetailPath(job.id))
    },
  })
  const dateError = scope === 'date_range' ? exportDateError(dateFrom, dateTo) : undefined

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!dateError && !create.isPending) create.mutate()
  }

  async function download(job: ExportRequest) {
    setDownloadError(undefined)
    setDownloadingId(job.id)
    try {
      await downloadApi(`/exports/${encodeURIComponent(job.id)}/download`, exportArchiveFilename(job))
    } catch (error) {
      setDownloadError(error)
    } finally {
      setDownloadingId(undefined)
    }
  }

  return <>
    <PageHeader title="Esportazioni" subtitle="Archivi PDF distinti per documenti completi e parziali da verificare, con storico delle richieste." />
    <Alert severity="info" sx={{ mb: 2 }}>
      I nuovi PDF riproducono il contenuto della scheda Scontrino al momento della richiesta, senza sezioni aggiuntive.
      Le tracce tecniche sono escluse dai nuovi PDF. I RAW originali e gli archivi già creati rimangono conservati.
    </Alert>
    <Card component="form" onSubmit={submit} sx={{ p: 2.5, mb: 3 }}>
      <Typography variant="h2" sx={{ mb: 2 }}>Nuova richiesta</Typography>
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))', lg: 'repeat(3, minmax(0, 1fr))' }, gap: 2, alignItems: 'start' }}>
        <FormControl fullWidth disabled={scope === 'latest_10'}>
          <InputLabel id="export-content-label">Contenuto archivio</InputLabel>
          <Select labelId="export-content-label" label="Contenuto archivio" value={documentScope} onChange={(event) => setDocumentScope(event.target.value as ExportDocumentScope)}>
            <MenuItem value="complete">Documenti completi</MenuItem>
            <MenuItem value="partial">Parziali da verificare</MenuItem>
          </Select>
        </FormControl>
        <FormControl fullWidth>
          <InputLabel id="export-period-label">Periodo</InputLabel>
          <Select labelId="export-period-label" label="Periodo" value={scope} onChange={(event) => { const next = event.target.value as ExportScope; setScope(next); if (next === 'latest_10') setDocumentScope('complete') }}>
            <MenuItem value="all_history">Intero storico disponibile</MenuItem>
            <MenuItem value="date_range">Intervallo di date</MenuItem>
            <MenuItem value="latest_10">Ultimi 10 documenti completi</MenuItem>
          </Select>
        </FormControl>
        <FormControl fullWidth disabled={devices.isLoading || Boolean(devices.error)}>
          <InputLabel id="export-devices-label" shrink>Dispositivi</InputLabel>
          <Select multiple displayEmpty labelId="export-devices-label" label="Dispositivi" value={deviceIds}
            onChange={(event) => setDeviceIds(typeof event.target.value === 'string' ? event.target.value.split(',') : event.target.value)}
            renderValue={(selected) => selected.length ? selected.map((id) => deviceLabel(id, devices.data)).join(', ') : 'Tutti i dispositivi'}>
            {devices.data?.map((device) => <MenuItem key={device.id} value={device.id}>
              <Checkbox checked={deviceIds.includes(device.id)} />
              <ListItemText primary={deviceLabel(device.id, devices.data)} secondary={device.id} />
            </MenuItem>)}
          </Select>
          <FormHelperText>Nessuna selezione include tutti i dispositivi.</FormHelperText>
        </FormControl>
        {scope === 'date_range' && <>
          <TextField required fullWidth type="date" label="Dal giorno" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} slotProps={{ inputLabel: { shrink: true } }} />
          <TextField required fullWidth type="date" label="Al giorno incluso" value={dateTo} onChange={(event) => setDateTo(event.target.value)} slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: dateFrom } }} />
        </>}
        <Button type="submit" variant="contained" startIcon={<PictureAsPdfOutlined />} disabled={Boolean(dateError) || create.isPending} sx={{ minHeight: 56, whiteSpace: 'nowrap' }}>
          {create.isPending ? 'Invio richiesta…' : scope === 'latest_10' ? 'Crea ZIP ultimi 10' : 'Crea archivio PDF'}
        </Button>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
        {scope === 'latest_10' ? 'Gli ultimi 10 documenti completi sono selezionati per acquisizione più recente, tra i dispositivi scelti. Al termine puoi aprire i PDF conservati nell’archivio prima di scaricare lo ZIP.' : <>
        Si usa la data osservata nel documento; quando assente, la data di acquisizione del server. Giorni inclusi, fuso {DISPLAY_TIME_ZONE}.
        Lo storico mostra la copertura effettiva dei documenti selezionati. Massimo 10.000 PDF per richiesta: per volumi maggiori suddividere il periodo o selezionare un dispositivo.
        </>}
      </Typography>
      <Stack spacing={1} sx={{ mt: 2 }} aria-live="polite">
        {dateError && <Alert severity="warning">{dateError}</Alert>}
        {documentScope === 'partial' && <Alert severity="warning">L’archivio dei parziali contiene documenti incompleti da verificare ed è separato dall’archivio principale dei completi. I PDF mantengono il testo acquisito.</Alert>}
        {devices.error && <Alert severity="warning">Elenco dispositivi non disponibile. È possibile esportare tutti i dispositivi.</Alert>}
        {create.error && <ErrorState error={create.error} />}
        {create.isSuccess && <Alert severity="success">Richiesta registrata. Lo stato e il download sono disponibili nello storico.</Alert>}
      </Stack>
    </Card>
    <PageHeader title="Storico richieste" subtitle="Le richieste e i relativi esiti restano consultabili dopo l’uscita dall’applicazione." actions={
      <Button startIcon={<RefreshOutlined />} onClick={() => { void history.refetch() }} disabled={history.isFetching}>Aggiorna</Button>
    } />
    {downloadError != null && <Box sx={{ mb: 2 }}><ErrorState error={downloadError} /></Box>}
    <Card>
      {history.isLoading ? <LoadingState label="Caricamento storico esportazioni…" /> : history.error ? <ErrorState error={history.error} /> : !history.data?.items.length ? <EmptyState label="Nessuna richiesta di esportazione." /> : <>
        <TableContainer>
          <Table aria-label="Storico esportazioni">
            <TableHead><TableRow>
              <TableCell>Richiesta</TableCell><TableCell>Periodo e dispositivi</TableCell><TableCell>Stato</TableCell><TableCell>Documenti</TableCell><TableCell>Archivio</TableCell>
            </TableRow></TableHead>
            <TableBody>{history.data.items.map((job) => <TableRow key={job.id}>
              <TableCell sx={{ minWidth: 190 }}>
                <Typography variant="body2" fontWeight={700}>{formatDateTime(job.created_at)}</Typography>
                <Typography variant="body2">{job.requested_by}</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>ID {job.id}</Typography>
              </TableCell>
              <TableCell sx={{ minWidth: 290, maxWidth: 450 }}>
                <ExportSelection job={job} />
                <Typography variant="body2" sx={{ mt: 0.5 }}>{job.device_ids.length ? job.device_ids.map((id) => deviceLabel(id, devices.data)).join(', ') : 'Tutti i dispositivi'}</Typography>
              </TableCell>
              <TableCell sx={{ minWidth: 200, maxWidth: 360 }}>
                <Chip size="small" color={statusColors[job.status] ?? 'default'} label={exportStatusLabel(job.status)} />
                {job.started_at && <Typography variant="caption" display="block" sx={{ mt: 1 }}>Avvio: {formatDateTime(job.started_at)}</Typography>}
                {job.completed_at && <Typography variant="caption" display="block">Fine: {formatDateTime(job.completed_at)}</Typography>}
                {job.error && <Typography variant="body2" color="error" sx={{ mt: 1, overflowWrap: 'anywhere' }}>{job.error}</Typography>}
              </TableCell>
              <TableCell sx={{ minWidth: 140 }}>
                <Typography variant="body2">{job.completed_documents} / {job.total_documents} PDF</Typography>
                {job.status === 'running' && <LinearProgress aria-label={`Preparazione archivio ${job.id}`} variant={job.total_documents ? 'determinate' : 'indeterminate'} value={exportProgress(job)} sx={{ mt: 1 }} />}
                {job.status === 'completed' && job.total_documents === 0 && <Typography variant="caption" color="text.secondary">Nessun documento corrisponde al contenuto e al periodo scelti.</Typography>}
              </TableCell>
              <TableCell sx={{ minWidth: 160 }}>
                {job.status === 'completed' ? <>
                  <Button component={Link} to={exportDetailPath(job.id)} startIcon={<FolderOpenOutlined />} size="small" aria-label={`Apri archivio della richiesta ${job.id}`} sx={{ mb: .5 }}>Apri archivio</Button>
                  <Button startIcon={<DownloadOutlined />} variant="outlined" size="small" onClick={() => { void download(job) }} disabled={Boolean(downloadingId)} aria-label={`Scarica ZIP della richiesta ${job.id}`}>
                    {downloadingId === job.id ? 'Download…' : 'Scarica ZIP'}
                  </Button>
                  {job.archive_bytes != null && <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>{bytes.format(job.archive_bytes)}</Typography>}
                  {job.archive_sha256 && <Tooltip title={job.archive_sha256}><Typography variant="caption" color="text.secondary" sx={{ cursor: 'help' }}>SHA-256 disponibile</Typography></Tooltip>}
                </> : '—'}
              </TableCell>
            </TableRow>)}</TableBody>
          </Table>
        </TableContainer>
        <TablePagination component="div" count={history.data.total} page={page} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} onPageChange={(_, next) => setPage(next)} onRowsPerPageChange={(event) => { setLimit(Number(event.target.value)); setPage(0) }} labelRowsPerPage="Richieste per pagina" />
      </>}
    </Card>
  </>
}
