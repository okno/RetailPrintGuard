export const TECHNICAL_DOCUMENT_TYPE = 'DEVICE_RESPONSE'
export const ALL_EVIDENCE_FILTER = '__ALL__'

const DOCUMENT_TYPE_LABELS: Record<string, string> = {
  KITCHEN_ORDER: 'COMANDA',
  ORDER_CHANGE: 'VARIAZIONE COMANDA',
  ORDER: 'ORDINE',
  PRE_BILL: 'PRECONTO',
  MANAGEMENT_DOCUMENT: 'DOCUMENTO GESTIONALE',
  COMMERCIAL_DOCUMENT: 'DOCUMENTO COMMERCIALE',
  SHIFT_END_REPORT: 'REPORT DI FINE TURNO',
  INVOICE: 'FATTURA',
  CONFORMING_COPY: 'COPIA CONFORME',
  CANCELLATION: 'ANNULLAMENTO',
  REFUND: 'RIMBORSO',
  REPRINT: 'RISTAMPA',
  PAYMENT: 'PAGAMENTO',
  DEVICE_RESPONSE: 'RISPOSTA TECNICA RCH',
  UNKNOWN: 'SCONOSCIUTO',
}

const POS_DEVICE_LABELS: Record<string, string> = {
  pos_1: 'BAR',
  pos_2: 'CUCINA',
  pos_3: 'PIZZERIA',
}

export function documentTypeLabel(value: string) {
  return DOCUMENT_TYPE_LABELS[value] ?? value.replaceAll('_', ' ')
}

export function deviceLabel(value: string) {
  return POS_DEVICE_LABELS[value.toLowerCase()] ?? value
}

export function documentTimestampEvidenceLabel(value?: string) {
  if (value === 'RCH_PRINTED_TEXT') return 'Stampata dalla cassa RCH'
  if (value === 'ESC_POS_PRINTED_OPERATOR_LINE') return 'Stampata sulla comanda POS'
  return value ? 'Osservata nel documento' : undefined
}

/**
 * Keep the UI-only "all evidence" value out of the backend query. During a
 * rolling update the client also applies this filter, so an older API cannot
 * accidentally mix protocol responses into the primary document view.
 */
export function documentApiSearchParams(uiParams: URLSearchParams) {
  const apiParams = new URLSearchParams(uiParams)
  apiParams.delete('period')
  const selectedType = apiParams.get('type') ?? ''
  if (selectedType === ALL_EVIDENCE_FILTER) {
    apiParams.delete('type')
    apiParams.delete('exclude_type')
    apiParams.set('include_technical', 'true')
  } else if (!selectedType) {
    apiParams.delete('type')
    apiParams.set('exclude_type', TECHNICAL_DOCUMENT_TYPE)
    apiParams.delete('include_technical')
  } else if (selectedType === TECHNICAL_DOCUMENT_TYPE) {
    apiParams.delete('exclude_type')
    apiParams.set('include_technical', 'true')
  } else {
    apiParams.delete('exclude_type')
    apiParams.delete('include_technical')
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

export function confidencePercent(value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return undefined
  const percent = value <= 1 ? value * 100 : value
  return Math.round(percent * 10) / 10
}
