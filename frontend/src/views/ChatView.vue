<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  queryApi,
  type Citation,
  type Collection,
  type QueryResult,
  type QueryStreamEvent,
} from '../api/query'

type RunState = 'idle' | 'streaming' | 'completed' | 'interrupted' | 'rate_limited' | 'error'
type ChatTurn = { role: 'user' | 'assistant'; content: string }

const question = ref('')
const mode = ref<'standard' | 'deep'>('standard')
const collections = ref<Collection[]>([])
const selectedCollections = ref<string[]>([])
const collectionsError = ref(false)
const runState = ref<RunState>('idle')
const currentStage = ref('')
const queryId = ref('')
const result = ref<QueryResult | null>(null)
const streamError = ref('')
const retryAfter = ref<number>()
const history = ref<ChatTurn[]>([])
let controller: AbortController | undefined

const canSend = computed(
  () => question.value.trim().length > 0 && question.value.length <= 2_000 && runState.value !== 'streaming',
)
const citations = computed<Citation[]>(() => result.value?.citations ?? [])
const stageLabel = computed(() => {
  const labels: Record<string, string> = {
    planning: '理解问题与范围',
    retrieving: 'Dense / Sparse 双路检索',
    reranking: '融合与重排证据',
    recovering: '恢复完整上下文',
    answering: '生成并核验引用',
  }
  return labels[currentStage.value] ?? '已接收，准备检索'
})

onMounted(async () => {
  try {
    collections.value = await queryApi.listCollections()
  } catch {
    collectionsError.value = true
  }
})
onBeforeUnmount(() => controller?.abort())

function handleEvent(event: QueryStreamEvent): void {
  if (event.type === 'accepted') queryId.value = event.queryId
  if (event.type === 'progress') currentStage.value = event.stage
  if (event.type === 'completed') {
    result.value = event.result
    queryId.value = event.result.query_id
    history.value.push({ role: 'assistant', content: event.result.answer })
    runState.value = 'completed'
  }
  if (event.type === 'error') {
    queryId.value = event.queryId ?? queryId.value
    if (event.code === 'RATE_LIMITED') {
      runState.value = 'rate_limited'
      streamError.value = '当前匿名查询额度已用完，请稍后再试。'
    } else {
      runState.value = 'error'
      streamError.value = '查询执行失败，可复制 Query ID 反馈问题。'
    }
  }
}

async function submit(): Promise<void> {
  const text = question.value.trim()
  if (!canSend.value || !text) return
  const requestHistory = history.value.slice(-12)
  history.value.push({ role: 'user', content: text })
  question.value = ''
  runState.value = 'streaming'
  currentStage.value = ''
  queryId.value = ''
  result.value = null
  streamError.value = ''
  retryAfter.value = undefined
  controller = new AbortController()
  try {
    await queryApi.stream(
      {
        query: text,
        mode: mode.value,
        scope: { collection_ids: selectedCollections.value },
        history: requestHistory,
      },
      controller.signal,
      handleEvent,
    )
    if (runState.value === 'streaming') runState.value = 'interrupted'
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === 'AbortError') {
      runState.value = 'interrupted'
      return
    }
    if (caught instanceof ApiError && caught.status === 429) {
      runState.value = 'rate_limited'
      retryAfter.value = caught.retryAfterSeconds
      streamError.value = '当前匿名查询额度已用完，请稍后再试。'
      return
    }
    runState.value = 'error'
    streamError.value = caught instanceof ApiError && caught.requestId
      ? `服务暂时不可用（Request ID: ${caught.requestId}）`
      : '连接查询服务失败，请检查网络后重新发起一次查询。'
  } finally {
    controller = undefined
  }
}

function stop(): void {
  controller?.abort()
}
</script>

<template>
  <section class="chat-page">
    <header class="chat-heading">
      <div><p class="section-kicker">PUBLIC RAG · EVIDENCE FIRST</p><h1>知识问答</h1><p>答案只依据当前 Demo Tenant 的已授权文档；证据不足时会明确拒答。</p></div>
      <div class="quota-note"><span>匿名额度</span><strong>Standard / Deep</strong><small>达到上限后不会降级为无依据回答</small></div>
    </header>

    <div class="chat-layout">
      <aside class="chat-controls">
        <div class="control-block">
          <span class="control-label">回答模式</span>
          <button class="mode-option" :class="{ active: mode === 'standard' }" type="button" @click="mode = 'standard'">
            <span>S</span><div><strong>Standard</strong><small>单轮检索 · 速度优先</small></div><i></i>
          </button>
          <button class="mode-option" :class="{ active: mode === 'deep' }" type="button" @click="mode = 'deep'">
            <span>D</span><div><strong>Deep</strong><small>证据恢复 · 完整优先</small></div><i></i>
          </button>
        </div>
        <div class="control-block">
          <span class="control-label">知识范围</span>
          <p v-if="collectionsError" class="scope-state">集合暂不可用，将由服务端限定安全范围。</p>
          <p v-else-if="!collections.length" class="scope-state">当前租户暂无集合，查询会返回无结果。</p>
          <label v-for="collection in collections" :key="collection.id" class="scope-choice">
            <input v-model="selectedCollections" type="checkbox" :value="collection.id" />
            <span><strong>{{ collection.name }}</strong><small>{{ collection.ready_document_count }} 个可查询文档</small></span>
          </label>
          <small v-if="collections.length" class="scope-hint">不勾选表示搜索当前 Demo Tenant 全部集合。</small>
        </div>
      </aside>

      <div class="conversation-panel">
        <div v-if="!history.length && runState === 'idle'" class="empty-conversation">
          <span class="empty-conversation__mark">?</span>
          <h2>从一个可验证的问题开始</h2>
          <p>例如：比较两份制度中关于年假的规定，并标明出处。</p>
        </div>

        <div v-for="(turn, index) in history" :key="index" class="chat-turn" :class="`chat-turn--${turn.role}`">
          <span>{{ turn.role === 'user' ? '你' : 'A' }}</span><div>{{ turn.content }}</div>
        </div>

        <div v-if="runState === 'streaming'" class="stream-card" data-testid="stream-status">
          <span class="stream-pulse"></span><div><strong>{{ stageLabel }}</strong><small v-if="queryId">Query ID · {{ queryId }}</small></div>
          <button type="button" @click="stop">停止</button>
        </div>

        <div v-if="result" class="answer-meta" :class="`answer-meta--${result.status}`">
          <strong>{{ result.status === 'answered' ? '已通过证据核验' : '已触发有边界拒答' }}</strong>
          <span>{{ mode === 'deep' ? 'Deep' : 'Standard' }} · {{ citations.length }} 条引用</span>
        </div>

        <div v-if="citations.length" class="citations" data-testid="citations">
          <h3>引用证据</h3>
          <details v-for="citation in citations" :key="citation.id">
            <summary><span>[{{ citation.id }}]</span><strong>{{ citation.title }}</strong><small>{{ citation.page ? `第 ${citation.page} 页` : citation.section ?? '文档片段' }}</small></summary>
            <blockquote>{{ citation.quote }}</blockquote>
            <p>{{ citation.source_name }} · Root {{ citation.root_id.slice(0, 10) }}…</p>
          </details>
        </div>

        <div v-if="runState === 'interrupted'" class="query-alert query-alert--warning" role="status">
          <strong>连接已中断</strong><p>已保留当前页面内容，不会自动无限重连。请重新发起查询。</p><code v-if="queryId">{{ queryId }}</code>
        </div>
        <div v-if="runState === 'rate_limited'" class="query-alert query-alert--limit" role="alert">
          <strong>匿名额度暂时用完</strong><p>{{ streamError }}<template v-if="retryAfter"> 约 {{ retryAfter }} 秒后可重试。</template></p>
        </div>
        <div v-if="runState === 'error'" class="query-alert query-alert--error" role="alert">
          <strong>本次查询未完成</strong><p>{{ streamError }}</p><code v-if="queryId">{{ queryId }}</code>
        </div>

        <form class="composer" data-testid="chat-composer" @submit.prevent="submit">
          <textarea v-model="question" maxlength="2000" rows="3" placeholder="输入一个需要引用依据的问题…" aria-label="问题"></textarea>
          <div><span :class="{ 'count-limit': question.length >= 1900 }">{{ question.length }} / 2000</span><button class="send-button" type="submit" :disabled="!canSend">{{ runState === 'streaming' ? '查询中' : '发送' }} <i>↗</i></button></div>
        </form>
      </div>
    </div>
  </section>
</template>
