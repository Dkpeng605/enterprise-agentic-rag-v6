<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import { workspaceApi, type Job, type JobListItem, type JobStatus } from '../api/workspace'

const activeStatuses = new Set<string>(['queued', 'leased', 'running', 'retry_wait'])
const jobs = ref<JobListItem[]>([])
const nextCursor = ref<string>()
const statusFilter = ref<JobStatus | ''>('')
const selected = ref<Job | JobListItem>()
const loading = ref(true)
const loadingMore = ref(false)
const refreshing = ref(false)
const error = ref('')
const requestId = ref('')
const requestedJobId = new URLSearchParams(globalThis.location?.search ?? '').get('job')
let refreshTimer: ReturnType<typeof setInterval> | undefined

const hasActiveJobs = computed(() => jobs.value.some((job) => activeStatuses.has(job.status)))
const counts = computed(() => ({
  active: jobs.value.filter((job) => activeStatuses.has(job.status)).length,
  succeeded: jobs.value.filter((job) => job.status === 'succeeded').length,
  failed: jobs.value.filter((job) => ['failed', 'cancelled'].includes(job.status)).length,
}))

function setError(caught: unknown): void {
  error.value = caught instanceof ApiError ? caught.message : '无法载入摄取任务。'
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

async function loadJobs(append = false, quiet = false): Promise<void> {
  if (quiet) refreshing.value = true
  else append ? (loadingMore.value = true) : (loading.value = true)
  if (!quiet) {
    error.value = ''
    requestId.value = ''
  }
  try {
    const page = await workspaceApi.listJobs({
      status: statusFilter.value || undefined,
      cursor: append ? nextCursor.value : undefined,
      limit: 20,
    })
    jobs.value = append ? [...jobs.value, ...page.items] : page.items
    nextCursor.value = page.nextCursor
    if (selected.value) selected.value = jobs.value.find((job) => job.id === selected.value?.id) ?? selected.value
  } catch (caught) {
    if (!quiet) setError(caught)
  } finally {
    loading.value = false
    loadingMore.value = false
    refreshing.value = false
  }
}

async function selectRequestedJob(): Promise<void> {
  if (!requestedJobId) return
  const listed = jobs.value.find((job) => job.id === requestedJobId)
  if (listed) {
    selected.value = listed
    return
  }
  try {
    selected.value = await workspaceApi.getJob(requestedJobId)
  } catch (caught) {
    setError(caught)
  }
}

async function initialLoad(): Promise<void> {
  await loadJobs()
  await selectRequestedJob()
}

function statusLabel(status: string): string {
  return {
    queued: '等待 Worker', leased: '已取得租约', running: '正在执行', retry_wait: '等待重试',
    succeeded: '已完成', failed: '失败', cancelled: '已取消',
  }[status] ?? status
}

function stageLabel(job: Job | JobListItem): string {
  if (job.stage) return job.stage
  if (job.type === 'delete') return '等待清理文档'
  return job.status === 'queued' ? '等待摄取调度' : job.type
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value))
}

onMounted(async () => {
  await initialLoad()
  refreshTimer = setInterval(() => {
    if (hasActiveJobs.value && !refreshing.value) void loadJobs(false, true)
  }, 5_000)
})
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <section class="ingestion-page">
    <header class="workspace-heading">
      <div><p class="section-kicker">PIPELINE CONTROL</p><h1>摄取任务</h1><p>任务状态来自 PostgreSQL 租约状态机；页面只轮询活跃任务，不伪造进度或自动重启失败任务。</p></div>
      <div class="ingestion-summary"><div><span>ACTIVE</span><strong>{{ counts.active }}</strong></div><div><span>SUCCEEDED</span><strong>{{ counts.succeeded }}</strong></div><div><span>FAILED</span><strong>{{ counts.failed }}</strong></div></div>
    </header>

    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert"><strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="initialLoad">重试</button></div>

    <div class="ingestion-toolbar"><label>状态<select v-model="statusFilter" @change="loadJobs()"><option value="">全部任务</option><option value="queued">Queued</option><option value="leased">Leased</option><option value="running">Running</option><option value="retry_wait">Retry wait</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label><span v-if="hasActiveJobs"><i></i>{{ refreshing ? '正在同步状态' : '每 5 秒同步活跃任务' }}</span><button type="button" @click="loadJobs()">立即刷新</button></div>

    <div v-if="loading" class="job-skeleton" aria-busy="true" aria-label="正在载入摄取任务"><i v-for="index in 4" :key="index"></i></div>
    <div v-else-if="!jobs.length" class="jobs-empty"><span>0</span><h2>当前没有摄取任务</h2><p>上传文档或删除文档后，任务会出现在这里。</p><RouterLink class="button button--primary" to="/workspace/documents">前往文档管理</RouterLink></div>
    <div v-else class="jobs-layout">
      <div class="job-list" data-testid="job-list">
        <button v-for="job in jobs" :key="job.id" type="button" :class="{ active: selected?.id === job.id }" @click="selected = job">
          <span class="job-orb" :class="`job-orb--${job.status}`">{{ job.type === 'delete' ? 'DL' : 'IN' }}</span>
          <span><strong>{{ stageLabel(job) }}</strong><small>{{ job.id.slice(0, 12) }}… · {{ dateLabel(job.created_at) }}</small></span>
          <i>{{ job.progress }}%</i><em>{{ statusLabel(job.status) }}</em>
        </button>
        <button v-if="nextCursor" class="load-more" type="button" :disabled="loadingMore" @click="loadJobs(true)">{{ loadingMore ? '载入中…' : '载入更多任务' }}</button>
      </div>

      <aside class="job-detail" data-testid="selected-job">
        <template v-if="selected">
          <p class="section-kicker">JOB INSPECTOR</p><h2>{{ stageLabel(selected) }}</h2><span class="state-chip" :class="`state-chip--${selected.status}`">{{ statusLabel(selected.status) }}</span>
          <div class="job-progress"><div><i :style="{ width: `${selected.progress}%` }"></i></div><strong>{{ selected.progress }}%</strong></div>
          <dl><div><dt>Job ID</dt><dd>{{ selected.id }}</dd></div><div><dt>Document ID</dt><dd>{{ selected.document_id }}</dd></div><div><dt>Version ID</dt><dd>{{ selected.version_id }}</dd></div><div><dt>尝试次数</dt><dd>{{ selected.attempts }} / {{ selected.max_attempts }}</dd></div><div><dt>可执行时间</dt><dd>{{ dateLabel(selected.available_at) }}</dd></div><div><dt>最近心跳</dt><dd>{{ selected.heartbeat_at ? dateLabel(selected.heartbeat_at) : '尚无' }}</dd></div></dl>
          <div v-if="selected.error_code" class="job-error" role="alert"><span>STABLE ERROR</span><strong>{{ selected.error_code }}</strong><p>{{ selected.error_message || '任务失败，未提供可公开的错误详情。' }}</p></div>
          <p v-if="selected.cancel_requested" class="cancel-note">已请求取消，Worker 会在下一个安全检查点停止。</p>
        </template>
        <div v-else class="job-detail-empty"><span>↙</span><p>选择一项任务查看阶段、租约进度与稳定错误。</p></div>
      </aside>
    </div>
  </section>
</template>
