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
  { kind: 'llm', label: 'LLM', short: 'LLM' },
  { kind: 'embedding', label: 'Embedding', short: 'EM' },
  { kind: 'reranker', label: 'Rerank', short: 'RR' },
  { kind: 'vector_store', label: 'Vector Store', short: 'VS' },
  { kind: 'splitter', label: 'Splitter', short: 'SP' },
  { kind: 'evaluator', label: 'Evaluator', short: 'EV' },
]

const state = ref<ViewState>('loading')
const overview = ref<WorkspaceOverview>()
const health = ref<HealthReport>()
const errorMessage = ref('')
const requestId = ref('')

const documentTotal = computed(() => {
  if (!overview.value) return 0
  return Object.values(overview.value.document_counts).reduce((sum, count) => sum + count, 0)
})
const isEmpty = computed(
  () => documentTotal.value === 0 && overview.value?.queries_24h === 0 && !overview.value.recent_activity.length,
)
const isDegraded = computed(() => Boolean(health.value && (!health.value.ready || health.value.status !== 'healthy')))
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
    errorMessage.value = caught instanceof ApiError ? caught.message : '无法载入租户运行数据，请稍后重试。'
    requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
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
        <p class="section-kicker">WORKSPACE · LIVE OPERATIONS</p>
        <h1>租户总览</h1>
        <p>只展示当前租户的真实业务数据与已注册 Provider；未配置项会明确标记为不可用。</p>
      </div>
      <div v-if="state === 'ready' && overview" class="overview-freshness">
        <span :class="{ 'status-dot--degraded': isDegraded }"></span>
        <div><strong>{{ isDegraded ? '服务处于降级态' : '服务运行正常' }}</strong><small>快照 {{ timeLabel(overview.generated_at) }}</small></div>
      </div>
    </header>

    <div v-if="state === 'loading'" class="overview-skeleton" aria-label="正在载入租户总览" aria-busy="true">
      <i v-for="index in 10" :key="index"></i>
    </div>

    <div v-else-if="state === 'error'" class="overview-error" role="alert">
      <span>!</span><div><p class="section-kicker">OVERVIEW UNAVAILABLE</p><h2>租户总览暂时无法载入</h2><p>{{ errorMessage }}</p><code v-if="requestId">Request ID · {{ requestId }}</code><button class="button button--primary" type="button" @click="load">重新载入 <b>↗</b></button></div>
    </div>

    <template v-else-if="overview && health">
      <aside v-if="isDegraded" class="degraded-banner" role="status">
        <span>DEGRADED</span><div><strong>部分依赖尚未就绪</strong><p>页面继续展示可确认的数据；请根据下方 Provider 和健康检查定位缺失能力。</p></div>
      </aside>

      <div v-if="isEmpty" class="overview-empty" data-testid="overview-empty">
        <span>0</span><div><p class="section-kicker">EMPTY WORKSPACE</p><h2>工作区还没有业务数据</h2><p>系统已为匿名用户准备独立 Demo Tenant。创建集合并上传第一份文档后，摄取进度和检索指标会出现在这里。</p><RouterLink class="button button--primary" to="/workspace/documents">管理文档 <b>↗</b></RouterLink></div>
      </div>

      <section class="overview-section" aria-labelledby="providers-title">
        <div class="section-heading"><div><p class="section-kicker">PLUGGABLE RUNTIME</p><h2 id="providers-title">Provider 状态</h2></div><span>{{ health.providers.length }} 个已注册实例 <RouterLink class="section-heading__link" to="/admin/providers">查看目录与选择 →</RouterLink></span></div>
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
        <div class="section-heading"><div><p class="section-kicker">TENANT METRICS</p><h2 id="metrics-title">系统指标</h2></div><span>过去 24 小时</span></div>
        <div class="tenant-metrics">
          <article><small>集合</small><strong>{{ metric(overview.collection_count) }}</strong><p>当前活跃 collection</p></article>
          <article><small>文档</small><strong>{{ metric(documentTotal) }}</strong><p><i class="metric-ready"></i>{{ overview.document_counts.ready }} ready · <i class="metric-failed"></i>{{ overview.document_counts.failed }} failed</p></article>
          <article><small>索引单元</small><strong>{{ metric(overview.leaf_count) }}</strong><p>{{ overview.root_count }} roots / {{ overview.leaf_count }} leaves</p></article>
          <article><small>查询量</small><strong>{{ metric(overview.queries_24h) }}</strong><p>{{ overview.query_errors_24h }} 次错误</p></article>
          <article><small>Query P95</small><strong>{{ metric(overview.query_p95_ms, ' ms') }}</strong><p>端到端执行耗时</p></article>
          <article><small>错误率</small><strong>{{ percent(overview.query_error_rate) }}</strong><p>error / failed / cancelled</p></article>
        </div>
      </section>

      <section class="overview-section activity-section" aria-labelledby="activity-title">
        <div class="section-heading"><div><p class="section-kicker">RECENT ACTIVITY</p><h2 id="activity-title">最近任务</h2></div><span>最多 6 条</span></div>
        <div v-if="overview.recent_activity.length" class="activity-list">
          <article v-for="item in overview.recent_activity" :key="`${item.kind}-${item.id}`">
            <span>{{ item.kind === 'ingestion' ? 'IN' : 'EV' }}</span><div><strong>{{ item.kind === 'ingestion' ? '摄取任务' : '评测运行' }} · {{ item.label }}</strong><small>{{ timeLabel(item.started_at) }} · {{ item.id.slice(0, 12) }}…</small></div><i :class="`activity-status--${item.status}`">{{ item.progress == null ? item.status : `${item.progress}%` }}</i>
          </article>
        </div>
        <p v-else class="activity-empty">暂无摄取或评测任务。这里不会用演示数字填充空白。</p>
      </section>
    </template>
  </section>
</template>
