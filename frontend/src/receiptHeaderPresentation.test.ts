import { describe, expect, it } from 'vitest'
import {
  configuredReceiptHeaderText,
  RECEIPT_HEADER_NOT_AVAILABLE,
  receiptHeaderEvidenceLabel,
  receiptHeaderSummary,
} from './receiptHeaderPresentation'

describe('receipt header presentation', () => {
  it('keeps observed and configured provenance distinct', () => {
    expect(receiptHeaderEvidenceLabel('RCH_PRINTED_HEADER')).toBe(
      'Osservata nel blocco iniziale stampato dalla RCH',
    )
    expect(receiptHeaderEvidenceLabel('DEVICE_METADATA_CONFIGURED')).toBe(
      'Configurata sul dispositivo (non osservata nel flusso)',
    )
  })

  it('does not imply that a missing header was observed', () => {
    expect(receiptHeaderEvidenceLabel(undefined)).toBe(RECEIPT_HEADER_NOT_AVAILABLE)
    expect(receiptHeaderSummary(null)).toBe(RECEIPT_HEADER_NOT_AVAILABLE)
  })

  it('prefers the synthetic merchant name and falls back to the legal name', () => {
    const configured = {
      schema_version: 1 as const,
      merchant_name: null,
      legal_name: 'SYNTHETIC HOSPITALITY S.R.L.',
      address_lines: ['VIA DEL LABORATORIO 1'],
      phone: null,
      tax_code: null,
      vat_number: '00000000000',
      evidence: 'DEVICE_METADATA_CONFIGURED' as const,
    }
    expect(receiptHeaderSummary(configured)).toBe('SYNTHETIC HOSPITALITY S.R.L.')
    expect(receiptHeaderSummary({ ...configured, merchant_name: 'LAB HOTEL' })).toBe('LAB HOTEL')
    expect(configuredReceiptHeaderText(configured)).toContain(
      'INTESTAZIONE DOCUMENTO (CONFIGURATA)\nSYNTHETIC HOSPITALITY S.R.L.',
    )
    expect(configuredReceiptHeaderText(configured)).toContain(
      'Configurata sul dispositivo (non osservata nel flusso)',
    )
    expect(configuredReceiptHeaderText({
      ...configured,
      evidence: 'RCH_PRINTED_HEADER',
    })).toBeNull()
  })
})
