<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const form = reactive({ email: '', password: '' })
const submitting = ref(false)
const message = ref('')

async function submit(): Promise<void> {
  submitting.value = true
  message.value = ''
  try {
    await auth.login(form)
    const requested = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    const destination = requested.startsWith('/') && !requested.startsWith('//') ? requested : '/admin/providers'
    await router.replace(destination)
  } catch (caught) {
    const error = caught instanceof ApiError ? caught : null
    message.value = error?.status === 401 ? '邮箱或密码不正确。' : '暂时无法登录，请稍后重试。'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="login-layout">
    <div class="login-context">
      <p class="section-kicker">SYSTEM BOUNDARY</p>
      <h1>系统管理<br /><em>独立授权。</em></h1>
      <p>匿名工作区拥有 Demo Tenant 的业务权限，但 Provider、租户、用户与审计数据始终位于独立系统边界之后。</p>
      <RouterLink to="/workspace/overview">继续使用匿名工作区 →</RouterLink>
    </div>
    <form class="login-card" data-testid="login-form" @submit.prevent="submit">
      <span class="login-card__mark">A</span>
      <h2>管理员登录</h2>
      <p>使用部署时配置的 bootstrap 管理员凭据。</p>
      <label>邮箱<input v-model.trim="form.email" name="email" type="email" autocomplete="username" required /></label>
      <label>密码<input v-model="form.password" name="password" type="password" autocomplete="current-password" required /></label>
      <p v-if="message" class="form-error" role="alert">{{ message }}</p>
      <button class="button button--primary login-submit" type="submit" :disabled="submitting">
        {{ submitting ? '正在验证…' : '安全登录' }}
      </button>
      <small>凭据仅发送至同源 FastAPI；会话使用 HttpOnly Cookie，写操作使用 CSRF Token。</small>
    </form>
  </section>
</template>
