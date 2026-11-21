import { request } from './client'

/** 分区抽样质量门禁接口封装，对应后端 /api/sampling-quality。 */

export type QualityThresholds = {
  pass_max: number
  fail_max: number
  min_table_coverage: number
}

export type PartitionSpecPayload = {
  partition: string
  sampling_rate: number
  population_rows: number
  planned_sample_n: number
  min_coverage: number
}

export type Diagnostic = {
  code: string
  partition: string | null
  message: string
  detail: Record<string, unknown>
  resolved: boolean
}

export type PartitionQuality = {
  partition: string
  plan_version: number
  sampling_rate: number
  planned_sample_n: number
  received_sample_n: number
  bad_sample_n: number
  coverage: number
  weighted_total: number
  weighted_bad: number
  weighted_bad_rate: number
  naive_bad_rate: number
  tier: string
  status: string
  expected_population: number
  diagnostics: Diagnostic[]
  calc_snapshot: Record<string, unknown>
}

export type TableQuality = {
  table_name: string
  plan_id: string
  plan_version: number
  weighted_bad_rate: number
  naive_bad_rate: number
  tier: string
  status: string
  table_coverage: number
  received_sample_n: number
  bad_sample_n: number
  partitions: PartitionQuality[]
  diagnostics: Diagnostic[]
  computed_at: string
  calc_snapshot: Record<string, unknown>
}

export type ReviewItem = {
  review_id: string
  table_name: string
  partition: string
  row_id: string
  batch_id: string
  plan_version: number
  reason: string
  sample_detail: Record<string, unknown>
  weight: number
  status: string
  assignee: string | null
  rectification: string | null
  created_at: string
  updated_at: string
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `接口调用失败：${response.status}`)
  }
  return response.json()
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('抽样质量数据读取失败')
  return response.json()
}

export function createPlan(
  tableName: string,
  thresholds: QualityThresholds,
  partitions: PartitionSpecPayload[],
  note = '',
) {
  return postJson('/api/sampling-quality/plans', {
    plan_id: `PLAN-${tableName.replace(/\W/g, '_')}`,
    table_name: tableName,
    thresholds,
    partitions,
    note,
  })
}

export function revisePlan(
  tableName: string,
  thresholds: QualityThresholds,
) {
  return postJson(`/api/sampling-quality/plans/${tableName}/revise`, { thresholds })
}

export function registerBatch(
  tableName: string,
  batchId: string,
  partition: string,
  planVersion: number,
  expectedN: number,
) {
  return postJson('/api/sampling-quality/batches', {
    table_name: tableName,
    batch_id: batchId,
    partition,
    plan_version: planVersion,
    expected_n: expectedN,
  })
}

export function ingestSamples(
  tableName: string,
  partition: string,
  planVersion: number,
  batchId: string,
  rows: { row_id: string; is_bad: boolean; detail?: Record<string, unknown> }[],
  isLate = false,
) {
  return postJson('/api/sampling-quality/samples', {
    table_name: tableName,
    partition,
    plan_version: planVersion,
    batch_id: batchId,
    is_late: isLate,
    rows,
  })
}

export function evaluate(tableName: string) {
  return postJson<TableQuality>(`/api/sampling-quality/evaluate/${tableName}`)
}

export function fetchSnapshots(tableName: string) {
  return getJson<{ table_name: string; snapshots: TableQuality[] }>(
    `/api/sampling-quality/evaluate/${tableName}/snapshots`,
  )
}

export function fetchReviews(tableName: string, status?: string) {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return getJson<{ table_name: string; total: number; items: ReviewItem[] }>(
    `/api/sampling-quality/reviews/${tableName}${query}`,
  )
}

export function submitReview(
  tableName: string,
  reviewId: string,
  action: 'confirm' | 'rectify' | 'dismiss',
  extra: { assignee?: string; rectification?: string } = {},
) {
  return postJson<{ ok: boolean; item: ReviewItem }>(
    `/api/sampling-quality/reviews/${tableName}/${reviewId}`,
    { action, ...extra },
  )
}

export function fetchReport(tableName: string) {
  return getJson<Record<string, unknown>>(
    `/api/sampling-quality/report/${tableName}`,
  )
}

/** 构造 n 条样本，前 badN 条为坏行（演示剧本用）。 */
export function makeRows(prefix: string, n: number, badN: number, reason = 'NULL/空值') {
  return Array.from({ length: n }, (_, i) => ({
    row_id: `${prefix}-${String(i).padStart(3, '0')}`,
    is_bad: i < badN,
    detail: i < badN ? { reason } : {},
  }))
}
