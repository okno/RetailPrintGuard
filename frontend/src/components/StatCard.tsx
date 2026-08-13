import { Box, Card, CardContent, Typography } from '@mui/material'
import type { ReactNode } from 'react'

export function StatCard({
  label,
  value,
  helper,
  icon,
  tone = '#173f5f',
}: {
  label: string
  value: string | number
  helper?: string
  icon: ReactNode
  tone?: string
}) {
  return (
    <Card sx={{ height: '100%' }}>
      <CardContent sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
        <Box>
          <Typography variant="body2" color="text.secondary" fontWeight={650}>
            {label}
          </Typography>
          <Typography variant="h4" sx={{ mt: 0.75, fontWeight: 760, letterSpacing: '-0.03em' }}>
            {value}
          </Typography>
          {helper && <Typography variant="caption" color="text.secondary">{helper}</Typography>}
        </Box>
        <Box sx={{ color: tone, bgcolor: `${tone}12`, borderRadius: 2, p: 1.25, height: 44 }}>{icon}</Box>
      </CardContent>
    </Card>
  )
}
