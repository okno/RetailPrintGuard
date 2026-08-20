import { describe, expect, it } from 'vitest'
import {
  DISPLAY_TIME_ZONE,
  formatDateTime,
  formatDocumentDateTime,
  mediumDateTime,
} from './format'

describe('enterprise display timezone', () => {
  it('always renders timestamps in Europe/Rome instead of the browser timezone', () => {
    expect(DISPLAY_TIME_ZONE).toBe('Europe/Rome')
    expect(mediumDateTime.resolvedOptions().timeZone).toBe('Europe/Rome')
    expect(formatDateTime('2026-08-14T12:00:00Z')).toContain('14:00:00')
  })

  it('does not invent seconds when the printer exposed minute precision only', () => {
    expect(formatDocumentDateTime('2026-08-20T00:44:00Z', 'MINUTE')).toContain('02:44')
    expect(formatDocumentDateTime('2026-08-20T00:44:00Z', 'MINUTE')).not.toContain('02:44:00')
    expect(formatDocumentDateTime('2026-08-20T00:44:07Z', 'SECOND')).toContain('02:44:07')
  })
})
