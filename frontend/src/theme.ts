import { alpha, createTheme, type PaletteMode, type Theme } from '@mui/material/styles'

export const APP_THEME_STORAGE_KEY = 'retailprintguard.ui.theme.v1'
export const DEFAULT_APP_THEME_ID = 'office'

export const APP_THEME_OPTIONS = [
  { id: 'office', label: 'Office professionale', shortLabel: 'Office', description: 'Chiaro, sobrio e adatto al lavoro quotidiano.', swatch: '#173f5f' },
  { id: 'dark', label: 'Scuro professionale', shortLabel: 'Scuro', description: 'Blu antracite riposante per ambienti poco illuminati.', swatch: '#68b7e8' },
  { id: 'unix', label: 'Unix old school', shortLabel: 'Unix', description: 'Terminale classico color ambra, monospazio e bordi netti.', swatch: '#d9a441' },
  { id: 'hacker', label: 'Hacker neon', shortLabel: 'Hacker', description: 'Nero profondo con accenti verdi e ciano ad alto contrasto.', swatch: '#4bf28f' },
] as const

export type AppThemeId = (typeof APP_THEME_OPTIONS)[number]['id']
export type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>

export interface AppChromeTokens {
  drawerBackground: string
  drawerText: string
  drawerMuted: string
  drawerAccent: string
  drawerDivider: string
  drawerSelected: string
  drawerSelectedHover: string
  appBarBackground: string
  appBarBorder: string
  loginStart: string
  loginMiddle: string
  loginEnd: string
  loginFooter: string
  receiptPaper: string
  receiptInk: string
  cardShadow: string
  browserThemeColor: string
}

declare module '@mui/material/styles' {
  interface Theme {
    appChrome: AppChromeTokens
  }
  interface ThemeOptions {
    appChrome?: AppChromeTokens
  }
}

interface ThemePreset {
  mode: PaletteMode
  primary: { main: string; dark: string; light: string; contrastText: string }
  secondary: { main: string; dark: string; light: string; contrastText: string }
  background: { default: string; paper: string }
  text: { primary: string; secondary: string }
  divider: string
  error: string
  warning: string
  success: string
  fontFamily: string
  radius: number
  chrome: AppChromeTokens
}

const presets: Record<AppThemeId, ThemePreset> = {
  office: {
    mode: 'light',
    primary: { main: '#173f5f', dark: '#102a43', light: '#3d6a89', contrastText: '#ffffff' },
    secondary: { main: '#c47f17', dark: '#8a590f', light: '#e2ad58', contrastText: '#0d2639' },
    background: { default: '#f3f6f8', paper: '#ffffff' },
    text: { primary: '#102a43', secondary: '#526777' },
    divider: '#dfe7ec',
    error: '#b42318',
    warning: '#b54708',
    success: '#157f3d',
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif',
    radius: 10,
    chrome: {
      drawerBackground: '#102a43',
      drawerText: '#d9e2ec',
      drawerMuted: '#9fb3c3',
      drawerAccent: '#f2b84b',
      drawerDivider: 'rgba(255,255,255,.08)',
      drawerSelected: 'rgba(242,184,75,.14)',
      drawerSelectedHover: 'rgba(242,184,75,.22)',
      appBarBackground: '#ffffff',
      appBarBorder: '#e5ebef',
      loginStart: '#102a43',
      loginMiddle: '#173f5f',
      loginEnd: '#2f6c7e',
      loginFooter: '#d9e2ec',
      receiptPaper: '#fffef9',
      receiptInk: '#1f2933',
      cardShadow: '0 8px 30px rgba(16,42,67,.06)',
      browserThemeColor: '#102a43',
    },
  },
  dark: {
    mode: 'dark',
    primary: { main: '#68b7e8', dark: '#2a789f', light: '#a8daf7', contrastText: '#071722' },
    secondary: { main: '#f0b75e', dark: '#bc7e27', light: '#ffd692', contrastText: '#15100a' },
    background: { default: '#0d151d', paper: '#17212b' },
    text: { primary: '#e8f1f7', secondary: '#a8bac7' },
    divider: 'rgba(206,226,239,.14)',
    error: '#ff766e',
    warning: '#ffb45b',
    success: '#62d38b',
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif',
    radius: 10,
    chrome: {
      drawerBackground: '#08121b',
      drawerText: '#e1edf4',
      drawerMuted: '#90a7b8',
      drawerAccent: '#68b7e8',
      drawerDivider: 'rgba(225,237,244,.1)',
      drawerSelected: 'rgba(104,183,232,.18)',
      drawerSelectedHover: 'rgba(104,183,232,.27)',
      appBarBackground: '#111b24',
      appBarBorder: 'rgba(206,226,239,.13)',
      loginStart: '#061018',
      loginMiddle: '#122735',
      loginEnd: '#193c52',
      loginFooter: '#d7eaf5',
      receiptPaper: '#f7f3e8',
      receiptInk: '#202830',
      cardShadow: '0 12px 34px rgba(0,0,0,.24)',
      browserThemeColor: '#08121b',
    },
  },
  unix: {
    mode: 'dark',
    primary: { main: '#d9a441', dark: '#9a701e', light: '#f4ce7b', contrastText: '#17120a' },
    secondary: { main: '#8fbf68', dark: '#5f8d41', light: '#bde39e', contrastText: '#0e160a' },
    background: { default: '#15130f', paper: '#211e18' },
    text: { primary: '#f1e4bd', secondary: '#c4b78f' },
    divider: 'rgba(241,228,189,.18)',
    error: '#f07c64',
    warning: '#e6b85c',
    success: '#94c66d',
    fontFamily: '"Cascadia Mono", "IBM Plex Mono", Consolas, monospace',
    radius: 2,
    chrome: {
      drawerBackground: '#090a08',
      drawerText: '#e8d9a9',
      drawerMuted: '#a99e7a',
      drawerAccent: '#d9a441',
      drawerDivider: 'rgba(232,217,169,.15)',
      drawerSelected: 'rgba(217,164,65,.2)',
      drawerSelectedHover: 'rgba(217,164,65,.3)',
      appBarBackground: '#11110e',
      appBarBorder: 'rgba(217,164,65,.25)',
      loginStart: '#070806',
      loginMiddle: '#17150f',
      loginEnd: '#2b2517',
      loginFooter: '#e8d9a9',
      receiptPaper: '#f1e6c9',
      receiptInk: '#221d13',
      cardShadow: 'none',
      browserThemeColor: '#090a08',
    },
  },
  hacker: {
    mode: 'dark',
    primary: { main: '#4bf28f', dark: '#19a956', light: '#9dffc1', contrastText: '#021007' },
    secondary: { main: '#38d4e8', dark: '#128ea0', light: '#91f3ff', contrastText: '#021013' },
    background: { default: '#020805', paper: '#07140c' },
    text: { primary: '#d8ffe6', secondary: '#85c99e' },
    divider: 'rgba(75,242,143,.18)',
    error: '#ff6380',
    warning: '#ffd166',
    success: '#4bf28f',
    fontFamily: '"Cascadia Mono", "JetBrains Mono", Consolas, monospace',
    radius: 5,
    chrome: {
      drawerBackground: '#010503',
      drawerText: '#caffda',
      drawerMuted: '#72b68a',
      drawerAccent: '#4bf28f',
      drawerDivider: 'rgba(75,242,143,.16)',
      drawerSelected: 'rgba(75,242,143,.17)',
      drawerSelectedHover: 'rgba(75,242,143,.27)',
      appBarBackground: '#030b07',
      appBarBorder: 'rgba(75,242,143,.22)',
      loginStart: '#010503',
      loginMiddle: '#041109',
      loginEnd: '#071c10',
      loginFooter: '#baffd1',
      receiptPaper: '#eaf9ef',
      receiptInk: '#061a0d',
      cardShadow: '0 0 0 1px rgba(75,242,143,.08), 0 10px 34px rgba(0,0,0,.34)',
      browserThemeColor: '#010503',
    },
  },
}

export function normalizeAppThemeId(value: unknown): AppThemeId {
  return typeof value === 'string' && APP_THEME_OPTIONS.some((option) => option.id === value)
    ? value as AppThemeId
    : DEFAULT_APP_THEME_ID
}

function browserStorage(): ThemeStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

export function readStoredTheme(storage: ThemeStorage | null = browserStorage()): AppThemeId {
  if (!storage) return DEFAULT_APP_THEME_ID
  try {
    return normalizeAppThemeId(storage.getItem(APP_THEME_STORAGE_KEY))
  } catch {
    return DEFAULT_APP_THEME_ID
  }
}

export function writeStoredTheme(themeId: AppThemeId, storage: ThemeStorage | null = browserStorage()): void {
  if (!storage) return
  try {
    storage.setItem(APP_THEME_STORAGE_KEY, normalizeAppThemeId(themeId))
  } catch {
    // Private browsing and browser policy can make localStorage unavailable.
  }
}

export function createAppTheme(themeId: AppThemeId): Theme {
  const preset = presets[normalizeAppThemeId(themeId)]
  return createTheme({
    appChrome: preset.chrome,
    palette: {
      mode: preset.mode,
      primary: preset.primary,
      secondary: preset.secondary,
      error: { main: preset.error },
      warning: { main: preset.warning },
      success: { main: preset.success },
      background: preset.background,
      text: preset.text,
      divider: preset.divider,
    },
    typography: {
      fontFamily: preset.fontFamily,
      h1: { fontSize: '1.75rem', fontWeight: 750, letterSpacing: '-0.02em' },
      h2: { fontSize: '1.25rem', fontWeight: 700 },
      button: { textTransform: 'none', fontWeight: 650 },
    },
    shape: { borderRadius: preset.radius },
    components: {
      MuiButton: { defaultProps: { disableElevation: true } },
      MuiCard: {
        styleOverrides: {
          root: ({ theme }) => ({
            backgroundImage: 'none',
            border: `1px solid ${alpha(theme.palette.primary.main, theme.palette.mode === 'dark' ? 0.2 : 0.09)}`,
            boxShadow: theme.appChrome.cardShadow,
          }),
        },
      },
      MuiPaper: { styleOverrides: { root: { backgroundImage: 'none' } } },
      MuiTableCell: {
        styleOverrides: {
          head: ({ theme }) => ({
            backgroundColor: alpha(theme.palette.primary.main, theme.palette.mode === 'dark' ? 0.1 : 0.045),
            color: theme.palette.text.primary,
            fontWeight: 700,
          }),
        },
      },
      MuiCssBaseline: {
        styleOverrides: {
          '::selection': {
            backgroundColor: alpha(preset.primary.main, 0.35),
          },
        },
      },
    },
  })
}

export const theme = createAppTheme(DEFAULT_APP_THEME_ID)
