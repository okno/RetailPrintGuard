export const DISPLAY_TIME_ZONE = 'Europe/Rome'

export const shortDateTime = new Intl.DateTimeFormat('it-IT', {
  dateStyle: 'short',
  timeStyle: 'short',
  timeZone: DISPLAY_TIME_ZONE,
})

export const mediumDateTime = new Intl.DateTimeFormat('it-IT', {
  dateStyle: 'short',
  timeStyle: 'medium',
  timeZone: DISPLAY_TIME_ZONE,
})

export function formatDateTime(value?: string | Date): string {
  if (!value) return 'Mai registrata'
  const parsed = value instanceof Date ? value : new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Data non valida' : mediumDateTime.format(parsed)
}
