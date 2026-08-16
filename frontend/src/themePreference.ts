import { createContext, useContext } from 'react'
import { APP_THEME_OPTIONS, type AppThemeId } from './theme'

export interface ThemePreferenceValue {
  themeId: AppThemeId
  setThemeId: (themeId: AppThemeId) => void
}

export const ThemePreferenceContext = createContext<ThemePreferenceValue | null>(null)

export function useThemePreference(): ThemePreferenceValue & { options: typeof APP_THEME_OPTIONS } {
  const context = useContext(ThemePreferenceContext)
  if (!context) throw new Error('useThemePreference deve essere usato dentro ThemePreferenceProvider')
  return { ...context, options: APP_THEME_OPTIONS }
}
