import { apiClient } from './client'
import type { RequirementFileView, RequirementItemView } from './contracts'

export function uploadRequirement(file: File): Promise<RequirementFileView> {
  const body = new FormData()
  body.append('file', file)
  return apiClient.request('/api/requirement-files', { method: 'POST', body })
}

export function getRequirementFile(id: string): Promise<RequirementFileView> {
  return apiClient.request(`/api/requirement-files/${encodeURIComponent(id)}`)
}

export function listRequirementItems(id: string): Promise<RequirementItemView[]> {
  return apiClient.request(`/api/requirement-files/${encodeURIComponent(id)}/items`)
}
