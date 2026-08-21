import type { ReceiptHeaderEvidence, ReceiptHeaderRecord } from './types'

export const RECEIPT_HEADER_NOT_AVAILABLE = 'Non osservata nel flusso e non configurata'

export function receiptHeaderEvidenceLabel(evidence: ReceiptHeaderEvidence | string | null | undefined) {
  if (evidence === 'RCH_PRINTED_HEADER') {
    return 'Osservata nel blocco iniziale stampato dalla RCH'
  }
  if (evidence === 'DEVICE_METADATA_CONFIGURED') {
    return 'Configurata sul dispositivo (non osservata nel flusso)'
  }
  return RECEIPT_HEADER_NOT_AVAILABLE
}

export function receiptHeaderSummary(header: ReceiptHeaderRecord | null | undefined) {
  if (!header) return RECEIPT_HEADER_NOT_AVAILABLE
  return header.merchant_name?.trim()
    || header.legal_name?.trim()
    || 'Intestazione disponibile senza denominazione'
}

export function configuredReceiptHeaderText(header: ReceiptHeaderRecord | null | undefined) {
  if (!header || header.evidence !== 'DEVICE_METADATA_CONFIGURED') return null
  const lines = ['INTESTAZIONE DOCUMENTO (CONFIGURATA)']
  if (header.merchant_name) lines.push(header.merchant_name)
  if (header.legal_name) lines.push(header.legal_name)
  lines.push(...header.address_lines)
  if (header.phone) lines.push(`Telefono ${header.phone}`)
  if (header.tax_code) lines.push(`C.F. ${header.tax_code}`)
  if (header.vat_number) lines.push(`P.IVA ${header.vat_number}`)
  lines.push('', receiptHeaderEvidenceLabel(header.evidence))
  return lines.join('\n')
}
