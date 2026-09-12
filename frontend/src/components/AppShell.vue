<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const menuOpen = ref(false)

const primaryNavigation = [
  { to: '/chat', label: '知识问答', mark: '问' },
  { to: '/workspace/overview', label: '租户总览', mark: '览' },
  { to: '/workspace/documents', label: '文档管理', mark: '档' },
  { to: '/workspace/ingestion', label: '摄取任务', mark: '取' },
  { to: '/workspace/traces/queries', label: 'Query Trace', mark: 'Q' },
  { to: '/workspace/traces/ingestion', label: 'Ingestion Trace', mark: 'I' },
  { to: '/workspace/evaluations', label: '评测中心', mark: '评' },
]

const systemNavigation = [
  { to: '/admin/providers', label: 'Provider 管理' },
  { to: '/admin/tenants', label: '租户管理' },
  { to: '/admin/users', label: '用户与角色' },
  { to: '/admin/audit', label: '审计日志' },
]

const identityLabel = computed(() =>
  auth.isSystemAdmin ? auth.profile?.email : `${auth.profile?.tenant.slug ?? 'demo'} · 匿名演示`,
)
const sessionKind = computed(() => {
  if (!auth.profile) return '正在建立会话'
  return auth.isAnonymous ? '匿名全功能' : '管理员'
})
const consoleKind = computed(() => {
  if (!auth.profile) return 'SESSION BOOTSTRAP'
  return auth.isAnonymous ? 'DEMO TENANT' : 'SYSTEM CONSOLE'
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
        <p class="nav-caption">工作区</p>
        <RouterLink v-for="item in primaryNavigation" :key="item.to" :to="item.to" @click="menuOpen = false">
          <span class="nav-mark">{{ item.mark }}</span>{{ item.label }}
        </RouterLink>
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
          <span class="topbar__eyebrow">{{ consoleKind }}</span>
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
