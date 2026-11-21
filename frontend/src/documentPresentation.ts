import type { Device, DocumentLine, DocumentScope } from './types'

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
  DEVICE_RESPONSE: 'RISPOSTA TECNICA DISPOSITIVO',
  UNKNOWN: 'SCONOSCIUTO',
}

export function documentTypeLabel(value: string) {
  return DOCUMENT_TYPE_LABELS[value] ?? value.replaceAll('_', ' ')
}

export function isEpsonDailyReport(document: Pick<{
  type: string
  subtype: string
}, 'type' | 'subtype'>) {
  return document.type === 'SHIFT_END_REPORT'
    && /^EPSON_(?:X|Z|XZ)_REPORT_OBSERVED$/.test(document.subtype)
}

export function documentPreviewTabLabel(document: Pick<{ type: string }, 'type'>) {
  return document.type === 'SHIFT_END_REPORT' ? 'Rapporto' : 'Scontrino'
}

export function deviceLabel(value: string, devices?: readonly Pick<Device, 'id' | 'name' | 'display_name'>[]) {
  const device = devices?.find((candidate) => candidate.id === value)
  return device?.display_name?.trim() || device?.name?.trim() || value
}

export function documentDeviceOptions(
  devices: readonly Pick<Device, 'id' | 'name' | 'display_name'>[] | undefined,
  selectedId: string,
) {
  const options = (devices ?? []).map((device) => ({ id: device.id, label: deviceLabel(device.id, devices) }))
  // Keep a saved filter selectable while the device list loads or if the device was removed.
  if (selectedId && !options.some((option) => option.id === selectedId)) {
    options.unshift({ id: selectedId, label: selectedId })
  }
  return options
}

export function documentFilterParams(params: URLSearchParams, key: string, value: string, limit: number) {
  const next = new URLSearchParams(params)
  value ? next.set(key, value) : next.delete(key)
  next.set('offset', '0')
  next.set('limit', String(limit))
  return next
}

export function documentTimestampEvidenceLabel(value?: string) {
  if (value === 'RCH_PRINTED_TEXT') return 'Stampata dalla cassa RCH'
  if (value === 'ESC_POS_PRINTED_OPERATOR_LINE') return 'Stampata sulla comanda POS'
  return value ? 'Osservata nel documento' : undefined
}

export function selectedDocumentScope(params: URLSearchParams): DocumentScope {
  const scope = params.get('document_scope')
  if (scope === 'complete' || scope === 'partial' || scope === 'all') return scope
  // Preserve links saved before content and document type became separate filters.
  if (params.get('type') === ALL_EVIDENCE_FILTER || params.get('type') === TECHNICAL_DOCUMENT_TYPE || params.get('include_technical') === 'true') return 'all'
  return 'complete'
}

export function documentApiSearchParams(uiParams: URLSearchParams) {
  const apiParams = new URLSearchParams(uiParams)
  apiParams.delete('period')
  apiParams.delete('exclude_type')
  const scope = selectedDocumentScope(uiParams)
  apiParams.set('document_scope', scope)
  if (apiParams.get('type') === ALL_EVIDENCE_FILTER || !apiParams.get('type')) apiParams.delete('type')
  if (scope === 'all') apiParams.set('include_technical', 'true')
  else apiParams.delete('include_technical')
  return apiParams
}

export function confidencePercent(value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return undefined
  const percent = value <= 1 ? value * 100 : value
  return Math.round(percent * 10) / 10
}

/** API null means unobserved; it must not become a zero price or lose provenance. */
export function documentLinePrice(line: Pick<DocumentLine, 'unit_price' | 'derived_unit_price'>) {
  if (line.unit_price != null) return { value: line.unit_price, derived: false }
  if (line.derived_unit_price != null) return { value: line.derived_unit_price, derived: true }
  return undefined
}
