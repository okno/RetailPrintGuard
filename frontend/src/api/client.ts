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
  }
}

export function session() {
  return { token: accessToken, user: currentUser }
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
  const response = await fetch(`${API_ROOT}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = await response.json()
  if (!response.ok) {
    throw new ApiError(body.detail ?? 'Autenticazione non riuscita', response.status)
  }
  accessToken = body.access_token
  currentUser = body.user
  return body.user as User
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers })
  if (response.status === 401) clearSession()
  if (!response.ok) {
    let message = `Errore HTTP ${response.status}`
    try {
      const body = await response.json()
      message = body.detail ?? body.message ?? message
    } catch {
      // The status and correlation ID still provide a bounded diagnostic.
    }
    throw new ApiError(message, response.status, response.headers.get('X-Correlation-ID') ?? undefined)
  }
  return (await response.json()) as T
}

export async function rawDocument(documentId: string): Promise<Uint8Array> {
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await fetch(`${API_ROOT}/documents/${encodeURIComponent(documentId)}/raw`, { headers })
  if (!response.ok) throw new ApiError('Payload RAW non disponibile', response.status)
  return new Uint8Array(await response.arrayBuffer())
}

export async function downloadApi(path: string, filename: string): Promise<void> {
  const headers = new Headers()
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await fetch(`${API_ROOT}${path}`, { headers })
  if (response.status === 401) clearSession()
  if (!response.ok) throw new ApiError('Esportazione non disponibile', response.status)
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
