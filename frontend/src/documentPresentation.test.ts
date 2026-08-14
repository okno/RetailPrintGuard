import { describe, expect, it } from 'vitest'
import {
  ALL_EVIDENCE_FILTER,
  documentApiSearchParams,
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
    expect(technical.toString()).toBe('type=DEVICE_RESPONSE')

    const all = documentApiSearchParams(new URLSearchParams(`type=${ALL_EVIDENCE_FILTER}`))
    expect(all.has('type')).toBe(false)
    expect(all.has('exclude_type')).toBe(false)
  })

  it('never deduplicates distinct jobs merely because their content is equal', () => {
    const documents = [
      { id: 'job-one', type: 'COMMERCIAL_DOCUMENT', sha256: 'same' },
      { id: 'job-two', type: 'COMMERCIAL_DOCUMENT', sha256: 'same' },
    ]
    expect(presentedDocuments(documents, '')).toEqual(documents)
  })
})
