import type { DocumentRecord, DocumentScope } from './types'

export const DOCUMENT_SCOPE_LABELS: Record<DocumentScope, string> = {
  complete: 'Documenti e comande completi',
  partial: 'Parziali da verificare',
  all: 'Tutte le evidenze',
}

const REASONS: Record<string, string> = {
  COMPLETE_PRINTABLE_DOCUMENT: 'Documento completo disponibile in PDF.',
  PARTIAL_PRINTABLE_DOCUMENT: 'Il contenuto è parziale e richiede verifica. Il PDF è disponibile nell’archivio separato dei parziali.',
  DEVICE_RESPONSE: 'Risposta tecnica del dispositivo: non è un documento o una comanda stampabile.',
  TECHNICAL_CANCEL_COMMAND: 'Comando tecnico di annullamento: non contiene un documento stampabile.',
  EMPTY_BODY: 'Il contenuto acquisito non contiene testo stampabile.',
  CONTROL_ONLY_BODY: 'Il contenuto contiene soltanto comandi o codici di controllo.',
  FOOTER_ONLY: 'È presente soltanto un frammento finale, senza il corpo del documento.',
  EMPTY_REPORT_TEMPLATE: 'Modello di report vuoto, privo di contenuto da esportare.',
  UNCLASSIFIED_DOCUMENT: 'Il contenuto non è riconosciuto come documento o comanda stampabile.',
  OTHER_DOCUMENT_SCOPE: 'Disponibile nell’altro archivio: completi o parziali.',
  NO_ACTIVE_VERSION: 'Non è disponibile una versione interpretabile del documento.',
}

export function documentReasonLabel(code?: string) {
  return code && REASONS[code] ? REASONS[code] : 'La classificazione per il PDF non è disponibile. Le evidenze acquisite restano consultabili.'
}

export function canDownloadDocumentPdf(document: Pick<DocumentRecord, 'pdf_category'>) {
  return document.pdf_category === 'complete' || document.pdf_category === 'partial'
}

export function documentPdfLabel(document: Pick<DocumentRecord, 'pdf_category'>) {
  if (document.pdf_category === 'complete') return 'Completo'
  if (document.pdf_category === 'partial') return 'Parziale da verificare'
  if (document.pdf_category === 'excluded') return 'Evidenza esclusa dai PDF'
  return 'Classificazione non disponibile'
}

export function documentsMatchScope(documents: readonly Pick<DocumentRecord, 'pdf_category'>[], scope: DocumentScope) {
  return scope === 'all' || documents.every((document) => document.pdf_category === scope)
}
