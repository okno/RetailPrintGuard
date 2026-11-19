import { describe, expect, it } from 'vitest'
import { canExport, exportArchiveFilename, exportCoverageLabel, exportDateError, exportDateLabel, exportDocumentScopeLabel, exportPollingInterval, exportProgress, exportRequestBody, isLegacyExport } from './exports'
import type { ExportSelectionMetadata } from './types'

const selection: ExportSelectionMetadata = {
  schema_version: 1,
  policy_version: 'synthetic-policy-v1',
  scope: 'all_history',
  document_scope: 'complete',
  scanned_documents: 6,
  selected_documents: 2,
  excluded_documents: 4,
  category_counts: { complete: 2, partial: 1, excluded: 3 },
  exclusion_counts: { DEVICE_RESPONSE: 3, OTHER_DOCUMENT_SCOPE: 1 },
  coverage_from: '2026-08-13',
  coverage_to: '2026-09-11',
  document_date_count: 1,
  capture_date_count: 1,
}

describe('export period and permission boundaries', () => {
  it('accepts inclusive single-day and leap-day requests without converting calendar dates to UTC', () => {
    expect(exportDateError('2028-02-29', '2028-02-29')).toBeUndefined()
    expect(exportDateError('2026-03-29', '2026-10-25')).toBeUndefined()
    expect(exportDateLabel('2026-03-29')).toBe('29/03/2026')
    expect(exportDateLabel('2026-10-25')).toBe('25/10/2026')
  })

  it('rejects missing, rolled-over, malformed and reversed calendar dates', () => {
    for (const value of ['', '2026-02-29', '2026-04-31', '2026-13-01', '01/10/2026', '0000-01-01']) {
      expect(exportDateError(value, '2026-12-31')).toBeDefined()
      expect(exportDateError('2026-01-01', value)).toBeDefined()
    }
    expect(exportDateError('2026-10-02', '2026-10-01')).toContain('successiva')
  })

  it('restricts export access to administrators and auditors', () => {
    expect(canExport([])).toBe(false)
    expect(canExport(['OPERATOR', 'READ_ONLY'])).toBe(false)
    expect(canExport(['AUDITOR'])).toBe(true)
    expect(canExport(['ADMIN'])).toBe(true)
  })

  it('requests the whole history without artificial dates and keeps complete and partial scopes separate', () => {
    const base = { scope: 'all_history' as const, dateFrom: '1900-01-01', dateTo: '2099-12-31', deviceIds: [] }
    expect(exportRequestBody({ ...base, documentScope: 'complete' })).toEqual({ scope: 'all_history', document_scope: 'complete', device_ids: [] })
    expect(exportRequestBody({ ...base, documentScope: 'partial' })).toEqual({ scope: 'all_history', document_scope: 'partial', device_ids: [] })
  })

  it('sends inclusive calendar dates only when an explicit interval is selected', () => {
    expect(exportRequestBody({ scope: 'date_range', documentScope: 'partial', dateFrom: '2026-03-29', dateTo: '2026-03-29', deviceIds: ['synthetic-device'] })).toEqual({
      scope: 'date_range', document_scope: 'partial', date_from: '2026-03-29', date_to: '2026-03-29', device_ids: ['synthetic-device'],
    })
  })

  it('requests the latest ten complete documents without dates or partial content', () => {
    expect(exportRequestBody({ scope: 'latest_10', documentScope: 'partial', dateFrom: '2042-01-01', dateTo: '2042-12-31', deviceIds: ['synthetic-device'] })).toEqual({ scope: 'latest_10', document_scope: 'complete', device_ids: ['synthetic-device'] })
  })
})

describe('durable export history presentation', () => {
  it('polls only unfinished jobs and stops on success or failure', () => {
    expect(exportPollingInterval(undefined)).toBe(false)
    expect(exportPollingInterval([])).toBe(false)
    expect(exportPollingInterval([{ status: 'queued' }])).toBe(3_000)
    expect(exportPollingInterval([{ status: 'failed' }, { status: 'running' }])).toBe(3_000)
    expect(exportPollingInterval([{ status: 'completed' }, { status: 'failed' }])).toBe(false)
  })

  it('keeps zero-document and partial-job progress finite and bounded', () => {
    expect(exportProgress({ total_documents: 0, completed_documents: 0 })).toBe(0)
    expect(exportProgress({ total_documents: 4, completed_documents: 3 })).toBe(75)
    expect(exportProgress({ total_documents: 4, completed_documents: 5 })).toBe(100)
  })

  it('identifies unfiltered legacy archives and never treats their request bounds as coverage', () => {
    expect(isLegacyExport({ selection_metadata: null, document_scope: 'legacy_all' })).toBe(true)
    expect(isLegacyExport({})).toBe(true)
    expect(isLegacyExport({ selection_metadata: selection, document_scope: 'complete' })).toBe(false)
    expect(exportCoverageLabel(selection)).toBe('13/08/2026 – 11/09/2026')
    expect(exportCoverageLabel({ ...selection, selected_documents: 0, coverage_from: null, coverage_to: null })).toBe('Nessun documento selezionato')
    expect(exportDateLabel(null)).toBe('Non disponibile')
  })

  it('uses the server archive filename including its real coverage and content scope', () => {
    const filename = 'RetailPrintGuard_completi_2026-08-13_2026-09-11_synthetic.zip'
    expect(exportArchiveFilename({ id: 'synthetic', archive_filename: filename })).toBe(filename)
    expect(exportArchiveFilename({ id: 'legacy' })).toBe('RetailPrintGuard_archivio_precedente_legacy.zip')
    expect(exportDocumentScopeLabel('partial')).toBe('Parziali da verificare')
    expect(exportDocumentScopeLabel('legacy_all')).toContain('non filtrato')
  })
})
