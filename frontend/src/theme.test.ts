import { describe, expect, it } from 'vitest'
import { getContrastRatio } from '@mui/material/styles'
import {
  APP_THEME_OPTIONS,
  APP_THEME_STORAGE_KEY,
  createAppTheme,
  DEFAULT_APP_THEME_ID,
  normalizeAppThemeId,
  readStoredTheme,
  writeStoredTheme,
  type ThemeStorage,
} from './theme'

describe('application themes', () => {
  it('exposes four stable and unique theme identifiers', () => {
    const ids = APP_THEME_OPTIONS.map((option) => option.id)
    expect(ids).toEqual(['office', 'dark', 'unix', 'hacker'])
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('creates distinct themes with the expected modes and readable contrast', () => {
    for (const option of APP_THEME_OPTIONS) {
      const theme = createAppTheme(option.id)
      expect(theme.palette.mode).toBe(option.id === 'office' ? 'light' : 'dark')
      expect(theme.palette.getContrastText(theme.palette.primary.main)).toBeTruthy()
      expect(getContrastRatio(theme.palette.primary.main, theme.palette.primary.contrastText)).toBeGreaterThanOrEqual(4.5)
      expect(getContrastRatio(theme.palette.secondary.main, theme.palette.secondary.contrastText)).toBeGreaterThanOrEqual(4.5)
      expect(theme.appChrome.drawerBackground).not.toBe(theme.appChrome.drawerText)
      expect(theme.appChrome.receiptPaper).not.toBe(theme.appChrome.receiptInk)
    }
  })

  it('normalizes unknown and malformed values to Office', () => {
    expect(normalizeAppThemeId('hacker')).toBe('hacker')
    expect(normalizeAppThemeId('')).toBe(DEFAULT_APP_THEME_ID)
    expect(normalizeAppThemeId('{"theme":"dark"}')).toBe(DEFAULT_APP_THEME_ID)
    expect(normalizeAppThemeId(null)).toBe(DEFAULT_APP_THEME_ID)
  })

  it('reads and writes a valid preference', () => {
    const values = new Map<string, string>()
    const storage: ThemeStorage = {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => { values.set(key, value) },
    }
    writeStoredTheme('unix', storage)
    expect(values.get(APP_THEME_STORAGE_KEY)).toBe('unix')
    expect(readStoredTheme(storage)).toBe('unix')
  })

  it('fails safely when browser storage is absent or denied', () => {
    expect(readStoredTheme(null)).toBe(DEFAULT_APP_THEME_ID)
    const denied: ThemeStorage = {
      getItem: () => { throw new DOMException('denied', 'SecurityError') },
      setItem: () => { throw new DOMException('denied', 'SecurityError') },
    }
    expect(readStoredTheme(denied)).toBe(DEFAULT_APP_THEME_ID)
    expect(() => writeStoredTheme('dark', denied)).not.toThrow()
  })
})
