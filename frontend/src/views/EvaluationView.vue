<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { ApiError } from '../api/client'
import {
  evaluationApi,
  type EvaluationCatalog,
  type EvaluationComparison,
  type EvaluationRun,
  type EvaluationStatus,
} from '../api/evaluations'

const catalog = ref<EvaluationCatalog>()
const runs = ref<EvaluationRun[]>([])
const detail = ref<EvaluationRun>()
const comparison = ref<EvaluationComparison>()
const selectedId = ref('')
const baseId = ref('')
const candidateId = ref('')
const mode = ref<'all' | 'standard' | 'deep'>('all')
const maxCases = ref(10)
const status = ref<EvaluationStatus | ''>('')
const nextCursor = ref<string>()
const loading = ref(true)
const detailLoading = ref(false)
const running = ref(false)
const comparing = ref(false)
const error = ref('')
const requestId = ref('')
let poller: ReturnType<typeof setInterval> | undefined

const profile = computed(() => catalog.value?.profiles[0])
const availableCases = computed(() => catalog.value?.case_counts[mode.value] ?? 0)
const selectedCases = computed(() => Math.min(maxCases.value, availableCases.value))
const estimatedCalls = computed(() => selectedCases.value * (profile.value?.estimated_llm_calls_per_case ?? 0))
const active = computed(() => runs.value.some((run) => run.status === 'queued' || run.status === 'running'))
const completedRuns = computed(() => runs.value.filter((run) => run.status === 'succeeded'))
const metrics = computed(() => Object.entries(detail.value?.aggregate_metrics ?? {}))
const failedCases = computed(() => {
  const report = detail.value?.report
  if (!report || !Array.isArray(report.cases)) return []
  return report.cases.filter((item): item is Record<string, unknown> => {
    if (!item || typeof item !== 'object') return false
    const values = Object.values((item as Record<string, unknown>).metrics ?? {})
    return values.some((value) => typeof value === 'number' && value < 1)
  })
})

function setError(caught: unknown, fallback: string): void {
  error.value = caught instanceof ApiError ? caught.message : fallback
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

async function loadRuns(append = false): Promise<void> {
  if (!append) loading.value = true
  error.value = ''
  requestId.value = ''
  try {
    const page = await evaluationApi.list({
      status: status.value || undefined,
      cursor: append ? nextCursor.value : undefined,
      limit: 20,
    })
    runs.value = append ? [...runs.value, ...page.items] : page.items
    nextCursor.value = page.nextCursor
    if (!append && page.items[0] && !selectedId.value) await selectRun(page.items[0].id)
    if (selectedId.value) {
      const latest = runs.value.find((run) => run.id === selectedId.value)
      const selectedIsActive = detail.value
        && ['queued', 'running'].includes(detail.value.status)
      if (latest && (selectedIsActive
        || latest.status !== detail.value?.status
        || latest.completed_cases !== detail.value?.completed_cases)) {
        await selectRun(latest.id)
      }
    }
  } catch (caught) {
    setError(caught, '无法载入评测历史。')
  } finally {
    loading.value = false
  }
}

async function selectRun(runId: string): Promise<void> {
  selectedId.value = runId
  detailLoading.value = true
  try {
    const result = await evaluationApi.get(runId)
    if (selectedId.value === runId) detail.value = result
  } catch (caught) {
    if (selectedId.value === runId) setError(caught, '无法载入评测报告。')
  } finally {
    if (selectedId.value === runId) detailLoading.value = false
  }
}

async function startRun(): Promise<void> {
  if (!catalog.value || !profile.value) return
  running.value = true
  error.value = ''
  try {
    const run = await evaluationApi.create({
      dataset_revision: catalog.value.dataset_revision,
      mode: mode.value,
      provider_profile: profile.value.id,
      max_cases: selectedCases.value,
      max_llm_calls: catalog.value.max_llm_calls,
    })
    runs.value = [run, ...runs.value.filter((item) => item.id !== run.id)]
    await selectRun(run.id)
    await loadRuns()
  } catch (caught) {
    setError(caught, '无法启动评测。')
  } finally {
    running.value = false
  }
}

async function compareRuns(): Promise<void> {
  if (!baseId.value || !candidateId.value) return
  comparing.value = true
  try {
    comparison.value = await evaluationApi.compare(baseId.value, candidateId.value)
  } catch (caught) {
    setError(caught, '无法比较这两次运行。')
  } finally {
    comparing.value = false
  }
}

function statusLabel(value: string): string {
  return { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败' }[value] ?? value
}

function modeLabel(value: string): string {
  return { all: '全部模式', standard: 'Standard', deep: 'Deep' }[value] ?? value
}

function metricLabel(value: string): string {
  return {
    document_recall_at_5: 'Document Recall@5', root_recall_at_5: 'Root Recall@5',
    mrr_at_10: 'Root MRR@10', citation_coverage: '引用覆盖率',
    citation_validity: '引用有效率', abstention_accuracy: '拒答准确率',
  }[value] ?? value
}

function reasonLabel(value: string): string {
  return {
    SAME_RUN: '请选择两次不同的运行', RUN_NOT_SUCCEEDED: '两次运行都必须成功完成',
    REPORT_MISSING: '至少一份聚合报告缺失', METADATA_INCOMPLETE: 'dataset/index/prompt/provider 元数据不完整',
    DATASET_MISMATCH: 'Dataset revision 不一致', MODE_MISMATCH: '查询模式不一致',
    PROVIDER_MISMATCH: '模型服务配置不一致', MODEL_MISMATCH: '模型版本不一致',
    PROMPT_MISMATCH: 'Prompt revision 不一致', INDEX_MISMATCH: 'Index revision 不一致',
    CASE_SET_MISMATCH: '实际 Case 集合不一致',
  }[value] ?? value
}

function percent(value: number | null): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function runProgress(run: EvaluationRun): number {
  return Math.round((run.completed_cases / Math.max(run.total_cases, 1)) * 100)
}

function short(value: string | null): string {
  return value ? value.slice(0, 10) : '—'
}

function download(kind: 'json' | 'md'): void {
  if (!detail.value?.report) return
  const content = kind === 'json'
    ? JSON.stringify(detail.value.report, null, 2)
    : [`# Evaluation ${detail.value.id}`, '', ...metrics.value.map(([name, value]) => `- ${metricLabel(name)}: ${percent(value)}`)].join('\n')
  const link = document.createElement('a')
  link.href = URL.createObjectURL(new Blob([content], { type: kind === 'json' ? 'application/json' : 'text/markdown' }))
  link.download = `evaluation-${detail.value.id}.${kind}`
  link.click()
  URL.revokeObjectURL(link.href)
}

watch(active, (value) => {
  if (value && !poller) poller = setInterval(() => void loadRuns(), 1000)
  if (!value && poller) { clearInterval(poller); poller = undefined }
})

onMounted(async () => {
  await loadPage()
})

async function loadPage(): Promise<void> {
  try {
    catalog.value = await evaluationApi.catalog()
    maxCases.value = Math.min(10, catalog.value.max_cases)
    await loadRuns()
  } catch (caught) {
    setError(caught, '无法载入评测配置。')
    loading.value = false
  }
}
onBeforeUnmount(() => { if (poller) clearInterval(poller) })
</script>

<template>
  <section class="evaluation-page">
    <header class="workspace-heading"><div><p class="section-kicker">RAG QUALITY · CONTROLLED EVALUATION</p><h1>RAG 效果评测</h1><p>使用标准问题集检查检索与回答质量，保留可复现报告，并只比较输入快照一致的结果。</p></div><span class="evaluation-local">本地评测 · 不调用模型</span></header>
    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert"><strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="loadPage()">重试</button></div>

    <section v-if="catalog" class="evaluation-launcher" data-testid="evaluation-launcher">
      <div><p class="section-kicker">NEW RUN</p><h2>运行配置</h2><p>{{ catalog.dataset_label }}</p></div>
      <label>模式<select v-model="mode"><option value="all">全部 · {{ catalog.case_counts.all }}</option><option value="standard">Standard · {{ catalog.case_counts.standard }}</option><option value="deep">Deep · {{ catalog.case_counts.deep }}</option></select></label>
      <label>最大 Case<input v-model.number="maxCases" type="number" min="1" :max="Math.min(catalog.max_cases, availableCases)" /></label>
      <label>模型服务配置<select disabled><option>{{ profile?.label }}</option></select></label>
      <div class="evaluation-budget"><span>执行前预算</span><strong>{{ selectedCases }} Cases · ≤ {{ estimatedCalls }} LLM calls</strong><small>部署上限 {{ catalog.max_cases }} Cases / {{ catalog.max_llm_calls }} LLM calls；本 Profile 不访问远程模型。</small></div>
      <button class="button button--primary" type="button" :disabled="running || selectedCases < 1 || active" @click="startRun">{{ running || active ? '评测运行中…' : '启动评测' }}</button>
    </section>

    <div class="evaluation-grid">
      <section class="evaluation-history"><div class="evaluation-section-head"><div><p class="section-kicker">RUN HISTORY</p><h2>历史运行</h2></div><label>状态<select v-model="status" @change="loadRuns()"><option value="">全部</option><option value="running">Running</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option></select></label></div>
        <div v-if="loading" class="trace-skeleton" aria-busy="true"><i></i><i></i><i></i></div>
        <div v-else-if="!runs.length" class="evaluation-empty" data-testid="evaluation-empty"><strong>0 RUNS</strong><p>还没有匹配的评测运行。</p></div>
        <div v-else class="evaluation-run-list" data-testid="evaluation-run-list"><button v-for="run in runs" :key="run.id" type="button" :class="{ active: selectedId === run.id }" @click="selectRun(run.id)"><span :class="`evaluation-status evaluation-status--${run.status}`">{{ statusLabel(run.status) }}</span><span><strong>{{ modeLabel(run.mode) }} · {{ run.total_cases }} Cases</strong><small>{{ short(run.commit_sha) }} · {{ new Date(run.created_at).toLocaleString('zh-CN') }}</small><i><b :style="{ width: `${runProgress(run)}%` }"></b></i></span><em>{{ runProgress(run) }}%</em></button><button v-if="nextCursor" class="load-more" type="button" @click="loadRuns(true)">载入更多 Run</button></div>
      </section>

      <section class="evaluation-report">
        <div v-if="detailLoading" class="trace-detail-loading" aria-busy="true"><i></i><p>正在读取评测报告…</p></div>
        <template v-else-if="detail"><div class="evaluation-section-head"><div><p class="section-kicker">RUN REPORT</p><h2>{{ statusLabel(detail.status) }} · {{ modeLabel(detail.mode) }}</h2><code>{{ detail.id }}</code></div><div class="evaluation-export"><button type="button" :disabled="!detail.report" @click="download('json')">JSON</button><button type="button" :disabled="!detail.report" @click="download('md')">Markdown</button></div></div>
          <div v-if="detail.status === 'queued' || detail.status === 'running'" class="evaluation-running" role="status"><span>{{ detail.completed_cases }} / {{ detail.total_cases }}</span><div><i :style="{ width: `${runProgress(detail)}%` }"></i></div><p>逐 Case 保存进度；同一 tenant 同时只运行一个评测。</p></div>
          <div v-else-if="detail.error_code" class="ingestion-trace-error"><span>STABLE ERROR</span><strong>{{ detail.error_code }}</strong><p>报告未生成；内部异常不会返回浏览器。</p></div>
          <div v-if="metrics.length" class="evaluation-metrics" data-testid="evaluation-metrics"><article v-for="([name, value]) in metrics" :key="name"><span>{{ metricLabel(name) }}</span><strong>{{ percent(value) }}</strong></article></div>
          <dl class="evaluation-snapshot"><div><dt>Dataset</dt><dd>{{ detail.dataset_revision ?? '缺失' }}</dd></div><div><dt>Index</dt><dd>{{ detail.index_revision ?? '缺失' }}</dd></div><div><dt>Prompt</dt><dd>{{ detail.prompt_revision ?? '缺失' }}</dd></div><div><dt>Provider</dt><dd>{{ detail.provider ?? '缺失' }} / {{ detail.model ?? '缺失' }}</dd></div><div><dt>Usage</dt><dd>{{ detail.usage.llm_calls ?? 0 }} LLM · {{ detail.usage.tokens ?? 0 }} tokens</dd></div><div><dt>Failed Cases</dt><dd>{{ failedCases.length }}</dd></div></dl>
          <div v-if="failedCases.length" class="evaluation-failures"><h3>低于满分的 Case</h3><code v-for="item in failedCases" :key="String(item.case_id)">{{ item.case_id }}</code></div>
        </template>
        <div v-else class="evaluation-empty"><strong>SELECT</strong><p>选择一次运行查看快照和指标。</p></div>
      </section>
    </div>

    <section class="evaluation-compare" data-testid="evaluation-compare"><div><p class="section-kicker">CONTROLLED COMPARISON</p><h2>比较两次运行</h2><p>只有 Dataset、Case 集、Mode、Index、Prompt 与 Provider 快照一致时才计算 Candidate − Base。</p></div><label>Base<select v-model="baseId"><option value="">选择 Base</option><option v-for="run in completedRuns" :key="run.id" :value="run.id">{{ short(run.id) }} · {{ modeLabel(run.mode) }}</option></select></label><label>Candidate<select v-model="candidateId"><option value="">选择 Candidate</option><option v-for="run in completedRuns" :key="run.id" :value="run.id">{{ short(run.id) }} · {{ modeLabel(run.mode) }}</option></select></label><button type="button" :disabled="!baseId || !candidateId || comparing" @click="compareRuns">{{ comparing ? '比较中…' : '比较' }}</button>
      <div v-if="comparison" class="comparison-result" :class="{ 'comparison-result--blocked': !comparison.comparable }"><template v-if="comparison.comparable"><strong>可以比较</strong><div><span v-for="(value, name) in comparison.deltas" :key="name">{{ metricLabel(String(name)) }} <b>{{ value == null ? '—' : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}pp` }}</b></span></div></template><template v-else><strong>不可比较</strong><ul><li v-for="reason in comparison.reasons" :key="reason">{{ reasonLabel(reason) }}</li></ul></template></div>
    </section>
  </section>
</template>
