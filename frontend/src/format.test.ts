import { describe, expect, it } from 'vitest'
import { DISPLAY_TIME_ZONE, formatDateTime, mediumDateTime } from './format'

describe('enterprise display timezone', () => {
  it('always renders timestamps in Europe/Rome instead of the browser timezone', () => {
    expect(DISPLAY_TIME_ZONE).toBe('Europe/Rome')
    expect(mediumDateTime.resolvedOptions().timeZone).toBe('Europe/Rome')
    expect(formatDateTime('2026-08-14T12:00:00Z')).toContain('14:00:00')
  })
})
