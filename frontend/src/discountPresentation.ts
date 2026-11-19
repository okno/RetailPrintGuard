const euros = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' })

/** An unavailable amount must not be presented as a measured zero. */
export function discountMoney(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') return 'Non disponibile'
  const amount = Number(value)
  return Number.isFinite(amount) ? euros.format(amount) : 'Non disponibile'
}

export function discountKindLabel(kind: string) {
  if (kind === 'employee_30') return 'Dipendenti 30% esplicito'
  if (kind === 'generic_30') return '30% generico'
  if (kind === 'other') return 'Altro sconto'
  return 'Tipo non disponibile'
}

const reasons: Record<string, string> = {
  line_limit: 'Il documento supera il limite di righe analizzabili.',
  inactive_sale_row: 'Sono presenti righe rimosse o annullate.',
  discount_without_negative_amount: 'La riga di sconto non contiene un importo negativo verificabile.',
  discount_basis_unverified: 'La percentuale non è riconciliata con la base della riga precedente.',
  unclassified_negative_sale_row: 'È presente una riduzione non classificata come sconto.',
  printed_total_missing: 'Il totale stampato non è stato individuato.',
  discount_after_total: 'È presente una riga di sconto dopo il totale.',
  document_total_not_reconciled: 'La somma delle righe non coincide con il totale del documento.',
  document_incomplete: 'Il documento è incompleto.',
  document_type_not_sale: 'Evidenza tecnica o riepilogativa: non conteggiata tra gli sconti dei documenti di vendita.',
  document_not_printable: 'Nessun contenuto documentale utilizzabile per l’analisi degli sconti.',
}

export function discountReasonLabel(code: string) {
  return reasons[code] ?? `Verifica richiesta: ${code.replaceAll('_', ' ')}`
}

export function discountDrilldownPath(period: URLSearchParams, kind?: string, reconciliation?: 'verified' | 'review') {
  const query = new URLSearchParams()
  for (const name of ['from', 'to', 'device_id']) {
    const value = period.get(name)
    if (value) query.set(name, value)
  }
  // Preserve an explicit full-history choice when arriving from the dashboard.
  if (!query.has('from') && !query.has('to')) query.set('period', 'all')
  if (kind) query.set('kind', kind)
  if (reconciliation) query.set('reconciliation', reconciliation)
  return `/sconti?${query.toString()}`
}
