import { archivePdf } from './api/client'

export function archivePdfPath(exportId: string, sequence: number) {
  if (!Number.isSafeInteger(sequence) || sequence < 1) throw new Error('Sequenza PDF non valida.')
  return `/exports/${encodeURIComponent(exportId)}/documents/${sequence}/pdf`
}

/** A single selection owns one fetch and one object URL, both released on change/close. */
export function loadArchivePreview(path: string, ready: (url: string) => void, failed: (error: unknown) => void) {
  const controller = new AbortController()
  let active = true
  let url: string | undefined
  void archivePdf(path, controller.signal).then((blob) => {
    if (!active) return
    url = URL.createObjectURL(blob)
    ready(url)
  }).catch((error: unknown) => { if (active) failed(error) })
  return () => {
    active = false
    controller.abort()
    if (url) URL.revokeObjectURL(url)
  }
}

export function downloadPreview(url: string, filename: string) {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename.split('/').at(-1) || 'documento.pdf'
  anchor.rel = 'noopener'
  anchor.click()
}
