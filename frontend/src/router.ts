import type { Pinia } from 'pinia'
import {
  createRouter,
  createWebHistory,
  type Router,
  type RouterHistory,
  type RouteRecordRaw,
} from 'vue-router'

import { useAuthStore } from './stores/auth'
import HomeView from './views/HomeView.vue'
import ChatView from './views/ChatView.vue'
import DocumentsView from './views/DocumentsView.vue'
import EvaluationView from './views/EvaluationView.vue'
import LoginView from './views/LoginView.vue'
import IngestionView from './views/IngestionView.vue'
import IngestionTraceView from './views/IngestionTraceView.vue'
import OverviewView from './views/OverviewView.vue'
import PlaceholderView from './views/PlaceholderView.vue'
import QueryTraceView from './views/QueryTraceView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    access: 'public' | 'workspace' | 'system'
    title: string
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: HomeView, meta: { access: 'public', title: '首页' } },
  {
    path: '/chat',
    name: 'chat',
    component: ChatView,
    meta: { access: 'workspace', title: '知识问答' },
  },
  {
    path: '/workspace/overview',
    name: 'overview',
    component: OverviewView,
    meta: { access: 'workspace', title: '租户总览' },
  },
  {
    path: '/workspace/documents',
    name: 'documents',
    component: DocumentsView,
    meta: { access: 'workspace', title: '文档管理' },
  },
  {
    path: '/workspace/ingestion',
    name: 'ingestion',
    component: IngestionView,
    meta: { access: 'workspace', title: '摄取任务' },
  },
  {
    path: '/workspace/traces/queries',
    name: 'query-traces',
    component: QueryTraceView,
    meta: { access: 'workspace', title: 'Query Trace' },
  },
  {
    path: '/workspace/traces/ingestion',
    name: 'ingestion-traces',
    component: IngestionTraceView,
    meta: { access: 'workspace', title: 'Ingestion Trace' },
  },
  {
    path: '/workspace/evaluations',
    name: 'evaluations',
    component: EvaluationView,
    meta: { access: 'workspace', title: '评测中心' },
  },
  { path: '/login', name: 'login', component: LoginView, meta: { access: 'public', title: '管理员登录' } },
  ...[
    ['/admin/providers', 'admin-providers', 'Provider 管理'],
    ['/admin/tenants', 'admin-tenants', '租户管理'],
    ['/admin/users', 'admin-users', '用户与角色'],
    ['/admin/audit', 'admin-audit', '审计日志'],
  ].map(([path, name, title]) => ({
    path,
    name,
    component: PlaceholderView,
    props: { eyebrow: 'SYSTEM ADMIN', title, description: '该系统管理模块将在 M9 按独立 EDD Slice 实现。' },
    meta: { access: 'system' as const, title },
  })),
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export function createAppRouter(pinia: Pinia, history?: RouterHistory): Router {
  const router = createRouter({
    history: history ?? createWebHistory(),
    routes,
    scrollBehavior: () => ({ top: 0 }),
  })
  const auth = useAuthStore(pinia)

  auth.onUnauthorized(() => {
    const current = router.currentRoute.value
    if (current.name !== 'login') {
      void router.replace({ name: 'login', query: { redirect: current.fullPath } })
    }
  })

  router.beforeEach(async (to) => {
    if (to.name === 'login') {
      return auth.isSystemAdmin ? { name: 'admin-providers' } : undefined
    }
    try {
      await auth.bootstrap()
    } catch {
      if (to.meta.access !== 'public') {
        return { name: 'login', query: { redirect: to.fullPath } }
      }
    }
    if (to.meta.access === 'system' && !auth.isSystemAdmin) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
  })

  router.afterEach((to, _from, failure) => {
    if (!failure) document.title = `${to.meta.title} · Enterprise RAG`
  })

  return router
}
