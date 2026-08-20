import { describe, expect, it } from 'vitest'
import {
  DEFAULT_DOCUMENT_COLUMNS,
  moveDocumentColumn,
  parseDocumentColumnOrder,
} from './documentColumns'

describe('document column order', () => {
  it('restores a saved order and appends columns introduced by an update', () => {
    expect(parseDocumentColumnOrder('["references","type"]')).toEqual([
      'references',
      'type',
      ...DEFAULT_DOCUMENT_COLUMNS.filter((column) => !['references', 'type'].includes(column)),
    ])
  })

  it('rejects malformed, duplicate and unknown values safely', () => {
    expect(parseDocumentColumnOrder('not-json')).toEqual(DEFAULT_DOCUMENT_COLUMNS)
    expect(parseDocumentColumnOrder('["device","device","unexpected"]')[0]).toBe('device')
    expect(parseDocumentColumnOrder('["device","device","unexpected"]')).toHaveLength(
      DEFAULT_DOCUMENT_COLUMNS.length,
    )
  })

  it('moves a source column immediately before its target', () => {
    expect(moveDocumentColumn(DEFAULT_DOCUMENT_COLUMNS, 'device', 'document_time')[0]).toBe(
      'device',
    )
  })
})
