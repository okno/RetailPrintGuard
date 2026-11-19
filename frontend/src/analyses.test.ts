import { describe, expect, it } from 'vitest'
import { analysisCreateBody, analysisPollingInterval, analysisProgress, analysisReportFilename, canAnalyze, currentDiscountResults, type AnalysisRun, type CurrentDiscountResults } from './analyses'

const run: AnalysisRun = { id: 'synthetic', status: 'running', stage: 'parsing', requested_by: 'synthetic-auditor', created_at: '2042-01-01T12:00:00Z', started_at: null, completed_at: null, total_jobs: 10, completed_jobs: 4, failed_jobs: 0, total_documents: 20, completed_documents: 5, error: null, parser_versions: {}, policy_version: 'synthetic', summary: null }

describe('full-history analysis controls', () => {
  it('always requests the full history without consultation filters or invented bounds', () => {
    expect(analysisCreateBody()).toEqual({ scope: 'all_history' })
    expect(canAnalyze(['AUDITOR'])).toBe(true)
    expect(canAnalyze(['ADMIN'])).toBe(true)
    expect(canAnalyze(['READ_ONLY', 'OPERATOR'])).toBe(false)
  })

  it('polls only active runs and leaves completed and failed history stable', () => {
    expect(analysisPollingInterval([{ status: 'queued' }])).toBe(3000)
    expect(analysisPollingInterval([{ status: 'failed' }, { status: 'running' }])).toBe(3000)
    expect(analysisPollingInterval([{ status: 'completed' }, { status: 'failed' }])).toBe(false)
    expect(analysisPollingInterval(undefined)).toBe(false)
  })

  it('uses stage-specific units without inventing an overall percentage', () => {
    expect(analysisProgress(run)).toEqual({ completed: 4, total: 10, unit: 'job', percent: 40 })
    expect(analysisProgress({ ...run, stage: 'discounts' })).toEqual({ completed: 5, total: 20, unit: 'documenti', percent: 25 })
    expect(analysisProgress({ ...run, stage: 'correlation' })).toBeUndefined()
    expect(analysisProgress({ ...run, total_jobs: 0 })?.percent).toBeUndefined()
    expect(analysisProgress({ ...run, completed_jobs: 15 })?.percent).toBe(100)
  })

  it('names reports by durable run identity and requested format', () => {
    expect(analysisReportFilename('synthetic', 'json')).toBe('RetailPrintGuard_rianalisi_synthetic.json')
    expect(analysisReportFilename('synthetic', 'csv')).toBe('RetailPrintGuard_rianalisi_synthetic.csv')
  })

  it('uses filtered server aggregates rather than summing the visible result page', () => {
    const response: CurrentDiscountResults = {
      items: [], total: 12, limit: 25, offset: 0, date_basis: 'captured_at', counting_basis: 'document_identity', policy_version: 'synthetic',
      summary: { observed_employee_discount_total: '45.60', observed_employee_discount_count: 10, observed_generic_30_discount_total: '0.60', observed_generic_30_discount_count: 2, arithmetic_reconciled_employee_discount_total: '30.00', arithmetic_reconciled_employee_discount_count: 8 },
    }
    const result = currentDiscountResults(response)
    expect(result.summary?.employee_total).toBe('45.60')
    expect(result.summary?.employee_arithmetic_verified_total).toBe('30.00')
    expect(result.summary?.generic_30_total).toBe('0.60')
    expect(result.summary?.documents_with_discounts).toBe(12)
    expect(result.summary?.observed_rows).toBeUndefined()
  })
})
