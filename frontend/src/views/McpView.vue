<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import { mcpApi, type McpCapabilityCatalog, type McpTransport } from '../api/mcp'

type ViewState = 'loading' | 'ready' | 'error'

const state = ref<ViewState>('loading')
const catalog = ref<McpCapabilityCatalog>()
const errorMessage = ref('')
const requestId = ref('')

const mountedTransportCount = computed(
  () => catalog.value?.transports.filter((item) => item.status === 'mounted').length ?? 0,
)

function transportLabel(transport: McpTransport): string {
  return {
    factory_declared: '已声明',
    requires_factory: '需要组合函数',
    mounted: '当前已挂载',
    external_composition_required: '需要外部组合',
  }[transport.status]
}

function transportClass(transport: McpTransport): string {
  return transport.status === 'mounted' ? 'mcp-transport--ready' : 'mcp-transport--pending'
}

function resourceKindLabel(kind: string): string {
  return kind === 'resource' ? '固定资源' : '资源模板'
}

async function load(): Promise<void> {
  state.value = 'loading'
  errorMessage.value = ''
  requestId.value = ''
  try {
    catalog.value = await mcpApi.load()
    state.value = 'ready'
  } catch (caught) {
    state.value = 'error'
    errorMessage.value = caught instanceof ApiError ? caught.message : '无法载入 MCP 能力目录，请稍后重试。'
    requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
  }
}

onMounted(load)
</script>

<template>
  <section class="mcp-page">
    <header class="mcp-heading">
      <div>
        <p class="section-kicker">MCP · CAPABILITY CATALOG</p>
        <h1>MCP 生态</h1>
        <p>展示当前服务端 MCP 注册定义、授权边界和传输装配状态。这里不显示 Token、Prompt 或文档正文。</p>
      </div>
      <div v-if="state === 'ready' && catalog" class="mcp-identity">
        <span class="status-dot"></span>
        <div><strong>{{ catalog.server_name }}</strong><small>协议服务 {{ catalog.server_version }} · {{ mountedTransportCount }} 个当前端点已挂载</small></div>
      </div>
    </header>

    <div v-if="state === 'loading'" class="mcp-state" aria-busy="true" aria-label="正在载入 MCP 能力目录">
      <i v-for="index in 8" :key="index"></i>
    </div>

    <div v-else-if="state === 'error'" class="mcp-error" role="alert">
      <p class="section-kicker">MCP CATALOG UNAVAILABLE</p>
      <h2>能力目录暂时无法载入</h2>
      <p>{{ errorMessage }}</p>
      <code v-if="requestId">Request ID · {{ requestId }}</code>
      <button class="button button--primary" type="button" @click="load">重新载入 <b>↗</b></button>
    </div>

    <template v-else-if="catalog">
      <section class="mcp-summary" aria-label="MCP 统计">
        <article><small>Tools</small><strong>{{ catalog.tools.length }}</strong><p>全部为只读能力</p></article>
        <article><small>Resources</small><strong>{{ catalog.resources.length }}</strong><p>固定资源与模板</p></article>
        <article><small>授权 Scopes</small><strong>{{ new Set(catalog.tools.flatMap((item) => item.required_scopes)).size }}</strong><p>由令牌服务端约束</p></article>
        <article><small>传输</small><strong>{{ catalog.transports.length }}</strong><p>{{ mountedTransportCount }} 个当前端点已挂载</p></article>
      </section>

      <section class="mcp-section" aria-labelledby="mcp-transports-title">
        <div class="section-heading"><div><p class="section-kicker">TRANSPORTS</p><h2 id="mcp-transports-title">传输方式</h2></div><span>状态来自当前组合根</span></div>
        <div class="mcp-transport-grid">
          <article v-for="transport in catalog.transports" :key="transport.name" class="mcp-transport" :class="transportClass(transport)">
            <header><span>{{ transport.name === 'stdio' ? 'STDIO' : 'STREAMABLE HTTP' }}</span><b>{{ transportLabel(transport) }}</b></header>
            <strong>{{ transport.detail }}</strong>
            <code v-if="transport.endpoint">{{ transport.endpoint }}</code>
            <small v-else>端点由部署组合提供</small>
          </article>
        </div>
      </section>

      <section class="mcp-section" aria-labelledby="mcp-tools-title">
        <div class="section-heading"><div><p class="section-kicker">READ-ONLY TOOLS</p><h2 id="mcp-tools-title">工具目录</h2></div><span>SDK 注册定义</span></div>
        <div class="mcp-tool-list">
          <article v-for="tool in catalog.tools" :key="tool.name" class="mcp-tool">
            <div><strong>{{ tool.name }}</strong><p>{{ tool.description }}</p></div>
            <div class="mcp-scope-list"><span v-for="scope in tool.required_scopes" :key="scope">{{ scope }}</span><b>READ ONLY</b></div>
          </article>
        </div>
      </section>

      <section class="mcp-section" aria-labelledby="mcp-resources-title">
        <div class="section-heading"><div><p class="section-kicker">RESOURCES</p><h2 id="mcp-resources-title">资源目录</h2></div><span>授权后按 Scope 与集合范围过滤</span></div>
        <div class="mcp-resource-list">
          <article v-for="resource in catalog.resources" :key="resource.uri" class="mcp-resource">
            <div><strong>{{ resource.uri }}</strong><p>{{ resource.description }}</p></div>
            <span>{{ resourceKindLabel(resource.kind) }}</span>
          </article>
        </div>
      </section>
    </template>
  </section>
</template>
