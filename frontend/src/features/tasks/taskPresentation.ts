import type { BuildStatus } from '@/api/contracts'

const terminalStatuses = new Set(['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED'])

export function isTerminalStatus(status: string): status is BuildStatus {
  return terminalStatuses.has(status)
}

export interface VersionDirectionPresentation {
  label: string
  tone: 'up' | 'down' | 'same' | 'unknown'
  symbol: string
}

export function presentVersionDirection(direction: string | null): VersionDirectionPresentation {
  if (direction === 'UPGRADE') return { label: '升级', tone: 'up', symbol: '↑' }
  if (direction === 'DOWNGRADE') return { label: '降级', tone: 'down', symbol: '↓' }
  if (direction === 'UNCHANGED') return { label: '未变化', tone: 'same', symbol: '→' }
  return { label: '未确定', tone: 'unknown', symbol: '?' }
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date)
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`
  return `${(value / 1024 ** 3).toFixed(2)} GiB`
}
