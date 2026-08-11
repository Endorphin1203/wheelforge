import type { Page, Route } from '@playwright/test'

const nowSeconds = Math.floor(Date.now() / 1000)
const tokenPayload = Buffer.from(JSON.stringify({ sub: 'admin-id', role: 'ADMIN', iat: nowSeconds, exp: nowSeconds + 1800 })).toString('base64url')
const accessToken = `eyJhbGciOiJIUzI1NiJ9.${tokenPayload}.test-signature`

export const requirementFile = {
  id: 'file-1', originalName: 'requirements.txt', sizeBytes: 31, sha256: 'a'.repeat(64),
  parseStatus: 'PARSED', parseError: null, createdAt: '2026-08-11T10:00:00Z',
}

export const target = {
  id: 'target-1', code: 'linux-arm64-cp311-manylinux2014', os: 'LINUX', architecture: 'ARM64',
  pythonImplementation: 'CPYTHON', pythonVersion: '3.11', pythonFullVersion: '3.11.9',
  platformTag: 'manylinux2014_aarch64', abiTags: ['cp311'], validationType: 'STATIC', validationPolicyVersion: 'wheel-tags-v1',
}

export const task = {
  id: 'task-1', requirementFileId: 'file-1', targetProfileId: 'target-1', sourceTaskId: null,
  status: 'PARTIAL_SUCCESS', progress: 100, currentStage: 'DONE', solveMode: 'COMPATIBLE', cancelRequested: false,
  failureCode: null, failureMessage: null, createdAt: '2026-08-11T10:00:00', startedAt: '2026-08-11T10:00:01', finishedAt: '2026-08-11T10:00:24',
  validationLevel: 'STATIC', installVerified: false, validationMessage: 'Static compatibility validation only',
  targetSnapshot: { profileId: target.id, profileCode: target.code, os: target.os, architecture: target.architecture, pythonImplementation: target.pythonImplementation, pythonVersion: target.pythonVersion, pythonFullVersion: target.pythonFullVersion, platformTag: target.platformTag, abiTags: target.abiTags, validationType: 'STATIC', validationPolicyVersion: 'wheel-tags-v1', profileVersion: 0 },
}

export const artifact = {
  id: 'artifact-1', buildTaskId: 'task-1', artifactType: 'WHEELHOUSE_ZIP', filename: 'wheelforge-linux-arm64-py311.zip',
  sizeBytes: 48736210, sha256: 'b'.repeat(64), buildStatus: 'PARTIAL_SUCCESS', expiresAt: '2026-09-10T10:00:24',
  downloadCount: 2, createdAt: '2026-08-11T10:00:24', validationLevel: 'STATIC', installVerified: false,
  validationMessage: 'Static compatibility validation only',
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

export async function installMockApi(page: Page): Promise<void> {
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()

    if (!path.startsWith('/api/')) return route.fallback()

    if (path === '/api/auth/login' && method === 'POST') return json(route, { accessToken, expiresAt: new Date((nowSeconds + 1800) * 1000).toISOString() })
    if (path === '/api/requirement-files' && method === 'POST') return json(route, requirementFile, 202)
    if (path === '/api/requirement-files/file-1') return json(route, requirementFile)
    if (path === '/api/requirement-files/file-1/items') return json(route, [
      { id: 'item-1', lineNo: 1, normalizedName: 'numpy', extras: [], specifier: '==2.0.0', marker: null, originalText: 'numpy==2.0.0', supported: true, errorCode: null, errorMessage: null },
      { id: 'item-2', lineNo: 2, normalizedName: 'fastapi', extras: [], specifier: '==0.115.0', marker: null, originalText: 'fastapi==0.115.0', supported: true, errorCode: null, errorMessage: null },
    ])
    if (path === '/api/target-profiles') return json(route, [target, { ...target, id: 'target-2', code: 'windows-x64-cp312', os: 'WINDOWS', architecture: 'X86_64', pythonVersion: '3.12', pythonFullVersion: '3.12.5', platformTag: 'win_amd64', abiTags: ['cp312'] }])
    if (path === '/api/build-tasks' && method === 'POST') return json(route, task, 201)
    if (path === '/api/build-tasks' && method === 'GET') return json(route, [task])
    if (path === '/api/build-tasks/task-1') return json(route, task)
    if (path === '/api/build-tasks/task-1/logs') return json(route, [
      { sequence: 1, stage: 'RESOLVING', level: 'INFO', message: 'Resolved compatible dependency set', context: {}, createdAt: '2026-08-11T10:00:03' },
      { sequence: 2, stage: 'DOWNLOADING', level: 'WARN', message: 'Original numpy version had no ARM64 Wheel', context: {}, createdAt: '2026-08-11T10:00:08' },
      { sequence: 3, stage: 'PACKAGING', level: 'INFO', message: 'Artifact package created', context: {}, createdAt: '2026-08-11T10:00:23' },
    ])
    if (path === '/api/build-tasks/task-1/resolved-packages') return json(route, [
      { normalizedName: 'numpy', finalVersion: '1.26.4', dependencyType: 'DIRECT', originalConstraint: '==2.0.0', strictVersion: '2.0.0', changeDirection: 'DOWNGRADE', changeReason: '目标架构无原版本 Wheel', candidateAttempts: [], wheelFilename: 'numpy-1.26.4-cp311-manylinux2014_aarch64.whl', wheelTags: ['cp311-cp311-manylinux2014_aarch64'], packageSource: 'TSINGHUA', sha256: 'c'.repeat(64), wheelStatus: 'DOWNLOADED', errorMessage: null },
    ])
    if (path === '/api/build-tasks/task-1/version-comparison') return json(route, [
      { packageName: 'numpy', dependencyType: 'DIRECT', originalConstraint: '==2.0.0', strictVersion: '2.0.0', finalVersion: '1.26.4', changeDirection: 'DOWNGRADE', changeReason: '目标架构无原版本 Wheel', wheelStatus: 'DOWNLOADED', packageSource: 'TSINGHUA' },
      { packageName: 'fastapi', dependencyType: 'DIRECT', originalConstraint: '==0.115.0', strictVersion: '0.115.0', finalVersion: '0.116.1', changeDirection: 'UPGRADE', changeReason: '传递依赖兼容', wheelStatus: 'DOWNLOADED', packageSource: 'ALIYUN' },
    ])
    if (path === '/api/artifacts' && method === 'GET') return json(route, [artifact])
    if (path === '/api/artifacts/artifact-1/download') return route.fulfill({ status: 200, contentType: 'application/zip', headers: { 'Content-Disposition': 'attachment; filename="wheelhouse.zip"' }, body: 'PK mock zip' })
    if (path === '/api/admin/users' && method === 'GET') return json(route, [{ id: 'admin-id', username: 'admin', role: 'ADMIN', status: 'ACTIVE', createdAt: '2026-08-01T00:00:00', initialPassword: null }])
    if (path === '/api/admin/package-sources') return json(route, [
      { id: 'source-1', code: 'TSINGHUA', displayName: '清华镜像', baseUrl: 'https://pypi.tuna.tsinghua.edu.cn/simple', priorityNo: 10, enabled: true, timeoutSeconds: 30, failureCount: 0, updatedAt: '2026-08-11T00:00:00', version: 0 },
      { id: 'source-2', code: 'ALIYUN', displayName: '阿里云镜像', baseUrl: 'https://mirrors.aliyun.com/pypi/simple', priorityNo: 20, enabled: true, timeoutSeconds: 30, failureCount: 1, updatedAt: '2026-08-11T00:00:00', version: 1 },
      { id: 'source-3', code: 'PYPI', displayName: 'PyPI', baseUrl: 'https://pypi.org/simple', priorityNo: 30, enabled: true, timeoutSeconds: 30, failureCount: 0, updatedAt: '2026-08-11T00:00:00', version: 0 },
    ])
    if (path.startsWith('/api/admin/package-sources/') && method === 'PUT') return json(route, { ...(await request.postDataJSON()), id: path.split('/').at(-1), code: 'TSINGHUA', displayName: '清华镜像', baseUrl: 'https://pypi.tuna.tsinghua.edu.cn/simple', failureCount: 0, updatedAt: '2026-08-11T00:00:00', version: 1 })
    if (path === '/api/admin/system-config') return json(route, [
      { key: 'maxConcurrentBuilds', value: 2, description: 'Maximum concurrent builds for one Worker', updatedBy: null, updatedAt: '2026-08-11T00:00:00', version: 0 },
      { key: 'artifactRetentionDays', value: 30, description: 'Artifact retention period in days', updatedBy: null, updatedAt: '2026-08-11T00:00:00', version: 0 },
      { key: 'retentionEnabled', value: true, description: 'Whether automatic Artifact retention cleanup is enabled', updatedBy: null, updatedAt: '2026-08-11T00:00:00', version: 0 },
    ])
    return json(route, { code: 'NOT_FOUND', message: `No mock for ${method} ${path}`, fieldErrors: {}, traceId: 'mock-trace' }, 404)
  })
}
