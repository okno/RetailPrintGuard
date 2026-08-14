import { describe, expect, it } from 'vitest'
import {
  DOCUMENT_DETAIL_PARAM,
  DOCUMENT_DETAIL_ROUTE,
  TRANSACTION_DETAIL_PARAM,
  TRANSACTION_DETAIL_ROUTE,
  documentDetailPath,
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
})
