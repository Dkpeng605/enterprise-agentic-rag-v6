<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '../api/client'
import {
  workspaceApi,
  type DocumentPipeline,
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
    if (result.roots[0]) await selectRoot(result.roots[0].id)
  } catch (caught) {
    setError(caught, '无法载入文档处理链路。')
  } finally {
    loading.value = false
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
        <article><span>03</span><small>SPLITTER</small><strong>{{ pipeline.splitter_provider || '旧数据未记录' }}</strong><em>{{ pipeline.splitter_version || '—' }}</em></article>
        <i>→</i>
        <article><span>04</span><small>INDEX UNITS</small><strong>{{ pipeline.root_count }} Roots</strong><em>{{ pipeline.leaf_count }} Leaves</em></article>
      </section>

      <div class="pipeline-config">
        <div><span>文件</span><strong>{{ pipeline.source_name }}</strong></div>
        <div><span>Target tokens</span><strong>{{ pipeline.splitter_settings.target_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Max tokens</span><strong>{{ pipeline.splitter_settings.max_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Overlap tokens</span><strong>{{ pipeline.splitter_settings.overlap_tokens ?? '旧数据未记录' }}</strong></div>
        <div><span>Tokenizer</span><strong>{{ pipeline.splitter_settings.tokenizer ?? '旧数据未记录' }}</strong></div>
      </div>

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
  </section>
</template>
