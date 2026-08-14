import { Card, Switch, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@mui/material'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, scopedQueryKey, session } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { ErrorState, LoadingState } from '../components/State'
import { StatusChip } from '../components/StatusChip'
import type { FraudRule } from '../types'

export function RulesPage() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: scopedQueryKey('rules'), queryFn: () => api<FraudRule[]>('/rules') })
  const update = useMutation({ mutationFn: ({ code, enabled }: { code: string; enabled: boolean }) => api<FraudRule>(`/rules/${encodeURIComponent(code)}?enabled=${enabled}`, { method: 'PATCH' }), onSuccess: () => queryClient.invalidateQueries({ queryKey: ['rules'] }) })
  const isAdmin = session().user?.roles.includes('ADMIN') ?? false
  return <>
    <PageHeader title="Regole antifrode" subtitle="Configurazione versionata del motore deterministico e spiegabile." />
    <Card>{query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : <Table><TableHead><TableRow><TableCell>Regola</TableCell><TableCell>Severità</TableCell><TableCell align="right">Peso</TableCell><TableCell align="right">Soglia</TableCell><TableCell>Versione</TableCell><TableCell>Attiva</TableCell></TableRow></TableHead><TableBody>{query.data?.map((rule) => <TableRow key={rule.code}><TableCell><Typography fontWeight={700}>{rule.name}</Typography><Typography variant="caption" color="text.secondary">{rule.code}</Typography></TableCell><TableCell><StatusChip value={rule.severity} /></TableCell><TableCell align="right">{rule.weight}</TableCell><TableCell align="right">{rule.threshold ?? '—'}</TableCell><TableCell>v{rule.version}</TableCell><TableCell><Switch checked={rule.enabled} disabled={!isAdmin || update.isPending} onChange={(_, enabled) => update.mutate({ code: rule.code, enabled })} inputProps={{ 'aria-label': `${rule.name}: ${rule.enabled ? 'attiva' : 'disattiva'}` }} /></TableCell></TableRow>)}</TableBody></Table>}</Card>
  </>
}
