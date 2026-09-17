<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import { overviewApi, type HealthReport, type ProviderDiagnostic } from '../api/overview'
import { useAuthStore } from '../stores/auth'

type ViewState = 'loading' | 'ready' | 'error'

const auth = useAuthStore()
const state = ref<ViewState>('loading')
const health = ref<HealthReport>()
const errorMessage = ref('')
const requestId = ref('')

const modelProviders = computed(() => (health.value?.providers ?? []).filter((provider) =>
  ['llm', 'embedding', 'reranker', 'vision', 'sparse'].includes(provider.kind),
))
const healthyCount = computed(() => modelProviders.value.filter((provider) => provider.health === 'healthy').length)
const remoteCount = computed(() => modelProviders.value.filter((provider) => provider.is_remote).length)

function kindLabel(kind: string): string {
  return {
    llm: '生成模型',
    embedding: '向量模型',
    reranker: '重排模型',
    vision: '视觉模型',
    sparse: '关键词检索',
  }[kind] ?? kind
}

function healthLabel(status: string): string {
  return {
    healthy: '健康',
    degraded: '降级',
    unavailable: '不可用',
    unknown: '待首次探测',
  }[status] ?? status
}

function providerClass(provider: ProviderDiagnostic): string {
  return `model-status-card--${provider.health}`
}

async function load(): Promise<void> {
  state.value = 'loading'
  errorMessage.value = ''
  requestId.value = ''
  try {
    const snapshot = await overviewApi.load()
    health.value = snapshot.health
    state.value = 'ready'
  } catch (caught) {
    state.value = 'error'
    errorMessage.value = caught instanceof ApiError ? caught.message : '模型运行状态暂时无法载入。'
    requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
  }
}

onMounted(load)
</script>

<template>
  <section class="model-status-page">
    <header class="workspace-heading">
      <div>
        <p class="section-kicker">MODEL SERVICES · STATUS &amp; SELECTION</p>
        <h1>模型状态与选配</h1>
        <p>查看知识问答当前使用的生成、向量、重排、视觉与关键词检索能力；模型密钥和连接地址不会下发到浏览器。</p>
      </div>
      <button class="button button--secondary" type="button" :disabled="state === 'loading'" @click="load">刷新状态</button>
    </header>

    <div v-if="state === 'loading'" class="model-status-loading" aria-busy="true">正在读取模型服务状态…</div>
    <div v-else-if="state === 'error'" class="workspace-alert workspace-alert--error" role="alert">
      <strong>{{ errorMessage }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="load">重试</button>
    </div>

    <template v-else-if="health">
      <section class="model-status-summary" aria-label="模型服务摘要">
        <div><span>运行状态</span><strong>{{ health.ready ? '服务已就绪' : '服务未就绪' }}</strong></div>
        <div><span>模型能力</span><strong>{{ modelProviders.length }}</strong></div>
        <div><span>健康实例</span><strong>{{ healthyCount }} / {{ modelProviders.length }}</strong></div>
        <div><span>远程 API</span><strong>{{ remoteCount }}</strong></div>
      </section>

      <section class="model-status-section" aria-labelledby="active-models-title">
        <div class="section-heading"><div><p class="section-kicker">ACTIVE MODEL SERVICES</p><h2 id="active-models-title">当前运行中的模型</h2></div><span>状态来自服务端实时诊断</span></div>
        <div v-if="modelProviders.length" class="model-status-grid">
          <article v-for="provider in modelProviders" :key="`${provider.kind}:${provider.name}`" class="model-status-card" :class="providerClass(provider)">
            <header><span>{{ kindLabel(provider.kind) }}</span><i></i></header>
            <h3>{{ provider.name }}</h3>
            <p>{{ provider.version }}</p>
            <small>{{ provider.capabilities.join(' · ') || '未声明额外能力' }}</small>
            <footer><strong>{{ healthLabel(provider.health) }}</strong><span>{{ provider.is_remote ? '远程 API' : '本地运行' }}</span></footer>
          </article>
        </div>
        <div v-else class="model-status-empty">当前运行时没有注册可展示的模型服务。</div>
      </section>

      <section class="model-selection-boundary" aria-labelledby="selection-boundary-title">
        <div><p class="section-kicker">SELECTION BOUNDARY</p><h2 id="selection-boundary-title">模型选配为什么需要管理员权限</h2></div>
        <p>生成与重排模型影响所有查询；向量模型还决定索引维度、分词上限和已有文档兼容性。切换后可能需要重启服务并安全重建索引，因此演示访客可以查看状态，但不能修改共享运行时。</p>
        <RouterLink v-if="auth.isSystemAdmin" class="button button--primary" to="/admin/providers">进入模型选配与索引 <span>↗</span></RouterLink>
        <RouterLink v-else class="button button--secondary" :to="{ path: '/login', query: { redirect: '/admin/providers' } }">管理员登录后选配</RouterLink>
      </section>
    </template>
  </section>
</template>
