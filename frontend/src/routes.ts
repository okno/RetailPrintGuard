export const DOCUMENT_DETAIL_PARAM = 'documentId' as const
export const TRANSACTION_DETAIL_PARAM = 'transactionId' as const

export const DOCUMENT_DETAIL_ROUTE = `/documenti/:${DOCUMENT_DETAIL_PARAM}`
export const TRANSACTION_DETAIL_ROUTE = `/transazioni/:${TRANSACTION_DETAIL_PARAM}`

export function documentDetailPath(documentId: string) {
  return `/documenti/${encodeURIComponent(documentId)}`
}

export function transactionDetailPath(transactionId: string) {
  return `/transazioni/${encodeURIComponent(transactionId)}`
}
