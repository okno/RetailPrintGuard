import { describe, expect, it } from 'vitest'
import { statusColor } from './StatusChip'

describe('statusColor', () => {
  it('maps operational and fraud states to stable enterprise colors', () => {
    expect(statusColor('ONLINE')).toBe('success')
    expect(statusColor('UNDER_REVIEW')).toBe('warning')
    expect(statusColor('CRITICAL')).toBe('error')
    expect(statusColor('UNKNOWN')).toBe('default')
  })
})
