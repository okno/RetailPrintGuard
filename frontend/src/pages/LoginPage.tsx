import { FactCheckOutlined, LockOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Container, TextField, Typography } from '@mui/material'
import { alpha } from '@mui/material/styles'
import { FormEvent, useState } from 'react'
import { login } from '../api/client'
import { ThemeSwitcher } from '../components/ThemeSwitcher'
import type { User } from '../types'

export function LoginPage({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await login(username, password))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Accesso non riuscito')
    } finally {
      setBusy(false)
    }
  }
  return (
    <Box sx={{
      minHeight: '100vh',
      display: 'grid',
      placeItems: 'center',
      p: 2,
      position: 'relative',
      background: (theme) => `linear-gradient(135deg, ${theme.appChrome.loginStart} 0%, ${theme.appChrome.loginMiddle} 58%, ${theme.appChrome.loginEnd} 120%)`,
    }}>
      <Box sx={{ position: 'absolute', top: { xs: 12, sm: 20 }, right: { xs: 12, sm: 20 }, zIndex: 1 }}>
        <ThemeSwitcher />
      </Box>
      <Container maxWidth="xs">
        <Card sx={{ overflow: 'visible' }}>
          <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
            <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', mb: 3 }}>
              <Box sx={{
                bgcolor: (theme) => alpha(theme.palette.secondary.main, theme.palette.mode === 'dark' ? 0.2 : 0.12),
                color: 'secondary.main',
                p: 1.2,
                borderRadius: 2,
              }}><FactCheckOutlined /></Box>
              <Box><Typography variant="h2">RetailPrintGuard</Typography><Typography color="text.secondary" variant="body2">Piattaforma antifrode retail</Typography></Box>
            </Box>
            <Typography component="h1" variant="h1" sx={{ mb: 1 }}>Accesso</Typography>
            <Typography color="text.secondary" sx={{ mb: 3 }}>Inserisci le credenziali aziendali autorizzate.</Typography>
            {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
            <Box component="form" onSubmit={submit} noValidate>
              <TextField fullWidth required autoComplete="username" label="Nome utente" value={username} onChange={(e) => setUsername(e.target.value)} sx={{ mb: 2 }} />
              <TextField fullWidth required type="password" autoComplete="current-password" label="Password" value={password} onChange={(e) => setPassword(e.target.value)} sx={{ mb: 3 }} />
              <Button fullWidth type="submit" size="large" variant="contained" disabled={busy || !username || password.length < 8} startIcon={<LockOutlined />}>
                {busy ? 'Verifica…' : 'Accedi in sicurezza'}
              </Button>
            </Box>
          </CardContent>
        </Card>
        <Typography variant="caption" sx={(theme) => ({ display: 'block', textAlign: 'center', color: theme.appChrome.loginFooter, mt: 2 })}>Accesso registrato nell’audit log · rete autorizzata</Typography>
      </Container>
    </Box>
  )
}
