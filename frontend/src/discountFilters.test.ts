import { describe, expect, it } from 'vitest'
import { discountResultEndpoint, discountResultQuery } from './discountFilters'

describe('discount result consultation', () => {
  it('keeps UTC boundaries and filters without sending UI state to the API', () => {
    const source = new URLSearchParams({ period: 'custom', from: '2042-10-25T00:00:00+02:00', to: '2042-10-26T00:00:00+01:00', device_id: 'synthetic', kind: 'employee_30', reconciliation: 'unverified', analysis_id: 'run/one', offset: '25', limit: '25' })
    const query = discountResultQuery(source)
    expect(query.get('from')).toBe(source.get('from'))
    expect(query.get('to')).toBe(source.get('to'))
    expect(query.get('reconciliation')).toBe('unverified')
    expect(query.has('period')).toBe(false)
    expect(query.has('analysis_id')).toBe(false)
    expect(discountResultEndpoint(source)).toContain('/analyses/run%2Fone/results?')
  })

  it('uses all-history current results without artificial bounds and bounds pagination', () => {
    const query = discountResultQuery(new URLSearchParams('period=all&limit=999999&offset=-1'))
    expect(Object.fromEntries(query)).toEqual({ limit: '25', offset: '0' })
    expect(discountResultEndpoint(new URLSearchParams())).toBe('/discounts?limit=25&offset=0')
  })

  it('maps review filters to the current and immutable-result API contracts', () => {
    expect(discountResultQuery(new URLSearchParams('reconciliation=review')).get('reconciliation')).toBe('review')
    expect(discountResultQuery(new URLSearchParams('reconciliation=review&analysis_id=synthetic')).get('reconciliation')).toBe('unverified')
    expect(discountResultQuery(new URLSearchParams('kind=all&reconciliation=all')).has('kind')).toBe(false)
  })

  it('defaults snapshots to discounts while leaving all evidence explicitly accessible', () => {
    expect(discountResultQuery(new URLSearchParams('analysis_id=synthetic')).get('with_discounts')).toBe('true')
    const all = discountResultQuery(new URLSearchParams('analysis_id=synthetic&with_discounts=false'))
    expect(all.get('with_discounts')).toBe('false')
    expect(all.has('kind')).toBe(false)
    expect(discountResultQuery(new URLSearchParams('analysis_id=synthetic&with_discounts=false&kind=employee_30')).get('with_discounts')).toBe('true')
    expect(discountResultQuery(new URLSearchParams('with_discounts=false')).has('with_discounts')).toBe(false)
  })
})
