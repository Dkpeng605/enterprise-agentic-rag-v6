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
const imagePreviewErrors = ref<Record<string, boolean>>({})

type ImageFact = {
  page: number | null
  ordinal: number
  name: string
  mediaType: string
  width: number | null
  height: number | null
  sha256: string
  objectKey: string
  caption: string | null
  captionStatus: string
  captionErrorCode: string | null
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function textValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function nullableTextValue(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function nullableNumberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function normalizeImage(value: unknown, index: number): ImageFact | undefined {
  const item = recordValue(value)
  if (!item) return undefined
  return {
    page: nullableNumberValue(item.page),
    ordinal: nullableNumberValue(item.ordinal) ?? index,
    name: textValue(item.name, `image-${index + 1}`),
    mediaType: textValue(item.media_type, '未知 MIME'),
    width: nullableNumberValue(item.width),
    height: nullableNumberValue(item.height),
    sha256: textValue(item.sha256, '未记录'),
    objectKey: textValue(item.object_key, '未记录'),
    caption: nullableTextValue(item.caption),
    captionStatus: textValue(item.caption_status, 'unknown'),
    captionErrorCode: nullableTextValue(item.caption_error_code),
  }
}

const imageFacts = computed<ImageFact[]>(() => {
  const value = rootDetail.value?.metadata.images
  return Array.isArray(value)
    ? value.map((item, index) => normalizeImage(item, index)).filter((item): item is ImageFact => item !== undefined)
    : []
})

const hasImageMetadata = computed(() => {
  const metadata = rootDetail.value?.metadata
  return Boolean(metadata && (
    Object.prototype.hasOwnProperty.call(metadata, 'images')
    || Object.prototype.hasOwnProperty.call(metadata, 'vision_image_count')
  ))
})

const visionFacts = computed(() => {
  const metadata = rootDetail.value?.metadata ?? {}
  const counts = recordValue(metadata.vision_caption_status_counts)
  const count = (status: string): number => {
    const value = counts?.[status]
    return typeof value === 'number' && Number.isFinite(value) ? value : imageFacts.value.filter((image) => image.captionStatus === status).length
  }
  return {
    provider: textValue(metadata.vision_provider, '未记录'),
    model: textValue(metadata.vision_model, '未记录'),
    remote: metadata.vision_remote === true,
    degraded: metadata.vision_degraded === true || imageFacts.value.some((image) => image.captionStatus === 'degraded'),
    imageCount: typeof metadata.vision_image_count === 'number' ? metadata.vision_image_count : imageFacts.value.length,
    captionCount: typeof metadata.vision_caption_count === 'number' ? metadata.vision_caption_count : count('created'),
    createdCount: count('created'),
    skippedCount: count('skipped'),
    degradedCount: count('degraded'),
  }
})

const retrievalCaptions = computed(() => {
  const leaves = rootDetail.value?.leaves ?? []
  return imageFacts.value
    .filter((image) => image.caption)
    .map((image) => ({ image, included: leaves.some((leaf) => leaf.retrieval_text.includes(image.caption as string)) }))
})

const includedCaptionCount = computed(() => retrievalCaptions.value.filter((item) => item.included).length)

function imageStatusLabel(status: string): string {
  return { created: 'created · 已生成', skipped: 'skipped · 未启用', degraded: 'degraded · 已降级' }[status] ?? `${status} · 未知状态`
}

function imageStatusClass(status: string): string {
  return `image-status--${status}`
}

function imageDimension(image: ImageFact): string {
  return image.width !== null && image.height !== null ? `${image.width} × ${image.height}` : '尺寸未记录'
}

function shortHash(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value
}

function pageLabel(page: number | null): string {
  return page === null ? '页码未记录' : `第 ${page} 页`
}

function imageUrl(image: ImageFact): string {
  return workspaceApi.getDocumentImageUrl(documentId.value, image.sha256)
}

function markImagePreviewError(sha256: string): void {
  imagePreviewErrors.value = { ...imagePreviewErrors.value, [sha256]: true }
}

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
  if (caught instanceof ApiError && caught.code === 'LLM_UNAVAILABLE') {
    error.value = '远程 LLM 当前不可用，请检查 endpoint、模型和网络；原文与现有索引不会被修改。'
  } else if (caught instanceof ApiError && caught.code === 'LLM_INVALID_RESPONSE') {
    error.value = '远程 LLM 返回的清洗结果无法通过安全校验，原文与现有索引已保留。'
  } else {
    error.value = caught instanceof ApiError ? caught.message : fallback
  }
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
        <h1>文档解析与切分</h1>
        <p>逐项核对解析、确定性清洗、原文块与检索块，以及最终送入检索的实际文本；数据始终受当前工作区边界保护。</p>
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
        <div><span>Embedding limit</span><strong>{{ pipeline.splitter_settings.embedding_token_limit ?? '未提供' }}</strong></div>
        <div><span>Safe budget</span><strong>{{ pipeline.splitter_settings.max_tokens ?? '—' }}</strong></div>
        <div><span>Hard cuts</span><strong>{{ pipeline.splitter_settings.hard_cut_count ?? '—' }}</strong></div>
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
          <p v-else>保护项：Root 数量/顺序、词法内容顺序、数字、URL、邮箱、引号值、标题、表头与 fenced code；仅允许修复 PDF/OCR 版面空白、段落换行、标题/表格间距和跨行断词，或删除跨 Root 重复的原始首/尾噪声行。</p>
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

      <div v-if="!roots.length" class="trace-empty"><span>0</span><h2>尚无可检查内容</h2><p>文档处理完成并进入就绪状态后，这里会显示真实原文块与检索块。</p></div>
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

            <section v-if="hasImageMetadata" class="image-enrichment-panel" data-testid="image-enrichment">
              <div class="trace-section-head">
                <div><p class="section-kicker">IMAGE ENRICHMENT</p><h3>图片提取与 Caption 事实</h3></div>
                <span>{{ visionFacts.imageCount }} 张图片 · {{ visionFacts.captionCount }} 个有效 Caption</span>
              </div>
              <div class="image-enrichment-summary">
                <div><span>VISION PROVIDER / MODEL</span><strong>{{ visionFacts.provider }} / {{ visionFacts.model }}</strong><small>{{ visionFacts.remote ? '远程 Vision · 已记录' : '本地或关闭 · 已记录' }}</small></div>
                <div><span>CAPTION STATUS</span><strong>created {{ visionFacts.createdCount }} · skipped {{ visionFacts.skippedCount }} · degraded {{ visionFacts.degradedCount }}</strong><small :class="{ 'image-fact-warning': visionFacts.degraded }">{{ visionFacts.degraded ? '至少一张图片未生成 Caption' : '本 Root 无 Caption 降级' }}</small></div>
                <div><span>RETRIEVAL INCLUSION</span><strong>{{ includedCaptionCount }}/{{ retrievalCaptions.length }} 个 Caption</strong><small>{{ includedCaptionCount ? 'Caption 已进入首个 Leaf 的 retrieval_text' : retrievalCaptions.length ? 'Caption 未出现在已持久化 retrieval_text' : '没有可进入检索的 Caption' }}</small></div>
              </div>
              <div v-if="imageFacts.length" class="image-fact-list">
                <article v-for="image in imageFacts" :key="`${image.ordinal}-${image.sha256}`" class="image-fact-card">
                  <header><strong>IMAGE {{ image.ordinal + 1 }}</strong><b>{{ image.name }}</b><span :class="['image-status', imageStatusClass(image.captionStatus)]">{{ imageStatusLabel(image.captionStatus) }}</span></header>
                  <dl>
                    <div><dt>来源</dt><dd>{{ pageLabel(image.page) }} · {{ imageDimension(image) }} · {{ image.mediaType }}</dd></div>
                    <div><dt>SHA-256</dt><dd :title="image.sha256">{{ shortHash(image.sha256) }}</dd></div>
                    <div><dt>Object key</dt><dd :title="image.objectKey">{{ image.objectKey }}</dd></div>
                    <div><dt>检索状态</dt><dd>{{ image.caption ? (retrievalCaptions.find((item) => item.image === image)?.included ? 'Caption 已进入 retrieval_text' : 'Caption 未进入 retrieval_text') : '无 Caption' }}</dd></div>
                  </dl>
                  <div v-if="image.sha256 !== '未记录'" class="image-preview-wrap">
                    <img
                      v-if="!imagePreviewErrors[image.sha256]"
                      class="image-preview"
                      :src="imageUrl(image)"
                      :alt="`${image.name} 原图预览`"
                      loading="lazy"
                      @error="markImagePreviewError(image.sha256)"
                    >
                    <p v-if="imagePreviewErrors[image.sha256]" class="image-preview-error" role="status">图片预览加载失败；metadata 与 Caption 仍可用。</p>
                  </div>
                  <p v-if="image.caption" class="image-caption">{{ image.caption }}</p>
                  <p v-else class="image-caption image-caption--empty">{{ image.captionErrorCode ? `未生成 Caption · ${image.captionErrorCode}` : '未生成 Caption' }}</p>
                </article>
              </div>
              <p v-else class="trace-inline-empty">当前 Root 没有 Loader 提取的图片；此结论来自 PostgreSQL Root metadata，而非当前运行配置。</p>
              <p class="metric-disclaimer">图片预览通过当前租户、文档 active version 和 SHA-256 校验后的受保护接口读取；以上 metadata 与 Caption 来自 Root 持久化事实源，Caption 是否进入检索以已保存 Leaf 文本为准。</p>
            </section>

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
              <div class="trace-section-head"><div><p class="section-kicker">LEAF CHUNKS</p><h3>实际检索单元</h3></div><span>Leaf 不重叠；Root 负责上下文恢复，offset 基于清洗后 Root</span></div>
              <article v-for="leaf in rootDetail.leaves" :key="leaf.id" class="leaf-card">
                <header><strong>LEAF {{ leaf.ordinal + 1 }}</strong><code>{{ leaf.id }}</code><span>{{ leaf.token_count }} tokens</span><span>{{ String(leaf.metadata.boundary ?? 'boundary 未记录') }}</span><span v-if="leaf.metadata.hard_cut" class="leaf-warning">TOKEN HARD CUT</span><span>{{ leaf.start_offset ?? '—' }} → {{ leaf.end_offset ?? '—' }}</span><span>overlap {{ leaf.overlap_chars }} chars</span></header>
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
