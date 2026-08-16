import { CssBaseline, ThemeProvider } from '@mui/material'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  createAppTheme,
  readStoredTheme,
  writeStoredTheme,
  type AppThemeId,
} from './theme'
import { ThemePreferenceContext } from './themePreference'

export function ThemePreferenceProvider({ children }: { children: ReactNode }) {
  const [themeId, setThemeIdState] = useState<AppThemeId>(() => readStoredTheme())
  const theme = useMemo(() => createAppTheme(themeId), [themeId])
  const setThemeId = useCallback((nextThemeId: AppThemeId) => {
    writeStoredTheme(nextThemeId)
    setThemeIdState(nextThemeId)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.rpgTheme = themeId
    document.documentElement.style.colorScheme = theme.palette.mode
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme.palette.mode)
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme.appChrome.browserThemeColor)
  }, [theme, themeId])

  const value = useMemo(() => ({ themeId, setThemeId }), [setThemeId, themeId])
  return (
    <ThemePreferenceContext.Provider value={value}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </ThemeProvider>
    </ThemePreferenceContext.Provider>
  )
}
