import { Chip, type ChipProps } from '@mui/material'

const danger = new Set(['CRITICAL', 'OPEN', 'OFFLINE', 'FAILED', 'INCOMPLETE'])
const warning = new Set(['HIGH', 'UNDER_REVIEW', 'DEGRADED', 'PARTIAL'])
const success = new Set(['ONLINE', 'COMPLETE', 'CLOSED', 'JUSTIFIED', 'CONFIRMED'])

export function statusColor(value: string): ChipProps['color'] {
  const normalized = value.toUpperCase()
  return danger.has(normalized)
    ? 'error'
    : warning.has(normalized)
      ? 'warning'
      : success.has(normalized)
        ? 'success'
        : 'default'
}

export function StatusChip({ value }: { value: string }) {
  return <Chip label={value.replaceAll('_', ' ')} color={statusColor(value)} size="small" variant="outlined" />
}
