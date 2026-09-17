import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import App from '../src/App.vue'
import type { SessionProfile } from '../src/api/client'
import { authApi, configureApiSession } from '../src/api/client'
import { createAppRouter } from '../src/router'
import { useAuthStore } from '../src/stores/auth'

vi.mock('../src/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/api/client')>()
  return {
    ...original,
    configureApiSession: vi.fn(),
    authApi: {
      me: vi.fn(),
      login: vi.fn(),
      logout: vi.fn(),
    },
  }
})

vi.mock('../src/api/workspace', () => ({
  workspaceApi: {
    listCollections: vi.fn().mockResolvedValue([]),
    listDocuments: vi.fn().mockResolvedValue({ items: [] }),
  },
}))

vi.mock('../src/api/mcp', () => ({
  mcpApi: { load: vi.fn().mockResolvedValue({ server_name: 'test', server_version: '0.1.0', tools: [], resources: [], transports: [] }) },
}))

const anonymous: SessionProfile = {
  actor_type: 'anonymous',
  role: 'demo_operator',
  tenant: { id: '01900000-0000-7000-8000-00000000d001', slug: 'demo' },
  permissions: ['documents:manage'],
  csrf_token: 'anonymous-csrf',
  expires_at: '2026-09-13T10:00:00Z',
  email: null,
}

const administrator: SessionProfile = {
  actor_type: 'user',
  role: 'super_admin',
  tenant: { id: '01900000-0000-7000-8000-00000000a001', slug: 'system-admin' },
  permissions: ['system:read'],
  csrf_token: 'admin-csrf',
  expires_at: '2026-09-13T10:00:00Z',
  email: 'admin@example.test',
}

async function mountAt(path: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createAppRouter(pinia, createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const wrapper = mount(App, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, router, auth: useAuthStore(pinia) }
}

describe('application shell and authorization', () => {
  beforeEach(() => {
    vi.stubGlobal('scrollTo', vi.fn())
    vi.mocked(authApi.me).mockReset().mockResolvedValue(anonymous)
    vi.mocked(authApi.login).mockReset().mockResolvedValue(administrator)
    vi.mocked(authApi.logout).mockReset().mockResolvedValue()
  })

  afterEach(() => vi.unstubAllGlobals())

  it('automatically creates an anonymous session and permits workspace routes', async () => {
    const { wrapper, router, auth } = await mountAt('/workspace/documents')

    expect(router.currentRoute.value.fullPath).toBe('/workspace/documents')
    expect(auth.isAnonymous).toBe(true)
    expect(wrapper.get('.sidebar__footer').text()).toContain('演示工作区 · 匿名访客')
    expect(wrapper.get('h1').text()).toBe('文档与切分')
    expect(wrapper.text()).toContain('模型状态与选配')
    expect(wrapper.text()).toContain('RAG 运行概览')
  })

  it('permits anonymous users to open the MCP capability catalog', async () => {
    const { wrapper, router } = await mountAt('/workspace/mcp')

    expect(router.currentRoute.value.fullPath).toBe('/workspace/mcp')
    expect(wrapper.get('h1').text()).toBe('MCP 能力目录')
  })

  it('redirects an anonymous identity away from system routes', async () => {
    const { wrapper, router } = await mountAt('/admin/providers')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/admin/providers')
    expect(wrapper.get('[data-testid="login-form"]')).toBeTruthy()
  })

  it('logs in and returns to the originally requested system route', async () => {
    const { wrapper, router, auth } = await mountAt('/admin/providers')
    await wrapper.get('input[name="email"]').setValue('admin@example.test')
    await wrapper.get('input[name="password"]').setValue('correct-password')
    await wrapper.get('[data-testid="login-form"]').trigger('submit')
    await flushPromises()

    expect(auth.isSystemAdmin).toBe(true)
    expect(authApi.login).toHaveBeenCalledWith({
      email: 'admin@example.test',
      password: 'correct-password',
    })
    expect(router.currentRoute.value.fullPath).toBe('/admin/providers')
    expect(wrapper.get('.topbar__eyebrow').text()).toBe('系统管理')
    expect(wrapper.get('.topbar strong').text()).toBe('模型选配与索引')
  })

  it('clears the identity and redirects to login after a protected 401', async () => {
    const { router, auth } = await mountAt('/workspace/overview')
    const latest = vi.mocked(configureApiSession).mock.calls.at(-1)?.[0]

    latest?.onUnauthorized?.()
    await flushPromises()

    expect(auth.profile).toBeNull()
    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/workspace/overview')
  })
})
