import { describe, expect, it } from 'vitest'
import {
  NOT_OBSERVED_IN_FLOW,
  rchClockOffsetLabel,
  rchSerialEvidenceLabel,
  rchTimestampEvidenceLabel,
} from './rchIdentityPresentation'

describe('RCH identity presentation', () => {
  it('explains the signed footer minus application clock offset', () => {
    expect(rchClockOffsetLabel(-120)).toContain('−120 s · footer RCH indietro di 2 min')
    expect(rchClockOffsetLabel(75)).toContain('+75 s · footer RCH avanti di 1 min 15 s')
    expect(rchClockOffsetLabel(0)).toContain('orologi allineati')
  })

  it('does not turn absent wire evidence into an observed value', () => {
    expect(rchClockOffsetLabel(null)).toContain('non sono stati osservati nel flusso')
    expect(rchTimestampEvidenceLabel(undefined)).toBe(NOT_OBSERVED_IN_FLOW)
    expect(rchSerialEvidenceLabel(undefined)).toBe(NOT_OBSERVED_IN_FLOW)
  })

  it('marks a configured serial as not observed on the wire', () => {
    expect(rchSerialEvidenceLabel('DEVICE_METADATA_CONFIGURED')).toBe(
      'Metadato dispositivo configurato (non osservato nel flusso)',
    )
    expect(rchSerialEvidenceLabel('RCH_PRINTED_RT_PREFIX')).toContain('prefisso RT stampato')
  })
})
