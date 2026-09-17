<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  overviewApi,
  type HealthReport,
  type ProviderDiagnostic,
  type WorkspaceOverview,
} from '../api/overview'

type ViewState = 'loading' | 'ready' | 'error'
type ProviderSlot = { kind: string; label: string; short: string }

const providerSlots: ProviderSlot[] = [
  { kind: 'llm', label: '生成模型', short: 'LLM' },
  { kind: 'embedding', label: '向量模型', short: 'EM' },
  { kind: 'reranker', label: '重排模型', short: 'RR' },
  { kind: 'vector_store', label: '向量存储', short: 'VS' },
  { kind: 'splitter', label: '文档切分', short: 'SP' },
  { kind: 'evaluator', label: '评测组件', short: 'EV' },
]

const state = ref<ViewState>('loading')
const overview = ref<WorkspaceOverview>()
const health = ref<HealthReport>()
const errorMessage = ref('')
const requestId = ref('')
const seedingDemo = ref(false)
const seedMessage = ref('')

const documentTotal = computed(() => {
  if (!overview.value) return 0
  return Object.values(overview.value.document_counts).reduce((sum, count) => sum + count, 0)
})
const isEmpty = computed(
  () => documentTotal.value === 0 && overview.value?.queries_24h === 0 && !overview.value.recent_activity.length,
)
const hasProviderFailure = computed(() => {
  const report = health.value
  if (!report) return false
  return report.providers.some((provider) => ['degraded', 'unavailable'].includes(provider.health))
    || report.checks.some((check) => check.kind === 'provider' && check.status === 'unavailable')
})
const hasNonProviderIssue = computed(() => Boolean(
  health.value?.checks.some((check) => check.kind !== 'provider' && check.status !== 'healthy'),
))
const isDegraded = computed(() => Boolean(
  health.value && (!health.value.ready || hasProviderFailure.value || hasNonProviderIssue.value),
))
const probePending = computed(() => Boolean(
  health.value
  && health.value.ready
  && !isDegraded.value
  && health.value.providers.some((provider) => provider.health === 'unknown'),
))
const providerGroups = computed(() => {
  const providers = health.value?.providers ?? []
  return providerSlots.map((slot) => ({
    ...slot,
    providers: providers.filter((provider) => provider.kind === slot.kind),
  }))
})

async function load(): Promise<void> {
  state.value = 'loading'
  errorMessage.value = ''
  requestId.value = ''
  try {
    const snapshot = await overviewApi.load()
    overview.value = snapshot.overview
    health.value = snapshot.health
    state.value = 'ready'
  } catch (caught) {
    state.value = 'error'
    errorMessage.value = caught instanceof ApiError ? caught.message : '无法载入 RAG 运行数据，请稍后重试。'
    requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
  }
}

async function seedDemo(): Promise<void> {
  if (seedingDemo.value) return
  seedingDemo.value = true
  seedMessage.value = ''
  try {
    const result = await overviewApi.seedDemo()
    seedMessage.value = `已提交 ${result.documents.length} 份演示文档；处理完成后可在文档、链路观测和问答页面查看真实结果。`
    await load()
  } catch (caught) {
    seedMessage.value = caught instanceof ApiError ? caught.message : '演示数据提交失败，请稍后重试。'
  } finally {
    seedingDemo.value = false
  }
}

function providerStatus(providers: ProviderDiagnostic[]): string {
  if (!providers.length) return 'unavailable'
  if (providers.some((provider) => provider.health === 'unavailable')) return 'unavailable'
  if (providers.some((provider) => !['healthy', 'unknown'].includes(provider.health))) return 'degraded'
  return providers.every((provider) => provider.health === 'healthy') ? 'healthy' : 'unknown'
}

function healthLabel(status: string): string {
  return {
    healthy: '健康',
    degraded: '降级',
    unavailable: '不可用',
    unknown: '待探测',
  }[status] ?? status
}

function metric(value: number | null | undefined, suffix = ''): string {
  return value == null ? '—' : `${new Intl.NumberFormat('zh-CN').format(value)}${suffix}`
}

function percent(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function outcome(status: string): number {
  // Keep the operations page readable while an older backend is being rolled
  // forward. The current API always supplies this map, but a missing map must
  // not make the whole overview fail to render.
  return overview.value?.query_outcome_counts?.[status] ?? 0
}

function timeLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

onMounted(load)
</script>

<template>
  <section class="overview-page">
    <header class="overview-heading">
      <div>
        <p class="section-kicker">RAG OPERATIONS · LIVE STATUS</p>
        <h1>RAG 运行概览</h1>
        <p>集中查看知识库规模、问答表现、模型与基础组件健康状态，以及最近发生的处理任务。</p>
      </div>
      <div v-if="state === 'ready' && overview" class="overview-freshness">
        <span :class="{ 'status-dot--degraded': isDegraded }"></span>
        <div><strong>{{ isDegraded ? '服务处于降级态' : probePending ? '服务运行中 · 远程模型待探测' : '服务运行正常' }}</strong><small>快照 {{ timeLabel(overview.generated_at) }}</small></div>
      </div>
    </header>

    <div v-if="state === 'loading'" class="overview-skeleton" aria-label="正在载入 RAG 运行概览" aria-busy="true">
      <i v-for="index in 10" :key="index"></i>
    </div>

    <div v-else-if="state === 'error'" class="overview-error" role="alert">
      <span>!</span><div><p class="section-kicker">OVERVIEW UNAVAILABLE</p><h2>RAG 运行概览暂时无法载入</h2><p>{{ errorMessage }}</p><code v-if="requestId">Request ID · {{ requestId }}</code><button class="button button--primary" type="button" @click="load">重新载入 <b>↗</b></button></div>
    </div>

    <template v-else-if="overview && health">
      <aside v-if="isDegraded" class="degraded-banner" role="status">
        <span>DEGRADED</span><div><strong>部分依赖尚未就绪</strong><p>页面继续展示可确认的数据；请根据下方模型与基础组件状态定位缺失能力。</p></div>
      </aside>
      <aside v-else-if="probePending" class="degraded-banner" role="status">
        <span>待探测</span><div><strong>远程模型尚未完成首次探测</strong><p>生成、重排等模型会在首次真实请求后显示健康或具体错误；这不是故障，页面当前可以正常使用。</p></div>
      </aside>

      <div v-if="seedMessage" class="overview-seed-message" role="status">{{ seedMessage }}</div>

      <div v-if="isEmpty" class="overview-empty" data-testid="overview-empty">
        <span>0</span><div><p class="section-kicker">EMPTY WORKSPACE</p><h2>工作区还没有业务数据</h2><p>可加载进入真实文档处理流水线的演示资料，也可以上传自己的文档。</p><button class="button button--primary" type="button" :disabled="seedingDemo" @click="seedDemo">{{ seedingDemo ? '提交中…' : '加载演示数据' }} <b>↗</b></button><RouterLink class="button button--secondary" to="/workspace/documents">管理文档 <b>↗</b></RouterLink></div>
      </div>

      <section class="overview-section" aria-labelledby="providers-title">
        <div class="section-heading"><div><p class="section-kicker">MODELS &amp; DEPENDENCIES</p><h2 id="providers-title">模型与基础组件状态</h2></div><span>{{ health.providers.length }} 个已注册实例 <RouterLink class="section-heading__link" to="/workspace/models">查看模型状态与选配 →</RouterLink></span></div>
        <div class="provider-grid">
          <article v-for="group in providerGroups" :key="group.kind" class="provider-card" :class="`provider-card--${providerStatus(group.providers)}`">
            <div class="provider-card__top"><span>{{ group.short }}</span><i></i></div>
            <p>{{ group.label }}</p>
            <template v-if="group.providers.length">
              <strong>{{ group.providers.map((provider) => provider.name).join(' / ') }}</strong>
              <small>{{ group.providers.map((provider) => provider.version).join(' · ') }}</small>
            </template>
            <template v-else><strong>未注册</strong><small>当前运行时没有暴露该能力</small></template>
            <footer>{{ healthLabel(providerStatus(group.providers)) }}<span>{{ group.providers.some((provider) => provider.is_remote) ? 'REMOTE' : 'LOCAL / N.A.' }}</span></footer>
          </article>
        </div>
      </section>

      <section class="overview-section" aria-labelledby="metrics-title">
        <div class="section-heading"><div><p class="section-kicker">RAG METRICS</p><h2 id="metrics-title">运行指标</h2></div><span>过去 24 小时</span></div>
        <div class="tenant-metrics">
          <article><small>知识集合</small><strong>{{ metric(overview.collection_count) }}</strong><p>当前可用集合</p></article>
          <article><small>文档</small><strong>{{ metric(documentTotal) }}</strong><p><i class="metric-ready"></i>{{ overview.document_counts.ready }} 就绪 · <i class="metric-failed"></i>{{ overview.document_counts.failed }} 失败</p></article>
          <article><small>可检索切片</small><strong>{{ metric(overview.leaf_count) }}</strong><p>{{ overview.root_count }} 个原文块 / {{ overview.leaf_count }} 个检索块</p></article>
          <article><small>查询量</small><strong>{{ metric(overview.queries_24h) }}</strong><p>{{ outcome('answered') }} 完整回答 · {{ outcome('partial') }} 部分回答</p></article>
          <article><small>问答 P95</small><strong>{{ metric(overview.query_p95_ms, ' ms') }}</strong><p>端到端执行耗时</p></article>
          <article><small>错误率</small><strong>{{ percent(overview.query_error_rate) }}</strong><p>错误、失败或取消的查询占比</p></article>
          <article><small>拒答率</small><strong>{{ percent(overview.query_abstention_rate) }}</strong><p>{{ outcome('abstained') }} 次安全拒答 / 全部查询</p></article>
          <article><small>有效回答率</small><strong>{{ percent(overview.query_answer_rate) }}</strong><p>answered + partial / 全部查询</p></article>
          <article><small>生成降级</small><strong>{{ metric(overview.query_generation_degraded_24h) }}</strong><p>回答 JSON 未通过首次解析</p></article>
        </div>
      </section>

      <section class="overview-section activity-section" aria-labelledby="activity-title">
        <div class="section-heading"><div><p class="section-kicker">RECENT ACTIVITY</p><h2 id="activity-title">最近任务</h2></div><span>最多 6 条</span></div>
        <div v-if="overview.recent_activity.length" class="activity-list">
          <article v-for="item in overview.recent_activity" :key="`${item.kind}-${item.id}`">
            <span>{{ item.kind === 'ingestion' ? 'IN' : 'EV' }}</span><div><strong>{{ item.kind === 'ingestion' ? '文档处理任务' : '评测运行' }} · {{ item.label }}</strong><small>{{ timeLabel(item.started_at) }} · {{ item.id.slice(0, 12) }}…</small></div><i :class="`activity-status--${item.status}`">{{ item.progress == null ? item.status : `${item.progress}%` }}</i>
          </article>
        </div>
        <p v-else class="activity-empty">暂无文档处理或评测任务。这里不会用演示数字填充空白。</p>
      </section>
    </template>
  </section>
</template>
