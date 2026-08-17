import { describe, expect, it } from 'vitest'
import {
  isOperationalReductionFilter,
  operationalReductionQuery,
  operationalReductionTransactionsPath,
} from './dashboardDrilldown'

describe('dashboard economic drill-down', () => {
  it('preserves the sale period and requests only active economic reductions', () => {
    const path = operationalReductionTransactionsPath(new URLSearchParams({
      from: '2042-05-06T00:00:00.000Z',
      to: '2042-05-07T00:00:00.000Z',
      unrelated: 'ignored',
    }))
    const query = new URL(path, 'https://retailprintguard.invalid').searchParams

    expect(query.get('from')).toBe('2042-05-06T00:00:00.000Z')
    expect(query.get('to')).toBe('2042-05-07T00:00:00.000Z')
    expect(query.get('operational_economic_only')).toBe('true')
    expect(query.get('reduction_only')).toBe('true')
    expect(query.get('minimum_difference')).toBe('0.01')
    expect(query.has('unrelated')).toBe(false)
    expect(isOperationalReductionFilter(query)).toBe(true)
  })

  it('does not mistake a generic amount filter for the operational drill-down', () => {
    expect(isOperationalReductionFilter(new URLSearchParams('minimum_difference=0.01'))).toBe(false)
  })

  it('builds the dashboard episode query without leaking unrelated UI filters', () => {
    const query = operationalReductionQuery(new URLSearchParams({
      from: '2042-05-06T00:00:00.000Z',
      to: '2042-05-07T00:00:00.000Z',
      type: 'PRE_BILL',
      offset: '99',
    }), { limit: 8, offset: 0 })

    expect(query.get('from')).toBe('2042-05-06T00:00:00.000Z')
    expect(query.get('to')).toBe('2042-05-07T00:00:00.000Z')
    expect(query.get('limit')).toBe('8')
    expect(query.get('offset')).toBe('0')
    expect(query.get('operational_economic_only')).toBe('true')
    expect(query.get('reduction_only')).toBe('true')
    expect(query.get('minimum_difference')).toBe('0.01')
    expect(query.has('type')).toBe(false)
  })
})
