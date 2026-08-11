import { apiClient } from './client'
import type { PackageSourceView, SystemConfigView, UserRole, UserView } from './contracts'

export function listUsers(): Promise<UserView[]> {
  return apiClient.request('/api/admin/users')
}

export function createUser(input: { username: string; role: UserRole; initialPassword: string | null }): Promise<UserView> {
  return apiClient.request('/api/admin/users', { method: 'POST', body: input })
}

export function setUserStatus(id: string, status: 'ACTIVE' | 'DISABLED'): Promise<UserView> {
  return apiClient.request(`/api/admin/users/${encodeURIComponent(id)}/status`, { method: 'PUT', body: { status } })
}

export function listSources(): Promise<PackageSourceView[]> {
  return apiClient.request('/api/admin/package-sources')
}

export function updateSource(
  id: string,
  input: { enabled: boolean; priorityNo: number; timeoutSeconds: number },
): Promise<PackageSourceView> {
  return apiClient.request(`/api/admin/package-sources/${encodeURIComponent(id)}`, { method: 'PUT', body: input })
}

export function listConfig(): Promise<SystemConfigView[]> {
  return apiClient.request('/api/admin/system-config')
}

export function updateConfig(
  updates: Record<string, { value: unknown; version: number }>,
): Promise<SystemConfigView[]> {
  return apiClient.request('/api/admin/system-config', { method: 'PUT', body: updates })
}
