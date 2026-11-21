<template>
  <section class="page" data-testid="sampling-quality-page">
    <header class="page-head">
      <div>
        <h2>分区抽样质量门禁</h2>
        <p class="page-desc">
          按分区不等概率抽样，用明示阈值计算加权坏行率；样本迟到只给暂算值，覆盖达标后才确认整表结论。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="seed-scenario" type="button" @click="seedScenario">
          载入两层抽样演示剧本
        </button>
        <button class="btn" type="button" @click="recompute" :disabled="!tableName">重算质量结果</button>
      </div>
    </header>

    <p v-if="message" class="error-text" data-testid="message">{{ message }}</p>

    <!-- 整表结论 -->
    <div class="stat-row" v-if="result">
      <article class="stat-card">
        <span class="stat-label">整表结论（{{ result.status === 'CONFIRMED' ? '确认值' : '暂算值' }}）</span>
        <strong class="stat-value" :data-testid="`tier-${result.tier}`">{{ tierText(result.tier) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">加权坏行率</span>
        <strong class="stat-value" data-testid="weighted-rate">{{ pct(result.weighted_bad_rate) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">朴素坏行率（未计权对照）</span>
        <strong class="stat-value">{{ pct(result.naive_bad_rate) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">整表加权覆盖率</span>
        <strong class="stat-value" data-testid="table-coverage">{{ pct(result.table_coverage) }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">计划版本 / 坏行样本</span>
        <strong class="stat-value">v{{ result.plan_version }} / {{ result.bad_sample_n }}</strong>
      </article>
    </div>

    <!-- 分层质量结果 -->
    <h3>分层质量结果</h3>
    <table class="data-table" data-testid="partition-table" v-if="result">
      <thead>
        <tr>
          <th>分区</th><th>抽样概率</th><th>计划/收到样本</th><th>坏行样本</th>
          <th>覆盖率</th><th>加权坏行率</th><th>分层结论</th><th>确认状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="p in result.partitions" :key="p.partition" :data-testid="`partition-${p.partition}`">
          <td>{{ p.partition }}</td>
          <td>{{ pct(p.sampling_rate, 0) }}</td>
          <td>{{ p.planned_sample_n }} / {{ p.received_sample_n }}</td>
          <td>{{ p.bad_sample_n }}</td>
          <td :data-testid="`coverage-${p.partition}`">{{ pct(p.coverage) }}</td>
          <td>{{ pct(p.weighted_bad_rate) }}</td>
          <td>{{ tierText(p.tier) }}</td>
          <td>
            <span :class="['badge', p.status === 'CONFIRMED' ? 'badge-ok' : 'badge-warn']">
              {{ p.status === 'CONFIRMED' ? '确认' : '暂算' }}
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 诊断 -->
    <template v-if="result && result.diagnostics.length">
      <h3>覆盖与迟到诊断</h3>
      <ul class="diag-list" data-testid="diagnostics">
        <li v-for="(d, i) in result.diagnostics" :key="i" :class="{ resolved: d.resolved }">
          <span class="diag-code">{{ d.code }}</span>
          <span>{{ d.partition ?? '整表' }}：{{ d.message }}</span>
          <em v-if="d.resolved" class="diag-resolved">（已消解）</em>
        </li>
      </ul>
    </template>

    <!-- 剧本操作：模拟样本到达与阈值变更 -->
    <h3>场景操作</h3>
    <div class="filter-bar">
      <button class="btn" data-testid="step-small" type="button" :disabled="!seeded" @click="stepSmallArrival">
        1. 小分区首批到达（300/500，18坏行）
      </button>
      <button class="btn" data-testid="step-small-rest" type="button" :disabled="!seeded" @click="stepSmallRest">
        2. 补报小分区剩余200
      </button>
      <button class="btn" data-testid="step-large-part" type="button" :disabled="!seeded" @click="stepLargePartial">
        3. 大分区部分到达（200/500）
      </button>
      <button class="btn" data-testid="step-large-rest" type="button" :disabled="!seeded" @click="stepLargeRest">
        4. 补齐大分区剩余300（迟到）
      </button>
      <button class="btn" data-testid="step-revise" type="button" :disabled="!seeded" @click="stepRevise">
        5. 阈值收紧并重算（fail 5%→3%）
      </button>
    </div>

    <!-- 重算快照 -->
    <h3>重算快照（另存当时抽样数与坏行数）</h3>
    <table class="data-table" data-testid="snapshot-table" v-if="snapshots.length">
      <thead>
        <tr><th>计划版本</th><th>状态</th><th>使用样本数</th><th>坏行数</th><th>pass/fail阈值</th><th>计算时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="(s, i) in snapshots" :key="i">
          <td>v{{ s.plan_version }}</td>
          <td>{{ s.status === 'CONFIRMED' ? '确认' : '暂算' }}</td>
          <td>{{ s.calc_snapshot.used_sample_n }}</td>
          <td>{{ s.calc_snapshot.used_bad_n }}</td>
          <td>
            {{ pct(Number(thresholdsOf(s).pass_max), 0) }}
            / {{ pct(Number(thresholdsOf(s).fail_max), 0) }}
          </td>
          <td>{{ s.computed_at }}</td>
        </tr>
      </tbody>
    </table>

    <!-- 复查明细 -->
    <h3>复查明细（坏行溯源到分区 / 批次 / 样本）</h3>
    <table class="data-table" data-testid="review-table" v-if="reviews.length">
      <thead>
        <tr>
          <th>复查单</th><th>分区</th><th>样本行</th><th>批次</th><th>计划版本</th>
          <th>坏行原因</th><th>计权</th><th>状态</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in pagedReviews" :key="r.review_id" :data-testid="`review-${r.review_id}`">
          <td>{{ r.review_id }}</td>
          <td>{{ r.partition }}</td>
          <td>{{ r.row_id }}</td>
          <td>{{ r.batch_id }}</td>
          <td>v{{ r.plan_version }}</td>
          <td>{{ r.reason }}</td>
          <td>{{ r.weight }}</td>
          <td>
            <span :class="['badge', reviewBadgeClass(r.status)]">{{ reviewText(r.status) }}</span>
          </td>
          <td class="row-actions">
            <button
              v-if="r.status === 'PENDING'"
              class="link" type="button"
              @click="doReview(r.review_id, 'confirm')"
            >核验确认</button>
            <button
              v-if="r.status === 'CONFIRMED'"
              class="link" type="button"
              @click="doReview(r.review_id, 'rectify')"
            >提交整改</button>
            <button
              v-if="r.status === 'PENDING' || r.status === 'CONFIRMED'"
              class="link" type="button"
              @click="doReview(r.review_id, 'dismiss')"
            >误报驳回</button>
            <span v-else class="muted">已闭环</span>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="!reviews.length" class="page-foot">暂无复查单，坏行样本到达并重算后自动生成。</p>
    <footer class="page-foot" v-if="reviews.length">
      <span>共 {{ reviews.length }} 条复查明细，第 {{ reviewPage }} 页</span>
      <span>
        <button class="btn" type="button" :disabled="reviewPage <= 1" @click="reviewPage -= 1">上一页</button>
        <button
          class="btn" type="button"
          :disabled="reviewPage >= totalReviewPages"
          @click="reviewPage += 1"
        >下一页</button>
      </span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  createPlan,
  evaluate,
  fetchReviews,
  fetchSnapshots,
  ingestSamples,
  makeRows,
  registerBatch,
  revisePlan,
  submitReview,
  type ReviewItem,
  type TableQuality,
} from '@/api/sampling-quality'

const TABLE = 'dw.dwd_order_wide'
const SMALL = 'region=west'
const LARGE = 'region=east'

const tableName = ref('')
const seeded = ref(false)
const message = ref('')
const result = ref<TableQuality | null>(null)
const snapshots = ref<TableQuality[]>([])
const reviews = ref<ReviewItem[]>([])
const reviewPage = ref(1)
const reviewPageSize = 8

const pagedReviews = computed(() =>
  reviews.value.slice((reviewPage.value - 1) * reviewPageSize, reviewPage.value * reviewPageSize),
)
const totalReviewPages = computed(() => Math.max(1, Math.ceil(reviews.value.length / reviewPageSize)))

function pct(value: number, digits = 2): string {
  return `${(value * 100).toFixed(digits)}%`
}

function tierText(tier: string): string {
  return { PASS: '通过', WARN: '告警', FAIL: '坏行超标', NO_DATA: '无数据' }[tier] ?? tier
}

function reviewText(status: string): string {
  return {
    PENDING: '待核验',
    CONFIRMED: '已确认',
    RECTIFIED: '已整改',
    DISMISSED: '已驳回',
  }[status] ?? status
}

function reviewBadgeClass(status: string): string {
  if (status === 'RECTIFIED') return 'badge-ok'
  if (status === 'DISMISSED') return 'badge-muted'
  return 'badge-warn'
}

function thresholdsOf(s: TableQuality): { pass_max: number; fail_max: number } {
  return s.calc_snapshot.thresholds as { pass_max: number; fail_max: number }
}

async function refreshAll() {
  const [quality, snapshotResp, reviewResp] = await Promise.all([
    evaluate(TABLE),
    fetchSnapshots(TABLE),
    fetchReviews(TABLE),
  ])
  result.value = quality
  snapshots.value = snapshotResp.snapshots
  reviews.value = reviewResp.items
}

async function recompute() {
  message.value = ''
  try {
    await refreshAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '重算失败'
  }
}

async function seedScenario() {
  message.value = ''
  try {
    await createPlan(
      TABLE,
      { pass_max: 0.02, fail_max: 0.05, min_table_coverage: 0.8 },
      [
        { partition: SMALL, sampling_rate: 0.5, population_rows: 1000, planned_sample_n: 500, min_coverage: 0.8 },
        { partition: LARGE, sampling_rate: 0.1, population_rows: 5000, planned_sample_n: 500, min_coverage: 0.8 },
      ],
      '两层不等比例抽样演示',
    )
    await registerBatch(TABLE, 'B-S1', SMALL, 1, 300)
    await registerBatch(TABLE, 'B-S2', SMALL, 1, 200)
    await registerBatch(TABLE, 'B-L1', LARGE, 1, 500)
    tableName.value = TABLE
    seeded.value = true
    reviewPage.value = 1
    await refreshAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '剧本初始化失败（可能已初始化，请直接操作场景按钮）'
  }
}

async function stepSmallArrival() {
  await ingestSamples(TABLE, SMALL, 1, 'B-S1', makeRows('s1', 300, 18))
  await refreshAll()
}

async function stepSmallRest() {
  await ingestSamples(TABLE, SMALL, 1, 'B-S2', makeRows('s2', 200, 0), true)
  await refreshAll()
}

async function stepLargePartial() {
  await ingestSamples(TABLE, LARGE, 1, 'B-L1', makeRows('l1', 200, 20, '引用缺失'))
  await refreshAll()
}

async function stepLargeRest() {
  await ingestSamples(TABLE, LARGE, 1, 'B-L1', makeRows('l2', 300, 30, '金额越界'), true)
  await refreshAll()
}

async function stepRevise() {
  await revisePlan(TABLE, { pass_max: 0.01, fail_max: 0.03, min_table_coverage: 0.8 })
  await refreshAll()
}

async function doReview(reviewId: string, action: 'confirm' | 'rectify' | 'dismiss') {
  message.value = ''
  try {
    await submitReview(TABLE, reviewId, action, {
      assignee: action === 'confirm' ? '值班-数据工程组' : undefined,
      rectification: action === 'rectify' ? '回填问题字段并重跑对应分区ETL' : undefined,
    })
    const reviewResp = await fetchReviews(TABLE)
    reviews.value = reviewResp.items
  } catch (error) {
    message.value = error instanceof Error ? error.message : '复查操作失败'
  }
}
</script>

<style scoped>
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.badge-ok {
  background: #dcfae6;
  color: #027a48;
}
.badge-warn {
  background: #fef0c7;
  color: #b54708;
}
.badge-muted {
  background: #eaecf0;
  color: #475467;
}
.diag-list {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
  margin: 0 0 16px;
  list-style: none;
}
.diag-list li {
  padding: 4px 0;
  font-size: 13px;
}
.diag-list li.resolved {
  color: var(--muted);
}
.diag-code {
  display: inline-block;
  min-width: 220px;
  font-family: monospace;
  color: #b54708;
}
.diag-resolved {
  color: #027a48;
  font-style: normal;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
h3 {
  font-size: 15px;
  margin: 18px 0 8px;
}
</style>
