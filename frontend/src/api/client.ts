import type { User } from '../types'

const API_ROOT = '/api/v1'
let accessToken: string | null = null
let currentUser: User | null = null
const sessionListeners = new Set<() => void>()

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly correlationId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function describeDetail(detail: unknown): string | undefined {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== 'object') return String(item)
        const value = item as { loc?: unknown[]; msg?: unknown }
        const location = Array.isArray(value.loc) ? value.loc.join('.') : ''
        const message = typeof value.msg === 'string' ? value.msg : JSON.stringify(item)
        return location ? `${location}: ${message}` : message
      })
      .filter(Boolean)
    return messages.length ? messages.join(' · ') : undefined
  }
  return detail == null ? undefined : String(detail)
}

async function apiError(response: Response, fallback: string): Promise<ApiError> {
  const correlationId = response.headers.get('X-Correlation-ID') ?? undefined
  let message = fallback
  try {
    const text = await response.text()
    if (text) {
      try {
        const body = JSON.parse(text) as { detail?: unknown; message?: unknown; correlation_id?: unknown }
        message = describeDetail(body.detail) ?? describeDetail(body.message) ?? message
      } catch {
        // Reverse-proxy errors can be HTML. Do not expose that body in the UI.
      }
    }
  } catch {
    // Status and correlation ID still provide a bounded diagnostic.
  }
  return new ApiError(message, response.status, correlationId)
}

async function request(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch {
    throw new ApiError('Servizio non raggiungibile. Verificare rete e stato API.', 0)
  }
}

export function session() {
  return { token: accessToken, user: currentUser }
}

export function scopedQueryKey(...parts: readonly unknown[]) {
  return [...parts, { userId: currentUser?.id ?? 'anonymous' }] as const
}

export function clearSession() {
  accessToken = null
  currentUser = null
  sessionListeners.forEach((listener) => listener())
}

export function subscribeSession(listener: () => void) {
  sessionListeners.add(listener)
  return () => {
    sessionListeners.delete(listener)
  }
}

export async function login(username: string, password: string): Promise<User> {
  const response = await request(`${API_ROOT}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) {
    throw await apiError(response, `Autenticazione non riuscita (HTTP ${response.status})`)
  }
  const body = await response.json() as { access_token?: unknown; user?: User }
  if (typeof body.access_token !== 'string' || !body.user?.id) {
    throw new ApiError('Risposta di autenticazione non valida', response.status)
  }
  accessToken = body.access_token
  currentUser = body.user
  sessionListeners.forEach((listener) => listener())
  return body.user as User
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await request(`${API_ROOT}${path}`, { ...init, headers })
  if (response.status === 401) clearSession()
  if (!response.ok) {
    throw await apiError(response, `Errore HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export async function rawDocument(documentId: string, direction: 'request' | 'response' = 'request'): Promise<Uint8Array> {
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await request(`${API_ROOT}/documents/${encodeURIComponent(documentId)}/raw?direction=${direction}&preview_bytes=65536`, { headers })
  if (response.status === 401) clearSession()
  if (!response.ok) throw await apiError(response, 'Payload RAW non disponibile')
  return new Uint8Array(await response.arrayBuffer())
}

export async function downloadApi(path: string, filename: string): Promise<void> {
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await request(`${API_ROOT}${path}`, { headers })
  if (response.status === 401) clearSession()
  if (!response.ok) throw await apiError(response, 'Esportazione non disponibile')
  const blobUrl = URL.createObjectURL(await response.blob())
  try {
    const anchor = document.createElement('a')
    anchor.href = blobUrl
    anchor.download = filename
    anchor.rel = 'noopener'
    anchor.click()
  } finally {
    URL.revokeObjectURL(blobUrl)
  }
}
