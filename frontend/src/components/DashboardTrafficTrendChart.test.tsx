import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DashboardTrafficTrend } from '../types'
import { DashboardTrafficTrendChart } from './DashboardTrafficTrendChart'

function sample(granularity: DashboardTrafficTrend['granularity'] = 'hour'): DashboardTrafficTrend {
  return {
    granularity,
    timezone: 'Europe/Zurich',
    zero_filled: true,
    covers_total: 5,
    takeaways_total: { bar: 1, kitchen: 1, pizzeria: 0 },
    buckets: [
      { start: '2026-11-25T09:00:00+01:00', covers: 2, takeaways: { bar: 1, kitchen: 0, pizzeria: 0 } },
      { start: '2026-11-25T10:00:00+01:00', covers: 3, takeaways: { bar: 0, kitchen: 1, pizzeria: 0 } },
    ],
  }
}

describe('dashboard traffic trend chart', () => {
  it('shows global covers and separate takeaway order counts by department', () => {
    const html = renderToStaticMarkup(<DashboardTrafficTrendChart trend={sample()} />)

    expect(html).toContain('Coperti osservati: 5 persone')
    expect(html).toContain('Asporti BAR: 1 ordine')
    expect(html).toContain('Asporti CUCINA: 1 ordine')
    expect(html).toContain('Asporti PIZZERIA: 0 ordini')
    expect(html).toContain('le serie BAR, CUCINA e PIZZERIA non si sommano')
    expect(html).toContain('Asse X: ore. Asse Y: quantità di persone o ordini.')
    expect(html).toContain('Mostra i valori per ora')
    expect(html).toContain('09:00')
  })

  it('labels daily buckets and preserves a zero value', () => {
    const trend = sample('day')
    trend.buckets[1]!.start = '2026-11-26T00:00:00+01:00'
    const html = renderToStaticMarkup(<DashboardTrafficTrendChart trend={trend} />)

    expect(html).toContain('Asse X: giorni. Asse Y: quantità di persone o ordini.')
    expect(html).toContain('Mostra i valori per giorno')
    expect(html).toContain('PIZZERIA: 0')
    expect(html).toContain('26/11')
  })

  it('disambiguates the repeated local hour at daylight saving time', () => {
    const trend = sample()
    trend.buckets = [
      { start: '2026-10-25T02:00:00+02:00', covers: 1, takeaways: { bar: 0, kitchen: 0, pizzeria: 0 } },
      { start: '2026-10-25T02:00:00+01:00', covers: 1, takeaways: { bar: 0, kitchen: 0, pizzeria: 0 } },
    ]
    const html = renderToStaticMarkup(<DashboardTrafficTrendChart trend={trend} />)

    expect(html).toContain('GMT+2')
    expect(html).toContain('GMT+1')
  })

  it('marks a sparse extended range and leaves gaps in the line', () => {
    const trend = sample('day')
    trend.zero_filled = false
    trend.buckets[1]!.start = '2026-11-29T00:00:00+01:00'
    const html = renderToStaticMarkup(<DashboardTrafficTrendChart trend={trend} />)

    expect(html).toContain('gli intervalli senza osservazioni non sono riportati')
    expect(html).toMatch(/d="M 56\.0 [\d.]+ M 978\.0 [\d.]+"/)
  })

  it('shows the year on a multi-year axis and pages the complete detail table', () => {
    const trend = sample('day')
    trend.buckets = Array.from({ length: 51 }, (_, index) => ({
      start: new Date(Date.UTC(2026, 11, 15 + index)).toISOString(),
      covers: 1,
      takeaways: { bar: 0, kitchen: 0, pizzeria: 0 },
    }))
    const html = renderToStaticMarkup(<DashboardTrafficTrendChart trend={trend} />)

    expect(html).toContain('15/12/26')
    expect(html).toContain('03/02/27')
    expect(html).toContain('1–50 di 51')
    expect(html).toContain('Righe per pagina')
  })
})
