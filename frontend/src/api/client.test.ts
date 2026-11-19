import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, archivePdf, clearSession, downloadApi, login, scopedQueryKey, session } from './client'

afterEach(() => {
  clearSession()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('authenticated archive downloads', () => {
  it('fetches PDF bytes with a header token, abort signal and no cached response', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'synthetic-pdf-token', user: { id: 'auditor', username: 'auditor', roles: ['AUDITOR'], active: true } })))
      .mockResolvedValueOnce(new Response('%PDF-1.4\nsynthetic', { headers: { 'Content-Type': 'application/pdf' } }))
    vi.stubGlobal('fetch', fetchMock)
    await login('auditor', 'synthetic-password')
    const controller = new AbortController()
    const blob = await archivePdf('/exports/synthetic/documents/1/pdf', controller.signal)
    expect(await blob.text()).toBe('%PDF-1.4\nsynthetic')
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/api/v1/exports/synthetic/documents/1/pdf')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer synthetic-pdf-token')
    expect(init.signal).toBe(controller.signal)
    expect(init.cache).toBe('no-store')
  })

  it('rejects non-PDF bodies and oversized preview responses before displaying them', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('not a PDF', { headers: { 'Content-Type': 'application/pdf' } }))
      .mockResolvedValueOnce(new Response('%PDF-synthetic', { headers: { 'Content-Type': 'application/pdf', 'Content-Length': String(16 * 1024 * 1024 + 1) } })))
    await expect(archivePdf('/exports/synthetic/documents/1/pdf')).rejects.toThrow('PDF valido')
    await expect(archivePdf('/exports/synthetic/documents/1/pdf')).rejects.toThrow('16 MiB')
  })
  it('sends the current bearer token and releases the temporary download URL', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: 'synthetic-export-token',
        user: { id: 'auditor-test', username: 'auditor', roles: ['AUDITOR'], active: true },
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response('synthetic-zip-content', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const anchor = { href: '', download: '', rel: '', click: vi.fn() }
    vi.stubGlobal('document', { createElement: vi.fn().mockReturnValue(anchor) })
    const createUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic-export')
    const revokeUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)

    await login('auditor', 'synthetic-password')
    await downloadApi('/exports/synthetic-job/download', 'requested-archive.zip')

    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/api/v1/exports/synthetic-job/download')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer synthetic-export-token')
    expect(createUrl).toHaveBeenCalledOnce()
    expect(anchor.download).toBe('requested-archive.zip')
    expect(anchor.click).toHaveBeenCalledOnce()
    expect(revokeUrl).toHaveBeenCalledWith('blob:synthetic-export')
  })

  it('clears an expired session and never creates a download for an API error', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_token: 'synthetic-expiring-token',
        user: { id: 'auditor-test', username: 'auditor', roles: ['AUDITOR'], active: true },
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Token scaduto' }), { status: 401 })))
    await login('auditor', 'synthetic-password')
    expect(session().token).toBe('synthetic-expiring-token')
    const createUrl = vi.spyOn(URL, 'createObjectURL')
    await expect(downloadApi('/exports/synthetic-job/download', 'archive.zip')).rejects.toMatchObject({ status: 401 })
    expect(createUrl).not.toHaveBeenCalled()
    expect(session()).toEqual({ token: null, user: null })
  })
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
