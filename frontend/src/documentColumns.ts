export const DOCUMENT_COLUMN_STORAGE_KEY = 'retailprintguard.documents.column-order.v1'

export const DEFAULT_DOCUMENT_COLUMNS = [
  'document_time',
  'captured_at',
  'type',
  'references',
  'device',
  'total',
  'status',
  'confidence',
] as const

export type DocumentColumnId = (typeof DEFAULT_DOCUMENT_COLUMNS)[number]

const DOCUMENT_COLUMN_IDS = new Set<string>(DEFAULT_DOCUMENT_COLUMNS)

export function parseDocumentColumnOrder(serialized: string | null): DocumentColumnId[] {
  if (!serialized) return [...DEFAULT_DOCUMENT_COLUMNS]
  try {
    const value: unknown = JSON.parse(serialized)
    if (!Array.isArray(value)) return [...DEFAULT_DOCUMENT_COLUMNS]
    const unique = value.filter(
      (item, index): item is DocumentColumnId => (
        typeof item === 'string'
        && DOCUMENT_COLUMN_IDS.has(item)
        && value.indexOf(item) === index
      ),
    )
    return [
      ...unique,
      ...DEFAULT_DOCUMENT_COLUMNS.filter((column) => !unique.includes(column)),
    ]
  } catch {
    return [...DEFAULT_DOCUMENT_COLUMNS]
  }
}

export function moveDocumentColumn(
  order: readonly DocumentColumnId[],
  source: DocumentColumnId,
  target: DocumentColumnId,
): DocumentColumnId[] {
  if (source === target || !order.includes(source) || !order.includes(target)) return [...order]
  const targetIndex = order.indexOf(target)
  const next = order.filter((column) => column !== source)
  next.splice(targetIndex, 0, source)
  return next
}

export function isDocumentColumnId(value: string): value is DocumentColumnId {
  return DOCUMENT_COLUMN_IDS.has(value)
}
