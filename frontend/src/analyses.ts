import type { Page, Role } from './types'

export const ANALYSIS_ROLES: Role[] = ['ADMIN', 'AUDITOR']
export type AnalysisStatus = 'queued' | 'running' | 'completed' | 'failed'
export type AnalysisStage = 'queued' | 'parsing' | 'correlation' | 'fraud' | 'discounts' | 'completed' | 'failed'
export type DiscountKind = 'employee_30' | 'generic_30' | 'other'

export interface DiscountSummary {
  employee_total: string
  generic_30_total: string
  other_discount_total?: string
  employee_arithmetic_verified_total?: string
  generic_30_arithmetic_verified_total?: string
  observed_rows?: number
  documents_with_discounts: number
  arithmetic_verified_rows?: number
  arithmetic_unverified_rows?: number
  employee_document_count?: number
  generic_30_document_count?: number
  arithmetic_verified_employee_document_count?: number
  documents_without_raw_reprocessing?: number
  jobs_without_request_raw?: number
}

export interface AnalysisRun {
  id: string
  status: AnalysisStatus
  stage: AnalysisStage
  requested_by: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  total_jobs: number
  completed_jobs: number
  failed_jobs: number
  total_documents: number
  completed_documents: number
  error: string | null
  parser_versions: Record<string, unknown>
  policy_version: string
  summary: DiscountSummary | null
}

export interface DiscountObservation {
  sequence: number
  label: string
  amount: string
  rate_percent: string | null
  kind: DiscountKind
  basis: string | null
  arithmetic_verified: boolean
  source_text: string
}

export interface AnalysisResult {
  analysis_id?: string
  document_id: string
  document_version_id: string
  device_id: string
  device_name?: string
  captured_at: string
  document_timestamp: string | null
  document_type: string
  complete: boolean
  observations: DiscountObservation[]
  employee_total: string
  generic_30_total: string
  other_discount_total: string
  arithmetic_reconciled: boolean
  reasons: string[]
  reprocessed_from_raw?: boolean
  reprocessing_note?: string | null
}

export interface DiscountResults extends Page<AnalysisResult> {
  summary?: DiscountSummary
}

export interface CurrentDiscountResults extends Page<{
  document_id: string
  document_version_id: string
  device_id: string
  device_name: string
  type: string
  complete: boolean
  captured_at: string
  document_timestamp: string | null
  discounts: {
    observations: DiscountObservation[]
    employee_total: string
    generic_30_total: string
    other_discount_total: string
    arithmetic_reconciled: boolean
    reasons: string[]
    policy_version: string
  }
}> {
  summary: {
    observed_employee_discount_total: string
    observed_employee_discount_count: number
    observed_generic_30_discount_total: string
    observed_generic_30_discount_count: number
    arithmetic_reconciled_employee_discount_total: string
    arithmetic_reconciled_employee_discount_count: number
  }
  date_basis: 'captured_at'
  counting_basis: 'document_identity'
  policy_version: string
}

export function currentDiscountResults(response: CurrentDiscountResults): DiscountResults {
  const totals = response.summary
  return {
    total: response.total,
    limit: response.limit,
    offset: response.offset,
    items: response.items.map((item) => ({ ...item, ...item.discounts, document_type: item.type })),
    summary: {
      employee_total: totals.observed_employee_discount_total,
      generic_30_total: totals.observed_generic_30_discount_total,
      employee_arithmetic_verified_total: totals.arithmetic_reconciled_employee_discount_total,
      employee_document_count: totals.observed_employee_discount_count,
      generic_30_document_count: totals.observed_generic_30_discount_count,
      arithmetic_verified_employee_document_count: totals.arithmetic_reconciled_employee_discount_count,
      documents_with_discounts: response.total,
    },
  }
}

export function canAnalyze(roles: readonly Role[]) {
  return ANALYSIS_ROLES.some((role) => roles.includes(role))
}

export function analysisPending(run: Pick<AnalysisRun, 'status'> | undefined) {
  return run?.status === 'queued' || run?.status === 'running'
}

export function analysisPollingInterval(runs: readonly Pick<AnalysisRun, 'status'>[] | undefined) {
  return runs?.some(analysisPending) ? 3_000 : false
}

export function analysisStatusLabel(status: string) {
  return ({ queued: 'In coda', running: 'In esecuzione', completed: 'Completata', failed: 'Non riuscita' } as Record<string, string>)[status] ?? status
}

export function analysisStageLabel(stage: string) {
  return ({ queued: 'Attesa di avvio', parsing: 'Rielaborazione dei RAW', correlation: 'Correlazione dei documenti', fraud: 'Verifica delle regole antifrode', discounts: 'Analisi degli sconti', completed: 'Analisi completata', failed: 'Analisi interrotta da un errore' } as Record<string, string>)[stage] ?? stage
}

export function analysisProgress(run: AnalysisRun) {
  const counts = run.stage === 'parsing' ? { completed: run.completed_jobs, total: run.total_jobs, unit: 'job' }
    : run.stage === 'discounts' ? { completed: run.completed_documents, total: run.total_documents, unit: 'documenti' }
      : undefined
  return counts ? { ...counts, percent: counts.total > 0 ? Math.max(0, Math.min(100, counts.completed / counts.total * 100)) : undefined } : undefined
}

export function analysisCreateBody() {
  return { scope: 'all_history' as const }
}

export function analysisReportFilename(id: string, format: 'json' | 'csv') {
  return `RetailPrintGuard_rianalisi_${id}.${format}`
}
