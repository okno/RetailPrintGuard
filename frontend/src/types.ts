export type Role = 'ADMIN' | 'AUDITOR' | 'OPERATOR' | 'READ_ONLY'

export interface User {
  id: string
  username: string
  roles: Role[]
  active: boolean
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Dashboard {
  documents: number
  orders: number
  pre_bills: number
  management_documents: number
  commercial_documents: number
  open_alerts: number
  critical_alerts: number
  economic_difference: string
  devices_online: number
  devices_offline: number
  spool_bytes: number
  parse_errors: number
  alert_trend: Array<Record<string, unknown>>
  anomaly_concentration: Array<Record<string, unknown>>
}

export interface Device {
  id: string
  name: string
  type: string
  mac_address?: string
  department?: string
  role?: string
  enabled: boolean
  online: boolean
  listen_endpoint: string
  target_endpoint: string
  last_connection_at?: string
  last_print_at?: string
  last_response_at?: string
  spool_bytes: number
  pending_jobs: number
  service_version?: string
  last_error?: string
}

export interface ProxySession {
  id: string
  device_id: string
  source_endpoint: string
  target_endpoint: string
  opened_at: string
  closed_at?: string
  close_reason?: string
  request_bytes: number
  response_bytes: number
  complete: boolean
}

export interface SystemEvent {
  id: string
  service: string
  severity: string
  event_type: string
  message: string
  device_id?: string
  session_id?: string
  job_id?: string
  correlation_id?: string
  occurred_at: string
  error?: string
}

export interface Diagnostics {
  generated_at: string
  database: string
  spool: string
  parser_errors: number
  incomplete_jobs: number
  recent_events: SystemEvent[]
}

export interface DocumentLine {
  sequence: number
  item_code?: string
  description?: string
  quantity?: string
  unit_price?: string
  original_unit_price?: string
  modified_unit_price?: string
  discount?: string
  tax_rate?: string
  line_total?: string
  state?: string
  removed: boolean
  cancelled: boolean
  raw_text?: string
}

export interface DocumentRecord {
  id: string
  device_id: string
  job_id: string
  type: string
  subtype: string
  external_code?: string
  order_code?: string
  table_code?: string
  operator_code?: string
  terminal_code?: string
  document_timestamp?: string
  captured_at: string
  gross_total?: string
  net_total?: string
  discount_total?: string
  tax_total?: string
  status: string
  normalized_text: string
  parser_name: string
  parser_version: string
  confidence: number
  sha256: string
  complete: boolean
  warnings: string[]
  lines: DocumentLine[]
  payments: Array<Record<string, unknown>>
  correlations: Array<Record<string, unknown>>
}

export interface Transaction {
  id: string
  order_id?: string
  occurred_at: string
  table_code?: string
  order_code?: string
  operator_code?: string
  initial_total?: string
  pre_bill_total?: string
  fiscal_total?: string
  difference?: string
  status: string
  document_count: number
  alert_count: number
  correlation_confidence: number
  timeline: Array<Record<string, unknown>>
  diff: Record<string, unknown>
}

export interface AlertRecord {
  id: string
  rule_code: string
  severity: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
  score: number
  status: string
  opened_at: string
  transaction_id?: string
  device_ids: string[]
  document_ids: string[]
  description: string
  explanation: string
  economic_difference?: string
  confidence: number
  assigned_to?: string
  acknowledged_at?: string
  closed_at?: string
  resolution_reason?: string
  evidence: Array<Record<string, unknown>>
  history: Array<Record<string, unknown>>
}

export interface ImportBatch {
  id: string
  source_type: string
  source_root: string
  started_at: string
  completed_at?: string
  status: string
  discovered: number
  imported: number
  duplicates: number
  failed: number
  report: Record<string, unknown>
}

export interface SearchHit {
  entity_type: string
  entity_id: string
  occurred_at: string
  title: string
  subtitle?: string
  highlights: string[]
}

export interface FraudRule {
  code: string
  name: string
  enabled: boolean
  version: number
  severity: string
  weight: number
  threshold?: string
  configuration: Record<string, unknown>
}
