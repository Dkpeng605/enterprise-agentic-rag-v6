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
import LoginView from './views/LoginView.vue'
import PlaceholderView from './views/PlaceholderView.vue'

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
    component: PlaceholderView,
    props: { eyebrow: 'PUBLIC RAG', title: '知识问答', description: '公开问答将在 M7-02 接入 SSE、引用与限流反馈。' },
    meta: { access: 'workspace', title: '知识问答' },
  },
  {
    path: '/workspace/overview',
    name: 'overview',
    component: PlaceholderView,
    props: { eyebrow: 'WORKSPACE', title: '租户总览', description: 'Provider 与系统运行指标将在 M7-03 接入。' },
    meta: { access: 'workspace', title: '租户总览' },
  },
  {
    path: '/workspace/documents',
    name: 'documents',
    component: PlaceholderView,
    props: { eyebrow: 'KNOWLEDGE', title: '文档管理', description: '集合与文档生命周期将在 M7-04 接入。' },
    meta: { access: 'workspace', title: '文档管理' },
  },
  {
    path: '/workspace/ingestion',
    name: 'ingestion',
    component: PlaceholderView,
    props: { eyebrow: 'PIPELINE', title: '摄取任务', description: '上传与异步任务进度将在 M7-04 接入。' },
    meta: { access: 'workspace', title: '摄取任务' },
  },
  {
    path: '/workspace/traces/queries',
    name: 'query-traces',
    component: PlaceholderView,
    props: { eyebrow: 'OBSERVABILITY', title: 'Query Trace', description: '检索瀑布图与排名变化将在 M7-05 接入。' },
    meta: { access: 'workspace', title: 'Query Trace' },
  },
  {
    path: '/workspace/traces/ingestion',
    name: 'ingestion-traces',
    component: PlaceholderView,
    props: { eyebrow: 'OBSERVABILITY', title: 'Ingestion Trace', description: '阶段、批次与错误视图将在 M7-06 接入。' },
    meta: { access: 'workspace', title: 'Ingestion Trace' },
  },
  {
    path: '/workspace/evaluations',
    name: 'evaluations',
    component: PlaceholderView,
    props: { eyebrow: 'EDD', title: '评测中心', description: '预算、运行、历史与比较将在 M7-07 接入。' },
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
