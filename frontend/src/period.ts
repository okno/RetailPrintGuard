import { DISPLAY_TIME_ZONE } from './format'

export type PeriodPreset = 'all' | 'today' | 'yesterday' | 'week' | 'custom'

type CalendarParts = { year: number; month: number; day: number; hour: number; minute: number }

const partsFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: DISPLAY_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

function parts(value: Date): CalendarParts {
  const result = Object.fromEntries(
    partsFormatter.formatToParts(value).map((part) => [part.type, part.value]),
  )
  return {
    year: Number(result.year),
    month: Number(result.month),
    day: Number(result.day),
    hour: Number(result.hour),
    minute: Number(result.minute),
  }
}

function shifted(value: CalendarParts, days: number): CalendarParts {
  const date = new Date(Date.UTC(value.year, value.month - 1, value.day + days))
  return {
    year: date.getUTCFullYear(),
    month: date.getUTCMonth() + 1,
    day: date.getUTCDate(),
    hour: value.hour,
    minute: value.minute,
  }
}

function zonedToUtc(value: CalendarParts): Date {
  const wallClock = Date.UTC(
    value.year,
    value.month - 1,
    value.day,
    value.hour,
    value.minute,
  )
  let candidate = wallClock
  // Two passes resolve the Europe/Rome offset, including DST boundaries.
  for (let index = 0; index < 2; index += 1) {
    const observed = parts(new Date(candidate))
    const observedClock = Date.UTC(
      observed.year,
      observed.month - 1,
      observed.day,
      observed.hour,
      observed.minute,
    )
    candidate -= observedClock - wallClock
  }
  return new Date(candidate)
}

export function presetPeriod(
  preset: Exclude<PeriodPreset, 'all' | 'custom'>,
  now = new Date(),
) {
  const current = parts(now)
  const today = { ...current, hour: 0, minute: 0 }
  const start = preset === 'today' ? today : preset === 'yesterday' ? shifted(today, -1) : shifted(today, -6)
  const end = preset === 'yesterday' ? today : shifted(today, 1)
  return { from: zonedToUtc(start).toISOString(), to: zonedToUtc(end).toISOString() }
}

export function utcFromRomeInput(value: string): string | undefined {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) return undefined
  return zonedToUtc({
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
  }).toISOString()
}

export function romeInputFromUtc(value?: string | null): string {
  if (!value) return ''
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return ''
  const valueParts = parts(parsed)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${valueParts.year}-${pad(valueParts.month)}-${pad(valueParts.day)}T${pad(valueParts.hour)}:${pad(valueParts.minute)}`
}

export function apiPeriodParams(
  source: URLSearchParams,
  defaultPreset: PeriodPreset = 'all',
) {
  const result = new URLSearchParams(source)
  const preset = (result.get('period') as PeriodPreset | null) ?? defaultPreset
  result.delete('period')
  if (!result.has('from') && !result.has('to') && !['all', 'custom'].includes(preset)) {
    const values = presetPeriod(preset as 'today' | 'yesterday' | 'week')
    result.set('from', values.from)
    result.set('to', values.to)
  }
  return result
}
