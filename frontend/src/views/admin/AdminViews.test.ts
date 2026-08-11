import { render, screen } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(), createUser: vi.fn(), setUserStatus: vi.fn(),
  listSources: vi.fn(), updateSource: vi.fn(),
}))
vi.mock('@/api/admin', () => mocks)

import SourcesView from './SourcesView.vue'
import UsersView from './UsersView.vue'

describe('administrator views', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reveals a generated initial password only in the immediate result dialog', async () => {
    mocks.listUsers.mockResolvedValue([])
    mocks.createUser.mockResolvedValue({ id: 'user-1', username: 'builder', role: 'USER', status: 'ACTIVE', createdAt: '2026-08-11T00:00:00', initialPassword: 'Temp-Only-123!' })
    render(UsersView, { global: { plugins: [createPinia(), ElementPlus] } })

    await userEvent.click(await screen.findByRole('button', { name: '创建用户' }))
    await userEvent.type(screen.getByLabelText('用户名'), 'builder')
    await userEvent.click(screen.getByRole('button', { name: '确认创建' }))
    expect(await screen.findByText('Temp-Only-123!')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '我已记录' }))
    expect(screen.queryByText('Temp-Only-123!')).not.toBeInTheDocument()
  })

  it('renders built-in source URLs as read-only text', async () => {
    mocks.listSources.mockResolvedValue([{ id: 'source-1', code: 'TSINGHUA', displayName: '清华镜像', baseUrl: 'https://pypi.tuna.tsinghua.edu.cn/simple', priorityNo: 10, enabled: true, timeoutSeconds: 30, failureCount: 0, updatedAt: '2026-08-11T00:00:00', version: 0 }])
    render(SourcesView, { global: { plugins: [createPinia(), ElementPlus] } })

    expect(await screen.findByText('https://pypi.tuna.tsinghua.edu.cn/simple')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('https://pypi.tuna.tsinghua.edu.cn/simple')).not.toBeInTheDocument()
  })
})
