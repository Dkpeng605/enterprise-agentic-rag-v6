<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  traceApi,
  type IngestionTraceStatus,
  type IngestionTraceView,
  type TraceSummary,
} from '../api/traces'

const traces = ref<TraceSummary[]>([])
const nextCursor = ref<string>()
const selectedId = ref('')
const detail = ref<IngestionTraceView>()
const status = ref<IngestionTraceStatus | ''>('')
const loading = ref(true)
const loadingMore = ref(false)
const detailLoading = ref(false)
const error = ref('')
const requestId = ref('')
const duration = computed(() => Math.max(detail.value?.summary.duration_ms ?? 0, 1))

function setError(caught: unknown, fallback: string): void {
  error.value = caught instanceof ApiError ? caught.message : fallback
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

async function loadTraces(append = false): Promise<void> {
  append ? (loadingMore.value = true) : (loading.value = true)
  error.value = ''
  requestId.value = ''
  try {
    const page = await traceApi.listIngestion({
      status: status.value || undefined,
      cursor: append ? nextCursor.value : undefined,
      limit: 20,
    })
    traces.value = append ? [...traces.value, ...page.items] : page.items
    nextCursor.value = page.nextCursor
    if (!append) {
      selectedId.value = ''
      detail.value = undefined
      if (page.items[0]) await selectTrace(page.items[0].trace_id)
    }
  } catch (caught) {
    setError(caught, '无法载入文档处理链路。')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function selectTrace(traceId: string): Promise<void> {
  selectedId.value = traceId
  detailLoading.value = true
  error.value = ''
  const requestedId = traceId
  try {
    const result = await traceApi.getIngestion(traceId)
    if (selectedId.value === requestedId) detail.value = result
  } catch (caught) {
    if (selectedId.value === requestedId) {
      detail.value = undefined
      setError(caught, '无法载入文档处理链路详情。')
    }
  } finally {
    if (selectedId.value === requestedId) detailLoading.value = false
  }
}

function statusLabel(value: string): string {
  return { succeeded: '已完成', failed: '失败', retry_wait: '等待重试', cancelled: '已取消' }[value] ?? value
}

function stageLabel(value: string): string {
  return {
    'rag.ingestion': 'Ingestion Run', 'rag.ingestion.load': 'Load',
    'rag.ingestion.images': 'Image Enrichment', 'rag.ingestion.clean': 'Clean',
    'rag.ingestion.split': 'Split', 'rag.ingestion.persist': 'Persist',
    'rag.ingestion.project': 'Vector Projection', 'rag.ingestion.finalize': 'Finalize',
  }[value] ?? value.replace(/^rag\.ingestion\.?/, '')
}

function stageStyle(offsetMs: number, durationMs: number): Record<string, string> {
  return {
    left: `${Math.min(98, (offsetMs / duration.value) * 100)}%`,
    width: `${Math.max(1.5, Math.min(100, (durationMs / duration.value) * 100))}%`,
  }
}

function stageCounts(stage: IngestionTraceView['stages'][number]): string {
  const values: string[] = []
  if (stage.root_count != null) values.push(`${stage.root_count} Root`)
  if (stage.leaf_count != null) values.push(`${stage.leaf_count} Leaf`)
  if (stage.verified_count != null) values.push(`${stage.verified_count}/${stage.expected_count ?? '—'} verified`)
  if (stage.batch_count != null) values.push(`${stage.batch_count} batches/phase`)
  return values.join(' · ')
}

function phaseLabel(value: string): string {
  return value === 'staging' ? 'STAGING' : value === 'activation' ? 'ACTIVATION' : value.toUpperCase()
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

onMounted(() => loadTraces())
</script>

<template>
  <section class="trace-page ingestion-trace-page">
    <header class="workspace-heading">
      <div><p class="section-kicker">RAG OBSERVABILITY · DOCUMENTS</p><h1>文档处理观测</h1><p>查看解析、切分、向量化各阶段的实际耗时、处理批次与稳定错误。</p></div>
      <div class="trace-legend"><span><i class="legend-dot"></i>完成阶段</span><span><i class="legend-dot legend-dot--deep"></i>Projection Batch</span><span><i class="legend-dot legend-dot--degraded"></i>失败阶段</span></div>
    </header>

    <div class="trace-toolbar ingestion-trace-toolbar"><label>结果<select v-model="status" @change="loadTraces()"><option value="">全部结果</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="retry_wait">Retry wait</option><option value="cancelled">Cancelled</option></select></label><button type="button" @click="loadTraces()">刷新</button></div>
    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert"><strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="loadTraces()">重试</button></div>
    <div v-if="loading" class="trace-skeleton" aria-busy="true" aria-label="正在载入文档处理链路"><i></i><i></i><i></i></div>
    <div v-else-if="!traces.length" class="trace-empty" data-testid="ingestion-trace-empty"><span>0</span><h2>还没有匹配的文档处理链路</h2><p>后台完成一次成功、重试、失败或取消尝试后，链路记录才会出现在这里。</p><RouterLink class="button button--primary" to="/workspace/documents">前往文档与切分</RouterLink></div>
    <div v-else class="trace-layout">
      <aside class="trace-list" data-testid="ingestion-trace-list"><button v-for="trace in traces" :key="trace.trace_id" type="button" :class="{ active: selectedId === trace.trace_id }" @click="selectTrace(trace.trace_id)"><span class="trace-mode" :class="`ingestion-status--${trace.status}`">{{ trace.status === 'succeeded' ? 'OK' : trace.status === 'retry_wait' ? 'RETRY' : 'ERR' }}</span><span><strong>{{ statusLabel(trace.status) }}</strong><small>{{ trace.subject_id.slice(0, 13) }}… · {{ dateLabel(trace.started_at) }}</small></span><span class="trace-duration">{{ Math.round(trace.duration_ms) }}<small>ms</small></span></button><button v-if="nextCursor" class="load-more" type="button" :disabled="loadingMore" @click="loadTraces(true)">{{ loadingMore ? '载入中…' : '载入更多记录' }}</button></aside>

      <main class="trace-detail">
        <div v-if="detailLoading" class="trace-detail-loading" aria-busy="true"><i></i><p>正在投影阶段与批次…</p></div>
        <template v-else-if="detail">
          <header class="trace-detail-head"><div><p class="section-kicker">TRACE INSPECTOR</p><h2>{{ statusLabel(detail.summary.status) }} · Attempt {{ detail.attempt }}</h2><code>{{ detail.summary.trace_id }}</code></div><dl><div><dt>耗时</dt><dd>{{ Math.round(detail.summary.duration_ms) }} ms</dd></div><div><dt>进度</dt><dd>{{ detail.progress }}%</dd></div><div><dt>阶段</dt><dd>{{ detail.stages.length }}</dd></div></dl></header>
          <div v-if="detail.error_code" class="ingestion-trace-error" role="status"><span>STABLE ERROR</span><div><strong>{{ detail.error_code }}</strong><p>只展示 Worker 保存的稳定错误码；供应商消息与异常堆栈不会进入该投影。</p></div><RouterLink :to="`/workspace/ingestion?job=${detail.summary.subject_id}`">查看任务</RouterLink></div>

          <section class="trace-section"><div class="trace-section-head"><div><p class="section-kicker">STAGE WATERFALL</p><h3>处理阶段</h3></div><span>0 ms → {{ Math.round(detail.summary.duration_ms) }} ms</span></div><div class="waterfall ingestion-waterfall" data-testid="ingestion-waterfall"><article v-for="stage in detail.stages" :key="stage.span_id"><div><strong><b v-if="stage.parent_span_id">↳</b>{{ stageLabel(stage.name) }}</strong><small>+{{ Math.round(stage.offset_ms) }} ms · {{ stage.duration_ms.toFixed(1) }} ms</small><em v-if="stageCounts(stage)">{{ stageCounts(stage) }}</em></div><div class="waterfall-track"><i :class="{ 'waterfall-bar--degraded': stage.status === 'ERROR' }" :style="stageStyle(stage.offset_ms, stage.duration_ms)"></i></div></article><p v-if="!detail.stages.length" class="trace-inline-empty">该链路没有已持久化的处理阶段。</p></div></section>

          <section class="trace-section"><div class="trace-section-head"><div><p class="section-kicker">VECTOR BATCHES</p><h3>Projection 批次</h3></div><span>{{ detail.batches.length }} 个已观测批次</span></div><div v-if="detail.batches.length" class="batch-table" data-testid="ingestion-batches"><div class="batch-row batch-row--head"><span>PHASE</span><span>BATCH</span><span>ITEMS</span><span>WRITTEN</span><span>DURATION</span><span>STATUS</span></div><div v-for="batch in detail.batches" :key="batch.span_id" class="batch-row"><span><b>{{ phaseLabel(batch.phase) }}</b><small>+{{ Math.round(batch.offset_ms) }} ms</small></span><span>{{ batch.batch_index }} / {{ batch.batch_count }}</span><span>{{ batch.item_count }}</span><span>{{ batch.written_count ?? '—' }}</span><span>{{ batch.duration_ms.toFixed(1) }} ms</span><span :class="{ 'batch-status--error': batch.status === 'ERROR' }">{{ batch.status === 'ERROR' ? 'ERROR' : 'OK' }}</span></div></div><p v-else class="trace-inline-empty">本次尝试未进入向量批处理，或旧 Trace 尚未记录批次 Span。</p></section>
        </template>
      </main>
    </div>
  </section>
</template>
