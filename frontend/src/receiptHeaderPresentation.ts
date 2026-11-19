import type { ReceiptHeaderEvidence, ReceiptHeaderRecord } from './types'

export const RECEIPT_HEADER_NOT_AVAILABLE = 'Non osservata nel flusso e non configurata'

export function receiptHeaderEvidenceLabel(evidence: ReceiptHeaderEvidence | string | null | undefined) {
  if (evidence === 'RCH_PRINTED_HEADER') {
    return 'Osservata nel blocco iniziale stampato dalla RCH'
  }
  if (evidence === 'RCH_DGFE_EXPORT') {
    return 'Estratta dall\'export della memoria fiscale DGFE'
  }
  if (evidence === 'DEVICE_METADATA_CONFIGURED') {
    return ''
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
  if (!header || !['DEVICE_METADATA_CONFIGURED', 'RCH_DGFE_EXPORT'].includes(header.evidence)) return null
  const lines = header.evidence === 'RCH_DGFE_EXPORT'
    ? ['INTESTAZIONE DOCUMENTO (MEMORIA FISCALE DGFE)']
    : []
  if (header.merchant_name) lines.push(header.merchant_name)
  if (header.legal_name) lines.push(header.legal_name)
  lines.push(...header.address_lines)
  if (header.phone) lines.push(`Telefono ${header.phone}`)
  if (header.tax_code) lines.push(`C.F. ${header.tax_code}`)
  if (header.vat_number) lines.push(`P.IVA ${header.vat_number}`)
  const provenance = receiptHeaderEvidenceLabel(header.evidence)
  if (provenance) lines.push('', provenance)
  return lines.join('\n')
}
