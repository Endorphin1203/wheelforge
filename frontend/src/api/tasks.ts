import { apiClient } from './client'
import type { BuildLogView, BuildTaskView, ResolvedPackageView, VersionComparisonRow } from './contracts'

export interface CreateBuildTaskInput {
  requirementFileId: string
  targetProfileId: string
  solveMode: 'COMPATIBLE'
}

export function createBuildTask(input: CreateBuildTaskInput): Promise<BuildTaskView> {
  return apiClient.request('/api/build-tasks', { method: 'POST', body: input })
}

export function listBuildTasks(): Promise<BuildTaskView[]> {
  return apiClient.request('/api/build-tasks')
}

export function getBuildTask(id: string): Promise<BuildTaskView> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}`)
}

export function cancelBuildTask(id: string): Promise<BuildTaskView> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
}

export function retryBuildTask(id: string): Promise<BuildTaskView> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' })
}

export function deleteBuildTask(id: string): Promise<void> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function listBuildLogs(id: string, afterSequence = 0): Promise<BuildLogView[]> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}/logs?afterSequence=${afterSequence}`)
}

export function listResolvedPackages(id: string): Promise<ResolvedPackageView[]> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}/resolved-packages`)
}

export function listVersionComparison(id: string): Promise<VersionComparisonRow[]> {
  return apiClient.request(`/api/build-tasks/${encodeURIComponent(id)}/version-comparison`)
}
