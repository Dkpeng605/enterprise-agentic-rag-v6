<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '../api/client'
import {
  workspaceApi,
  type Collection,
  type DocumentDetail,
  type DocumentStatus,
  type DocumentSummary,
  type UploadResult,
} from '../api/workspace'

type Panel = 'create' | 'edit' | 'upload' | 'collection-delete' | 'document-detail' | 'document-delete'

const collections = ref<Collection[]>([])
const documents = ref<DocumentSummary[]>([])
const nextCursor = ref<string>()
const selectedCollection = ref('')
const statusFilter = ref<DocumentStatus | ''>('')
const keyword = ref('')
const loading = ref(true)
const loadingMore = ref(false)
const saving = ref(false)
const error = ref('')
const requestId = ref('')
const notice = ref('')
const panel = ref<Panel>()
const activeCollection = ref<Collection>()
const activeDocument = ref<DocumentSummary>()
const documentDetail = ref<DocumentDetail>()
const collectionName = ref('')
const collectionDescription = ref('')
const collectionVisibility = ref<'private' | 'tenant' | 'public'>('tenant')
const deleteConfirmation = ref('')
const uploadTitle = ref('')
const uploadOrganization = ref('')
const uploadVisibility = ref<'private' | 'tenant' | 'public'>('tenant')
const uploadFile = ref<File>()
const uploadResult = ref<UploadResult>()

const currentCollection = computed(
  () => collections.value.find((item) => item.id === selectedCollection.value),
)

function setError(caught: unknown, fallback: string): void {
  error.value = caught instanceof ApiError ? caught.message : fallback
  requestId.value = caught instanceof ApiError ? caught.requestId ?? '' : ''
}

function clearFeedback(): void {
  error.value = ''
  requestId.value = ''
  notice.value = ''
}

async function loadCollections(): Promise<void> {
  collections.value = await workspaceApi.listCollections()
}

async function loadDocuments(append = false): Promise<void> {
  append ? (loadingMore.value = true) : (loading.value = true)
  try {
    const page = await workspaceApi.listDocuments({
      collection: selectedCollection.value || undefined,
      status: statusFilter.value || undefined,
      keyword: keyword.value.trim() || undefined,
      cursor: append ? nextCursor.value : undefined,
      limit: 20,
    })
    documents.value = append ? [...documents.value, ...page.items] : page.items
    nextCursor.value = page.nextCursor
  } catch (caught) {
    setError(caught, '无法载入文档列表。')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function initialLoad(): Promise<void> {
  clearFeedback()
  loading.value = true
  try {
    await Promise.all([loadCollections(), loadDocuments()])
  } catch (caught) {
    setError(caught, '无法载入知识库工作区。')
    loading.value = false
  }
}

function openCreate(): void {
  clearFeedback()
  collectionName.value = ''
  collectionDescription.value = ''
  collectionVisibility.value = 'tenant'
  panel.value = 'create'
}

function openEdit(collection: Collection): void {
  clearFeedback()
  activeCollection.value = collection
  collectionName.value = collection.name
  collectionDescription.value = collection.description ?? ''
  collectionVisibility.value = collection.visibility as typeof collectionVisibility.value
  panel.value = 'edit'
}

async function saveCollection(): Promise<void> {
  if (!collectionName.value.trim() || saving.value) return
  saving.value = true
  clearFeedback()
  try {
    if (panel.value === 'edit' && activeCollection.value) {
      await workspaceApi.updateCollection(activeCollection.value.id, {
        name: collectionName.value.trim(),
        description: collectionDescription.value.trim() || null,
        visibility: collectionVisibility.value,
      })
      notice.value = '集合设置已更新。'
    } else {
      const created = await workspaceApi.createCollection({
        name: collectionName.value.trim(),
        description: collectionDescription.value.trim() || null,
        visibility: collectionVisibility.value,
      })
      selectedCollection.value = created.id
      notice.value = '集合已创建，可立即上传文档。'
    }
    panel.value = undefined
    await loadCollections()
  } catch (caught) {
    setError(caught, '集合保存失败。')
  } finally {
    saving.value = false
  }
}

function openCollectionDelete(collection: Collection): void {
  clearFeedback()
  activeCollection.value = collection
  deleteConfirmation.value = ''
  panel.value = 'collection-delete'
}

async function deleteCollection(): Promise<void> {
  const collection = activeCollection.value
  if (!collection || deleteConfirmation.value !== collection.name || saving.value) return
  saving.value = true
  clearFeedback()
  try {
    await workspaceApi.deleteCollection(collection.id, deleteConfirmation.value)
    if (selectedCollection.value === collection.id) selectedCollection.value = ''
    panel.value = undefined
    notice.value = '集合删除已受理；集合内文档将由后台任务安全清理。'
    await Promise.all([loadCollections(), loadDocuments()])
  } catch (caught) {
    setError(caught, '集合删除失败。')
  } finally {
    saving.value = false
  }
}

function openUpload(): void {
  clearFeedback()
  uploadTitle.value = ''
  uploadOrganization.value = ''
  uploadVisibility.value = 'tenant'
  uploadFile.value = undefined
  uploadResult.value = undefined
  panel.value = 'upload'
}

function chooseFile(event: Event): void {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0]
  if (!uploadTitle.value && uploadFile.value) {
    uploadTitle.value = uploadFile.value.name.replace(/\.[^.]+$/, '')
  }
}

async function upload(): Promise<void> {
  if (!uploadFile.value || !uploadTitle.value.trim() || !selectedCollection.value || saving.value) return
  saving.value = true
  clearFeedback()
  try {
    uploadResult.value = await workspaceApi.uploadDocument({
      file: uploadFile.value,
      collectionId: selectedCollection.value,
      title: uploadTitle.value.trim(),
      organization: uploadOrganization.value.trim() || undefined,
      visibility: uploadVisibility.value,
    })
    notice.value = uploadResult.value.deduplicated ? '相同内容已存在，已复用原摄取任务。' : '上传完成，摄取任务已进入队列。'
    await Promise.all([loadCollections(), loadDocuments()])
  } catch (caught) {
    setError(caught, '文档上传失败。')
  } finally {
    saving.value = false
  }
}

async function openDocument(document: DocumentSummary): Promise<void> {
  clearFeedback()
  activeDocument.value = document
  documentDetail.value = undefined
  panel.value = 'document-detail'
  try {
    documentDetail.value = await workspaceApi.getDocument(document.id)
  } catch (caught) {
    setError(caught, '无法载入文档详情。')
  }
}

function openDocumentDelete(document: DocumentSummary): void {
  clearFeedback()
  activeDocument.value = document
  panel.value = 'document-delete'
}

async function deleteDocument(): Promise<void> {
  if (!activeDocument.value || saving.value) return
  saving.value = true
  clearFeedback()
  try {
    const jobId = await workspaceApi.deleteDocument(activeDocument.value.id)
    panel.value = undefined
    notice.value = `删除任务已创建 · ${jobId.slice(0, 12)}…`
    await Promise.all([loadCollections(), loadDocuments()])
  } catch (caught) {
    setError(caught, '文档删除失败。')
  } finally {
    saving.value = false
  }
}

function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
}

onMounted(initialLoad)
</script>

<template>
  <section class="documents-page">
    <header class="workspace-heading">
      <div><p class="section-kicker">KNOWLEDGE OPERATIONS</p><h1>文档管理</h1><p>匿名用户拥有当前 Demo Tenant 的完整业务权限；系统配置与其他租户始终不可见。</p></div>
      <div class="heading-actions"><button class="button button--secondary" type="button" @click="openCreate">新建集合</button><button class="button button--primary" type="button" :disabled="!selectedCollection" @click="openUpload">上传文档 <span>↗</span></button></div>
    </header>

    <div v-if="error" class="workspace-alert workspace-alert--error" role="alert"><strong>{{ error }}</strong><code v-if="requestId">Request ID · {{ requestId }}</code><button type="button" @click="initialLoad">重试</button></div>
    <div v-if="notice" class="workspace-alert workspace-alert--success" role="status">{{ notice }}</div>

    <div class="documents-layout">
      <aside class="collection-panel">
        <div class="panel-caption"><span>COLLECTIONS</span><strong>{{ collections.length }}</strong></div>
        <button class="collection-item" :class="{ active: selectedCollection === '' }" type="button" @click="selectedCollection = ''; loadDocuments()"><span>全部文档</span><small>{{ collections.reduce((sum, item) => sum + item.document_count, 0) }}</small></button>
        <article v-for="collection in collections" :key="collection.id" class="collection-row" :class="{ active: selectedCollection === collection.id }">
          <button type="button" @click="selectedCollection = collection.id; loadDocuments()"><span><strong>{{ collection.name }}</strong><small>{{ collection.ready_document_count }} ready / {{ collection.document_count }} total</small></span><i>{{ collection.is_seed ? 'SEED' : collection.visibility }}</i></button>
          <div><button type="button" :aria-label="`编辑集合 ${collection.name}`" @click="openEdit(collection)">编辑</button><button type="button" :disabled="collection.is_seed" :aria-label="`删除集合 ${collection.name}`" @click="openCollectionDelete(collection)">删除</button></div>
        </article>
        <p v-if="!collections.length && !loading" class="panel-empty">暂无集合</p>
      </aside>

      <main class="document-main">
        <div class="document-toolbar">
          <label><span>搜索</span><input v-model="keyword" type="search" placeholder="标题、文件名…" @keyup.enter="loadDocuments()" /></label>
          <label><span>状态</span><select v-model="statusFilter" @change="loadDocuments()"><option value="">全部状态</option><option value="pending">Pending</option><option value="processing">Processing</option><option value="ready">Ready</option><option value="failed">Failed</option><option value="deleting">Deleting</option></select></label>
          <button type="button" @click="loadDocuments()">应用筛选</button>
          <small>{{ currentCollection?.name ?? '全部集合' }}</small>
        </div>

        <div v-if="loading" class="document-skeleton" aria-busy="true" aria-label="正在载入文档"><i v-for="index in 5" :key="index"></i></div>
        <div v-else-if="!documents.length" class="document-empty"><span>＋</span><h2>这里还没有匹配的文档</h2><p>{{ selectedCollection ? '上传第一份资料，系统会创建可追踪的摄取任务。' : '选择集合或调整筛选条件。' }}</p><button v-if="selectedCollection" class="button button--primary" type="button" @click="openUpload">上传文档</button></div>
        <div v-else class="document-table" data-testid="document-list">
          <div class="document-table__head"><span>文档</span><span>状态</span><span>版本 / 大小</span><span>更新时间</span><span></span></div>
          <article v-for="document in documents" :key="document.id">
            <button class="document-identity" type="button" @click="openDocument(document)"><span>{{ document.source_name.split('.').pop()?.toUpperCase() }}</span><div><strong>{{ document.title }}</strong><small>{{ document.organization || '未设置组织' }} · {{ document.source_name }}</small></div></button>
            <span class="state-chip" :class="`state-chip--${document.status}`">{{ document.status }}</span>
            <div class="document-meta"><strong>{{ document.sha256_prefix }}</strong><small>{{ sizeLabel(document.size_bytes) }}</small></div>
            <time>{{ dateLabel(document.updated_at) }}</time>
            <button class="row-delete" type="button" :aria-label="`删除文档 ${document.title}`" @click="openDocumentDelete(document)">删除</button>
          </article>
          <button v-if="nextCursor" class="load-more" type="button" :disabled="loadingMore" @click="loadDocuments(true)">{{ loadingMore ? '载入中…' : '载入更多' }}</button>
        </div>
      </main>
    </div>

    <div v-if="panel" class="sheet-backdrop" @click.self="panel = undefined">
      <section class="workspace-sheet" role="dialog" aria-modal="true" :aria-label="panel">
        <button class="sheet-close" type="button" aria-label="关闭" @click="panel = undefined">×</button>

        <form v-if="panel === 'create' || panel === 'edit'" @submit.prevent="saveCollection">
          <p class="section-kicker">{{ panel === 'create' ? 'NEW COLLECTION' : 'COLLECTION SETTINGS' }}</p><h2>{{ panel === 'create' ? '新建集合' : '编辑集合' }}</h2>
          <label>集合名称<input v-model="collectionName" maxlength="200" required /></label>
          <label>描述<textarea v-model="collectionDescription" maxlength="2000" rows="4"></textarea></label>
          <label>可见性<select v-model="collectionVisibility"><option value="tenant">Tenant</option><option value="private">Private</option><option value="public">Public</option></select></label>
          <button class="button button--primary" type="submit" :disabled="saving || !collectionName.trim()">{{ saving ? '保存中…' : '保存集合' }}</button>
        </form>

        <form v-else-if="panel === 'upload'" @submit.prevent="upload">
          <p class="section-kicker">INGESTION ENTRY</p><h2>上传文档</h2><p class="sheet-lead">目标集合：{{ currentCollection?.name }}</p>
          <label class="file-drop">选择文件<input type="file" accept=".pdf,.docx,.html,.htm,.txt,.md,.markdown,.xlsx,.xls,.csv" required @change="chooseFile" /><span>{{ uploadFile?.name ?? 'PDF / Office / HTML / Text / Spreadsheet' }}</span></label>
          <label>标题<input v-model="uploadTitle" maxlength="500" required /></label>
          <label>组织<input v-model="uploadOrganization" maxlength="200" /></label>
          <label>可见性<select v-model="uploadVisibility"><option value="tenant">Tenant</option><option value="private">Private</option><option value="public">Public</option></select></label>
          <button class="button button--primary" type="submit" :disabled="saving || !uploadFile || !uploadTitle.trim()">{{ saving ? '上传中…' : '上传并创建任务' }}</button>
          <div v-if="uploadResult" class="upload-result" role="status"><strong>Job {{ uploadResult.job_id.slice(0, 12) }}…</strong><span>{{ uploadResult.status }}</span><RouterLink :to="`/workspace/ingestion?job=${uploadResult.job_id}`">查看摄取进度 →</RouterLink></div>
        </form>

        <div v-else-if="panel === 'document-detail'" class="detail-sheet">
          <p class="section-kicker">DOCUMENT DETAIL</p><h2>{{ activeDocument?.title }}</h2>
          <div v-if="!documentDetail" class="detail-loading">正在读取文档事实源…</div>
          <template v-else><div class="detail-facts"><p><span>状态</span><strong>{{ documentDetail.status }}</strong></p><p><span>Root / Leaf</span><strong>{{ documentDetail.root_count }} / {{ documentDetail.leaf_count }}</strong></p><p><span>SHA-256</span><strong>{{ documentDetail.sha256_prefix }}…</strong></p><p><span>媒体类型</span><strong>{{ documentDetail.media_type }}</strong></p><p><span>版本 ID</span><strong>{{ documentDetail.version_id }}</strong></p><p><span>可见性</span><strong>{{ documentDetail.visibility }}</strong></p></div><div v-if="documentDetail.recent_job" class="detail-job"><span>最近任务</span><strong>{{ documentDetail.recent_job.stage || documentDetail.recent_job.type }}</strong><i>{{ documentDetail.recent_job.progress }}% · {{ documentDetail.recent_job.status }}</i><RouterLink :to="`/workspace/ingestion?job=${documentDetail.recent_job.id}`">查看任务 →</RouterLink></div><div v-if="documentDetail.version_error_code" class="detail-error"><strong>{{ documentDetail.version_error_code }}</strong><p>{{ documentDetail.version_error_message }}</p></div></template>
        </div>

        <div v-else-if="panel === 'collection-delete'" class="danger-sheet"><p class="section-kicker">CONFIRM DELETION</p><h2>删除集合</h2><p>集合内非删除态文档会立即退出查询范围，并转交后台 Saga 清理。Seed 集合不可删除。</p><label>输入 <strong>{{ activeCollection?.name }}</strong> 确认<input v-model="deleteConfirmation" /></label><button class="button danger-button" type="button" :disabled="saving || deleteConfirmation !== activeCollection?.name" @click="deleteCollection">确认删除</button></div>
        <div v-else-if="panel === 'document-delete'" class="danger-sheet"><p class="section-kicker">SAFE DELETE SAGA</p><h2>删除“{{ activeDocument?.title }}”</h2><p>文档会先退出 ready 状态，再由幂等后台任务清理向量、内容与无引用对象。</p><button class="button danger-button" type="button" :disabled="saving" @click="deleteDocument">{{ saving ? '正在创建任务…' : '创建删除任务' }}</button></div>
      </section>
    </div>
  </section>
</template>
