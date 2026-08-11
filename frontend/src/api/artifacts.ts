import { apiClient } from './client'
import type { ArtifactView } from './contracts'

export function listArtifacts(limit = 50, beforeCreatedAt?: string, beforeId?: string): Promise<ArtifactView[]> {
  const query = new URLSearchParams({ limit: String(limit) })
  if (beforeCreatedAt) query.set('beforeCreatedAt', beforeCreatedAt)
  if (beforeId) query.set('beforeId', beforeId)
  return apiClient.request(`/api/artifacts?${query}`)
}

export function getArtifact(id: string): Promise<ArtifactView> {
  return apiClient.request(`/api/artifacts/${encodeURIComponent(id)}`)
}
