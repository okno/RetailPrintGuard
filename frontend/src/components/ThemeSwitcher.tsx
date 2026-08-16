import { PaletteOutlined } from '@mui/icons-material'
import { Box, MenuItem, Select, Tooltip, Typography } from '@mui/material'
import { APP_THEME_OPTIONS, type AppThemeId } from '../theme'
import { useThemePreference } from '../themePreference'

export function ThemeSwitcher() {
  const { themeId, setThemeId } = useThemePreference()
  const selectedTheme = APP_THEME_OPTIONS.find((option) => option.id === themeId) ?? APP_THEME_OPTIONS[0]
  return (
    <Tooltip title={`Tema: ${selectedTheme.label}`}>
      <Select<AppThemeId>
        size="small"
        value={themeId}
        onChange={(event) => setThemeId(event.target.value as AppThemeId)}
        inputProps={{ 'aria-label': 'Tema interfaccia' }}
        renderValue={(value) => {
          const option = APP_THEME_OPTIONS.find((candidate) => candidate.id === value)
          return <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <PaletteOutlined fontSize="small" aria-hidden="true" />
            <Box
              aria-hidden="true"
              sx={{ width: 12, height: 12, borderRadius: '50%', bgcolor: option?.swatch ?? 'primary.main', border: '1px solid', borderColor: 'divider', flexShrink: 0 }}
            />
            <Typography component="span" variant="body2" sx={{ display: { xs: 'none', sm: 'inline' }, fontWeight: 650 }}>
              {option?.shortLabel ?? value}
            </Typography>
          </Box>
        }}
        MenuProps={{ PaperProps: { sx: { minWidth: 260, maxWidth: 'calc(100vw - 16px)' } } }}
        sx={{
          minWidth: { xs: 64, sm: 156 },
          bgcolor: 'background.paper',
          '& .MuiSelect-select': { display: 'flex', alignItems: 'center', py: 0.75, pl: { xs: 1, sm: 1.5 } },
          '& .MuiSelect-icon': { display: { xs: 'none', sm: 'block' } },
        }}
      >
        {APP_THEME_OPTIONS.map((option) => (
          <MenuItem key={option.id} value={option.id} sx={{ alignItems: 'flex-start', gap: 1.5, py: 1.25 }}>
            <Box
              aria-hidden="true"
              sx={{ width: 16, height: 16, mt: 0.25, borderRadius: '50%', bgcolor: option.swatch, border: '1px solid', borderColor: 'divider', flexShrink: 0 }}
            />
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="body2" fontWeight={700}>{option.label}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', whiteSpace: 'normal' }}>{option.description}</Typography>
            </Box>
          </MenuItem>
        ))}
      </Select>
    </Tooltip>
  )
}
