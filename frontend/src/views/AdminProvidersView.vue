<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  providerApi,
  type ProviderCatalog,
  type ProviderIndexStatus,
  type ProviderDiagnostic,
  type ProviderKind,
  type ProviderOption,
} from '../api/providers'

type ViewState = 'loading' | 'ready' | 'error'

const kindLabels: Record<string, string> = {
  embedding: 'Embedding',
  reranker: 'Reranker',
  llm: 'LLM',
  vision: 'Vision',
  vector_store: 'Vector Store',
  splitter: 'Splitter',
  evaluator: 'Evaluator',
  sparse_encoder: 'Sparse Encoder',
}

const state = ref<ViewState>('loading')
const catalog = ref<ProviderCatalog>()
const errorMessage = ref('')
const actionMessage = ref('')
const actionError = ref('')
const savingKey = ref('')
const indexStatus = ref<ProviderIndexStatus>()
const reindexing = ref(false)

const selectableKinds = ['embedding', 'reranker', 'vision', 'sparse_encoder'] as const
const optionsByKind = computed(() =>
  selectableKinds.map((kind) => ({
    kind,
    label: kindLabels[kind],
    options: (catalog.value?.options ?? []).filter((option) => option.kind === kind),
  })),
)
const currentProviders = computed(() => catalog.value?.providers ?? [])
const currentLlm = computed(() => currentProviders.value.filter((provider) => provider.kind === 'llm'))
const pendingRestart = computed(() => Boolean(catalog.value?.selection.pending_restart))

function providerLabel(kind: string): string {
  return kindLabels[kind] ?? kind
}

function healthLabel(health: string): string {
  return { healthy: '健康', degraded: '降级', unavailable: '不可用', unknown: '待探测' }[health] ?? health
}

function providerStatusClass(provider: ProviderDiagnostic): string {
  return `provider-admin-card--${provider.health}`
}

function optionValue(kind: ProviderKind): string {
  const providerKind = kind === 'sparse_encoder' || kind === 'vision'
  const field = providerKind ? `${kind}_provider` : `${kind}_model`
  const pendingField = providerKind ? `pending_${kind}_provider` : `pending_${kind}_model`
  const pending = catalog.value?.selection[pendingField]
  if (typeof pending === 'string') return pending
  const current = catalog.value?.selection[field]
  return typeof current === 'string' ? current : ''
}

function optionSelected(option: ProviderOption): boolean {
  return optionValue(option.kind as ProviderKind) === option.key
}

function formatOptionMeta(option: ProviderOption): string {
  const facts: string[] = []
  if (option.dimension != null) facts.push(`${option.dimension} 维`)
  if (option.input_token_limit != null) facts.push(`${option.input_token_limit} tokens`)
  if (option.language_note) facts.push(option.language_note)
  return facts.join(' · ')
}

async function load(): Promise<void> {
  state.value = 'loading'
  errorMessage.value = ''
  try {
    const [loadedCatalog, loadedIndexStatus] = await Promise.all([
      providerApi.load(),
      providerApi.indexStatus(),
    ])
    catalog.value = loadedCatalog
    indexStatus.value = loadedIndexStatus
    state.value = 'ready'
  } catch (caught) {
    state.value = 'error'
    errorMessage.value = caught instanceof ApiError ? caught.message : 'Provider 目录暂时无法载入。'
  }
}

async function reindex(): Promise<void> {
  if (reindexing.value) return
  reindexing.value = true
  actionMessage.value = ''
  actionError.value = ''
  try {
    const result = await providerApi.reindex()
    indexStatus.value = await providerApi.indexStatus()
    actionMessage.value = `重建完成：${result.rebuilt_count} 个文档已切换到当前 Embedding revision，${result.skipped_count} 个无需处理。`
    if (result.failed_count || result.cleanup_failed_count) {
      actionError.value = `${result.failed_count} 个文档失败，${result.cleanup_failed_count} 个文档旧向量清理降级，请查看下方明细。`
    }
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught.message : '索引重建失败，请稍后重试。'
  } finally {
    reindexing.value = false
  }
}

async function selectProvider(kind: ProviderKind, key: string): Promise<void> {
  if (savingKey.value || optionValue(kind) === key) return
  savingKey.value = `${kind}:${key}`
  actionMessage.value = ''
  actionError.value = ''
  try {
    catalog.value = await providerApi.select(kind, key)
    actionMessage.value = '选择已保存。重启 Mac backend 后生效；Embedding 变更后请在下方执行安全索引重建。'
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught.message : 'Provider 选择失败，请稍后重试。'
  } finally {
    savingKey.value = ''
  }
}

onMounted(load)
</script>

<template>
  <section class="provider-admin-page">
    <header class="provider-admin-heading">
      <div>
        <p class="section-kicker">SYSTEM · PROVIDER CATALOG</p>
        <h1>Provider 管理</h1>
        <p>查看当前运行时真实注册的 Provider，并选择下一次启动要使用的 Embedding、Reranker、Vision 与 Sparse profile。</p>
      </div>
      <button class="button button--secondary" type="button" :disabled="state === 'loading'" @click="load">刷新目录</button>
    </header>

    <div v-if="state === 'loading'" class="provider-admin-state">正在读取当前运行时的 Provider…</div>
    <div v-else-if="state === 'error'" class="provider-admin-state provider-admin-state--error" role="alert">
      <strong>Provider 目录无法载入</strong><p>{{ errorMessage }}</p><button class="button button--primary" type="button" @click="load">重新载入</button>
    </div>

    <template v-else-if="catalog">
      <div v-if="pendingRestart" class="provider-admin-banner provider-admin-banner--pending" role="status">
        <strong>存在待生效选择</strong><span>配置已写入本机运行时目录，但当前进程仍使用旧模型；重启 backend 后才会切换。</span>
      </div>
      <div v-if="actionMessage" class="provider-admin-banner" role="status">{{ actionMessage }}</div>
      <div v-if="actionError" class="provider-admin-banner provider-admin-banner--error" role="alert">{{ actionError }}</div>

      <section class="provider-admin-section" aria-labelledby="active-provider-title">
        <div class="section-heading"><div><p class="section-kicker">LIVE REGISTRY</p><h2 id="active-provider-title">当前运行中的 Provider</h2></div><span>{{ currentProviders.length }} 个实例</span></div>
        <div class="provider-admin-grid">
          <article v-for="provider in currentProviders" :key="`${provider.kind}:${provider.name}`" class="provider-admin-card" :class="providerStatusClass(provider)">
            <header><span>{{ providerLabel(provider.kind) }}</span><i></i></header>
            <h3>{{ provider.name }}</h3>
            <p>{{ provider.version }}</p>
            <small>{{ provider.capabilities.join(' · ') || '无额外 capability' }}</small>
            <footer><strong>{{ healthLabel(provider.health) }}</strong><span>{{ provider.is_remote ? 'REMOTE' : 'LOCAL' }}</span></footer>
          </article>
        </div>
      </section>

      <section class="provider-admin-section" aria-labelledby="select-provider-title">
        <div class="section-heading"><div><p class="section-kicker">RESTART-BOUND PROFILES</p><h2 id="select-provider-title">可选模型与配置</h2></div><span>选择后保存到本机</span></div>
        <div class="provider-option-groups">
          <article v-for="group in optionsByKind" :key="group.kind" class="provider-option-group">
            <header><div><strong>{{ group.label }}</strong><small>当前：{{ optionValue(group.kind) || '未配置' }}</small></div><span>{{ group.options.length }} 个 profile</span></header>
            <label v-for="option in group.options" :key="option.key" class="provider-option" :class="{ 'provider-option--selected': optionSelected(option), 'provider-option--unavailable': !option.available }">
              <input type="radio" :name="group.kind" :value="option.key" :checked="optionSelected(option)" :disabled="Boolean(savingKey) || !option.available" @change="selectProvider(group.kind, option.key)">
              <span class="provider-option__body"><strong>{{ option.label }} <i>{{ option.provider }} · {{ option.is_remote ? 'REMOTE' : 'LOCAL' }}</i></strong><b>{{ option.model }}</b><small>{{ formatOptionMeta(option) }}<template v-if="option.note"> · {{ option.note }}</template><template v-if="option.unavailable_reason"> · {{ option.unavailable_reason }}</template></small></span>
              <em v-if="optionSelected(option)">{{ pendingRestart ? '待重启生效' : '当前' }}</em><em v-else-if="savingKey === `${group.kind}:${option.key}`">保存中</em>
              <em v-else-if="!option.available">未配置</em>
            </label>
          </article>
        </div>
      </section>

      <section v-if="indexStatus" class="provider-admin-section" aria-labelledby="index-status-title">
        <div class="section-heading">
          <div><p class="section-kicker">INDEX COMPATIBILITY</p><h2 id="index-status-title">Embedding 索引兼容状态</h2></div>
          <button class="button button--primary" type="button" :disabled="reindexing || indexStatus.incompatible_documents === 0" @click="reindex">
            {{ reindexing ? '重建中…' : '重建不兼容文档' }}
          </button>
        </div>
        <div class="provider-index-summary">
          <span>当前 revision <strong>{{ indexStatus.active_revision }}</strong></span>
          <span>维度 <strong>{{ indexStatus.embedding_dimension }}</strong></span>
          <span>兼容 <strong>{{ indexStatus.compatible_documents }}/{{ indexStatus.total_documents }}</strong></span>
          <span v-if="indexStatus.incompatible_documents" class="provider-index-warning">不兼容 {{ indexStatus.incompatible_documents }} 个</span>
          <span v-else class="provider-index-ok">全部可检索</span>
        </div>
        <p class="provider-index-help">Embedding 是向量维度、tokenizer 与 Milvus collection 的索引契约。切换后先重启 backend，再执行这里的重建；Reranker 切换不需要重建向量。</p>
        <div v-if="indexStatus.documents.length" class="provider-index-table">
          <div v-for="document in indexStatus.documents" :key="document.document_id" class="provider-index-row">
            <div><strong>{{ document.title }}</strong><small>{{ document.document_id }}</small></div>
            <span>{{ document.leaf_count }} Leaves · {{ document.vector_count }} vectors</span>
            <span :class="document.compatible ? 'provider-index-ok' : 'provider-index-warning'">{{ document.compatible ? '兼容' : '需要重建' }}</span>
            <small>{{ document.stored_revisions.join(' / ') || '无 Root revision' }}</small>
          </div>
        </div>
      </section>

      <section class="provider-admin-section provider-admin-note" aria-labelledby="provider-policy-title">
        <div class="section-heading"><div><p class="section-kicker">SELECTION POLICY</p><h2 id="provider-policy-title">生效边界</h2></div></div>
        <p>Embedding 的维度、tokenizer 上限和 Sparse 模式属于索引契约，不能在已有进程中静默热切换。切换 Embedding 或 Sparse 后请重启 Mac backend，再在上方执行安全重建；Vision 切换也需要重启，但不改变向量 revision。系统先写入新 revision，数据库切换成功后才清理旧 revision，失败时保留旧索引。</p>
        <p v-if="pendingRestart">当前运行中仍是 <code>{{ catalog.selection.embedding_model }}</code> / <code>{{ catalog.selection.reranker_model }}</code>；上方单选框显示的是重启后待生效配置。</p>
        <p>远程 Embedding/Reranker 的 endpoint 与密钥只由 backend 环境变量管理；前端仅显示是否已配置，不回显密钥。`BAAI/bge-m3` 为 1024 维，切换会生成隔离的新索引 revision。</p>
        <p v-if="currentLlm.length">当前 LLM：<strong>{{ currentLlm.map((provider) => `${provider.name} · ${provider.version}`).join(' / ') }}</strong>。LLM endpoint 与密钥同样不在前端回显或编辑。</p>
        <p v-else>当前没有注册 LLM Provider；endpoint、模型与密钥由本机环境变量管理。</p>
      </section>
    </template>
  </section>
</template>
