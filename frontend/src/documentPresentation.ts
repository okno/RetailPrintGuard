export const TECHNICAL_DOCUMENT_TYPE = 'DEVICE_RESPONSE'
export const ALL_EVIDENCE_FILTER = '__ALL__'

/**
 * Keep the UI-only "all evidence" value out of the backend query. During a
 * rolling update the client also applies this filter, so an older API cannot
 * accidentally mix protocol responses into the primary document view.
 */
export function documentApiSearchParams(uiParams: URLSearchParams) {
  const apiParams = new URLSearchParams(uiParams)
  const selectedType = apiParams.get('type') ?? ''
  if (selectedType === ALL_EVIDENCE_FILTER) {
    apiParams.delete('type')
    apiParams.delete('exclude_type')
  } else if (!selectedType) {
    apiParams.delete('type')
    apiParams.set('exclude_type', TECHNICAL_DOCUMENT_TYPE)
  } else {
    apiParams.delete('exclude_type')
  }
  return apiParams
}

/**
 * Hide only device protocol responses in the primary view. Never collapse
 * equal hashes or different job IDs: repeated prints remain distinct evidence.
 */
export function presentedDocuments<T extends { type: string }>(
  documents: readonly T[],
  selectedType: string,
) {
  if (selectedType) return [...documents]
  return documents.filter((document) => document.type !== TECHNICAL_DOCUMENT_TYPE)
}
