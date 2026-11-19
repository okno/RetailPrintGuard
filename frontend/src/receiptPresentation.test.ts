import { describe, expect, it } from 'vitest'
import { pdfDownloadFilename, receiptPreviewText } from './receiptPresentation'

const configuredHeader = {
  schema_version: 1 as const,
  merchant_name: 'HOTEL DI TEST',
  legal_name: null,
  address_lines: [],
  phone: null,
  tax_code: null,
  vat_number: null,
  evidence: 'DEVICE_METADATA_CONFIGURED' as const,
}

describe('shared receipt preview', () => {
  it('displays the server preview verbatim without duplicating its header or adding parser text', () => {
    const preview = 'HOTEL DI TEST\n\nArticolo                     7,50\n\n                           Firma  '
    expect(receiptPreviewText({
      receipt_preview: preview,
      receipt_text: 'Corpo legacy',
      normalized_text: 'Testo parser tecnico',
      receipt_header: configuredHeader,
    })).toBe(preview)
  })

  it('does not substitute legacy contents for an explicitly empty shared preview', () => {
    expect(receiptPreviewText({ receipt_preview: '', normalized_text: 'Altro testo' })).toBe('')
  })

  it('preserves the existing configured-header appearance when the API lacks a shared preview', () => {
    expect(receiptPreviewText({
      receipt_preview: null,
      receipt_text: 'Articolo      7,50\nTOT           7,50',
      normalized_text: 'Testo parser tecnico',
      receipt_header: configuredHeader,
    })).toBe('HOTEL DI TEST\n\nArticolo      7,50\nTOT           7,50')
  })

  it('keeps observed headers inside the receipt body and falls back to parser text when necessary', () => {
    expect(receiptPreviewText({
      receipt_text: 'INTESTAZIONE OSSERVATA\n\nOrdine di test',
      normalized_text: 'Testo tecnico',
      receipt_header: { ...configuredHeader, evidence: 'RCH_PRINTED_HEADER' },
    })).toBe('INTESTAZIONE OSSERVATA\n\nOrdine di test')
    expect(receiptPreviewText({ receipt_text: '', normalized_text: 'Testo acquisito' })).toBe('Testo acquisito')
    expect(receiptPreviewText({ normalized_text: '' })).toBe('Nessun testo documento disponibile.')
  })
})

describe('PDF download filename', () => {
  it('uses the exact API filename so individual and archived PDFs agree', () => {
    const name = '2026-09-11_151000_comanda_ordine-123_00000000-0000-4000-8000-000000000001.pdf'
    expect(pdfDownloadFilename({ id: 'legacy-id', pdf_filename: name })).toBe(name)
  })

  it('retains the previous filename for older APIs', () => {
    expect(pdfDownloadFilename({ id: 'legacy-id' })).toBe('legacy-id.pdf')
    expect(pdfDownloadFilename({ id: 'legacy-id', pdf_filename: null })).toBe('legacy-id.pdf')
  })
})
