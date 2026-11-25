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

export type DocumentPdfCategory = 'complete' | 'partial' | 'excluded'
export type DocumentScope = 'complete' | 'partial' | 'all'
export type ExportDocumentScope = Exclude<DocumentScope, 'all'>
export type ExportScope = 'all_history' | 'date_range' | 'latest_10'

export interface ExportSelectionMetadata {
  schema_version: number
  policy_version: string
  scope: ExportScope
  document_scope: ExportDocumentScope
  scanned_documents: number
  selected_documents: number
  excluded_documents: number
  exclusion_counts: Record<string, number>
  category_counts: Record<DocumentPdfCategory, number>
  coverage_from: string | null
  coverage_to: string | null
  document_date_count: number
  capture_date_count: number
  selection_basis?: string
  selection_order?: string
  requested_limit?: number
  candidate_documents?: number
  unscanned_documents?: number
  selection_capture_from?: string | null
  selection_capture_to?: string | null
}

export interface ArchiveDocument {
  archive_sequence: number
  document_id: string
  document_version_id: string
  device_id: string
  type: string
  filename: string
  pdf_bytes: number
  pdf_sha256: string
  document_timestamp: string | null
  captured_at: string
  complete: boolean
  preview_url: string
}

export interface ArchiveDocuments extends Page<ArchiveDocument> {
  legacy: boolean
  renderer_version: string
  export_id: string
}

export interface ExportRequest {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  date_from: string | null
  date_to: string | null
  scope?: ExportScope
  document_scope?: ExportDocumentScope | 'legacy_all'
  selection_metadata?: ExportSelectionMetadata | null
  archive_filename?: string
  timezone: string
  device_ids: string[]
  requested_by: string
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  total_documents: number
  completed_documents: number
  archive_bytes?: number | null
  archive_sha256?: string | null
  error?: string | null
  download_url?: string | null
}

export interface Dashboard {
  documents: number
  observed_covers?: number
  covers_counted_episodes?: number
  covers_ambiguous_episodes?: number
  covers_source_disagreements?: number
  covers_unlinked_documents?: number
  covers_unlinked_observations?: number
  complete_documents?: number
  partial_documents?: number
  excluded_evidence?: number
  total_evidence?: number
  complete_document_type_counts?: Record<string, number>
  first_document_captured_at?: string | null
  last_document_captured_at?: string | null
  orders: number
  pre_bills: number
  management_documents: number
  commercial_documents: number
  shift_end_reports?: number
  invoices?: number
  open_alerts: number
  critical_alerts: number
  economic_difference: string
  /** New antifraud summary fields are optional during rolling upgrades. */
  operational_alerts?: number
  economic_reduction_episodes?: number
  operational_economic_difference?: string
  incomplete_jobs?: number
  false_positive_alerts?: number
  justified_alerts?: number
  sold_product_quantity_bar?: string
  sold_product_quantity_bar_dine_in?: string
  sold_product_quantity_bar_takeaway?: string
  sold_product_quantity_bar_undetermined?: string
  sold_product_quantity_kitchen?: string
  sold_product_quantity_kitchen_dine_in?: string
  sold_product_quantity_kitchen_takeaway?: string
  sold_product_quantity_kitchen_undetermined?: string
  sold_product_quantity_pizzeria?: string
  sold_product_quantity_pizzeria_dine_in?: string
  sold_product_quantity_pizzeria_takeaway?: string
  sold_product_quantity_pizzeria_undetermined?: string
  employee_discount_total?: string
  observed_employee_discount_total?: string
  observed_employee_discount_count?: number
  observed_generic_30_discount_total?: string
  observed_generic_30_discount_count?: number
  arithmetic_reconciled_employee_discount_total?: string
  arithmetic_reconciled_employee_discount_count?: number
  devices_online: number
  devices_offline: number
  spool_bytes: number
  parse_errors: number
  alert_trend: Array<Record<string, unknown>>
  anomaly_concentration: Array<Record<string, unknown>>
}

export interface DashboardTrafficTrendBucket {
  start: string
  covers: number
  takeaways: { bar: number; kitchen: number; pizzeria: number }
}

export interface DashboardTrafficTrend {
  granularity: 'hour' | 'day'
  timezone: string
  zero_filled: boolean
  buckets: DashboardTrafficTrendBucket[]
  covers_total: number
  takeaways_total: { bar: number; kitchen: number; pizzeria: number }
}

export interface Device {
  id: string
  name: string
  display_name?: string
  type: string
  mac_address?: string
  department?: string
  role?: string
  enabled: boolean
  online: boolean
  listen_endpoint: string
  target_endpoint: string
  additional_ports?: number[]
  last_connection_at?: string
  last_print_at?: string
  last_response_at?: string
  spool_bytes: number
  pending_jobs: number
  service_version?: string
  last_error?: string
  reachability_checked_at?: string
  reachability_error?: string
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
  id?: string
  sequence: number
  course_code?: string
  item_code?: string
  description?: string
  quantity?: string
  unit_price?: string | null
  original_unit_price?: string
  modified_unit_price?: string
  discount?: string
  tax_rate?: string
  line_total?: string
  state?: string
  removed: boolean
  cancelled: boolean
  raw_text?: string
  derived_unit_price?: string | null
  derived_price_source?: string | null
  price_attributions?: Array<{
    id: string
    source_kind: string
    observed_unit_price?: string | null
    observed_line_total?: string
    confidence: string
    status: string
    match_basis: string
    source_document_id: string
    source_observed_at: string
  }>
}

export type ReceiptHeaderEvidence =
  | 'RCH_PRINTED_HEADER'
  | 'RCH_DGFE_EXPORT'
  | 'DEVICE_METADATA_CONFIGURED'

export interface ReceiptHeaderRecord {
  schema_version: 1
  merchant_name?: string | null
  legal_name?: string | null
  address_lines: string[]
  phone?: string | null
  tax_code?: string | null
  vat_number?: string | null
  evidence: ReceiptHeaderEvidence
}

export interface DocumentRecord {
  id: string
  device_id: string
  job_id: string
  type: string
  subtype: string
  external_document_code?: string
  external_document_code_suffix?: string
  resolved_external_document_code?: string
  resolved_external_document_code_provenance?: string
  commercial_reference_code?: string
  progressive_observation_status?: string
  external_code?: string
  order_code?: string
  table_code?: string
  service_mode?: 'DINE_IN' | 'TAKEAWAY' | 'UNDETERMINED' | null
  service_mode_evidence?: string | null
  covers?: number
  operator_code?: string
  terminal_code?: string
  document_timestamp?: string
  document_timestamp_precision?: 'MINUTE' | 'SECOND' | string
  document_timestamp_evidence?: string
  application_timestamp?: string | null
  application_timestamp_precision?: 'MINUTE' | 'SECOND' | string | null
  application_timestamp_evidence?: string | null
  rch_footer_timestamp?: string | null
  rch_footer_timestamp_precision?: 'MINUTE' | 'SECOND' | string | null
  rch_footer_timestamp_evidence?: string | null
  rch_serial_number?: string | null
  rch_serial_number_evidence?: string | null
  /** Footer RCH minus application timestamp. Negative means the RCH clock lags. */
  rch_clock_offset_seconds?: number | null
  captured_at: string
  gross_total?: string
  net_total?: string
  discount_total?: string
  tax_total?: string
  status: string
  normalized_text: string
  receipt_text?: string
  /** Shared preview used by the receipt tab and PDF; optional for older APIs. */
  receipt_preview?: string | null
  /** Server-selected name shared by the individual PDF and ZIP entry. */
  pdf_filename?: string | null
  pdf_category?: DocumentPdfCategory
  pdf_reason_code?: string
  pdf_policy_version?: string
  /** Structured header projection; absent during rolling upgrades of the API. */
  receipt_header?: ReceiptHeaderRecord | null
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

export interface JobRecord {
  id: string
  device_id: string
  session_id?: string
  external_job_id: string
  captured_at: string
  status: string
  request_bytes: number
  response_bytes: number
  manifest_sha256: string
  spool_path: string
  imported_at?: string
  parser_status?: string
  warnings: string[]
  review_state?: 'PENDING' | 'VERIFIED_USABLE' | 'EXCLUDED'
  analysis_excluded?: boolean
  reviewed_at?: string
  reviewed_by?: string
  review_reason?: string
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
