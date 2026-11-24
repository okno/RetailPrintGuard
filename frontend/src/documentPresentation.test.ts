import { describe, expect, it } from 'vitest'
import {
  ALL_EVIDENCE_FILTER,
  confidencePercent,
  deviceLabel,
  documentApiSearchParams,
  documentDeviceOptions,
  documentFilterParams,
  documentLinePrice,
  documentPreviewTabLabel,
  documentServiceMode,
  documentServiceModeLabel,
  documentTypeLabel,
  isEpsonDailyReport,
  selectedDocumentScope,
  serviceModeEvidenceLabel,
} from './documentPresentation'

describe('primary document presentation', () => {
  it('requests the server-computed complete scope before pagination', () => {
    const params = documentApiSearchParams(new URLSearchParams('limit=25&offset=0'))
    expect(params.get('document_scope')).toBe('complete')
    expect(params.get('limit')).toBe('25')
    expect(params.has('exclude_type')).toBe(false)
    expect(params.has('include_technical')).toBe(false)
  })

  it('keeps technical evidence available through an explicit filter', () => {
    const technical = documentApiSearchParams(new URLSearchParams('type=DEVICE_RESPONSE'))
    expect(technical.get('type')).toBe('DEVICE_RESPONSE')
    expect(technical.get('include_technical')).toBe('true')
    expect(technical.get('document_scope')).toBe('all')

    const all = documentApiSearchParams(new URLSearchParams(`type=${ALL_EVIDENCE_FILTER}`))
    expect(all.has('type')).toBe(false)
    expect(all.has('exclude_type')).toBe(false)
    expect(all.get('include_technical')).toBe('true')
    expect(all.get('document_scope')).toBe('all')
  })

  it('keeps the partial archive separate while preserving type and acquisition filters', () => {
    const params = documentApiSearchParams(new URLSearchParams('document_scope=partial&type=CANCELLATION&period=custom&from=2026-08-12T22%3A00%3A00Z&include_technical=true'))
    expect(params.get('document_scope')).toBe('partial')
    expect(params.get('type')).toBe('CANCELLATION')
    expect(params.get('from')).toBe('2026-08-12T22:00:00Z')
    expect(params.has('period')).toBe(false)
    expect(params.has('include_technical')).toBe(false)
    expect(selectedDocumentScope(new URLSearchParams('document_scope=invalid'))).toBe('complete')
  })

  it('renders fractional and percentage confidence on the same scale', () => {
    expect(confidencePercent(0.96)).toBe(96)
    expect(confidencePercent(96)).toBe(96)
    expect(confidencePercent(undefined)).toBeUndefined()
  })

  it('preserves missing prices and the provenance of attributed prices from API null values', () => {
    expect(documentLinePrice({ unit_price: null, derived_unit_price: null })).toBeUndefined()
    expect(documentLinePrice({})).toBeUndefined()
    expect(documentLinePrice({ unit_price: null, derived_unit_price: '2.00' })).toEqual({ value: '2.00', derived: true })
    expect(documentLinePrice({ unit_price: '0.00', derived_unit_price: '2.00' })).toEqual({ value: '0.00', derived: false })
  })

  it('uses operational Italian labels without changing persisted identifiers', () => {
    expect(documentTypeLabel('KITCHEN_ORDER')).toBe('COMANDA')
    expect(documentTypeLabel('SHIFT_END_REPORT')).toBe('REPORT DI FINE TURNO')
    expect(documentTypeLabel('DEVICE_RESPONSE')).toBe('RISPOSTA TECNICA DISPOSITIVO')
    expect(documentTypeLabel('INVOICE')).toBe('FATTURA')
    const devices = [
      { id: 'pos_1', name: 'Comande BAR' },
      { id: 'pos_2', name: 'Comande CUCINA' },
      { id: 'pos_3', name: 'Comande PIZZERIA' },
      { id: 'fiscal_test', name: 'Legacy name', display_name: 'Fiscale RECEPTION' },
    ]
    expect(deviceLabel('pos_1', devices)).toBe('Comande BAR')
    expect(deviceLabel('pos_2', devices)).toBe('Comande CUCINA')
    expect(deviceLabel('pos_3', devices)).toBe('Comande PIZZERIA')
    expect(deviceLabel('fiscal_test', devices)).toBe('Fiscale RECEPTION')
    expect(deviceLabel('pos_1')).toBe('pos_1')
    expect(deviceLabel('rch_1')).toBe('rch_1')
  })

  it('shows service mode only for sales and orders, keeping conflicts undetermined', () => {
    expect(documentServiceModeLabel({ type: 'COMMERCIAL_DOCUMENT', table_code: 'TABLE-TEST-1' })).toBe('In sede (Tavolo)')
    expect(documentServiceModeLabel({ type: 'KITCHEN_ORDER', order_code: 'ORDER-TEST-1' })).toBe('Asporto (Ordine)')
    expect(documentServiceMode({ type: 'MANAGEMENT_DOCUMENT', order_code: 'ORDER-TEST-1', table_code: 'TABLE-TEST-1' })).toBe('UNDETERMINED')
    expect(documentServiceModeLabel({ type: 'SHIFT_END_REPORT' })).toBeUndefined()
    expect(documentServiceModeLabel({ type: 'DEVICE_RESPONSE' })).toBeUndefined()
    expect(serviceModeEvidenceLabel({ service_mode_evidence: 'CONFLICTING_ORDER_AND_TABLE_MARKERS' })).toContain('Entrambi')
    expect(serviceModeEvidenceLabel({ service_mode_evidence: 'EPSON_SOAP_PRINT_REC_MESSAGE_TABLE' })).toBe('Tavolo osservato nel comando SOAP Epson')
  })

  it('presents the Epson daily closure as a report with its own Z progressive', () => {
    const epson = { type: 'SHIFT_END_REPORT', subtype: 'EPSON_XZ_REPORT_OBSERVED' }
    const rch = { type: 'SHIFT_END_REPORT', subtype: 'RCH_REPORT_FINE_TURNO_LITERAL' }
    expect(isEpsonDailyReport(epson)).toBe(true)
    expect(isEpsonDailyReport(rch)).toBe(false)
    expect(documentTypeLabel(epson.type)).toBe(documentTypeLabel(rch.type))
    expect(documentPreviewTabLabel(epson)).toBe('Rapporto')
    expect(documentPreviewTabLabel(rch)).toBe('Rapporto')
    expect(documentPreviewTabLabel({ type: 'COMMERCIAL_DOCUMENT' })).toBe('Scontrino')
    const params = documentApiSearchParams(new URLSearchParams('type=SHIFT_END_REPORT&device_id=epson_reception'))
    expect(params.get('document_scope')).toBe('complete')
    expect(params.get('type')).toBe('SHIFT_END_REPORT')
    expect(params.get('device_id')).toBe('epson_reception')
  })

  it('offers real devices by display name and retains a saved ID until devices load', () => {
    const devices = [
      { id: 'epson_reception', name: 'Epson', display_name: 'Fiscale reception' },
      { id: 'rch_bar', name: 'Cassa bar' },
    ]
    expect(documentDeviceOptions(devices, 'epson_reception')).toEqual([
      { id: 'epson_reception', label: 'Fiscale reception' },
      { id: 'rch_bar', label: 'Cassa bar' },
    ])
    expect(documentDeviceOptions(undefined, 'legacy_device')).toEqual([
      { id: 'legacy_device', label: 'legacy_device' },
    ])
    expect(documentDeviceOptions(devices, 'legacy_device')[0]).toEqual({
      id: 'legacy_device', label: 'legacy_device',
    })
  })

  it('selects and clears a device without losing the other document filters', () => {
    const original = new URLSearchParams('type=SHIFT_END_REPORT&document_scope=complete&order_code=A-1&offset=50&limit=25')
    const selected = documentFilterParams(original, 'device_id', 'epson_reception', 25)
    expect(selected.get('device_id')).toBe('epson_reception')
    expect(selected.get('type')).toBe('SHIFT_END_REPORT')
    expect(selected.get('order_code')).toBe('A-1')
    expect(selected.get('offset')).toBe('0')
    expect(documentApiSearchParams(selected).get('device_id')).toBe('epson_reception')
    expect(original.has('device_id')).toBe(false)
    const cleared = documentFilterParams(selected, 'device_id', '', 25)
    expect(cleared.has('device_id')).toBe(false)
    expect(cleared.get('type')).toBe('SHIFT_END_REPORT')
    expect(cleared.get('order_code')).toBe('A-1')
  })
})
