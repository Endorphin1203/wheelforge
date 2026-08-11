import { apiClient } from './client'
import type { TargetProfileView } from './contracts'

export function listTargetProfiles(): Promise<TargetProfileView[]> {
  return apiClient.request('/api/target-profiles')
}
