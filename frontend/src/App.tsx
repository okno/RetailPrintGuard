import { Alert, Box, Button, CircularProgress } from '@mui/material'
import { useQueryClient } from '@tanstack/react-query'
import { lazy, Suspense, useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { clearSession, session, subscribeSession } from './api/client'
import { Layout } from './components/Layout'
import { LoginPage } from './pages/LoginPage'
import { DOCUMENT_DETAIL_ROUTE, TRANSACTION_DETAIL_ROUTE } from './routes'
import type { User } from './types'

const AlertsPage = lazy(() => import('./pages/AlertsPage').then((module) => ({ default: module.AlertsPage })))
const DashboardPage = lazy(() => import('./pages/DashboardPage').then((module) => ({ default: module.DashboardPage })))
const DevicesPage = lazy(() => import('./pages/DevicesPage').then((module) => ({ default: module.DevicesPage })))
const DocumentDetailPage = lazy(() => import('./pages/DocumentDetailPage').then((module) => ({ default: module.DocumentDetailPage })))
const DocumentsPage = lazy(() => import('./pages/DocumentsPage').then((module) => ({ default: module.DocumentsPage })))
const ImportsPage = lazy(() => import('./pages/ImportsPage').then((module) => ({ default: module.ImportsPage })))
const IncompleteJobsPage = lazy(() => import('./pages/IncompleteJobsPage').then((module) => ({ default: module.IncompleteJobsPage })))
const RulesPage = lazy(() => import('./pages/RulesPage').then((module) => ({ default: module.RulesPage })))
const SearchPage = lazy(() => import('./pages/SearchPage').then((module) => ({ default: module.SearchPage })))
const SessionsPage = lazy(() => import('./pages/SessionsPage').then((module) => ({ default: module.SessionsPage })))
const DiagnosticsPage = lazy(() => import('./pages/DiagnosticsPage').then((module) => ({ default: module.DiagnosticsPage })))
const TransactionDetailPage = lazy(() => import('./pages/TransactionDetailPage').then((module) => ({ default: module.TransactionDetailPage })))
const TransactionsPage = lazy(() => import('./pages/TransactionsPage').then((module) => ({ default: module.TransactionsPage })))

function LoadingPage() {
  return <Box sx={{ minHeight: 320, display: 'grid', placeItems: 'center' }}>
    <CircularProgress aria-label="Caricamento pagina" />
  </Box>
}

export default function App() {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<User | null>(session().user)
  useEffect(() => subscribeSession(() => {
    // Never carry privileged evidence between authenticated principals.
    queryClient.clear()
    setUser(session().user)
  }), [queryClient])
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
        <Route path={TRANSACTION_DETAIL_ROUTE} element={<TransactionDetailPage />} />
        <Route path="/documenti" element={<DocumentsPage />} />
        <Route path={DOCUMENT_DETAIL_ROUTE} element={<DocumentDetailPage />} />
        <Route path="/alert" element={<AlertsPage />} />
        <Route path="/regole" element={<RulesPage />} />
        <Route path="/ricerca" element={<SearchPage />} />
        <Route path="/dispositivi" element={<DevicesPage />} />
        <Route path="/sessioni" element={<SessionsPage />} />
        <Route path="/diagnostica" element={<DiagnosticsPage />} />
        <Route path="/incompleti" element={<IncompleteJobsPage />} />
        <Route path="/importazioni" element={<ImportsPage />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<Alert severity="warning" action={<Button href="/">Dashboard</Button>}>Pagina non trovata.</Alert>} />
      </Routes>
    </Suspense>
  </Layout>
}
