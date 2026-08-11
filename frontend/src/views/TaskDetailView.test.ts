import { render, screen } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const mocks = vi.hoisted(() => ({
  getBuildTask: vi.fn(), listBuildLogs: vi.fn(), listResolvedPackages: vi.fn(),
  listVersionComparison: vi.fn(), listArtifacts: vi.fn(),
}))
vi.mock('@/api/tasks', () => ({
  getBuildTask: mocks.getBuildTask,
  listBuildLogs: mocks.listBuildLogs,
  listResolvedPackages: mocks.listResolvedPackages,
  listVersionComparison: mocks.listVersionComparison,
  cancelBuildTask: vi.fn(), retryBuildTask: vi.fn(), deleteBuildTask: vi.fn(),
}))
vi.mock('@/api/artifacts', () => ({ listArtifacts: mocks.listArtifacts }))

import TaskDetailView from './TaskDetailView.vue'

describe('TaskDetailView', () => {
  it('shows partial success, static validation evidence, and version downgrade text', async () => {
    mocks.getBuildTask.mockResolvedValue({
      id: 'task-1', requirementFileId: 'file-1', targetProfileId: 'target-1', sourceTaskId: null,
      status: 'PARTIAL_SUCCESS', progress: 100, currentStage: 'DONE', solveMode: 'COMPATIBLE', cancelRequested: false,
      failureCode: null, failureMessage: null, createdAt: '2026-08-11T10:00:00', startedAt: '2026-08-11T10:00:01', finishedAt: '2026-08-11T10:00:20',
      validationLevel: 'STATIC', installVerified: false, validationMessage: 'Static compatibility validation only',
      targetSnapshot: { profileId: 'target-1', profileCode: 'linux-arm-py311', os: 'LINUX', architecture: 'ARM64', pythonImplementation: 'CPYTHON', pythonVersion: '3.11', pythonFullVersion: '3.11.9', platformTag: 'manylinux2014_aarch64', abiTags: ['cp311'], validationType: 'STATIC', validationPolicyVersion: '1', profileVersion: 0 },
    })
    mocks.listBuildLogs.mockResolvedValue([])
    mocks.listResolvedPackages.mockResolvedValue([])
    mocks.listVersionComparison.mockResolvedValue([{ packageName: 'numpy', dependencyType: 'DIRECT', originalConstraint: '==2.0.0', strictVersion: '2.0.0', finalVersion: '1.26.4', changeDirection: 'DOWNGRADE', changeReason: '目标平台无原版本 Wheel', wheelStatus: 'DOWNLOADED', packageSource: 'TSINGHUA' }])
    mocks.listArtifacts.mockResolvedValue([])
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/tasks/:id', component: TaskDetailView }] })
    await router.push('/tasks/task-1')
    await router.isReady()
    render(TaskDetailView, { global: { plugins: [createPinia(), router, ElementPlus] } })

    expect(await screen.findByText('部分成功')).toBeInTheDocument()
    expect(screen.getByText('静态兼容性校验，不执行目标环境安装，不能证明目标机器一定安装成功。')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: '版本对比' }))
    expect(await screen.findByText('降级')).toBeInTheDocument()
    expect(screen.getByText('1.26.4')).toBeInTheDocument()
  })
})
