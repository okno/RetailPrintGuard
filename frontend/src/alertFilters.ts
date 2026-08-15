export type AlertListView = 'operational' | 'archive' | 'all'

export const DEFAULT_ALERT_VIEW: AlertListView = 'operational'

export function alertApiSearchParams(
  source: URLSearchParams,
  limit: number,
  offset: number,
) {
  const result = new URLSearchParams(source)
  if (!result.has('view')) result.set('view', DEFAULT_ALERT_VIEW)
  result.set('limit', String(limit))
  result.set('offset', String(offset))
  return result
}
