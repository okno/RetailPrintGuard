import { apiPeriodParams } from './period'

export function discountResultQuery(source: URLSearchParams) {
  const period = apiPeriodParams(source)
  const query = new URLSearchParams()
  for (const name of ['from', 'to', 'device_id', 'kind', 'reconciliation']) {
    const value = period.get(name)
    if (value) query.set(name, value)
  }
  if (query.get('kind') === 'all') query.delete('kind')
  if (source.get('analysis_id')) query.set('with_discounts', query.has('kind') || source.get('with_discounts') !== 'false' ? 'true' : 'false')
  const reconciliation = query.get('reconciliation')
  if (reconciliation === 'all') query.delete('reconciliation')
  else if (reconciliation === 'review' || reconciliation === 'unverified') query.set('reconciliation', source.has('analysis_id') ? 'unverified' : 'review')
  const requestedLimit = Number(source.get('limit') ?? 25)
  const requestedOffset = Number(source.get('offset') ?? 0)
  query.set('limit', String([25, 50, 100].includes(requestedLimit) ? requestedLimit : 25))
  query.set('offset', String(Number.isInteger(requestedOffset) && requestedOffset >= 0 ? requestedOffset : 0))
  return query
}

export function discountResultEndpoint(source: URLSearchParams) {
  const analysisId = source.get('analysis_id')
  const path = analysisId ? `/analyses/${encodeURIComponent(analysisId)}/results` : '/discounts'
  return `${path}?${discountResultQuery(source).toString()}`
}
