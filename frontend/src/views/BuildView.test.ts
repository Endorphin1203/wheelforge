import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

const mocks = vi.hoisted(() => ({
  uploadRequirement: vi.fn(),
  getRequirementFile: vi.fn(),
  listRequirementItems: vi.fn(),
  listTargetProfiles: vi.fn(),
  createBuildTask: vi.fn(),
}))

vi.mock('@/api/requirements', () => ({
  uploadRequirement: mocks.uploadRequirement,
  getRequirementFile: mocks.getRequirementFile,
  listRequirementItems: mocks.listRequirementItems,
}))
vi.mock('@/api/targets', () => ({ listTargetProfiles: mocks.listTargetProfiles }))
vi.mock('@/api/tasks', () => ({ createBuildTask: mocks.createBuildTask }))

import BuildView from './BuildView.vue'

const parsedFile = {
  id: 'file-1', originalName: 'requirements.txt', sizeBytes: 13, sha256: 'a'.repeat(64),
  parseStatus: 'PARSED', parseError: null, createdAt: '2026-08-11T00:00:00Z',
}
const target = {
  id: 'target-1', code: 'linux-arm-py311', os: 'LINUX', architecture: 'ARM64',
  pythonImplementation: 'CPYTHON', pythonVersion: '3.11', pythonFullVersion: '3.11.9',
  platformTag: 'manylinux2014_aarch64', abiTags: ['cp311'], validationType: 'STATIC',
  validationPolicyVersion: '1',
}

describe('BuildView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.uploadRequirement.mockResolvedValue(parsedFile)
    mocks.listRequirementItems.mockResolvedValue([
      { id: 'item-1', lineNo: 1, normalizedName: 'numpy', extras: [], specifier: '==1.26.4', marker: null, originalText: 'numpy==1.26.4', supported: true, errorCode: null, errorMessage: null },
    ])
    mocks.listTargetProfiles.mockResolvedValue([target])
    mocks.createBuildTask.mockResolvedValue({ id: 'task-1' })
  })

  it('uploads a valid file and creates a compatible task for the selected profile', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/build/new', component: BuildView },
        { path: '/tasks/:id', component: { template: '<div>task</div>' } },
      ],
    })
    await router.push('/build/new')
    await router.isReady()
    const { container } = render(BuildView, { global: { plugins: [createPinia(), router, ElementPlus] } })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    await fireEvent.change(input, { target: { files: [new File(['numpy==1.26.4'], 'requirements.txt')] } })
    expect(await screen.findByText('numpy==1.26.4')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '开始构建' }))

    await waitFor(() => expect(mocks.createBuildTask).toHaveBeenCalledWith({
      requirementFileId: 'file-1', targetProfileId: 'target-1', solveMode: 'COMPATIBLE',
    }))
    expect(router.currentRoute.value.fullPath).toBe('/tasks/task-1')
  })

  it('rejects files over 512 KiB without uploading', async () => {
    mocks.listTargetProfiles.mockResolvedValue([target])
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: BuildView }] })
    await router.push('/')
    await router.isReady()
    const { container } = render(BuildView, { global: { plugins: [createPinia(), router, ElementPlus] } })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    await fireEvent.change(input, { target: { files: [new File([new Uint8Array(524_289)], 'requirements.txt')] } })

    expect(await screen.findByText('文件不能超过 512 KiB')).toBeInTheDocument()
    expect(mocks.uploadRequirement).not.toHaveBeenCalled()
  })
})
