import { FactCheckOutlined, LockOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Container, TextField, Typography } from '@mui/material'
import { FormEvent, useState } from 'react'
import { login } from '../api/client'
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
    <Box sx={{ minHeight: '100vh', display: 'grid', placeItems: 'center', p: 2, background: 'linear-gradient(135deg,#102a43 0%,#173f5f 55%,#2f6c7e 100%)' }}>
      <Container maxWidth="xs">
        <Card sx={{ overflow: 'visible' }}>
          <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
            <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', mb: 3 }}>
              <Box sx={{ bgcolor: '#f2b84b22', color: '#b06b00', p: 1.2, borderRadius: 2 }}><FactCheckOutlined /></Box>
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
        <Typography variant="caption" sx={{ display: 'block', textAlign: 'center', color: '#d9e2ec', mt: 2 }}>Accesso registrato nell’audit log · rete autorizzata</Typography>
      </Container>
    </Box>
  )
}
