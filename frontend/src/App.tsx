import { Alert, Box, Button, CircularProgress } from '@mui/material'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { clearSession, session, subscribeSession } from './api/client'
import { Layout } from './components/Layout'
import { LoginPage } from './pages/LoginPage'
import type { User } from './types'

const AlertsPage = lazy(() => import('./pages/AlertsPage').then((module) => ({ default: module.AlertsPage })))
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const DevicesPage = lazy(() => import('./pages/DevicesPage').then((module) => ({ default: module.DevicesPage })))
const DocumentDetailPage = lazy(() => import('./pages/DocumentDetailPage').then((module) => ({ default: module.DocumentDetailPage })))
const DocumentsPage = lazy(() => import('./pages/DocumentsPage').then((module) => ({ default: module.DocumentsPage })))
const ImportsPage = lazy(() => import('./pages/ImportsPage').then((module) => ({ default: module.ImportsPage })))
const RulesPage = lazy(() => import('./pages/RulesPage').then((module) => ({ default: module.RulesPage })))
const SearchPage = lazy(() => import('./pages/SearchPage').then((module) => ({ default: module.SearchPage })))
const TransactionDetailPage = lazy(() => import('./pages/TransactionDetailPage').then((module) => ({ default: module.TransactionDetailPage })))
const TransactionsPage = lazy(() => import('./pages/TransactionsPage').then((module) => ({ default: module.TransactionsPage })))

function LoadingPage() {
  return <Box sx={{ minHeight: 320, display: 'grid', placeItems: 'center' }}>
    <CircularProgress aria-label="Caricamento pagina" />
  </Box>
}

export default function App() {
  const [user, setUser] = useState<User | null>(session().user)
  useEffect(() => subscribeSession(() => setUser(session().user)), [])
  if (!user) return <LoginPage onLogin={setUser} />
  function logout() {
    clearSession()
    setUser(null)
  }
  return <Layout user={user} onLogout={logout}>
    <Suspense fallback={<LoadingPage />}>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/transazioni" element={<TransactionsPage />} />
        <Route path="/transazioni/:transactionId" element={<TransactionDetailPage />} />
        <Route path="/documenti" element={<DocumentsPage />} />
        <Route path="/documenti/:documentId" element={<DocumentDetailPage />} />
        <Route path="/alert" element={<AlertsPage />} />
        <Route path="/regole" element={<RulesPage />} />
        <Route path="/ricerca" element={<SearchPage />} />
        <Route path="/dispositivi" element={<DevicesPage />} />
        <Route path="/importazioni" element={<ImportsPage />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Alert severity="warning" action={<Button href="/">Dashboard</Button>}>Pagina non trovata.</Alert>} />
      </Routes>
    </Suspense>
  </Layout>
}
