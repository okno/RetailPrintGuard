import { Box, FormControl, InputLabel, MenuItem, Select, TextField } from '@mui/material'
import {
  presetPeriod,
  romeInputFromUtc,
  utcFromRomeInput,
  type PeriodPreset,
} from '../period'

export function PeriodPicker({
  params,
  onChange,
  defaultPreset = 'all',
}: {
  params: URLSearchParams
  onChange: (next: URLSearchParams) => void
  defaultPreset?: PeriodPreset
}) {
  const inferred = params.has('from') || params.has('to') ? 'custom' : defaultPreset
  const preset = (params.get('period') as PeriodPreset | null) ?? inferred

  function setPreset(value: PeriodPreset) {
    const next = new URLSearchParams(params)
    next.set('period', value)
    next.set('offset', '0')
    if (value === 'all') {
      next.delete('from')
      next.delete('to')
    } else if (value !== 'custom') {
      const period = presetPeriod(value)
      next.set('from', period.from)
      next.set('to', period.to)
    } else if (!next.has('from') && !next.has('to')) {
      const period = presetPeriod('today')
      next.set('from', period.from)
      next.set('to', period.to)
    }
    onChange(next)
  }

  function setBoundary(name: 'from' | 'to', value: string) {
    const next = new URLSearchParams(params)
    const utc = utcFromRomeInput(value)
    utc ? next.set(name, utc) : next.delete(name)
    next.set('period', 'custom')
    next.set('offset', '0')
    onChange(next)
  }

  return <Box sx={{ display: 'contents' }}>
    <FormControl size="small">
      <InputLabel>Periodo</InputLabel>
      <Select
        label="Periodo"
        value={preset}
        onChange={(event) => setPreset(event.target.value as PeriodPreset)}
      >
        <MenuItem value="all">Tutto</MenuItem>
        <MenuItem value="today">Oggi</MenuItem>
        <MenuItem value="yesterday">Ieri</MenuItem>
        <MenuItem value="week">Ultimi 7 giorni</MenuItem>
        <MenuItem value="custom">Personalizzato</MenuItem>
      </Select>
    </FormControl>
    {preset === 'custom' && <>
      <TextField
        size="small"
        type="datetime-local"
        label="Da (ora italiana)"
        value={romeInputFromUtc(params.get('from'))}
        onChange={(event) => setBoundary('from', event.target.value)}
        slotProps={{ inputLabel: { shrink: true } }}
      />
      <TextField
        size="small"
        type="datetime-local"
        label="A (escluso, ora italiana)"
        value={romeInputFromUtc(params.get('to'))}
        onChange={(event) => setBoundary('to', event.target.value)}
        slotProps={{ inputLabel: { shrink: true } }}
      />
    </>}
  </Box>
}
