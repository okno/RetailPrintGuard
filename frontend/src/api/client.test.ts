import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, clearSession, login, scopedQueryKey, session } from './client'

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
})

describe('API client diagnostics and session isolation', () => {
  it('renders structured validation details with the correlation ID', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: [{ loc: ['query', 'q'], msg: 'Stringa troppo corta' }],
    }), {
      status: 422,
      headers: {
        'Content-Type': 'application/json',
        'X-Correlation-ID': 'request-validation-1',
      },
    })))

    const error = await api('/search?q=x').catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 422, correlationId: 'request-validation-1' })
    expect((error as Error).message).toContain('query.q: Stringa troppo corta')
  })

  it('does not expose an HTML reverse-proxy error body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Bad gateway</html>', {
      status: 502,
      headers: { 'X-Correlation-ID': 'gateway-502-1' },
    })))

    const error = await login('auditor', 'secret').catch((reason: unknown) => reason)
    expect(error).toMatchObject({ status: 502, correlationId: 'gateway-502-1' })
    expect((error as Error).message).toBe('Autenticazione non riuscita (HTTP 502)')
  })

  it('scopes query keys to the principal and clears the session on 401', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: 'token-one',
        user: { id: 'user-one', username: 'auditor', roles: ['AUDITOR'], active: true },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Token scaduto' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }))
    vi.stubGlobal('fetch', fetchMock)

    await login('auditor', 'secret')
    expect(scopedQueryKey('documents').at(-1)).toEqual({ userId: 'user-one' })
    await expect(api('/documents')).rejects.toMatchObject({ status: 401 })
    expect(session()).toEqual({ token: null, user: null })
    expect(scopedQueryKey('documents').at(-1)).toEqual({ userId: 'anonymous' })
  })
})
