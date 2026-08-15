export const OPERATIONAL_REDUCTION_FILTERS = Object.freeze({
  operational_economic_only: 'true',
  reduction_only: 'true',
  minimum_difference: '0.01',
})

export function operationalReductionTransactionsPath(period: URLSearchParams) {
  const query = new URLSearchParams()
  for (const key of ['from', 'to']) {
    const value = period.get(key)
    if (value) query.set(key, value)
  }
  for (const [key, value] of Object.entries(OPERATIONAL_REDUCTION_FILTERS)) {
    query.set(key, value)
  }
  return `/transazioni?${query.toString()}`
}

export function isOperationalReductionFilter(params: URLSearchParams) {
  return params.get('operational_economic_only') === 'true'
    && params.get('reduction_only') === 'true'
}
