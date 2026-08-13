import { alpha, createTheme } from '@mui/material/styles'

export const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#173f5f', dark: '#102a43', light: '#3d6a89' },
    secondary: { main: '#c47f17' },
    error: { main: '#b42318' },
    warning: { main: '#b54708' },
    success: { main: '#157f3d' },
    background: { default: '#f3f6f8', paper: '#ffffff' },
    text: { primary: '#102a43', secondary: '#526777' },
  },
  typography: {
    fontFamily: 'Inter, "Segoe UI", system-ui, sans-serif',
    h1: { fontSize: '1.75rem', fontWeight: 750, letterSpacing: '-0.02em' },
    h2: { fontSize: '1.25rem', fontWeight: 700 },
    button: { textTransform: 'none', fontWeight: 650 },
  },
  shape: { borderRadius: 10 },
  components: {
    MuiCard: {
      styleOverrides: {
        root: ({ theme }) => ({
          border: `1px solid ${alpha(theme.palette.primary.main, 0.09)}`,
          boxShadow: '0 8px 30px rgba(16, 42, 67, 0.06)',
        }),
      },
    },
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiTableCell: { styleOverrides: { head: { fontWeight: 700, color: '#334e68' } } },
  },
})
