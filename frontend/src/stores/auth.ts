import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { apiClient } from '@/api/client'
import type { AuthTokenResponse, UserRole } from '@/api/contracts'

const SESSION_KEY = 'wf.session'

interface StoredSession {
  accessToken: string
  expiresAt: string
}

interface TokenClaims {
  sub: string
  role: UserRole
  exp: number
}

function decodeClaims(token: string): TokenClaims | null {
  try {
    const payload = token.split('.')[1]
    if (!payload) return null
    const normalized = payload.replaceAll('-', '+').replaceAll('_', '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    const value = JSON.parse(atob(padded)) as Partial<TokenClaims>
    if (
      typeof value.sub !== 'string' ||
      (value.role !== 'USER' && value.role !== 'ADMIN') ||
      typeof value.exp !== 'number'
    ) {
      return null
    }
    return value as TokenClaims
  } catch {
    return null
  }
}

function readSession(): StoredSession | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<StoredSession>
    return typeof value.accessToken === 'string' && typeof value.expiresAt === 'string'
      ? (value as StoredSession)
      : null
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref<string | null>(null)
  const expiresAt = ref<string | null>(null)
  const subject = ref<string | null>(null)
  const role = ref<UserRole | null>(null)

  const isAuthenticated = computed(() => Boolean(accessToken.value && role.value))
  const isAdmin = computed(() => role.value === 'ADMIN')

  function applySession(session: StoredSession): boolean {
    const claims = decodeClaims(session.accessToken)
    const expiration = Date.parse(session.expiresAt)
    if (!claims || !Number.isFinite(expiration) || expiration <= Date.now() || claims.exp * 1000 <= Date.now()) {
      logout()
      return false
    }
    accessToken.value = session.accessToken
    expiresAt.value = session.expiresAt
    subject.value = claims.sub
    role.value = claims.role
    return true
  }

  function restore(): void {
    const session = readSession()
    if (!session || !applySession(session)) sessionStorage.removeItem(SESSION_KEY)
  }

  async function login(username: string, password: string): Promise<void> {
    const response = await apiClient.request<AuthTokenResponse>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
    })
    const session = { accessToken: response.accessToken, expiresAt: response.expiresAt }
    if (!applySession(session)) throw new Error('Invalid authentication token')
    sessionStorage.setItem(SESSION_KEY, JSON.stringify(session))
  }

  function logout(): void {
    accessToken.value = null
    expiresAt.value = null
    subject.value = null
    role.value = null
    sessionStorage.removeItem(SESSION_KEY)
  }

  return { accessToken, expiresAt, subject, role, isAuthenticated, isAdmin, restore, login, logout }
})
