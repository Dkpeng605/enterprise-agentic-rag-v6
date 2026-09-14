<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  workspaceApi,
  type DocumentPipeline,
  type LlmCleaningPreflight,
  type LlmCleaningResult,
  type PipelineRoot,
} from '../api/workspace'

const route = useRoute()
const documentId = computed(() => String(route.params.documentId))
const pipeline = ref<DocumentPipeline>()
const roots = ref<DocumentPipeline['roots']>([])
const selectedRootId = ref('')
const rootDetail = ref<PipelineRoot>()
const loading = ref(true)
const rootLoading = ref(false)
const loadingMore = ref(false)
const error = ref('')
const requestId = ref('')
const preflight = ref<LlmCleaningPreflight>()
const cleaningResult = ref<LlmCleaningResult>()
const confirmOpen = ref(false)
const remoteConfirmed = ref(false)
const cleaning = ref(false)

const llmAudit = computed(() => pipeline.value?.llm_cleaning ?? {})
const selectedLlmAudit = computed(() => {
  const value = rootDetail.value?.metadata.llm_cleaning
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
})

const preflightReason = computed(() => {
  if (!preflight.value) return '正在读取远程处理边界…'
  if (preflight.value.already_applied) return '当前文档版本已经完成一次 LLM 清洗，不能重复执行。'
  if (!preflight.value.provider) return '当前运行时未配置远程 LLM 清洗能力。'
  if (preflight.value.root_count > preflight.value.max_roots) return `Root 数 ${preflight.value.root_count} 超过单次上限 ${preflight.value.max_roots}。`
  if (preflight.value.input_chars > preflight.value.max_input_chars) return `输入字符 ${preflight.value.input_chars} 超过单次上限 ${preflight.value.max_input_chars}。`
  return preflight.value.reason ?? ''
})

function setError(caught: unknown, fallback: string): void {
  error.value = caught instanceof ApiError ? caught.message : fallback
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

async function loadPipeline(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await workspaceApi.getDocumentPipeline(documentId.value)
    pipeline.value = result
    roots.value = result.roots
    await loadPreflight()
    if (result.roots[0]) await selectRoot(result.roots[0].id)
  } catch (caught) {
    setError(caught, '无法载入文档处理链路。')
  } finally {
    loading.value = false
  }
}

async function loadPreflight(): Promise<void> {
  try {
    preflight.value = await workspaceApi.getLlmCleaningPreflight(documentId.value)
  } catch (caught) {
    setError(caught, '无法读取 LLM 清洗预检。')
  }
}

function openCleaningConfirm(): void {
  remoteConfirmed.value = false
  confirmOpen.value = true
}

async function runLlmCleaning(): Promise<void> {
  if (!remoteConfirmed.value || !preflight.value?.available || cleaning.value) return
  confirmOpen.value = false
  cleaning.value = true
  error.value = ''
  try {
    cleaningResult.value = await workspaceApi.runLlmCleaning(
      documentId.value,
      preflight.value.version_id,
    )
    await loadPipeline()
  } catch (caught) {
    setError(caught, 'LLM 清洗未完成，原索引已保留或恢复。')
  } finally {
    cleaning.value = false
  }
}

async function loadMore(): Promise<void> {
  const cursor = pipeline.value?.next_cursor
  if (cursor == null || loadingMore.value) return
  loadingMore.value = true
  try {
    const page = await workspaceApi.getDocumentPipeline(documentId.value, cursor)
    roots.value.push(...page.roots)
    pipeline.value = { ...page, roots: roots.value }
  } catch (caught) {
    setError(caught, '无法载入更多 Root。')
  } finally {
    loadingMore.value = false
  }
}

async function selectRoot(rootId: string): Promise<void> {
  selectedRootId.value = rootId
  rootLoading.value = true
  try {
    const result = await workspaceApi.getPipelineRoot(documentId.value, rootId)
    if (selectedRootId.value === rootId) rootDetail.value = result
  } catch (caught) {
    if (selectedRootId.value === rootId) setError(caught, '无法载入 Root 详情。')
  } finally {
    if (selectedRootId.value === rootId) rootLoading.value = false
  }
}

function locatorLabel(value: Record<string, unknown>): string {
  return Object.entries(value).map(([key, item]) => `${key}: ${String(item)}`).join(' · ')
}

function deltaLabel(raw: number, clean: number): string {
  const delta = clean - raw
  return delta === 0 ? '0' : `${delta > 0 ? '+' : ''}${delta}`
}

onMounted(loadPipeline)
</script>

<template>
  <section class="pipeline-page">
    <header class="workspace-heading">
      <div>
        <p class="section-kicker">DOCUMENT PIPELINE INSPECTOR</p>
        <h1>文档处理透视</h1>
        <p>逐项核对解析、确定性清洗、Root/Leaf 切分及送入检索的实际文本；数据始终受当前租户边界保护。</p>
      </div>
      <RouterLink class="button button--secondary" to="/workspace/documents">返回文档管理</RouterLink>
    </header>

    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert">
      <strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code>
      <button type="button" @click="loadPipeline">重试</button>
    </div>
    <div v-if="loading" class="pipeline-loading" aria-busy="true">正在读取 PostgreSQL 中的 Root / Leaf 事实源…</div>

    <template v-else-if="pipeline">
      <section class="pipeline-flow" aria-label="处理阶段">
        <article><span>01</span><small>PARSER</small><strong>{{ pipeline.parser_provider || '未记录' }}</strong><em>{{ pipeline.parser_version || '—' }}</em></article>
        <i>→</i>
        <article><span>02</span><small>CLEANER</small><strong>{{ pipeline.cleaner_provider || '旧数据未记录' }}</strong><em>{{ pipeline.cleaner_version || '—' }}</em></article>
        <i>→</i>
        <article :class="{ 'pipeline-stage--optional': !llmAudit.model }"><span>03</span><small>OPTIONAL LLM CLEAN</small><strong>{{ String(llmAudit.model || '未启用') }}</strong><em>{{ llmAudit.applied_at ? '已人工确认' : '默认不调用' }}</em></article>
        <i>→</i>
        <article><span>04</span><small>SPLITTER</small><strong>{{ pipeline.splitter_provider || '旧数据未记录' }}</strong><em>{{ pipeline.splitter_version || '—' }}</em></article>
        <i>→</i>
        <article><span>05</span><small>INDEX UNITS</small><strong>{{ pipeline.root_count }} Roots</strong><em>{{ pipeline.leaf_count }} Leaves</em></article>
      </section>

      <div class="pipeline-config">
        <div><span>文件</span><strong>{{ pipeline.source_name }}</strong></div>
        <div><span>Target tokens</span><strong>{{ pipeline.splitter_settings.target_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Max tokens</span><strong>{{ pipeline.splitter_settings.max_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Overlap tokens</span><strong>{{ pipeline.splitter_settings.overlap_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Tokenizer</span><strong>{{ pipeline.splitter_settings.tokenizer ?? '旧数据未记录' }}</strong></div>
      </div>

      <section class="llm-cleaning-panel" aria-label="人工 LLM 清洗">
        <div>
          <p class="section-kicker">MANUAL LLM CLEANING · OPT-IN</p>
          <h2>人工触发一次远程清洗</h2>
          <p>默认摄取只做本地确定性清洗。只有你明确确认后，当前版本的 <code>clean_text</code> 才会发送给远程模型；响应通过 JSON 结构与事实锚点校验后，系统才会重切分并重建索引。</p>
        </div>
        <dl v-if="preflight">
          <div><dt>Provider / Model</dt><dd>{{ preflight.provider || '未配置' }}<small>{{ preflight.model || '—' }}</small></dd></div>
          <div><dt>发送范围</dt><dd>{{ preflight.root_count }} Roots<small>{{ preflight.input_chars }} chars</small></dd></div>
          <div><dt>调用预算</dt><dd>{{ preflight.estimated_calls }} call<small>≤ {{ preflight.max_output_tokens }} output tokens</small></dd></div>
          <div><dt>数据边界</dt><dd>远程处理<small>发送 clean_text，不发送原文件</small></dd></div>
        </dl>
        <div class="llm-cleaning-actions">
          <p v-if="preflight && !preflight.available">{{ preflightReason }}</p>
          <p v-else>保护项：Root 数量/顺序、数字、URL、邮箱、引号值、标题、表头与 fenced code。</p>
          <button class="button button--primary" type="button" :disabled="!preflight?.available || cleaning" @click="openCleaningConfirm">{{ cleaning ? '正在远程清洗并重建索引…' : '预检通过，人工确认' }}</button>
        </div>
        <article v-if="cleaningResult" class="llm-cleaning-result" role="status">
          <strong>本次清洗已完成</strong>
          <span>{{ cleaningResult.changed_root_count }}/{{ cleaningResult.root_count }} Roots 变化</span>
          <span>Leaves {{ cleaningResult.leaf_count_before }} → {{ cleaningResult.leaf_count_after }}</span>
          <span>Tokens {{ cleaningResult.input_tokens }} in / {{ cleaningResult.output_tokens }} out</span>
          <span>{{ cleaningResult.llm_calls }} 次调用 · {{ cleaningResult.retry_count }} 次重试</span>
        </article>
        <article v-else-if="llmAudit.applied_at" class="llm-cleaning-result">
          <strong>当前版本已执行</strong>
          <span>{{ String(llmAudit.model || '—') }}</span>
          <span>{{ String(llmAudit.applied_at) }}</span>
          <span>{{ String(llmAudit.input_tokens ?? '—') }} in / {{ String(llmAudit.output_tokens ?? '—') }} out</span>
          <span>{{ String(llmAudit.changed_root_count ?? '—') }}/{{ String(llmAudit.root_count ?? pipeline.root_count) }} Roots 变化</span>
        </article>
      </section>

      <div v-if="!roots.length" class="trace-empty"><span>0</span><h2>尚无可检查内容</h2><p>文档完成摄取并进入 ready 后，这里会显示真实 Root 与 Leaf。</p></div>
      <div v-else class="pipeline-layout">
        <aside class="pipeline-roots">
          <div class="panel-caption"><span>ROOTS</span><strong>{{ pipeline.root_count }}</strong></div>
          <button v-for="root in roots" :key="root.id" type="button" :class="{ active: root.id === selectedRootId }" @click="selectRoot(root.id)">
            <span><b>#{{ root.ordinal + 1 }} · {{ root.kind }}</b><small>{{ locatorLabel(root.source_locator) }}</small></span>
            <em>{{ root.leaf_count }} leaves</em>
            <i :class="{ changed: root.changed }">{{ root.changed ? 'CLEANED' : 'UNCHANGED' }}</i>
          </button>
          <button v-if="pipeline.next_cursor != null" class="load-more" type="button" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '载入中…' : '载入更多 Root' }}</button>
        </aside>

        <main class="pipeline-detail">
          <div v-if="rootLoading" class="pipeline-loading" aria-busy="true">正在载入完整文本与 Leaf 边界…</div>
          <template v-else-if="rootDetail">
            <header>
              <div><p class="section-kicker">ROOT #{{ rootDetail.summary.ordinal + 1 }}</p><h2>{{ rootDetail.summary.kind }}</h2><code>{{ rootDetail.summary.id }}</code></div>
              <dl><div><dt>原始字符</dt><dd>{{ rootDetail.summary.raw_chars }}</dd></div><div><dt>清洗后</dt><dd>{{ rootDetail.summary.clean_chars }}</dd></div><div><dt>变化</dt><dd>{{ deltaLabel(rootDetail.summary.raw_chars, rootDetail.summary.clean_chars) }}</dd></div><div><dt>Leaf</dt><dd>{{ rootDetail.leaves.length }}</dd></div></dl>
            </header>

            <section class="cleaning-audit">
              <div class="trace-section-head"><div><p class="section-kicker">CLEANING AUDIT</p><h3>清洗规则执行记录</h3></div><span>{{ rootDetail.summary.cleaning_audit.length }} 条规则发生变更</span></div>
              <div v-if="rootDetail.summary.cleaning_audit.length" class="audit-grid">
                <article v-for="audit in rootDetail.summary.cleaning_audit" :key="`${audit.rule}-${audit.before_sha256}`"><strong>{{ audit.rule }}</strong><span>{{ audit.occurrences }} 次</span><small>{{ audit.before_sha256.slice(0, 10) }} → {{ audit.after_sha256.slice(0, 10) }}</small></article>
              </div>
              <p v-else class="trace-inline-empty">该 Root 未发生确定性清洗变化，或来自升级前尚未保存 audit 的摄取记录。</p>
              <article v-if="selectedLlmAudit" class="root-llm-audit">
                <div><small>MANUAL LLM AUDIT</small><strong>{{ String(selectedLlmAudit.provider) }} / {{ String(selectedLlmAudit.model) }}</strong></div>
                <span>{{ selectedLlmAudit.changed ? '文本已变化' : '模型保守原样返回' }}</span>
                <code>{{ String(selectedLlmAudit.before_sha256).slice(0, 16) }} → {{ String(selectedLlmAudit.after_sha256).slice(0, 16) }}</code>
                <small>{{ String(selectedLlmAudit.applied_at) }} · {{ String(selectedLlmAudit.input_tokens) }} in / {{ String(selectedLlmAudit.output_tokens) }} out</small>
              </article>
            </section>

            <section class="text-compare">
              <article><div><span>RAW INPUT</span><small>{{ rootDetail.raw_text.length }} chars</small></div><pre>{{ rootDetail.raw_text }}</pre></article>
              <article><div><span>CLEAN OUTPUT</span><small>{{ rootDetail.clean_text.length }} chars</small></div><pre>{{ rootDetail.clean_text }}</pre></article>
            </section>

            <section class="leaf-section">
              <div class="trace-section-head"><div><p class="section-kicker">LEAF CHUNKS</p><h3>实际检索单元</h3></div><span>offset 基于清洗后 Root；overlap 为相邻 Leaf 重叠字符数</span></div>
              <article v-for="leaf in rootDetail.leaves" :key="leaf.id" class="leaf-card">
                <header><strong>LEAF {{ leaf.ordinal + 1 }}</strong><code>{{ leaf.id }}</code><span>{{ leaf.token_count }} tokens</span><span>{{ leaf.start_offset ?? '—' }} → {{ leaf.end_offset ?? '—' }}</span><span>overlap {{ leaf.overlap_chars }} chars</span></header>
                <pre>{{ leaf.text }}</pre>
                <details v-if="leaf.retrieval_text !== leaf.text"><summary>查看增强后的 retrieval_text</summary><pre>{{ leaf.retrieval_text }}</pre></details>
              </article>
            </section>
          </template>
        </main>
      </div>
    </template>

    <div v-if="confirmOpen && preflight" class="sheet-backdrop" @click.self="confirmOpen = false">
      <section class="workspace-sheet llm-confirm" role="dialog" aria-modal="true" aria-labelledby="llm-cleaning-title">
        <button class="sheet-close" type="button" aria-label="关闭" @click="confirmOpen = false">×</button>
        <div><p class="section-kicker">REMOTE DATA DISCLOSURE</p><h2 id="llm-cleaning-title">确认发送至远程 LLM</h2></div>
        <p>这不是自动步骤。系统会把当前版本的 {{ preflight.root_count }} 个 <code>clean_text</code>（共 {{ preflight.input_chars }} 字符）发送给 <strong>{{ preflight.provider }} / {{ preflight.model }}</strong>，最多调用一次。远程服务可能产生费用并受其数据处理条款约束。</p>
        <label class="llm-confirm-check"><input v-model="remoteConfirmed" type="checkbox">我已了解 clean_text 将离开本机并由配置的远程模型处理，确认执行一次清洗、重切分和索引重建。</label>
        <div class="llm-confirm-actions"><button class="button button--secondary" type="button" @click="confirmOpen = false">取消</button><button class="button button--primary" type="button" :disabled="!remoteConfirmed" @click="runLlmCleaning">确认并执行</button></div>
      </section>
    </div>
  </section>
</template>
