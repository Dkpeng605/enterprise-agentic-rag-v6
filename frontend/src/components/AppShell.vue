<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const menuOpen = ref(false)

const navigationGroups = [
  {
    label: '应用',
    items: [{ to: '/chat', label: '知识问答', mark: '问' }],
  },
  {
    label: '知识库',
    items: [
      { to: '/workspace/documents', label: '文档与切分', mark: '档' },
      { to: '/workspace/ingestion', label: '文档处理任务', mark: '任' },
    ],
  },
  {
    label: '模型与扩展',
    items: [
      { to: '/workspace/models', label: '模型状态与选配', mark: '模' },
      { to: '/workspace/mcp', label: 'MCP 能力目录', mark: 'M' },
    ],
  },
  {
    label: '监控与评测',
    items: [
      { to: '/workspace/overview', label: 'RAG 运行概览', mark: '览' },
      { to: '/workspace/traces/queries', label: '问答链路观测', mark: '问' },
      { to: '/workspace/traces/ingestion', label: '文档处理观测', mark: '链' },
      { to: '/workspace/evaluations', label: 'RAG 效果评测', mark: '评' },
    ],
  },
]

const systemNavigation = [
  { to: '/admin/providers', label: '模型选配与索引' },
  { to: '/admin/tenants', label: '工作区管理' },
  { to: '/admin/users', label: '用户与权限' },
  { to: '/admin/audit', label: '操作审计' },
]

const identityLabel = computed(() =>
  auth.isSystemAdmin ? auth.profile?.email : '演示工作区 · 匿名访客',
)
const sessionKind = computed(() => {
  if (!auth.profile) return '正在建立会话'
  return auth.isAnonymous ? '演示模式' : '管理员'
})
</script>

<template>
  <div class="app-frame">
    <button class="mobile-menu" type="button" aria-label="切换导航" @click="menuOpen = !menuOpen">☰</button>
    <aside class="sidebar" :class="{ 'sidebar--open': menuOpen }">
      <RouterLink class="brand" to="/" @click="menuOpen = false">
        <span class="brand__signal"><i></i><i></i><i></i></span>
        <span><strong>ATLAS</strong><small>Enterprise RAG</small></span>
      </RouterLink>

      <nav aria-label="工作区导航">
        <section v-for="group in navigationGroups" :key="group.label" class="nav-group">
          <p class="nav-caption">{{ group.label }}</p>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" @click="menuOpen = false">
            <span class="nav-mark">{{ item.mark }}</span>{{ item.label }}
          </RouterLink>
        </section>
        <template v-if="auth.isSystemAdmin">
          <p class="nav-caption nav-caption--system">系统管理</p>
          <RouterLink v-for="item in systemNavigation" :key="item.to" :to="item.to" @click="menuOpen = false">
            <span class="nav-mark">S</span>{{ item.label }}
          </RouterLink>
        </template>
      </nav>

      <div class="sidebar__footer">
        <span class="status-dot"></span>
        <div><strong>{{ identityLabel }}</strong><small>{{ auth.profile?.role ?? '正在建立会话' }}</small></div>
      </div>
    </aside>

    <div v-if="menuOpen" class="mobile-backdrop" @click="menuOpen = false"></div>

    <main class="main-stage">
      <header class="topbar">
        <div>
          <span class="topbar__eyebrow">{{ route.meta.section }}</span>
          <strong>{{ route.meta.title }}</strong>
        </div>
        <div class="topbar__actions">
          <span class="session-badge"><i></i>{{ sessionKind }}</span>
          <RouterLink v-if="!auth.isSystemAdmin" class="text-action" to="/login">管理员登录</RouterLink>
          <button v-else class="text-action" type="button" @click="auth.logout">退出</button>
        </div>
      </header>
      <div class="page-stage"><RouterView /></div>
    </main>
  </div>
</template>
