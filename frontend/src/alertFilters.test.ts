import { describe, expect, it } from 'vitest'
import { alertApiSearchParams } from './alertFilters'

describe('alert workbench filters', () => {
  it('shows only operational alerts by default', () => {
    const result = alertApiSearchParams(new URLSearchParams(), 25, 0)
    expect(result.get('view')).toBe('operational')
    expect(result.get('limit')).toBe('25')
    expect(result.get('offset')).toBe('0')
  })

  it('preserves an explicit archive or all-evidence view', () => {
    expect(
      alertApiSearchParams(new URLSearchParams('view=archive'), 50, 100).get('view'),
    ).toBe('archive')
    expect(
      alertApiSearchParams(new URLSearchParams('view=all'), 50, 100).get('view'),
    ).toBe('all')
  })
})
