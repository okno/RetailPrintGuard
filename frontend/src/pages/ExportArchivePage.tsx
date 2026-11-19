import { ArrowBackOutlined, ChevronLeftOutlined, ChevronRightOutlined, CloseOutlined, DownloadOutlined, PauseOutlined, PictureAsPdfOutlined, PlayArrowOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, Chip, Dialog, DialogActions, DialogContent, DialogTitle, IconButton, LinearProgress, List, ListItemButton, ListItemText, Stack, TablePagination, Typography, useMediaQuery } from '@mui/material'
import { useTheme } from '@mui/material/styles'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, downloadApi, scopedQueryKey, session } from '../api/client'
import { archivePdfPath, downloadPreview, loadArchivePreview } from '../archivePreview'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { documentTypeLabel } from '../documentPresentation'
import { canExport, exportArchiveFilename, exportCoverageLabel, exportDocumentScopeLabel, exportPollingInterval, exportProgress, exportScopeLabel, exportStatusLabel, isLegacyExport } from '../exports'
import { formatDateTime } from '../format'
import type { ArchiveDocument, ArchiveDocuments, ExportRequest } from '../types'

function ArchiveLightbox({ exportId, total, initialSequence, known, onClose }: { exportId: string; total: number; initialSequence: number; known: ArchiveDocument[]; onClose: () => void }) {
  const theme = useTheme()
  const mobile = useMediaQuery(theme.breakpoints.down('md'))
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const [sequence, setSequence] = useState(initialSequence)
  const [playing, setPlaying] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [loadedKey, setLoadedKey] = useState<string>()
  const [preview, setPreview] = useState<{ key: string; url?: string; error?: unknown }>()
  const knownDocument = known.find((document) => document.archive_sequence === sequence)
  const metadata = useQuery({
    queryKey: scopedQueryKey('exports', exportId, 'document', sequence),
    queryFn: () => api<ArchiveDocuments>(`/exports/${encodeURIComponent(exportId)}/documents?limit=1&offset=${sequence - 1}`),
    enabled: !knownDocument,
  })
  const document = knownDocument ?? metadata.data?.items.find((item) => item.archive_sequence === sequence)
  const previewPath = archivePdfPath(exportId, sequence)
  const previewKey = `${previewPath}:${attempt}`
  const current = preview?.key === previewKey ? preview : undefined
  useEffect(() => loadArchivePreview(previewPath, (url) => setPreview({ key: previewKey, url }), (error) => { setPreview({ key: previewKey, error }); setPlaying(false) }), [previewPath, previewKey])
  useEffect(() => {
    const pauseWhenHidden = () => { if (window.document.hidden) setPlaying(false) }
    window.document.addEventListener('visibilitychange', pauseWhenHidden)
    return () => window.document.removeEventListener('visibilitychange', pauseWhenHidden)
  }, [])
  useEffect(() => {
    if (!playing || !current?.url || loadedKey !== previewKey || window.document.hidden) return
    const timer = window.setTimeout(() => {
      if (sequence >= total) setPlaying(false)
      else setSequence((value) => value + 1)
    }, 5_000)
    return () => window.clearTimeout(timer)
  }, [playing, current?.url, loadedKey, previewKey, sequence, total])
  function move(direction: -1 | 1) {
    setPlaying(false)
    setSequence((value) => Math.max(1, Math.min(total, value + direction)))
  }
  return <Dialog open fullScreen={mobile} fullWidth maxWidth="lg" onClose={onClose} aria-labelledby="archive-pdf-title" transitionDuration={reducedMotion ? 0 : undefined}
    onKeyDown={(event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); move(event.key === 'ArrowLeft' ? -1 : 1) } }}
    slotProps={{ paper: { sx: { height: mobile ? '100dvh' : '92vh', maxHeight: mobile ? '100dvh' : '92vh', overflow: 'hidden' } } }}>
    <DialogTitle id="archive-pdf-title" component="div" sx={{ pr: 7, py: 1.5 }}>
      <Typography fontWeight={700}>PDF {sequence} / {total}</Typography>
      <Typography variant="caption" display="block" sx={{ overflowWrap: 'anywhere' }}>{document?.filename ?? 'Caricamento nome documento…'}</Typography>
      <IconButton autoFocus aria-label="Chiudi anteprima PDF" onClick={onClose} sx={{ position: 'absolute', right: 8, top: 8 }}><CloseOutlined /></IconButton>
    </DialogTitle>
    <DialogContent dividers sx={{ p: 0, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {current?.error ? <Box sx={{ p: 2 }}><ErrorState error={current.error} /><Button sx={{ mt: 1 }} onClick={() => setAttempt((value) => value + 1)}>Riprova caricamento PDF</Button></Box>
        : current?.url ? <Box component="iframe" title={`PDF conservato ${sequence} di ${total}`} src={`${current.url}#toolbar=1&navpanes=0&view=Fit`} onLoad={() => setLoadedKey(previewKey)} sx={{ border: 0, width: '100%', flex: 1, minHeight: 0 }} />
          : <LoadingState label="Caricamento del PDF conservato nello ZIP…" />}
      {metadata.error && <Box sx={{ p: 1 }}><ErrorState error={metadata.error} /></Box>}
    </DialogContent>
    <DialogActions sx={{ px: 2, py: 1.5, flexWrap: 'wrap', justifyContent: 'center', gap: .5 }}>
      <IconButton aria-label="PDF precedente" disabled={sequence <= 1} onClick={() => move(-1)}><ChevronLeftOutlined /></IconButton>
      <Button startIcon={playing ? <PauseOutlined /> : <PlayArrowOutlined />} disabled={!playing && (!current?.url || loadedKey !== previewKey || total < 2)} onClick={() => { if (!playing && sequence === total) setSequence(1); setPlaying(!playing) }}>{playing ? 'Pausa slideshow' : 'Avvia slideshow'}</Button>
      <IconButton aria-label="PDF successivo" disabled={sequence >= total} onClick={() => move(1)}><ChevronRightOutlined /></IconButton>
      <Button startIcon={<DownloadOutlined />} disabled={!current?.url || !document} onClick={() => { if (current?.url && document) downloadPreview(current.url, document.filename) }}>Scarica PDF</Button>
      <Typography variant="caption" color="text.secondary" sx={{ width: '100%', textAlign: 'center' }}>Un PDF alla volta · slideshow ogni 5 secondi · Esc per chiudere, frecce dai controlli per navigare.</Typography>
    </DialogActions>
  </Dialog>
}

export function ExportArchivePage() {
  const { exportId } = useParams()
  if (!canExport(session().user?.roles ?? [])) return <Alert severity="warning">Gli archivi sono riservati ad amministratori e revisori.</Alert>
  if (!exportId) return <ErrorState error={new Error('Identificativo archivio mancante.')} />
  return <ArchiveWorkspace key={exportId} id={exportId} />
}

function ArchiveWorkspace({ id }: { id: string }) {
  const [limit, setLimit] = useState(10)
  const [page, setPage] = useState(0)
  const [openSequence, setOpenSequence] = useState<number>()
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState<unknown>()
  const job = useQuery({ queryKey: scopedQueryKey('exports', 'detail', id), queryFn: () => api<ExportRequest>(`/exports/${encodeURIComponent(id)}`), refetchInterval: (query) => exportPollingInterval(query.state.data ? [query.state.data] : undefined) })
  const documents = useQuery({ queryKey: scopedQueryKey('exports', id, 'documents', limit, page), queryFn: () => api<ArchiveDocuments>(`/exports/${encodeURIComponent(id)}/documents?limit=${limit}&offset=${page * limit}`), enabled: job.data?.status === 'completed' })
  async function download() {
    if (!job.data) return
    setDownloading(true)
    setDownloadError(undefined)
    try { await downloadApi(`/exports/${encodeURIComponent(id)}/download`, exportArchiveFilename(job.data)) }
    catch (error) { setDownloadError(error) }
    finally { setDownloading(false) }
  }
  if (job.isLoading) return <LoadingState label="Caricamento archivio…" />
  if (job.error || !job.data) return <ErrorState error={job.error} />
  const archive = job.data
  return <>
    <PageHeader title="Apri archivio" subtitle={`${exportScopeLabel(archive.scope)} · ${exportDocumentScopeLabel(archive.document_scope)}`} actions={<Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}><Button component={Link} to="/esportazioni" startIcon={<ArrowBackOutlined />}>Storico archivi</Button><Button variant="contained" startIcon={<DownloadOutlined />} disabled={archive.status !== 'completed' || downloading} onClick={() => { void download() }}>{downloading ? 'Download…' : 'Scarica ZIP completo'}</Button></Box>} />
    <Alert severity="info" sx={{ mb: 2 }}>Apri un PDF per visualizzarlo in lightbox oppure avvia lo slideshow. I file provengono esattamente dallo ZIP di questa richiesta; le successive rianalisi non li modificano. Il browser carica un solo PDF alla volta.</Alert>
    {(isLegacyExport(archive) || documents.data?.legacy) && <Alert severity="warning" sx={{ mb: 2 }}>Archivio precedente non filtrato: può contenere evidenze tecniche e documenti parziali. L’anteprima mantiene i PDF originali dell’archivio.</Alert>}
    <Card sx={{ p: 2, mb: 2 }}><Stack spacing={1}>
      <Box><Chip label={exportStatusLabel(archive.status)} color={archive.status === 'completed' ? 'success' : archive.status === 'failed' ? 'error' : 'info'} size="small" /></Box>
      <Typography>{archive.completed_documents} / {archive.total_documents} PDF · Richiesto da {archive.requested_by} il {formatDateTime(archive.created_at)}</Typography>
      {archive.scope === 'latest_10' && <Typography variant="body2">Ultimi documenti completi per acquisizione del server, dal più recente; massimo 10.</Typography>}
      {archive.selection_metadata && <Typography variant="body2">Copertura dei documenti: {exportCoverageLabel(archive.selection_metadata)}</Typography>}
      <Typography variant="caption" sx={{ overflowWrap: 'anywhere' }}>ID {archive.id}</Typography>
      {(archive.status === 'queued' || archive.status === 'running') && <LinearProgress aria-label="Preparazione archivio" variant={archive.total_documents ? 'determinate' : 'indeterminate'} value={exportProgress(archive)} />}
      {archive.error && <ErrorState error={new Error(archive.error)} />}
      {downloadError != null && <ErrorState error={downloadError} />}
    </Stack></Card>
    {archive.status === 'completed' && <Card>
      {documents.isLoading ? <LoadingState label="Caricamento elenco PDF conservati…" /> : documents.error ? <ErrorState error={documents.error} /> : !documents.data?.items.length ? <EmptyState label="Questo archivio non contiene PDF." /> : <>
        <List aria-label="PDF conservati nell’archivio" disablePadding>{documents.data.items.map((document) => <ListItemButton key={document.archive_sequence} onClick={() => setOpenSequence(document.archive_sequence)} aria-label={`Apri PDF ${document.archive_sequence}: ${document.filename}`} divider sx={{ alignItems: 'flex-start', gap: 1.5, py: 1.5 }}>
          <PictureAsPdfOutlined color="primary" sx={{ mt: .5, flexShrink: 0 }} />
          <ListItemText disableTypography primary={<Typography fontWeight={650} sx={{ overflowWrap: 'anywhere' }}>{document.archive_sequence}. {document.filename}</Typography>} secondary={<><Typography variant="body2" color="text.secondary">{documentTypeLabel(document.type)} · {document.device_id}</Typography><Typography variant="caption" display="block">Acquisito: {formatDateTime(document.captured_at)} · {document.document_timestamp ? `Data documento: ${formatDateTime(document.document_timestamp)}` : 'Data documento non osservata'}</Typography></>} />
        </ListItemButton>)}</List>
        <TablePagination component="div" count={documents.data.total} page={page} rowsPerPage={limit} rowsPerPageOptions={[10, 25, 50]} labelRowsPerPage="PDF per pagina" onPageChange={(_, value) => { setOpenSequence(undefined); setPage(value) }} onRowsPerPageChange={(event) => { setOpenSequence(undefined); setLimit(Number(event.target.value)); setPage(0) }} />
      </>}
    </Card>}
    {openSequence !== undefined && documents.data && <ArchiveLightbox exportId={id} total={documents.data.total} initialSequence={openSequence} known={documents.data.items} onClose={() => setOpenSequence(undefined)} />}
  </>
}
