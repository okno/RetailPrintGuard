import { describe, expect, it } from 'vitest'
import { additionalDeviceEndpoints } from './deviceEndpoints'

describe('additional printer endpoints', () => {
  it('preserves each endpoint address while exposing the additional port', () => {
    expect(additionalDeviceEndpoints('192.0.2.30:9100', [80])).toEqual(['192.0.2.30:80'])
    expect(additionalDeviceEndpoints('192.0.2.31:9100', [80])).toEqual(['192.0.2.31:80'])
  })

  it('preserves bracketed and legacy unbracketed IPv6 addresses', () => {
    expect(additionalDeviceEndpoints('[2001:db8::1]:9100', [80])).toEqual(['[2001:db8::1]:80'])
    expect(additionalDeviceEndpoints('2001:db8::1:9100', [80])).toEqual(['2001:db8::1:80'])
  })

  it('supports older APIs and avoids duplicate or invalid endpoints', () => {
    expect(additionalDeviceEndpoints('192.0.2.30:9100')).toEqual([])
    expect(additionalDeviceEndpoints('192.0.2.30:9100', [9100, 80, 80, 0, -1, 65536, 8.5])).toEqual(['192.0.2.30:80'])
  })

  it('does not guess an address or port boundary when the endpoint is missing', () => {
    expect(additionalDeviceEndpoints('', [80])).toEqual([])
    expect(additionalDeviceEndpoints('192.0.2.30', [80])).toEqual([])
    expect(additionalDeviceEndpoints(':9100', [80])).toEqual([])
  })
})
