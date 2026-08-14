import { Alert, Box, CircularProgress, Typography } from '@mui/material'
import { ApiError } from '../api/client'

export function LoadingState({ label = 'Caricamento…' }: { label?: string }) {
  return (
    <Box role="status" sx={{ display: 'grid', placeItems: 'center', minHeight: 240, gap: 2 }}>
      <CircularProgress size={30} />
      <Typography color="text.secondary">{label}</Typography>
    </Box>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : 'Impossibile caricare i dati.'
  const apiError = error instanceof ApiError ? error : undefined
  return (
    <Alert severity="error" role="alert">
      <Typography component="span" variant="body2">{message}</Typography>
      {apiError?.correlationId && (
        <Typography component="div" variant="caption" sx={{ mt: .5, fontFamily: 'monospace' }}>
          ID diagnostico: {apiError.correlationId}
        </Typography>
      )}
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
