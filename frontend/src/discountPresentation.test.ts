import { describe, expect, it } from 'vitest'
import { discountDrilldownPath, discountKindLabel, discountMoney, discountReasonLabel } from './discountPresentation'

describe('discount evidence presentation', () => {
  it('distinguishes missing statistics from a measured zero', () => {
    expect(discountMoney(undefined)).toBe('Non disponibile')
    expect(discountMoney(null)).toBe('Non disponibile')
    expect(discountMoney('')).toBe('Non disponibile')
    expect(discountMoney('not-a-number')).toBe('Non disponibile')
    expect(discountMoney('0')).toMatch(/0,00/)
    expect(discountMoney('30.10')).toMatch(/30,10/)
  })

  it('never attributes a generic thirty-percent discount to employees', () => {
    expect(discountKindLabel('employee_30')).toBe('Dipendenti 30% esplicito')
    expect(discountKindLabel('generic_30')).toBe('30% generico')
    expect(discountKindLabel('other')).toBe('Altro sconto')
    expect(discountKindLabel('future-kind')).toBe('Tipo non disponibile')
    expect(discountReasonLabel('document_total_not_reconciled')).toContain('non coincide')
    expect(discountReasonLabel('document_type_not_sale')).toContain('non conteggiata')
    expect(discountReasonLabel('document_not_printable')).toContain('Nessun contenuto documentale')
    expect(discountReasonLabel('future_reason')).toBe('Verifica richiesta: future reason')
  })

  it('keeps the dashboard evidence period and device without carrying unrelated filters', () => {
    const params = new URLSearchParams({ from: '2042-03-29T22:00:00Z', to: '2042-03-30T22:00:00Z', device_id: 'synthetic-device', offset: '25', operational_economic_only: 'true' })
    const target = new URL(discountDrilldownPath(params, 'employee_30'), 'https://example.test')
    expect(target.pathname).toBe('/sconti')
    expect(Object.fromEntries(target.searchParams)).toEqual({ from: params.get('from'), to: params.get('to'), device_id: 'synthetic-device', kind: 'employee_30' })
    expect(discountDrilldownPath(new URLSearchParams('period=all'))).toBe('/sconti?period=all')
    expect(discountDrilldownPath(new URLSearchParams(), 'employee_30', 'verified')).toBe('/sconti?period=all&kind=employee_30&reconciliation=verified')
  })
})
