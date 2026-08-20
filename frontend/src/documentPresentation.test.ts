import { describe, expect, it } from 'vitest'
import {
  ALL_EVIDENCE_FILTER,
  confidencePercent,
  deviceLabel,
  documentApiSearchParams,
  documentTypeLabel,
  presentedDocuments,
} from './documentPresentation'

describe('primary document presentation', () => {
  it('requests and presents business documents without device responses by default', () => {
    const params = documentApiSearchParams(new URLSearchParams('limit=25&offset=0'))
    expect(params.get('exclude_type')).toBe('DEVICE_RESPONSE')
    expect(presentedDocuments([
      { id: 'commercial', type: 'COMMERCIAL_DOCUMENT' },
      { id: 'response', type: 'DEVICE_RESPONSE' },
    ], '')).toEqual([{ id: 'commercial', type: 'COMMERCIAL_DOCUMENT' }])
  })

  it('keeps technical evidence available through an explicit filter', () => {
    const technical = documentApiSearchParams(new URLSearchParams('type=DEVICE_RESPONSE'))
    expect(technical.get('type')).toBe('DEVICE_RESPONSE')
    expect(technical.get('include_technical')).toBe('true')

    const all = documentApiSearchParams(new URLSearchParams(`type=${ALL_EVIDENCE_FILTER}`))
    expect(all.has('type')).toBe(false)
    expect(all.has('exclude_type')).toBe(false)
    expect(all.get('include_technical')).toBe('true')
  })

  it('never deduplicates distinct jobs merely because their content is equal', () => {
    const documents = [
      { id: 'job-one', type: 'COMMERCIAL_DOCUMENT', sha256: 'same' },
      { id: 'job-two', type: 'COMMERCIAL_DOCUMENT', sha256: 'same' },
    ]
    expect(presentedDocuments(documents, '')).toEqual(documents)
  })

  it('renders fractional and percentage confidence on the same scale', () => {
    expect(confidencePercent(0.96)).toBe(96)
    expect(confidencePercent(96)).toBe(96)
    expect(confidencePercent(undefined)).toBeUndefined()
  })

  it('uses operational Italian labels without changing persisted identifiers', () => {
    expect(documentTypeLabel('KITCHEN_ORDER')).toBe('COMANDA')
    expect(documentTypeLabel('SHIFT_END_REPORT')).toBe('REPORT DI FINE TURNO')
    expect(documentTypeLabel('INVOICE')).toBe('FATTURA')
    expect(deviceLabel('pos_1')).toBe('BAR')
    expect(deviceLabel('pos_2')).toBe('CUCINA')
    expect(deviceLabel('pos_3')).toBe('PIZZERIA')
    expect(deviceLabel('rch_1')).toBe('rch_1')
  })
})
