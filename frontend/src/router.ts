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
import AdminProvidersView from './views/AdminProvidersView.vue'
import LoginView from './views/LoginView.vue'
import McpView from './views/McpView.vue'
import ModelStatusView from './views/ModelStatusView.vue'
import IngestionView from './views/IngestionView.vue'
import IngestionTraceView from './views/IngestionTraceView.vue'
import OverviewView from './views/OverviewView.vue'
import PipelineInspectorView from './views/PipelineInspectorView.vue'
import PlaceholderView from './views/PlaceholderView.vue'
import QueryTraceView from './views/QueryTraceView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    access: 'public' | 'workspace' | 'system'
    section: string
    title: string
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: HomeView, meta: { access: 'public', section: '产品首页', title: '首页' } },
  {
    path: '/chat',
    name: 'chat',
    component: ChatView,
    meta: { access: 'workspace', section: '应用', title: '知识问答' },
  },
  {
    path: '/workspace/overview',
    name: 'overview',
    component: OverviewView,
    meta: { access: 'workspace', section: '监控与评测', title: 'RAG 运行概览' },
  },
  {
    path: '/workspace/models',
    name: 'model-status',
    component: ModelStatusView,
    meta: { access: 'workspace', section: '模型与扩展', title: '模型状态与选配' },
  },
  {
    path: '/workspace/documents',
    name: 'documents',
    component: DocumentsView,
    meta: { access: 'workspace', section: '知识库', title: '文档与切分' },
  },
  {
    path: '/workspace/documents/:documentId/pipeline',
    name: 'document-pipeline',
    component: PipelineInspectorView,
    meta: { access: 'workspace', section: '知识库', title: '文档解析与切分' },
  },
  {
    path: '/workspace/ingestion',
    name: 'ingestion',
    component: IngestionView,
    meta: { access: 'workspace', section: '知识库', title: '文档处理任务' },
  },
  {
    path: '/workspace/traces/queries',
    name: 'query-traces',
    component: QueryTraceView,
    meta: { access: 'workspace', section: '监控与评测', title: '问答链路观测' },
  },
  {
    path: '/workspace/traces/ingestion',
    name: 'ingestion-traces',
    component: IngestionTraceView,
    meta: { access: 'workspace', section: '监控与评测', title: '文档处理观测' },
  },
  {
    path: '/workspace/evaluations',
    name: 'evaluations',
    component: EvaluationView,
    meta: { access: 'workspace', section: '监控与评测', title: 'RAG 效果评测' },
  },
  {
    path: '/workspace/mcp',
    name: 'mcp',
    component: McpView,
    meta: { access: 'workspace', section: '模型与扩展', title: 'MCP 能力目录' },
  },
  { path: '/login', name: 'login', component: LoginView, meta: { access: 'public', section: '系统管理', title: '管理员登录' } },
  {
    path: '/admin/providers',
    name: 'admin-providers',
    component: AdminProvidersView,
    meta: { access: 'system', section: '系统管理', title: '模型选配与索引' },
  },
  ...[
    ['/admin/tenants', 'admin-tenants', '工作区管理'],
    ['/admin/users', 'admin-users', '用户与权限'],
    ['/admin/audit', 'admin-audit', '操作审计'],
  ].map(([path, name, title]) => ({
    path,
    name,
    component: PlaceholderView,
    props: { eyebrow: 'SYSTEM ADMIN', title, description: '该系统管理模块将在 M9 按独立 EDD Slice 实现。' },
    meta: { access: 'system' as const, section: '系统管理', title },
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
