import { configuredReceiptHeaderText } from './receiptHeaderPresentation'
import type { DocumentRecord } from './types'

type ReceiptDocument = Pick<DocumentRecord, 'receipt_preview' | 'receipt_text' | 'normalized_text' | 'receipt_header'>

export function receiptPreviewText(document: ReceiptDocument) {
  if (document.receipt_preview != null) return document.receipt_preview
  const body = document.receipt_text || document.normalized_text || 'Nessun testo documento disponibile.'
  const header = configuredReceiptHeaderText(document.receipt_header)
  return header ? `${header}\n\n${body}` : body
}

export function pdfDownloadFilename(document: Pick<DocumentRecord, 'id' | 'pdf_filename'>) {
  return document.pdf_filename || `${document.id}.pdf`
}
