import { ArrowBack, CodeOutlined, DownloadOutlined } from '@mui/icons-material'
import { Alert, Box, Button, Card, CardContent, Grid, Paper, Tab, Table, TableBody, TableCell, TableHead, TableRow, Tabs, Typography } from '@mui/material'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, rawDocument } from '../api/client'
import { ErrorState, LoadingState } from '../components/State'
import { PageHeader } from '../components/PageHeader'
import { StatusChip } from '../components/StatusChip'
import type { DocumentRecord } from '../types'

function hex(data: Uint8Array) { return Array.from(data.slice(0, 65536)).map((byte, index) => `${index % 16 === 0 ? `\n${index.toString(16).padStart(8,'0')}  ` : ''}${byte.toString(16).padStart(2,'0')} `).join('').trim() }

export function DocumentDetailPage() {
  const { id } = useParams(); const navigate = useNavigate(); const [tab,setTab]=useState(0); const [raw,setRaw]=useState<string>(); const [rawError,setRawError]=useState('')
  const query=useQuery({queryKey:['document',id],queryFn:()=>api<DocumentRecord>(`/documents/${id}`),enabled:Boolean(id)})
  if(query.isLoading)return <LoadingState/>; if(query.error||!query.data)return <ErrorState error={query.error}/>
  const doc=query.data
  async function loadRaw(){try{setRaw(hex(await rawDocument(doc.id)));setTab(2)}catch(error){setRawError(error instanceof Error?error.message:'RAW non disponibile')}}
  return <><PageHeader title={doc.subtype} subtitle={`${doc.type} · ${doc.device_id} · parser ${doc.parser_name} ${doc.parser_version}`} actions={<Box sx={{display:'flex',gap:1}}><Button startIcon={<ArrowBack/>} onClick={()=>navigate('/documenti')}>Indietro</Button><Button variant="outlined" startIcon={<CodeOutlined/>} onClick={loadRaw}>Apri RAW</Button></Box>}/>
    {rawError&&<Alert severity="error" sx={{mb:2}}>{rawError}</Alert>}
    <Grid container spacing={2.5}><Grid size={{xs:12,lg:8}}><Card><Tabs value={tab} onChange={(_,v)=>setTab(v)} aria-label="Viste documento"><Tab label="Scontrino"/><Tab label="Righe strutturate"/><Tab label="RAW tecnico"/></Tabs><CardContent>{tab===0&&<Paper variant="outlined" sx={{mx:'auto',maxWidth:520,p:3,bgcolor:'#fffef9',fontFamily:'ui-monospace,Consolas,monospace',whiteSpace:'pre-wrap',lineHeight:1.55}}>{doc.normalized_text||'Nessun testo normalizzato.'}</Paper>}{tab===1&&<Box sx={{overflowX:'auto'}}><Table size="small"><TableHead><TableRow><TableCell>#</TableCell><TableCell>Descrizione</TableCell><TableCell align="right">Q.tà</TableCell><TableCell align="right">Prezzo</TableCell><TableCell align="right">Totale</TableCell><TableCell>Stato</TableCell></TableRow></TableHead><TableBody>{doc.lines.map((line)=><TableRow key={line.sequence} sx={{textDecoration:line.removed?'line-through':'none',bgcolor:line.removed?'error.50':'transparent'}}><TableCell>{line.sequence}</TableCell><TableCell>{line.description??line.raw_text??'—'}</TableCell><TableCell align="right">{line.quantity??'—'}</TableCell><TableCell align="right">{line.unit_price??'—'}</TableCell><TableCell align="right">{line.line_total??'—'}</TableCell><TableCell>{line.removed?'Rimosso':line.cancelled?'Annullato':line.state??'Attivo'}</TableCell></TableRow>)}</TableBody></Table></Box>}{tab===2&&<Box><Typography variant="body2" color="text.secondary" sx={{mb:1}}>Vista limitata a 64 KiB. Il download completo è auditato.</Typography>{raw?<Paper component="pre" variant="outlined" sx={{p:2,maxHeight:520,overflow:'auto',fontSize:12,whiteSpace:'pre-wrap'}}>{raw}</Paper>:<Button startIcon={<DownloadOutlined/>} onClick={loadRaw}>Richiedi payload originale</Button>}</Box>}</CardContent></Card></Grid>
      <Grid size={{xs:12,lg:4}}><Card><CardContent><Typography variant="h2" sx={{mb:2}}>Provenienza</Typography><StatusChip value={doc.complete?'COMPLETE':'INCOMPLETE'}/>{[['Acquisito',new Date(doc.captured_at).toLocaleString('it-IT')],['Codice ordine',doc.order_code],['Tavolo',doc.table_code],['Operatore',doc.operator_code],['Hash SHA-256',doc.sha256],['Confidenza',`${doc.confidence}%`]].map(([label,value])=><Box key={label} sx={{mt:2}}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography sx={{wordBreak:'break-all'}}>{value||'—'}</Typography></Box>)}</CardContent></Card>{doc.warnings.length>0&&<Alert severity="warning" sx={{mt:2}}>{doc.warnings.join(' · ')}</Alert>}</Grid></Grid>
  </>
}
