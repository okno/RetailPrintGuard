import type { ExportDocumentScope, ExportRequest, ExportScope, ExportSelectionMetadata, Role } from './types'

export const EXPORT_ROLES: Role[] = ['ADMIN', 'AUDITOR']

export function canExport(roles: readonly Role[]) {
  return EXPORT_ROLES.some((role) => roles.includes(role))
}

function validCalendarDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  if (Number(value.slice(0, 4)) < 1) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

export function exportDateError(dateFrom: string, dateTo: string): string | undefined {
  if (!validCalendarDate(dateFrom) || !validCalendarDate(dateTo)) return 'Selezionare due date valide.'
  if (dateFrom > dateTo) return 'La data finale deve essere uguale o successiva alla data iniziale.'
  return undefined
}

export function isExportPending(job: Pick<ExportRequest, 'status'>) {
  return job.status === 'queued' || job.status === 'running'
}

export function exportPollingInterval(jobs: readonly Pick<ExportRequest, 'status'>[] | undefined) {
  return jobs?.some(isExportPending) ? 3_000 : false
}

export function exportStatusLabel(status: ExportRequest['status']) {
  return { queued: 'In coda', running: 'In preparazione', completed: 'Completata', failed: 'Non riuscita' }[status] ?? status
}

export function exportProgress(job: Pick<ExportRequest, 'completed_documents' | 'total_documents'>) {
  return job.total_documents > 0
    ? Math.max(0, Math.min(100, Math.round(job.completed_documents / job.total_documents * 100)))
    : 0
}

export function exportDateLabel(value: string | null | undefined) {
  if (!value) return 'Non disponibile'
  if (!validCalendarDate(value)) return value
  const [year, month, day] = value.split('-')
  return `${day}/${month}/${year}`
}

export function exportRequestBody(selection: {
  scope: ExportScope
  documentScope: ExportDocumentScope
  dateFrom: string
  dateTo: string
  deviceIds: string[]
}) {
  return {
    scope: selection.scope,
    document_scope: selection.scope === 'latest_10' ? 'complete' : selection.documentScope,
    device_ids: selection.deviceIds,
    ...(selection.scope === 'date_range' ? { date_from: selection.dateFrom, date_to: selection.dateTo } : {}),
  }
}

export function exportScopeLabel(scope?: ExportScope) {
  return scope === 'latest_10' ? 'Ultimi 10 documenti completi' : scope === 'all_history' ? 'Intero storico disponibile' : 'Intervallo di date'
}

export function isLegacyExport(job: Pick<ExportRequest, 'selection_metadata' | 'document_scope'>) {
  return !job.selection_metadata || job.document_scope === 'legacy_all'
}

export function exportDocumentScopeLabel(scope: ExportRequest['document_scope']) {
  return scope === 'partial' ? 'Parziali da verificare' : scope === 'complete' ? 'Documenti completi' : 'Archivio precedente non filtrato'
}

export function exportCoverageLabel(metadata: ExportSelectionMetadata) {
  if (!metadata.selected_documents) return 'Nessun documento selezionato'
  if (!metadata.coverage_from || !metadata.coverage_to) return 'Copertura effettiva non disponibile'
  return `${exportDateLabel(metadata.coverage_from)} – ${exportDateLabel(metadata.coverage_to)}`
}

export function exportArchiveFilename(job: Pick<ExportRequest, 'id' | 'archive_filename'>) {
  return job.archive_filename || `RetailPrintGuard_archivio_precedente_${job.id}.zip`
}
