import { Alert, Box, CircularProgress, Typography } from '@mui/material'

export function LoadingState({ label = 'Caricamento…' }: { label?: string }) {
  return (
    <Box role="status" sx={{ display: 'grid', placeItems: 'center', minHeight: 240, gap: 2 }}>
      <CircularProgress size={30} />
      <Typography color="text.secondary">{label}</Typography>
    </Box>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  return (
    <Alert severity="error" role="alert">
      {error instanceof Error ? error.message : 'Impossibile caricare i dati.'}
    </Alert>
  )
}

export function EmptyState({ label = 'Nessun risultato' }: { label?: string }) {
  return (
    <Box sx={{ py: 8, textAlign: 'center' }}>
      <Typography color="text.secondary">{label}</Typography>
    </Box>
  )
}
