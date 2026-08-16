import { Box, Card, CardActionArea, CardContent, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

export function StatCard({
  label,
  value,
  helper,
  icon,
  tone,
  to,
}: {
  label: string
  value: string | number
  helper?: string
  icon: ReactNode
  tone?: string
  to?: string
}) {
  const theme = useTheme()
  const resolvedTone = tone ?? theme.palette.primary.main
  const content = <CardContent sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}>
    <Box>
      <Typography variant="body2" color="text.secondary" fontWeight={650}>
        {label}
      </Typography>
      <Typography variant="h4" sx={{ mt: 0.75, fontWeight: 760, letterSpacing: '-0.03em' }}>
        {value}
      </Typography>
      {helper && <Typography variant="caption" color="text.secondary">{helper}</Typography>}
    </Box>
    <Box sx={{
      color: resolvedTone,
      bgcolor: alpha(resolvedTone, theme.palette.mode === 'dark' ? 0.2 : 0.08),
      borderRadius: 2,
      p: 1.25,
      height: 44,
    }}>{icon}</Box>
  </CardContent>
  return (
    <Card sx={{ height: '100%' }}>
      {to ? <CardActionArea component={Link} to={to} sx={{ height: '100%' }}>{content}</CardActionArea> : content}
    </Card>
  )
}
