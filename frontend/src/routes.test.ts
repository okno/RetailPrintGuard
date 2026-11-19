import { describe, expect, it } from 'vitest'
import {
  DOCUMENT_DETAIL_PARAM,
  DOCUMENT_DETAIL_ROUTE,
  ANALYSIS_DETAIL_PARAM,
  ANALYSIS_DETAIL_ROUTE,
  TRANSACTION_DETAIL_PARAM,
  TRANSACTION_DETAIL_ROUTE,
  documentDetailPath,
  analysisDetailPath,
  EXPORT_DETAIL_ROUTE,
  EXPORT_DETAIL_PARAM,
  exportDetailPath,
  transactionDetailPath,
} from './routes'

describe('detail route contracts', () => {
  it('keeps route parameter names and generated document links aligned', () => {
    expect(DOCUMENT_DETAIL_ROUTE).toBe(`/documenti/:${DOCUMENT_DETAIL_PARAM}`)
    expect(documentDetailPath('doc/one')).toBe('/documenti/doc%2Fone')
  })

  it('keeps route parameter names and generated transaction links aligned', () => {
    expect(TRANSACTION_DETAIL_ROUTE).toBe(`/transazioni/:${TRANSACTION_DETAIL_PARAM}`)
    expect(transactionDetailPath('transaction/one')).toBe('/transazioni/transaction%2Fone')
  })

  it('keeps analysis history links aligned and encodes identifiers', () => {
    expect(ANALYSIS_DETAIL_ROUTE).toBe(`/rianalisi/:${ANALYSIS_DETAIL_PARAM}`)
    expect(analysisDetailPath('analysis/one')).toBe('/rianalisi/analysis%2Fone')
  })

  it('addresses the frozen archive rather than a current document', () => {
    expect(EXPORT_DETAIL_ROUTE).toBe(`/esportazioni/:${EXPORT_DETAIL_PARAM}`)
    expect(exportDetailPath('export/one')).toBe('/esportazioni/export%2Fone')
  })
})
