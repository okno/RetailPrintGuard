import { describe, expect, it } from 'vitest'
import { canDownloadDocumentPdf, documentPdfLabel, documentReasonLabel, documentsMatchScope } from './documentEligibility'

describe('document PDF eligibility', () => {
  it('allows complete and partial documents from the server classification without requiring date or fiscal number', () => {
    expect(canDownloadDocumentPdf({ pdf_category: 'complete' })).toBe(true)
    expect(canDownloadDocumentPdf({ pdf_category: 'partial' })).toBe(true)
    expect(documentPdfLabel({ pdf_category: 'partial' })).toBe('Parziale da verificare')
  })

  it('blocks excluded documents and missing classifications instead of guessing from the text', () => {
    expect(canDownloadDocumentPdf({ pdf_category: 'excluded' })).toBe(false)
    expect(canDownloadDocumentPdf({})).toBe(false)
    expect(documentReasonLabel('TECHNICAL_CANCEL_COMMAND')).toContain('Comando tecnico')
    expect(documentReasonLabel('DEVICE_RESPONSE')).toContain('Risposta tecnica')
    expect(documentReasonLabel('FUTURE_REASON')).toContain('non è disponibile')
  })

  it('detects an outdated or inconsistent server response instead of silently filtering a paginated result', () => {
    expect(documentsMatchScope([{ pdf_category: 'complete' }, { pdf_category: 'complete' }], 'complete')).toBe(true)
    expect(documentsMatchScope([{ pdf_category: 'complete' }, { pdf_category: 'excluded' }], 'complete')).toBe(false)
    expect(documentsMatchScope([{}], 'partial')).toBe(false)
    expect(documentsMatchScope([{ pdf_category: 'partial' }], 'partial')).toBe(true)
    expect(documentsMatchScope([{ pdf_category: 'excluded' }, {}], 'all')).toBe(true)
  })
})
