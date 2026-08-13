import {
  AccountTreeOutlined,
  DashboardOutlined,
  DevicesOutlined,
  DescriptionOutlined,
  FactCheckOutlined,
  FileUploadOutlined,
  LogoutOutlined,
  MenuOutlined,
  SearchOutlined,
  SettingsSuggestOutlined,
  ShieldOutlined,
} from '@mui/icons-material'
import {
  AppBar,
  Avatar,
  Box,
  Divider,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Toolbar,
  Tooltip,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import { useState, type ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import type { User } from '../types'

const drawerWidth = 252
const navigation = [
  { label: 'Dashboard', path: '/', icon: <DashboardOutlined /> },
  { label: 'Transazioni', path: '/transazioni', icon: <AccountTreeOutlined /> },
  { label: 'Documenti', path: '/documenti', icon: <DescriptionOutlined /> },
  { label: 'Alert antifrode', path: '/alert', icon: <ShieldOutlined /> },
  { label: 'Regole', path: '/regole', icon: <SettingsSuggestOutlined /> },
  { label: 'Ricerca', path: '/ricerca', icon: <SearchOutlined /> },
  { label: 'Dispositivi', path: '/dispositivi', icon: <DevicesOutlined /> },
  { label: 'Importazioni', path: '/importazioni', icon: <FileUploadOutlined /> },
]

export function Layout({ children, user, onLogout }: { children: ReactNode; user: User; onLogout: () => void }) {
  const theme = useTheme()
  const desktop = useMediaQuery(theme.breakpoints.up('lg'))
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  const drawer = (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column', bgcolor: '#102a43', color: 'white' }}>
      <Toolbar sx={{ minHeight: 76, gap: 1.5 }}>
        <FactCheckOutlined sx={{ color: '#f2b84b' }} />
        <Box>
          <Typography fontWeight={780} letterSpacing="-0.02em">RetailPrintGuard</Typography>
          <Typography variant="caption" sx={{ color: '#9fb3c3' }}>Centro antifrode</Typography>
        </Box>
      </Toolbar>
      <Divider sx={{ borderColor: 'rgba(255,255,255,.08)' }} />
      <List sx={{ px: 1.5, py: 2 }}>
        {navigation.map((item) => (
          <ListItemButton
            key={item.path}
            component={NavLink}
            to={item.path}
            selected={location.pathname === item.path || (item.path !== '/' && location.pathname.startsWith(item.path))}
            onClick={() => setMobileOpen(false)}
            sx={{
              mb: 0.5,
              borderRadius: 1.5,
              color: '#d9e2ec',
              '&.Mui-selected': { bgcolor: 'rgba(242,184,75,.14)', color: '#fff' },
              '&.Mui-selected:hover': { bgcolor: 'rgba(242,184,75,.2)' },
            }}
          >
            <ListItemIcon sx={{ minWidth: 40, color: 'inherit' }}>{item.icon}</ListItemIcon>
            <ListItemText primary={item.label} primaryTypographyProps={{ fontWeight: 620 }} />
          </ListItemButton>
        ))}
      </List>
      <Box sx={{ mt: 'auto', p: 2 }}>
        <Divider sx={{ mb: 2, borderColor: 'rgba(255,255,255,.08)' }} />
        <Typography variant="body2" fontWeight={650}>{user.username}</Typography>
        <Typography variant="caption" sx={{ color: '#9fb3c3' }}>{user.roles.join(' · ')}</Typography>
      </Box>
    </Box>
  )
  return (
    <Box sx={{ minHeight: '100vh', display: 'flex' }}>
      {desktop ? (
        <Drawer variant="permanent" sx={{ width: drawerWidth, '& .MuiDrawer-paper': { width: drawerWidth, border: 0 } }}>{drawer}</Drawer>
      ) : (
        <Drawer open={mobileOpen} onClose={() => setMobileOpen(false)} sx={{ '& .MuiDrawer-paper': { width: drawerWidth } }}>{drawer}</Drawer>
      )}
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <AppBar position="sticky" color="inherit" elevation={0} sx={{ borderBottom: '1px solid #e5ebef' }}>
          <Toolbar sx={{ gap: 1 }}>
            {!desktop && <IconButton aria-label="Apri navigazione" onClick={() => setMobileOpen(true)}><MenuOutlined /></IconButton>}
            <Box sx={{ flex: 1 }} />
            <Avatar sx={{ width: 32, height: 32, bgcolor: 'primary.main', fontSize: 14 }}>{user.username.slice(0, 2).toUpperCase()}</Avatar>
            <Tooltip title="Esci"><IconButton aria-label="Esci" onClick={onLogout}><LogoutOutlined /></IconButton></Tooltip>
          </Toolbar>
        </AppBar>
        <Box component="main" sx={{ p: { xs: 2, md: 3.5 }, maxWidth: 1680, mx: 'auto' }}>{children}</Box>
      </Box>
    </Box>
  )
}
