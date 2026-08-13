import { Box, Typography } from '@mui/material'
import type { ReactNode } from 'react'

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return (
    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 2, mb: 3 }}>
      <Box>
        <Typography component="h1" variant="h1">{title}</Typography>
        <Typography color="text.secondary" sx={{ mt: 0.5 }}>{subtitle}</Typography>
      </Box>
      {actions}
    </Box>
  )
}
