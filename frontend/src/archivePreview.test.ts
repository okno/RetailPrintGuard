import { afterEach, describe, expect, it, vi } from 'vitest'
import { archivePdf } from './api/client'
import { archivePdfPath, loadArchivePreview } from './archivePreview'

vi.mock('./api/client', () => ({ archivePdf: vi.fn() }))
afterEach(() => vi.restoreAllMocks())

describe('one-PDF preview lifecycle', () => {
  it('targets immutable export membership and rejects invalid sequence numbers', () => {
    expect(archivePdfPath('archive/one', 10)).toBe('/exports/archive%2Fone/documents/10/pdf')
    for (const value of [0, -1, 1.5, NaN]) expect(() => archivePdfPath('archive', value)).toThrow()
  })

  it('aborts a pending selection and never displays its late result', async () => {
    let resolve!: (blob: Blob) => void
    vi.mocked(archivePdf).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic')
    const ready = vi.fn()
    const stop = loadArchivePreview('/exports/synthetic/documents/1/pdf', ready, vi.fn())
    const signal = vi.mocked(archivePdf).mock.calls.at(-1)?.[1]
    stop()
    resolve(new Blob(['%PDF-synthetic']))
    await Promise.resolve()
    expect(signal?.aborted).toBe(true)
    expect(create).not.toHaveBeenCalled()
    expect(ready).not.toHaveBeenCalled()
  })

  it('revokes the single loaded object URL on close or selection change', async () => {
    vi.mocked(archivePdf).mockResolvedValueOnce(new Blob(['%PDF-synthetic']))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic')
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const ready = vi.fn()
    const stop = loadArchivePreview('/exports/synthetic/documents/1/pdf', ready, vi.fn())
    await Promise.resolve()
    expect(ready).toHaveBeenCalledWith('blob:synthetic')
    stop()
    expect(revoke).toHaveBeenCalledWith('blob:synthetic')
  })
})
