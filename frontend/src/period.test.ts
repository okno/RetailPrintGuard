import { describe, expect, it } from 'vitest'
import { apiPeriodParams, presetPeriod, romeInputFromUtc, utcFromRomeInput } from './period'

describe('period filters in Europe/Rome', () => {
  it('builds half-open UTC days with the seasonal Rome offset', () => {
    expect(presetPeriod('today', new Date('2026-01-15T12:00:00Z'))).toEqual({
      from: '2026-01-14T23:00:00.000Z',
      to: '2026-01-15T23:00:00.000Z',
    })
    expect(presetPeriod('today', new Date('2026-08-15T12:00:00Z'))).toEqual({
      from: '2026-08-14T22:00:00.000Z',
      to: '2026-08-15T22:00:00.000Z',
    })
  })

  it('converts custom Rome values without depending on the browser timezone', () => {
    expect(utcFromRomeInput('2026-08-15T12:30')).toBe('2026-08-15T10:30:00.000Z')
    expect(romeInputFromUtc('2026-08-15T10:30:00.000Z')).toBe('2026-08-15T12:30')
  })

  it('removes UI-only state and applies the requested default preset', () => {
    const params = new URLSearchParams('q=tavolo&period=today')
    const result = apiPeriodParams(params, 'today')
    expect(result.get('period')).toBeNull()
    expect(result.get('q')).toBe('tavolo')
    expect(result.get('from')).toMatch(/Z$/)
    expect(result.get('to')).toMatch(/Z$/)
  })
})
