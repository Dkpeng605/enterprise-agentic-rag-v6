<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  traceApi,
  type QueryMode,
  type QueryTraceStatus,
  type QueryTraceView,
  type TraceSummary,
} from '../api/traces'

const traces = ref<TraceSummary[]>([])
const nextCursor = ref<string>()
const selectedId = ref('')
const detail = ref<QueryTraceView>()
const mode = ref<QueryMode | ''>('')
const status = ref<QueryTraceStatus | ''>('')
const degraded = ref<'' | 'true' | 'false'>('')
const loading = ref(true)
const loadingMore = ref(false)
const detailLoading = ref(false)
const error = ref('')
const requestId = ref('')

const duration = computed(() => Math.max(detail.value?.summary.duration_ms ?? 0, 1))
const rankStages = [
  { key: 'dense_rank', score: 'dense_score', label: 'DENSE' },
  { key: 'sparse_rank', score: 'sparse_score', label: 'SPARSE' },
  { key: 'rrf_rank', score: 'rrf_score', label: 'RRF' },
  { key: 'rerank_rank', score: 'rerank_score', label: 'RERANK' },
] as const

function setError(caught: unknown, fallback: string): void {
  error.value = caught instanceof ApiError ? caught.message : fallback
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

async function loadTraces(append = false): Promise<void> {
  append ? (loadingMore.value = true) : (loading.value = true)
  error.value = ''
  requestId.value = ''
  try {
    const page = await traceApi.listQueries({
      mode: mode.value || undefined,
      status: status.value || undefined,
      degraded: degraded.value === '' ? undefined : degraded.value === 'true',
      cursor: append ? nextCursor.value : undefined,
      limit: 20,
    })
    traces.value = append ? [...traces.value, ...page.items] : page.items
    nextCursor.value = page.nextCursor
    if (!append) {
      detail.value = undefined
      selectedId.value = ''
      if (page.items[0]) await selectTrace(page.items[0].trace_id)
    }
  } catch (caught) {
    setError(caught, '无法载入 Query Trace。')
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
    const result = await traceApi.getQuery(traceId)
    if (selectedId.value === requestedId) detail.value = result
  } catch (caught) {
    if (selectedId.value === requestedId) {
      detail.value = undefined
      setError(caught, '无法载入 Trace 详情。')
    }
  } finally {
    if (selectedId.value === requestedId) detailLoading.value = false
  }
}

function stageLabel(name: string): string {
  return {
    'rag.query': 'Query',
    'rag.query.stream': 'Query Stream',
    'rag.query_planning': 'Planning',
    'rag.retrieval': 'Hybrid Retrieval',
    'rag.retrieval.branch': 'Sub-query Branch',
    'rag.query_embedding': 'Dense Encoding',
    'rag.sparse_encoding': 'Sparse Encoding',
    'rag.dense_retrieval': 'Dense Retrieval',
    'rag.sparse_retrieval': 'Sparse Retrieval',
    'rag.rrf_fusion': 'RRF Fusion',
    'rag.auth_and_scope': 'Scope Guard',
    'rag.rerank': 'Rerank',
    'rag.rerank.provider': 'Rerank Provider',
    'rag.root_restore': 'Root Restore',
    'rag.deep_recovery': 'Deep Recovery',
    'rag.deep_recovery.assess': 'Evidence Assess',
    'rag.deep_recovery.round': 'Recovery Round',
    'rag.answer_generation': 'Answer',
    'rag.answer_verification': 'Citation Verify / Repair',
    'rag.response_finalize': 'Finalize',
  }[name] ?? name.replace(/^rag\./, '')
}

function stageStyle(offsetMs: number, durationMs: number): Record<string, string> {
  return {
    left: `${Math.min(98, (offsetMs / duration.value) * 100)}%`,
    width: `${Math.max(1.5, Math.min(100, (durationMs / duration.value) * 100))}%`,
  }
}

function rankValue(value: number | null): string {
  return value == null ? '—' : `#${value}`
}

function scoreValue(value: number | null): string {
  return value == null ? '—' : value.toFixed(value < 0.1 ? 4 : 3)
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

function modeLabel(value: string | null): string {
  return value === 'deep' ? 'DEEP' : 'STANDARD'
}

function statusLabel(value: string): string {
  return { answered: '已回答', abstained: '已拒答', no_results: '无结果', error: '错误', cancelled: '已取消' }[value] ?? value
}

function recoveryRoute(value: string): string {
  return {
    query_rewrite_hybrid: '查询改写 · Hybrid', hyde_dense: 'HyDE · Dense',
    exact_term_sparse: '精确词 · Sparse', scope_repair: '范围修复 · Hybrid',
  }[value] ?? value
}

function degradationLabel(value: string): string {
  return {
    planner: 'Planner 回退', reranker: 'Reranker 回退', retrieval: '检索降级',
    generation: '生成降级', evidence_assessor: '证据评估降级', answer_generation: '回答生成降级',
  }[value] ?? `${value} 降级`
}

function metricStageLabel(value: string): string {
  return {
    query_planning: '查询改写',
    rrf_fusion: 'RRF 融合', auth_and_scope: '权限回源', rerank: 'CrossEncoder 重排',
    root_restore: 'Root 恢复', evidence_assessment: '证据覆盖评估',
    answer_generation: 'LLM 结构化回答', answer_verification: '引用核验 / 修复',
  }[value] ?? value
}

function metricAttributeLabel(value: string): string {
  return {
    ranked_lists: '排序列表', unique_leaves: '唯一 Leaf', duplicate_collapsed: '重复合并', root_quota_dropped: 'Root 配额淘汰',
    top_k_dropped: 'Top-K 淘汰', rerank_candidates: '实际送入重排', truncated_roots: '截断 Root',
    used_chars: '证据字符', llm_calls: 'LLM 调用', input_tokens: '输入 token',
    output_tokens: '输出 token', citations: '引用',
    decision: '决策', status: '状态', issues: '问题', repairs: '修复次数',
  }[value] ?? value
}

function sparseAlgorithmLabel(value: string): string {
  return value === 'milvus_builtin_bm25' ? 'BM25（Milvus 原生）' : value
}

onMounted(() => loadTraces())
</script>

<template>
  <section class="trace-page">
    <header class="workspace-heading">
      <div><p class="section-kicker">QUERY OBSERVABILITY</p><h1>Query Trace</h1><p>按当前租户展示查询改写、子查询、逐阶段数量与候选排名；不展示 Prompt、隐藏推理、密钥或内部异常堆栈。</p></div>
      <div class="trace-legend"><span><i class="legend-dot"></i>正常阶段</span><span><i class="legend-dot legend-dot--deep"></i>Deep Recovery</span><span><i class="legend-dot legend-dot--degraded"></i>降级</span></div>
    </header>

    <div class="trace-toolbar">
      <label>模式<select v-model="mode" @change="loadTraces()"><option value="">全部模式</option><option value="standard">Standard</option><option value="deep">Deep</option></select></label>
      <label>结果<select v-model="status" @change="loadTraces()"><option value="">全部结果</option><option value="answered">Answered</option><option value="abstained">Abstained</option><option value="no_results">No results</option><option value="error">Error</option><option value="cancelled">Cancelled</option></select></label>
      <label>健康状态<select v-model="degraded" @change="loadTraces()"><option value="">全部</option><option value="false">正常</option><option value="true">已降级</option></select></label>
      <button type="button" @click="loadTraces()">刷新</button>
    </div>

    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert"><strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="loadTraces()">重试</button></div>
    <div v-if="loading" class="trace-skeleton" aria-busy="true" aria-label="正在载入 Query Trace"><i></i><i></i><i></i></div>
    <div v-else-if="!traces.length" class="trace-empty" data-testid="trace-empty"><span>0</span><h2>还没有匹配的 Query Trace</h2><p>完成一次知识问答后，持久化 Trace 会出现在这里；当前筛选不会由演示数据填充。</p><RouterLink class="button button--primary" to="/chat">发起知识问答</RouterLink></div>
    <div v-else class="trace-layout">
      <aside class="trace-list" data-testid="trace-list">
        <button v-for="trace in traces" :key="trace.trace_id" type="button" :class="{ active: selectedId === trace.trace_id }" @click="selectTrace(trace.trace_id)">
          <span class="trace-mode" :class="`trace-mode--${trace.mode}`">{{ modeLabel(trace.mode) }}</span>
          <span><strong>{{ statusLabel(trace.status) }}</strong><small>{{ trace.subject_id.slice(0, 13) }}… · {{ dateLabel(trace.started_at) }}</small></span>
          <span class="trace-duration">{{ Math.round(trace.duration_ms) }}<small>ms</small></span>
          <i v-if="trace.degraded">DEGRADED</i>
        </button>
        <button v-if="nextCursor" class="load-more" type="button" :disabled="loadingMore" @click="loadTraces(true)">{{ loadingMore ? '载入中…' : '载入更多 Trace' }}</button>
      </aside>

      <main class="trace-detail">
        <div v-if="detailLoading" class="trace-detail-loading" aria-busy="true"><i></i><p>正在投影瀑布与排名…</p></div>
        <template v-else-if="detail">
          <header class="trace-detail-head"><div><p class="section-kicker">TRACE INSPECTOR</p><h2>{{ modeLabel(detail.summary.mode) }} · {{ statusLabel(detail.summary.status) }}</h2><code>{{ detail.summary.trace_id }}</code></div><dl><div><dt>耗时</dt><dd>{{ Math.round(detail.summary.duration_ms) }} ms</dd></div><div><dt>Span</dt><dd>{{ detail.stages.length }}</dd></div><div><dt>LLM calls</dt><dd>{{ detail.usage.llm_calls ?? '—' }}</dd></div></dl></header>

          <div v-if="detail.degradations.length" class="trace-degraded" role="status"><span>DEGRADED</span><div><strong>本次查询触发了安全回退</strong><p v-for="item in detail.degradations" :key="item.component">{{ degradationLabel(item.component) }}<template v-if="item.provider"> · {{ item.provider }}</template></p></div></div>

          <section class="trace-section" data-testid="query-plan">
            <div class="trace-section-head"><div><p class="section-kicker">QUERY PLAN</p><h3>查询改写与子查询</h3></div><span v-if="detail.plan">{{ detail.plan.provider }} · {{ detail.plan.intent }} · {{ detail.plan.language }}</span></div>
            <template v-if="detail.plan">
              <div class="query-plan-copy"><article><span>ORIGINAL</span><p>{{ detail.plan.original_query }}</p></article><article><span>REWRITTEN</span><p>{{ detail.plan.rewritten_query }}</p></article></div>
              <div class="query-branches"><article v-for="(query, index) in detail.plan.sub_queries" :key="`${index}-${query}`"><span>SUB-QUERY {{ index + 1 }}</span><strong>{{ query }}</strong><small>{{ detail.plan.sub_queries.length === 1 ? '未拆分' : `共 ${detail.plan.sub_queries.length} 条并行检索分支` }}</small></article></div>
            </template>
            <p v-else class="trace-inline-empty">旧 Trace 未保存 QueryPlan，无法从最终结果反推改写或子查询。</p>
          </section>

          <section class="trace-section" data-testid="retrieval-metrics">
            <div class="trace-section-head"><div><p class="section-kicker">RUNTIME RETRIEVAL SIGNALS</p><h3>各分支与阶段数量</h3></div><span>运行观测，不等同于 Recall@K</span></div>
            <div v-if="detail.retrieval_branches.length" class="retrieval-branch-grid"><article v-for="branch in detail.retrieval_branches" :key="branch.branch_index"><header><span>BRANCH {{ branch.branch_index + 1 }}</span><strong>{{ branch.query }}</strong></header><dl><div><dt>Dense 返回</dt><dd>{{ branch.dense_returned }} / {{ branch.dense_requested }}</dd></div><div><dt>Sparse 返回 · 算法</dt><dd>{{ branch.sparse_returned }} / {{ branch.sparse_requested }} · {{ sparseAlgorithmLabel(branch.sparse_algorithm) }}</dd></div><div><dt>两路交集</dt><dd>{{ branch.overlap_count }}</dd></div><div><dt>唯一 Leaf</dt><dd>{{ branch.unique_count }}</dd></div></dl></article></div>
            <p v-else class="trace-inline-empty">旧 Trace 未保存分支计数。</p>
            <div v-if="detail.stage_metrics.length" class="stage-metric-flow"><article v-for="(metric, index) in detail.stage_metrics" :key="`${metric.stage}-${index}`"><span>{{ metricStageLabel(metric.stage) }}</span><strong>{{ metric.input_count }} → {{ metric.output_count }}</strong><small>淘汰 / 拒绝 {{ metric.dropped_count }}</small><ul v-if="Object.keys(metric.attributes).length"><li v-for="(value, key) in metric.attributes" :key="key">{{ metricAttributeLabel(String(key)) }} · {{ value }}</li></ul></article></div>
            <p class="metric-disclaimer">候选返回率、Dense/Sparse 交集、权限过滤与排名位移可以描述单次运行；Recall@K、MRR、NDCG 必须使用带 gold 标注的评测集计算，请在“评测中心”查看。</p>
          </section>

          <section class="trace-section"><div class="trace-section-head"><div><p class="section-kicker">LATENCY WATERFALL</p><h3>全链路瀑布</h3></div><span>0 ms → {{ Math.round(detail.summary.duration_ms) }} ms</span></div><div class="waterfall" data-testid="waterfall"><article v-for="stage in detail.stages" :key="stage.span_id"><div><strong><b v-if="stage.parent_span_id">↳</b>{{ stageLabel(stage.name) }}</strong><small>+{{ Math.round(stage.offset_ms) }} ms · {{ stage.duration_ms.toFixed(1) }} ms<template v-if="stage.parent_span_id"> · parent {{ stage.parent_span_id.slice(0, 6) }}</template></small></div><div class="waterfall-track"><i :class="{ 'waterfall-bar--deep': stage.name.includes('deep_recovery'), 'waterfall-bar--degraded': stage.degraded }" :style="stageStyle(stage.offset_ms, stage.duration_ms)"></i></div></article><p v-if="!detail.stages.length" class="trace-inline-empty">该 Trace 没有已持久化 Span。</p></div></section>

          <section class="trace-section"><div class="trace-section-head"><div><p class="section-kicker">RANK MOVEMENT</p><h3>Dense / Sparse → RRF → Rerank</h3></div><span>{{ detail.rankings.length }} 个可追踪候选 · 多分支取最佳 Dense/Sparse 名次</span></div><div v-if="detail.rankings.length" class="rank-table" data-testid="rank-table"><div class="rank-row rank-row--head"><span>候选 / Root</span><span v-for="item in rankStages" :key="item.key">{{ item.label }}</span></div><div v-for="candidate in detail.rankings" :key="candidate.leaf_id" class="rank-row"><span><strong>{{ candidate.leaf_id }}</strong><small>{{ candidate.root_id || 'Root 未记录' }}</small></span><span v-for="item in rankStages" :key="item.key"><b>{{ rankValue(candidate[item.key]) }}</b><small>{{ scoreValue(candidate[item.score]) }}</small></span></div></div><p v-else class="trace-inline-empty">当前 Trace 没有候选排名事件，无法推断排名变化。</p></section>

          <section v-if="detail.summary.mode === 'deep'" class="trace-section"><div class="trace-section-head"><div><p class="section-kicker">DEEP RECOVERY</p><h3>证据恢复轮次</h3></div><span>最多展示实际执行轮次</span></div><div v-if="detail.recovery_rounds.length" class="recovery-grid" data-testid="recovery-rounds"><article v-for="round in detail.recovery_rounds" :key="round.round_number"><span>ROUND {{ round.round_number }}</span><strong>{{ recoveryRoute(round.route) }}</strong><p>目标 {{ round.target_count }} · 返回 {{ round.returned_count }} · 新增 {{ round.added_count }}</p><small v-if="round.duplicate_count">去重 {{ round.duplicate_count }} 条</small></article></div><p v-else class="trace-inline-empty">本次 Deep 查询未触发 Recovery，或旧 Trace 未记录恢复 Span。</p></section>
        </template>
      </main>
    </div>
  </section>
</template>
