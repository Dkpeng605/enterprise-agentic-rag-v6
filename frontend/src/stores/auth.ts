import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError, authApi, configureApiSession, type LoginInput, type SessionProfile } from '../api/client'

export type AuthStatus = 'idle' | 'loading' | 'ready' | 'error'

export const useAuthStore = defineStore('auth', () => {
  const profile = ref<SessionProfile | null>(null)
  const status = ref<AuthStatus>('idle')
  const error = ref<ApiError | null>(null)
  const lastRequestId = ref<string>()
  let unauthorizedHandler: (() => void) | undefined

  const isAnonymous = computed(() => profile.value?.actor_type === 'anonymous')
  const isSystemAdmin = computed(
    () =>
      profile.value?.actor_type === 'user' &&
      ['super_admin', 'system_admin'].includes(profile.value.role),
  )

  function syncSession(next: SessionProfile | null): void {
    profile.value = next
    configureApiSession({
      csrfToken: next?.csrf_token,
      onRequestId: (requestId) => {
        lastRequestId.value = requestId
      },
      onUnauthorized: handleUnauthorized,
    })
  }

  function clear(): void {
    profile.value = null
    status.value = 'idle'
    configureApiSession({
      onRequestId: (requestId) => {
        lastRequestId.value = requestId
      },
      onUnauthorized: handleUnauthorized,
    })
  }

  function handleUnauthorized(): void {
    clear()
    unauthorizedHandler?.()
  }

  function onUnauthorized(handler: () => void): void {
    unauthorizedHandler = handler
  }

  async function bootstrap(): Promise<void> {
    if (status.value === 'loading' || (status.value === 'ready' && profile.value)) return
    status.value = 'loading'
    error.value = null
    try {
      syncSession(await authApi.me())
      status.value = 'ready'
    } catch (caught) {
      error.value = caught instanceof ApiError ? caught : new ApiError('会话初始化失败。', 0, 'UNKNOWN')
      status.value = 'error'
      throw caught
    }
  }

  async function login(input: LoginInput): Promise<void> {
    status.value = 'loading'
    error.value = null
    try {
      syncSession(await authApi.login(input))
      status.value = 'ready'
    } catch (caught) {
      error.value = caught instanceof ApiError ? caught : new ApiError('登录失败。', 0, 'UNKNOWN')
      status.value = profile.value ? 'ready' : 'error'
      throw caught
    }
  }

  async function logout(): Promise<void> {
    await authApi.logout()
    clear()
    await bootstrap()
  }

  return {
    profile,
    status,
    error,
    lastRequestId,
    isAnonymous,
    isSystemAdmin,
    bootstrap,
    login,
    logout,
    clear,
    onUnauthorized,
  }
})
