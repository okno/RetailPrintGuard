import {
  AccountTreeOutlined,
  AssignmentLateOutlined,
  DashboardOutlined,
  DevicesOutlined,
  DescriptionOutlined,
  FactCheckOutlined,
  FileUploadOutlined,
  LogoutOutlined,
  LanOutlined,
  MenuOutlined,
  MonitorHeartOutlined,
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
import type { Role, User } from '../types'
import { ThemeSwitcher } from './ThemeSwitcher'

const drawerWidth = 252
const reviewerRoles: Role[] = ['ADMIN', 'AUDITOR', 'OPERATOR']
const navigation: Array<{ label: string; path: string; icon: ReactNode; roles?: Role[] }> = [
  { label: 'Dashboard', path: '/', icon: <DashboardOutlined /> },
  { label: 'Transazioni', path: '/transazioni', icon: <AccountTreeOutlined /> },
  { label: 'Documenti', path: '/documenti', icon: <DescriptionOutlined /> },
  { label: 'Alert antifrode', path: '/alert', icon: <ShieldOutlined /> },
  { label: 'Regole', path: '/regole', icon: <SettingsSuggestOutlined /> },
  { label: 'Ricerca', path: '/ricerca', icon: <SearchOutlined /> },
  { label: 'Dispositivi', path: '/dispositivi', icon: <DevicesOutlined /> },
  { label: 'Sessioni TCP', path: '/sessioni', icon: <LanOutlined /> },
  { label: 'Diagnostica', path: '/diagnostica', icon: <MonitorHeartOutlined /> },
  { label: 'Job incompleti', path: '/incompleti', icon: <AssignmentLateOutlined />, roles: reviewerRoles },
  { label: 'Importazioni', path: '/importazioni', icon: <FileUploadOutlined />, roles: reviewerRoles },
]

export function Layout({ children, user, onLogout }: { children: ReactNode; user: User; onLogout: () => void }) {
  const theme = useTheme()
  const desktop = useMediaQuery(theme.breakpoints.up('lg'))
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()
  const drawer = (
    <Box sx={{
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      bgcolor: theme.appChrome.drawerBackground,
      color: theme.appChrome.drawerText,
    }}>
      <Toolbar sx={{ minHeight: 76, gap: 1.5 }}>
        <FactCheckOutlined sx={{ color: theme.appChrome.drawerAccent }} />
        <Box>
          <Typography fontWeight={780} letterSpacing="-0.02em">RetailPrintGuard</Typography>
          <Typography variant="caption" sx={{ color: theme.appChrome.drawerMuted }}>Centro antifrode</Typography>
        </Box>
      </Toolbar>
      <Divider sx={{ borderColor: theme.appChrome.drawerDivider }} />
      <List sx={{ px: 1.5, py: 2 }}>
        {navigation.filter((item) => !item.roles || item.roles.some((role) => user.roles.includes(role))).map((item) => (
          <ListItemButton
            key={item.path}
            component={NavLink}
            to={item.path}
            selected={location.pathname === item.path || (item.path !== '/' && location.pathname.startsWith(item.path))}
            onClick={() => setMobileOpen(false)}
            sx={{
              mb: 0.5,
              borderRadius: 1.5,
              color: theme.appChrome.drawerText,
              '&.Mui-selected': { bgcolor: theme.appChrome.drawerSelected, color: theme.appChrome.drawerText },
              '&.Mui-selected:hover': { bgcolor: theme.appChrome.drawerSelectedHover },
            }}
          >
            <ListItemIcon sx={{ minWidth: 40, color: 'inherit' }}>{item.icon}</ListItemIcon>
            <ListItemText primary={item.label} primaryTypographyProps={{ fontWeight: 620 }} />
          </ListItemButton>
        ))}
      </List>
      <Box sx={{ mt: 'auto', p: 2 }}>
        <Divider sx={{ mb: 2, borderColor: theme.appChrome.drawerDivider }} />
        <Typography variant="body2" fontWeight={650}>{user.username}</Typography>
        <Typography variant="caption" sx={{ color: theme.appChrome.drawerMuted }}>{user.roles.join(' · ')}</Typography>
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
        <AppBar position="sticky" color="inherit" elevation={0} sx={{ bgcolor: theme.appChrome.appBarBackground, borderBottom: '1px solid', borderColor: theme.appChrome.appBarBorder }}>
          <Toolbar sx={{ gap: 1 }}>
            {!desktop && <IconButton aria-label="Apri navigazione" onClick={() => setMobileOpen(true)}><MenuOutlined /></IconButton>}
            <Box sx={{ flex: 1 }} />
            <ThemeSwitcher />
            <Avatar sx={{ width: 32, height: 32, bgcolor: 'primary.main', fontSize: 14 }}>{user.username.slice(0, 2).toUpperCase()}</Avatar>
            <Tooltip title="Esci"><IconButton aria-label="Esci" onClick={onLogout}><LogoutOutlined /></IconButton></Tooltip>
          </Toolbar>
        </AppBar>
        <Box component="main" sx={{ p: { xs: 2, md: 3.5 }, maxWidth: 1680, mx: 'auto' }}>{children}</Box>
      </Box>
    </Box>
  )
}
