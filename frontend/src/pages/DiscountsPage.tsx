import { ManageSearchOutlined, OpenInNewOutlined, PercentOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, Chip, FormControl, Grid, InputLabel, MenuItem, Select, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey, session } from '../api/client'
import { canAnalyze, currentDiscountResults, type AnalysisResult, type AnalysisRun, type CurrentDiscountResults, type DiscountResults, type DiscountSummary } from '../analyses'
import { PageHeader } from '../components/PageHeader'
import { PeriodPicker } from '../components/PeriodPicker'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import { StatCard } from '../components/StatCard'
import { deviceLabel, documentTypeLabel } from '../documentPresentation'
import { discountResultEndpoint, discountResultQuery } from '../discountFilters'
import { discountKindLabel, discountMoney, discountReasonLabel } from '../discountPresentation'
import { formatDateTime } from '../format'
import { analysisDetailPath, documentDetailPath } from '../routes'
import type { Device } from '../types'

function DiscountSummaryCards({ summary, snapshot }: { summary: DiscountSummary; snapshot: boolean }) {
  return <>
    <Typography variant="h2" sx={{ mb: 1 }}>{snapshot ? 'Riepilogo dell’intera rianalisi' : 'Riepilogo della selezione'}</Typography>
    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{summary.documents_with_discounts} documenti con sconti{summary.observed_rows !== undefined && ` · ${summary.observed_rows} righe osservate`}. {snapshot ? 'I filtri dell’elenco sottostante non modificano questi totali né i report.' : 'I totali comprendono tutti gli sconti dei documenti selezionati, anche di tipo diverso dal filtro.'}</Typography>
    <Grid container spacing={2} sx={{ mb: 2 }}>
      <Grid size={{ xs: 12, md: 4 }}><StatCard label="Dipendenti 30% esplicito · osservati" value={discountMoney(summary.employee_total)} icon={<PercentOutlined />} helper={summary.employee_document_count === undefined ? undefined : `${summary.employee_document_count} documenti distinti`} /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><StatCard label="Dipendenti 30% · documento riconciliato" value={discountMoney(summary.employee_arithmetic_verified_total)} icon={<PercentOutlined />} helper={summary.arithmetic_verified_employee_document_count === undefined ? 'Verifica aritmetica del documento; non autorizzazione' : `${summary.arithmetic_verified_employee_document_count} documenti · non equivale ad autorizzazione`} /></Grid>
      <Grid size={{ xs: 12, md: 4 }}><StatCard label="30% generico · osservati" value={discountMoney(summary.generic_30_total)} icon={<PercentOutlined />} helper={summary.generic_30_arithmetic_verified_total !== undefined ? `Con aritmetica verificata: ${discountMoney(summary.generic_30_arithmetic_verified_total)}` : summary.generic_30_document_count !== undefined ? `${summary.generic_30_document_count} documenti · nessuna attribuzione ai dipendenti` : undefined} /></Grid>
    </Grid>
    {snapshot && <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Altri sconti osservati: {discountMoney(summary.other_discount_total)} · Righe con percentuale verificata: {summary.arithmetic_verified_rows ?? 'Non disponibile'} · Da verificare: {summary.arithmetic_unverified_rows ?? 'Non disponibile'}</Typography>}
  </>
}

function ObservationDetails({ result }: { result: AnalysisResult }) {
  const [open, setOpen] = useState(false)
  return <Box component="details" onToggle={(event) => setOpen(event.currentTarget.open)} sx={{ mt: 1 }}>
    <Typography component="summary" variant="body2" sx={{ cursor: 'pointer' }}>Righe osservate ({result.observations.length})</Typography>
    {open && <Stack spacing={1.5} sx={{ mt: 1.5 }}>
      {result.observations.map((observation, index) => <Box key={`${observation.sequence}-${index}`} sx={{ borderLeft: '3px solid', borderColor: 'divider', pl: 1.5 }}>
        <Typography variant="body2" fontWeight={650}>Riga {observation.sequence} · {discountKindLabel(observation.kind)} · {discountMoney(observation.amount)}</Typography>
        <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{observation.label}</Typography>
        <Typography variant="caption" display="block">Percentuale: {observation.rate_percent === null ? 'Non osservata' : `${observation.rate_percent}%`} · Base: {discountMoney(observation.basis)}</Typography>
        <Typography variant="caption" display="block" color={observation.arithmetic_verified ? 'text.secondary' : 'warning.main'}>{observation.arithmetic_verified ? 'Percentuale e importo riconciliati sulla riga.' : 'Percentuale o base della riga da verificare.'}</Typography>
        {observation.source_text && <Typography component="pre" variant="caption" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontFamily: 'monospace', mb: 0 }}>{observation.source_text}</Typography>}
      </Box>)}
      <Typography variant="caption" color="text.secondary" sx={{ overflowWrap: 'anywhere' }}>Versione documento analizzata: {result.document_version_id}</Typography>
    </Stack>}
  </Box>
}

export function DiscountsPage() {
  const [params, setParams] = useSearchParams()
  const analysisId = params.get('analysis_id')
  const allowedSnapshot = !analysisId || canAnalyze(session().user?.roles ?? [])
  const request = discountResultQuery(params)
  const limit = Number(request.get('limit'))
  const offset = Number(request.get('offset'))
  const query = useQuery({
    queryKey: scopedQueryKey('discounts', analysisId, request.toString()),
    queryFn: async () => analysisId
      ? api<DiscountResults>(discountResultEndpoint(params))
      : currentDiscountResults(await api<CurrentDiscountResults>(discountResultEndpoint(params))),
    enabled: allowedSnapshot,
  })
  const analysis = useQuery({
    queryKey: scopedQueryKey('analyses', 'detail', analysisId),
    queryFn: () => api<AnalysisRun>(`/analyses/${encodeURIComponent(analysisId ?? '')}`),
    enabled: Boolean(analysisId) && allowedSnapshot,
  })
  const devices = useQuery({ queryKey: scopedQueryKey('devices'), queryFn: () => api<Device[]>('/devices'), staleTime: 30_000 })
  function update(name: string, value: string) {
    const next = new URLSearchParams(params)
    if (name === 'kind' && value === '__all_evidence') {
      next.delete('kind')
      next.set('with_discounts', 'false')
    } else {
      value ? next.set(name, value) : next.delete(name)
      if (name === 'kind') next.delete('with_discounts')
    }
    next.set('offset', '0')
    setParams(next)
  }
  if (!allowedSnapshot) return <Alert severity="warning">I risultati delle rianalisi sono riservati ad amministratori e revisori.</Alert>
  const summary = analysisId ? analysis.data?.summary : query.data?.summary
  return <>
    <PageHeader title="Sconti" subtitle={analysisId ? 'Risultati conservati della rianalisi selezionata.' : 'Sconti osservati nelle interpretazioni attuali dei documenti.'} actions={canAnalyze(session().user?.roles ?? []) ? <Button component={Link} to={analysisId ? analysisDetailPath(analysisId) : '/rianalisi'} startIcon={<ManageSearchOutlined />}>{analysisId ? 'Rianalisi' : 'Rianalizza'}</Button> : undefined} />
    <Alert severity="info" sx={{ mb: 2 }}>La dicitura “dipendenti” deriva dal testo osservato; il 30% generico resta separato. La verifica aritmetica non prova l’autorizzazione e non modifica gli alert antifrode. Gli importi riguardano documenti: stampe diverse possono appartenere alla stessa vendita.</Alert>
    {analysisId && <Alert severity="info" sx={{ mb: 2 }}>Risultati congelati della richiesta {analysisId}. I collegamenti ai documenti aprono l’interpretazione attualmente disponibile. <Button component={Link} to="/sconti">Consulta risultati attuali</Button></Alert>}
    {analysis.error && <Box sx={{ mb: 2 }}><ErrorState error={analysis.error} /></Box>}
    {summary && <DiscountSummaryCards summary={summary} snapshot={Boolean(analysisId)} />}
    {analysisId && Boolean(summary?.documents_without_raw_reprocessing) && <Alert severity="warning" sx={{ mb: 2 }}>{summary?.documents_without_raw_reprocessing} documenti dell’intera rianalisi mantengono la precedente interpretazione per assenza del RAW richiesta. Le righe interessate sono segnalate nell’elenco.</Alert>}
    <Card sx={{ p: 2, mb: 2 }}>
      <Typography variant="h2" sx={{ mb: 2 }}>Filtri di consultazione</Typography>
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))', lg: 'repeat(4, minmax(0, 1fr))' }, gap: 2 }}>
        <PeriodPicker params={params} onChange={setParams} />
        <FormControl size="small" disabled={devices.isLoading || Boolean(devices.error)}>
          <InputLabel id="discount-device-label">Dispositivo</InputLabel>
          <Select labelId="discount-device-label" label="Dispositivo" value={params.get('device_id') ?? ''} onChange={(event) => update('device_id', event.target.value)}>
            <MenuItem value="">Tutti i dispositivi</MenuItem>
            {devices.data?.map((device) => <MenuItem key={device.id} value={device.id}>{deviceLabel(device.id, devices.data)}</MenuItem>)}
          </Select>
        </FormControl>
        <FormControl size="small">
          <InputLabel id="discount-kind-label">Tipo di sconto</InputLabel>
          <Select labelId="discount-kind-label" label="Tipo di sconto" value={params.get('kind') || (analysisId && params.get('with_discounts') === 'false' ? '__all_evidence' : '')} onChange={(event) => update('kind', event.target.value)}>
            <MenuItem value="">Tutti gli sconti</MenuItem>
            {analysisId && <MenuItem value="__all_evidence">Tutte le evidenze analizzate</MenuItem>}
            <MenuItem value="employee_30">Dipendenti 30% esplicito</MenuItem>
            <MenuItem value="generic_30">30% generico</MenuItem>
            <MenuItem value="other">Altri sconti</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small">
          <InputLabel id="discount-reconciliation-label">Aritmetica del documento</InputLabel>
          <Select labelId="discount-reconciliation-label" label="Aritmetica del documento" value={params.get('reconciliation') === 'unverified' ? 'review' : params.get('reconciliation') ?? ''} onChange={(event) => update('reconciliation', event.target.value)}>
            <MenuItem value="">Tutti gli esiti</MenuItem><MenuItem value="verified">Documento riconciliato</MenuItem><MenuItem value="review">Da verificare</MenuItem>
          </Select>
        </FormControl>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>Il periodo si riferisce all’acquisizione del server, in ora italiana, con estremo finale escluso. I filtri selezionano i documenti; il dettaglio mantiene tutte le righe di sconto del documento.</Typography>
      {devices.error && <Alert severity="warning" sx={{ mt: 1 }}>Elenco dispositivi non disponibile. Gli altri filtri restano utilizzabili.</Alert>}
    </Card>
    <Card>
      {query.isLoading ? <LoadingState label="Caricamento evidenze degli sconti…" /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessun documento corrisponde ai filtri selezionati." /> : <>
        <TableContainer><Table aria-label="Documenti con sconti">
          <TableHead><TableRow><TableCell>Documento e date</TableCell><TableCell>Dispositivo</TableCell><TableCell>Importi osservati</TableCell><TableCell>Verifica ed evidenze</TableCell><TableCell>Documento</TableCell></TableRow></TableHead>
          <TableBody>{query.data.items.map((result) => <TableRow key={`${result.document_id}-${result.document_version_id}`}>
            <TableCell sx={{ minWidth: 200, maxWidth: 290 }}><Typography fontWeight={650}>{documentTypeLabel(result.document_type)}</Typography><Typography variant="body2">Acquisito: {formatDateTime(result.captured_at)}</Typography><Typography variant="caption" display="block">{result.document_timestamp ? `Data documento: ${formatDateTime(result.document_timestamp)}` : 'Data documento non osservata'}</Typography>{!result.complete && <Chip size="small" color="warning" label="Documento incompleto" sx={{ mt: .5 }} />}{result.reprocessed_from_raw === false && <Typography variant="caption" display="block" color="warning.main" sx={{ mt: .5 }}>Interpretazione precedente: RAW richiesta non disponibile.</Typography>}</TableCell>
            <TableCell sx={{ minWidth: 150 }}><Typography variant="body2">{result.device_name || deviceLabel(result.device_id, devices.data)}</Typography></TableCell>
            <TableCell sx={{ minWidth: 210 }}><Typography variant="body2">Dipendenti 30%: {discountMoney(result.employee_total)}</Typography><Typography variant="body2">30% generico: {discountMoney(result.generic_30_total)}</Typography><Typography variant="body2">Altri sconti: {discountMoney(result.other_discount_total)}</Typography></TableCell>
            <TableCell sx={{ minWidth: 320, maxWidth: 520 }}><Chip size="small" variant="outlined" color={result.arithmetic_reconciled ? 'success' : 'warning'} label={result.arithmetic_reconciled ? 'Documento riconciliato' : 'Documento da verificare'} />{result.reasons.map((reason) => <Typography key={reason} variant="caption" display="block" sx={{ mt: .5 }}>{discountReasonLabel(reason)}</Typography>)}{result.observations.length ? <ObservationDetails result={result} /> : <Typography variant="body2" sx={{ mt: 1 }}>Nessuna riga di sconto osservata.</Typography>}</TableCell>
            <TableCell sx={{ minWidth: 160 }}><Button component={Link} to={documentDetailPath(result.document_id)} startIcon={<OpenInNewOutlined />}>{analysisId ? 'Documento attuale' : 'Apri documento'}</Button></TableCell>
          </TableRow>)}</TableBody>
        </Table></TableContainer>
        <TablePagination component="div" count={query.data.total} page={Math.floor(offset / limit)} rowsPerPage={limit} rowsPerPageOptions={[25, 50, 100]} labelRowsPerPage={analysisId ? 'Evidenze per pagina' : 'Documenti per pagina'} onPageChange={(_, page) => { const next = new URLSearchParams(params); next.set('offset', String(page * limit)); setParams(next) }} onRowsPerPageChange={(event) => { const next = new URLSearchParams(params); next.set('limit', event.target.value); next.set('offset', '0'); setParams(next) }} />
      </>}
    </Card>
  </>
}
