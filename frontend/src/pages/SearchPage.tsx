import { SearchOutlined } from '@mui/icons-material'
import { Box, Button, Card, InputAdornment, List, ListItemButton, ListItemText, TextField, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { FormEvent, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, scopedQueryKey } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { EmptyState, ErrorState, LoadingState } from '../components/State'
import type { Page, SearchHit } from '../types'
import { formatDateTime } from '../format'

function routeFor(hit: SearchHit) {
  const entityType = hit.entity_type.toUpperCase()
  if (entityType === 'DOCUMENT') return `/documenti/${hit.entity_id}`
  if (entityType === 'TRANSACTION') return `/transazioni/${hit.entity_id}`
  if (entityType === 'ORDER') return `/transazioni?order_code=${encodeURIComponent(hit.title)}`
  if (entityType === 'DEVICE') return '/dispositivi'
  return undefined
}

export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const [text, setText] = useState(params.get('q') ?? '')
  const navigate = useNavigate()
  const queryText = params.get('q') ?? ''
  const query = useQuery({
    queryKey: scopedQueryKey('search', queryText),
    queryFn: () => api<Page<SearchHit>>(`/search?q=${encodeURIComponent(queryText)}&limit=100`),
    enabled: queryText.length >= 2,
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    if (text.trim().length >= 2) setParams({ q: text.trim() })
  }
  return (
    <>
      <PageHeader title="Ricerca globale" subtitle="Documenti, ordini, tavoli, articoli, importi, hash e dispositivi." />
      <Card sx={{ p: 2, mb: 2 }}>
        <Box component="form" onSubmit={submit} sx={{ display: 'flex', gap: 1 }}>
          <TextField fullWidth autoFocus label="Cerca nelle evidenze" value={text} onChange={(event) => setText(event.target.value)} InputProps={{ startAdornment: <InputAdornment position="start"><SearchOutlined /></InputAdornment> }} />
          <Button type="submit" variant="contained" disabled={text.trim().length < 2}>Cerca</Button>
        </Box>
      </Card>
      <Card>
        {!queryText ? <EmptyState label="Inserisci almeno due caratteri per avviare la ricerca." /> : query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} /> : !query.data?.items.length ? <EmptyState label="Nessuna evidenza corrispondente." /> : (
          <List disablePadding>{query.data.items.map((hit) => {
            const target = routeFor(hit)
            return <ListItemButton key={`${hit.entity_type}-${hit.entity_id}`} divider disabled={!target} onClick={() => target && navigate(target)} sx={{ py: 1.5 }}>
              <ListItemText primary={<Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 2 }}><Typography fontWeight={700}>{hit.title}</Typography><Typography variant="caption" color="text.secondary">{formatDateTime(hit.occurred_at)}</Typography></Box>} secondary={<>{hit.subtitle}<br />{hit.highlights.join(' · ')}</>} />
            </ListItemButton>
          })}</List>
        )}
      </Card>
    </>
  )
}
