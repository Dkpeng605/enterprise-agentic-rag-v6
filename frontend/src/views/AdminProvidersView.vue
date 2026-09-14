<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  providerApi,
  type ProviderCatalog,
  type ProviderDiagnostic,
  type ProviderKind,
  type ProviderOption,
} from '../api/providers'

type ViewState = 'loading' | 'ready' | 'error'

const kindLabels: Record<string, string> = {
  embedding: 'Embedding',
  reranker: 'Reranker',
  llm: 'LLM',
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

const selectableKinds = ['embedding', 'reranker'] as const
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
  const current = catalog.value?.selection[`${kind}_model`]
  return typeof current === 'string' ? current : ''
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
    catalog.value = await providerApi.load()
    state.value = 'ready'
  } catch (caught) {
    state.value = 'error'
    errorMessage.value = caught instanceof ApiError ? caught.message : 'Provider 目录暂时无法载入。'
  }
}

async function selectProvider(kind: ProviderKind, key: string): Promise<void> {
  if (savingKey.value || optionValue(kind) === key) return
  savingKey.value = `${kind}:${key}`
  actionMessage.value = ''
  actionError.value = ''
  try {
    catalog.value = await providerApi.select(kind, key)
    actionMessage.value = '选择已保存。重启 Mac backend 后生效。Embedding 变更后还需要重新摄取文档。'
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
        <p>查看当前运行时真实注册的 Provider，并选择下一次启动要使用的本地 Embedding 与 Reranker profile。</p>
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
            <label v-for="option in group.options" :key="option.key" class="provider-option" :class="{ 'provider-option--selected': option.selected }">
              <input type="radio" :name="group.kind" :value="option.key" :checked="option.selected" :disabled="Boolean(savingKey)" @change="selectProvider(group.kind, option.key)">
              <span class="provider-option__body"><strong>{{ option.label }}</strong><b>{{ option.model }}</b><small>{{ formatOptionMeta(option) }}<template v-if="option.note"> · {{ option.note }}</template></small></span>
              <em v-if="option.selected">当前</em><em v-else-if="savingKey === `${group.kind}:${option.key}`">保存中</em>
            </label>
          </article>
        </div>
      </section>

      <section class="provider-admin-section provider-admin-note" aria-labelledby="provider-policy-title">
        <div class="section-heading"><div><p class="section-kicker">SELECTION POLICY</p><h2 id="provider-policy-title">生效边界</h2></div></div>
        <p>Embedding 的维度和 tokenizer 上限属于索引契约，不能在已有进程中静默热切换。切换 Embedding 后请重启 Mac backend，并重新摄取需要检索的文档；旧索引不会被自动伪装成新模型的结果。</p>
        <p v-if="currentLlm.length">当前 LLM：<strong>{{ currentLlm.map((provider) => `${provider.name} · ${provider.version}`).join(' / ') }}</strong>。LLM endpoint 与密钥仍由本机环境变量管理，不在前端回显或编辑。</p>
        <p v-else>当前没有注册 LLM Provider；endpoint、模型与密钥由本机环境变量管理。</p>
      </section>
    </template>
  </section>
</template>
