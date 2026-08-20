export const NOT_OBSERVED_IN_FLOW = 'Non osservato nel flusso'

export function rchTimestampEvidenceLabel(value: string | null | undefined) {
  if (value === 'RCH_PRINTED_TEXT') return 'Testo stampato dalla cassa RCH'
  if (value === 'RCH_APPLICATION_PRINTED_TEXT') return 'Testo applicativo stampato dalla RCH'
  if (value === 'RCH_FOOTER_PRINTED_TEXT') return 'Footer stampato dalla RCH'
  if (value === 'ESC_POS_PRINTED_OPERATOR_LINE') return 'Riga operatore stampata dal gestionale POS'
  if (value === 'DEVICE_METADATA_CONFIGURED') return 'Metadato dispositivo configurato (non osservato nel flusso)'
  return value ? `Evidenza dichiarata: ${value}` : NOT_OBSERVED_IN_FLOW
}

export function rchSerialEvidenceLabel(value: string | null | undefined) {
  if (value === 'RCH_PRINTED_RT_PREFIX') return 'Seriale osservato nel prefisso RT stampato dalla RCH'
  if (value === 'RCH_PRINTED_BARE_SERIAL_AFTER_FOOTER') return 'Seriale osservato dopo il footer stampato dalla RCH'
  if (value === 'DEVICE_METADATA_CONFIGURED') return 'Metadato dispositivo configurato (non osservato nel flusso)'
  return value ? `Evidenza dichiarata: ${value}` : NOT_OBSERVED_IN_FLOW
}

export function rchClockOffsetLabel(value: number | null | undefined) {
  if (value == null) {
    return 'Non calcolabile: uno o entrambi gli orari non sono stati osservati nel flusso'
  }
  if (value === 0) return '0 s · orologi allineati al minuto/secondo osservato'
  const magnitude = Math.abs(value)
  const minutes = Math.floor(magnitude / 60)
  const seconds = magnitude % 60
  const duration = [minutes ? `${minutes} min` : '', seconds ? `${seconds} s` : ''].filter(Boolean).join(' ')
  const relation = value < 0 ? 'footer RCH indietro' : 'footer RCH avanti'
  return `${value > 0 ? '+' : '−'}${magnitude} s · ${relation} di ${duration} rispetto all’ora applicativa`
}
