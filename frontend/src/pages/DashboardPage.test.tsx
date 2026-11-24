import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DepartmentQuantityCard } from './DashboardPage'

describe('department quantity card', () => {
  it('shows on-site, takeaway, and undetermined net quantities', () => {
    const html = renderToStaticMarkup(
      <DepartmentQuantityCard
        label="Prodotti BAR"
        total="3.5"
        dineIn="1"
        takeaway="2"
        undetermined="0.5"
        icon={<span />}
      />,
    )

    expect(html).toContain('Prodotti BAR · totale netto')
    expect(html).toContain('In sede (Tavolo)')
    expect(html).toContain('Asporto (Ordine)')
    expect(html).toContain('Non determinato: 0,5')
    expect(html).toContain('3,5')
  })

  it('keeps negative adjustments visible', () => {
    const html = renderToStaticMarkup(
      <DepartmentQuantityCard
        label="Prodotti CUCINA"
        total="-5"
        dineIn="0"
        takeaway="-5"
        undetermined="0"
        icon={<span />}
      />,
    )

    expect(html).toContain('-5')
    expect(html).toContain('quantità nette con rettifiche')
  })
})
