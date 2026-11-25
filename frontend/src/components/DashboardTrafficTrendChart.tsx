import { Box, Card, CardContent, Chip, Stack, Table, TableBody, TableCell, TableContainer, TableHead, TablePagination, TableRow, Typography } from '@mui/material'
import { useTheme } from '@mui/material/styles'
import { useState } from 'react'
import type { DashboardTrafficTrend, DashboardTrafficTrendBucket } from '../types'

type SeriesKey = 'covers' | 'bar' | 'kitchen' | 'pizzeria'

const SERIES: Array<{ key: SeriesKey; label: string }> = [
  { key: 'covers', label: 'Coperti osservati' },
  { key: 'bar', label: 'Asporti BAR' },
  { key: 'kitchen', label: 'Asporti CUCINA' },
  { key: 'pizzeria', label: 'Asporti PIZZERIA' },
]

function count(bucket: DashboardTrafficTrendBucket, series: SeriesKey): number {
  return series === 'covers' ? bucket.covers : bucket.takeaways[series]
}

function unit(series: SeriesKey, value: number): string {
  return series === 'covers' ? (value === 1 ? 'persona' : 'persone') : (value === 1 ? 'ordine' : 'ordini')
}

function axisMaximum(buckets: DashboardTrafficTrendBucket[]): { maximum: number; step: number } {
  let peak = 0
  for (const bucket of buckets) {
    for (const { key } of SERIES) peak = Math.max(peak, count(bucket, key))
  }
  if (peak <= 4) return { maximum: Math.max(1, peak), step: 1 }
  const rough = peak / 4
  const power = 10 ** Math.floor(Math.log10(rough))
  const step = [1, 2, 5, 10].map((factor) => factor * power).find((candidate) => candidate >= rough) ?? power * 10
  return { maximum: Math.ceil(peak / step) * step, step }
}

function formatBucket(start: string, granularity: DashboardTrafficTrend['granularity'], timezone: string, long = false, showYear = false): string {
  const date = new Date(start)
  if (Number.isNaN(date.getTime())) return start
  return new Intl.DateTimeFormat('it-IT', {
    timeZone: timezone,
    day: '2-digit',
    month: long ? 'long' : '2-digit',
    ...(long ? { year: 'numeric' as const } : showYear ? { year: '2-digit' as const } : {}),
    ...(granularity === 'hour' ? {
      hour: '2-digit' as const,
      minute: '2-digit' as const,
      timeZoneName: long ? 'shortOffset' as const : 'short' as const,
    } : {}),
  }).format(date)
}

function tickIndexes(length: number): number[] {
  if (length <= 1) return length ? [0] : []
  const intervals = Math.min(6, length - 1)
  return [...new Set(Array.from({ length: intervals + 1 }, (_, index) => Math.round(index * (length - 1) / intervals)))]
}

export function DashboardTrafficTrendChart({ trend }: { trend: DashboardTrafficTrend }) {
  const theme = useTheme()
  const [page, setPage] = useState(0)
  const [rowsPerPage, setRowsPerPage] = useState(50)
  const colors: Record<SeriesKey, string> = {
    covers: theme.palette.mode === 'dark' ? '#d4dee8' : '#34495e',
    bar: theme.palette.mode === 'dark' ? '#5ad2dd' : '#087e8b',
    kitchen: theme.palette.mode === 'dark' ? '#f5b75f' : '#ba6714',
    pizzeria: theme.palette.mode === 'dark' ? '#c7a5f8' : '#7b4db3',
  }
  const { buckets, granularity, timezone } = trend
  const safePage = Math.min(page, Math.max(0, Math.ceil(buckets.length / rowsPerPage) - 1))
  const timestamps = buckets.map((bucket) => Date.parse(bucket.start))
  const yearFormatter = new Intl.DateTimeFormat('en-US', { timeZone: timezone, year: 'numeric' })
  const showYearOnAxis = buckets.length > 1
    && yearFormatter.format(new Date(buckets[0]!.start)) !== yearFormatter.format(new Date(buckets[buckets.length - 1]!.start))
  const width = 1000
  const height = 360
  const left = 56
  const right = 22
  const top = 18
  const bottom = 62
  const plotWidth = width - left - right
  const plotHeight = height - top - bottom
  const { maximum, step } = axisMaximum(buckets)
  const firstTime = timestamps[0] ?? NaN
  const lastTime = timestamps[timestamps.length - 1] ?? NaN
  const x = (index: number) => left + (buckets.length === 1 ? plotWidth / 2
    : Number.isFinite(firstTime) && Number.isFinite(lastTime) && lastTime > firstTime
      ? (timestamps[index]! - firstTime) * plotWidth / (lastTime - firstTime)
      : index * plotWidth / (buckets.length - 1))
  const y = (value: number) => top + plotHeight * (1 - value / maximum)
  const label = granularity === 'hour' ? 'Ore' : 'Giorni'
  const gapThreshold = granularity === 'hour' ? 90 * 60_000 : 26 * 60 * 60_000
  const seriesPath = (series: SeriesKey) => buckets.map((bucket, index) => {
    const previous = index ? buckets[index - 1] : undefined
    const gap = !trend.zero_filled && previous && timestamps[index]! - timestamps[index - 1]! > gapThreshold
    return `${index && !gap ? 'L' : 'M'} ${x(index).toFixed(1)} ${y(count(bucket, series)).toFixed(1)}`
  }).join(' ')

  return <Card>
    <CardContent>
      <Typography variant="h2">Andamento di coperti e asporti</Typography>
      <Typography color="text.secondary" sx={{ mt: 0.5 }}>
        Coperti osservati complessivi (persone) e ordini da asporto per reparto, nel periodo selezionato. Un ordine che include più reparti compare in ciascuno di essi: le serie BAR, CUCINA e PIZZERIA non si sommano tra loro.
      </Typography>
      {!trend.zero_filled && <Typography color="warning.main" variant="body2" sx={{ mt: 1 }}>
        Periodo molto esteso: gli intervalli senza osservazioni non sono riportati. I tratti del grafico si interrompono in corrispondenza dei giorni o delle ore mancanti.
      </Typography>}
      <Stack direction="row" flexWrap="wrap" gap={1} sx={{ mt: 1.75, mb: 1 }} aria-label="Legenda del grafico">
        {SERIES.map(({ key, label: seriesLabel }) => <Chip
          key={key}
          size="small"
          variant="outlined"
          label={`${seriesLabel}: ${key === 'covers' ? trend.covers_total : trend.takeaways_total[key]} ${unit(key, key === 'covers' ? trend.covers_total : trend.takeaways_total[key])}`}
          sx={{ borderColor: colors[key], color: colors[key], fontWeight: 700 }}
        />)}
      </Stack>
      {buckets.length ? <Box sx={{ overflowX: 'auto' }}>
        <svg
          role="img"
          aria-label={`Grafico di coperti osservati e ordini da asporto per BAR, CUCINA e PIZZERIA. Asse X: ${label.toLowerCase()}. Asse Y: quantità di persone o ordini.`}
          viewBox={`0 0 ${width} ${height}`}
          style={{ display: 'block', width: '100%', minWidth: 700, height: 'auto' }}
        >
          <text x={left} y={12} fill={theme.palette.text.secondary} fontSize="12">Quantità · persone / ordini</text>
          {Array.from({ length: Math.floor(maximum / step) + 1 }, (_, index) => index * step).map((value) => <g key={value}>
            <line x1={left} x2={width - right} y1={y(value)} y2={y(value)} stroke={theme.palette.divider} strokeWidth="1" />
            <text x={left - 10} y={y(value) + 4} textAnchor="end" fill={theme.palette.text.secondary} fontSize="12">{value}</text>
          </g>)}
          {tickIndexes(buckets.length).map((index) => <g key={index}>
            <line x1={x(index)} x2={x(index)} y1={top + plotHeight} y2={top + plotHeight + 5} stroke={theme.palette.text.secondary} />
            <text
              x={x(index)} y={top + plotHeight + 22}
              textAnchor={index === 0 ? 'start' : index === buckets.length - 1 ? 'end' : 'middle'}
              fill={theme.palette.text.secondary} fontSize="12"
            >{formatBucket(buckets[index]!.start, granularity, timezone, false, showYearOnAxis)}</text>
          </g>)}
          {SERIES.map(({ key }) => <g key={key}>
            <path d={seriesPath(key)} fill="none" stroke={colors[key]} strokeWidth={key === 'covers' ? 3.5 : 2.5} strokeDasharray={key === 'covers' ? undefined : key === 'bar' ? undefined : key === 'kitchen' ? '8 4' : '2 4'} strokeLinejoin="round" />
            {buckets.length <= 80 && buckets.map((bucket, index) => <circle key={`${key}-${bucket.start}`} cx={x(index)} cy={y(count(bucket, key))} r="3" fill={colors[key]}>
              <title>{`${SERIES.find((item) => item.key === key)?.label}: ${count(bucket, key)} ${unit(key, count(bucket, key))} · ${formatBucket(bucket.start, granularity, timezone, true)}`}</title>
            </circle>)}
          </g>)}
          <text x={width / 2} y={height - 10} textAnchor="middle" fill={theme.palette.text.secondary} fontSize="13">{label} · {timezone}</text>
        </svg>
      </Box> : <Typography color="text.secondary" sx={{ py: 3 }}>Nessun dato nel periodo selezionato.</Typography>}
      {buckets.length > 0 && <Box component="details" sx={{ mt: 1.5 }}>
        <Box component="summary" sx={{ cursor: 'pointer', color: 'primary.main', fontWeight: 650 }}>Mostra i valori per {granularity === 'hour' ? 'ora' : 'giorno'}</Box>
        <TableContainer sx={{ maxHeight: 340, mt: 1 }}>
          <Table size="small" stickyHeader aria-label="Valori del grafico coperti e asporti">
            <TableHead><TableRow>
              <TableCell>{granularity === 'hour' ? 'Ora' : 'Giorno'}</TableCell>
              <TableCell align="right">Coperti (persone)</TableCell>
              <TableCell align="right">BAR (ordini)</TableCell>
              <TableCell align="right">CUCINA (ordini)</TableCell>
              <TableCell align="right">PIZZERIA (ordini)</TableCell>
            </TableRow></TableHead>
            <TableBody>{buckets.slice(safePage * rowsPerPage, (safePage + 1) * rowsPerPage).map((bucket) => <TableRow key={bucket.start}>
              <TableCell>{formatBucket(bucket.start, granularity, timezone, true)}</TableCell>
              <TableCell align="right">{bucket.covers}</TableCell>
              <TableCell align="right">{bucket.takeaways.bar}</TableCell>
              <TableCell align="right">{bucket.takeaways.kitchen}</TableCell>
              <TableCell align="right">{bucket.takeaways.pizzeria}</TableCell>
            </TableRow>)}</TableBody>
          </Table>
        </TableContainer>
        <TablePagination
          component="div"
          count={buckets.length}
          page={safePage}
          rowsPerPage={rowsPerPage}
          rowsPerPageOptions={[50, 100, 250]}
          labelRowsPerPage="Righe per pagina"
          labelDisplayedRows={({ from, to, count: total }) => `${from}–${to} di ${total}`}
          onPageChange={(_event, nextPage) => setPage(nextPage)}
          onRowsPerPageChange={(event) => { setRowsPerPage(Number(event.target.value)); setPage(0) }}
        />
      </Box>}
    </CardContent>
  </Card>
}
