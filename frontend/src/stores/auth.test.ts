import { createPinia, setActivePinia } from 'pinia'

import { useAuthStore } from './auth'

function token(claims: Record<string, unknown>): string {
  return `header.${btoa(JSON.stringify(claims)).replaceAll('=', '')}.signature`
}

describe('auth store', () => {
  beforeEach(() => {
    sessionStorage.clear()
    setActivePinia(createPinia())
  })

  it('restores a valid user session', () => {
    const accessToken = token({ sub: 'user-id', role: 'USER', exp: Math.floor(Date.now() / 1000) + 120 })
    sessionStorage.setItem(
      'wf.session',
      JSON.stringify({ accessToken, expiresAt: new Date(Date.now() + 120_000).toISOString() }),
    )

    const auth = useAuthStore()
    auth.restore()

    expect(auth.isAuthenticated).toBe(true)
    expect(auth.role).toBe('USER')
  })

  it('clears an expired session during restore', () => {
    const accessToken = token({ sub: 'user-id', role: 'ADMIN', exp: Math.floor(Date.now() / 1000) - 1 })
    sessionStorage.setItem(
      'wf.session',
      JSON.stringify({ accessToken, expiresAt: new Date(Date.now() - 1_000).toISOString() }),
    )

    const auth = useAuthStore()
    auth.restore()

    expect(auth.isAuthenticated).toBe(false)
    expect(sessionStorage.getItem('wf.session')).toBeNull()
  })

  it('rejects token payloads with unsupported roles', () => {
    const accessToken = token({ sub: 'user-id', role: 'AUDITOR', exp: Math.floor(Date.now() / 1000) + 120 })
    sessionStorage.setItem(
      'wf.session',
      JSON.stringify({ accessToken, expiresAt: new Date(Date.now() + 120_000).toISOString() }),
    )

    useAuthStore().restore()

    expect(sessionStorage.getItem('wf.session')).toBeNull()
  })
})
