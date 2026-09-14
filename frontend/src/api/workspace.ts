import type { components } from './schema'

import { apiClient, apiErrorFromResponse } from './client'

export type Collection = components['schemas']['CollectionResponse']
export type CollectionInput = components['schemas']['CollectionCreate']
export type CollectionPatch = components['schemas']['CollectionPatch']
export type DocumentSummary = components['schemas']['DocumentResponse']
export type DocumentDetail = components['schemas']['DocumentDetailResponse']
export type DocumentPipeline = components['schemas']['DocumentPipelineResponse']
export type PipelineRoot = components['schemas']['PipelineRootDetailResponse']
export type DocumentStatus = components['schemas']['DocumentStatus']
export type Job = components['schemas']['JobResponse']
export type JobListItem = components['schemas']['JobListItemResponse']
export type JobStatus = components['schemas']['JobStatus']
export type UploadResult = components['schemas']['UploadResponse']

export type DocumentFilters = {
  collection?: string
  status?: DocumentStatus
  mediaType?: string
  keyword?: string
  cursor?: string
  limit?: number
}

export type JobFilters = { status?: JobStatus; cursor?: string; limit?: number }
export type Page<T> = { items: T[]; nextCursor?: string }
export type UploadInput = {
  file: File
  collectionId: string
  title: string
  organization?: string
  visibility: components['schemas']['DocumentVisibility']
}

export interface WorkspaceApi {
  listCollections(): Promise<Collection[]>
  createCollection(input: CollectionInput): Promise<Collection>
  updateCollection(id: string, input: CollectionPatch): Promise<Collection>
  deleteCollection(id: string, confirmName: string): Promise<void>
  listDocuments(filters?: DocumentFilters): Promise<Page<DocumentSummary>>
  getDocument(id: string): Promise<DocumentDetail>
  getDocumentPipeline(id: string, cursor?: number): Promise<DocumentPipeline>
  getPipelineRoot(documentId: string, rootId: string): Promise<PipelineRoot>
  uploadDocument(input: UploadInput): Promise<UploadResult>
  deleteDocument(id: string): Promise<string>
  listJobs(filters?: JobFilters): Promise<Page<JobListItem>>
  getJob(id: string): Promise<Job>
}

export const workspaceApi: WorkspaceApi = {
  async listCollections() {
    const result = await apiClient.GET('/api/v1/collections')
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data.items
  },
  async createCollection(input) {
    const result = await apiClient.POST('/api/v1/collections', { body: input })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async updateCollection(id, input) {
    const result = await apiClient.PATCH('/api/v1/collections/{collection_id}', {
      params: { path: { collection_id: id } },
      body: input,
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async deleteCollection(id, confirmName) {
    const result = await apiClient.DELETE('/api/v1/collections/{collection_id}', {
      params: { path: { collection_id: id } },
      body: { confirm_name: confirmName },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
  },
  async listDocuments(filters = {}) {
    const result = await apiClient.GET('/api/v1/documents', {
      params: {
        query: {
          collection: filters.collection,
          status: filters.status,
          type: filters.mediaType,
          keyword: filters.keyword,
          cursor: filters.cursor,
          limit: filters.limit ?? 20,
        },
      },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return { items: result.data.items, nextCursor: result.data.next_cursor ?? undefined }
  },
  async getDocument(id) {
    const result = await apiClient.GET('/api/v1/documents/{document_id}', {
      params: { path: { document_id: id } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async getDocumentPipeline(id, cursor) {
    const result = await apiClient.GET('/api/v1/documents/{document_id}/pipeline', {
      params: { path: { document_id: id }, query: { cursor, limit: 50 } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async getPipelineRoot(documentId, rootId) {
    const result = await apiClient.GET(
      '/api/v1/documents/{document_id}/pipeline/roots/{root_id}',
      { params: { path: { document_id: documentId, root_id: rootId } } },
    )
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async uploadDocument(input) {
    const form = new FormData()
    form.append('file', input.file)
    form.append('collection_id', input.collectionId)
    form.append('title', input.title)
    if (input.organization) form.append('organization', input.organization)
    form.append('visibility', input.visibility)
    const result = await apiClient.POST('/api/v1/documents', {
      body: {
        file: input.file.name,
        collection_id: input.collectionId,
        title: input.title,
        organization: input.organization,
        visibility: input.visibility,
      },
      bodySerializer: () => form,
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
  async deleteDocument(id) {
    const result = await apiClient.DELETE('/api/v1/documents/{document_id}', {
      params: { path: { document_id: id } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data.job_id
  },
  async listJobs(filters = {}) {
    const result = await apiClient.GET('/api/v1/ingestion-jobs', {
      params: {
        query: {
          status: filters.status,
          cursor: filters.cursor,
          limit: filters.limit ?? 20,
        },
      },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return { items: result.data.items, nextCursor: result.data.next_cursor ?? undefined }
  },
  async getJob(id) {
    const result = await apiClient.GET('/api/v1/ingestion-jobs/{job_id}', {
      params: { path: { job_id: id } },
    })
    if (result.error) throw apiErrorFromResponse(result.error, result.response)
    return result.data
  },
}
