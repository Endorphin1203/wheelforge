import { apiClient, ApiError, type ApiClient } from '@/api/client'

export interface DownloadBrowser {
  createObjectURL: (blob: Blob) => string
  revokeObjectURL: (url: string) => void
  clickDownload: (url: string, filename: string) => void
}

const defaultBrowser: DownloadBrowser = {
  createObjectURL: (blob) => URL.createObjectURL(blob),
  revokeObjectURL: (url) => URL.revokeObjectURL(url),
  clickDownload: (url, filename) => {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  },
}

function safeFilename(value: string | null, id: string): string {
  const basename = value?.split(/[\\/]/).pop()?.replace(/[^a-zA-Z0-9._-]/g, '_')
  return basename?.toLowerCase().endsWith('.zip') ? basename : `wheelforge-artifact-${id}.zip`
}

export async function downloadArtifact(
  id: string,
  client: ApiClient = apiClient,
  browser: DownloadBrowser = defaultBrowser,
): Promise<void> {
  const result = await client.download(`/api/artifacts/${encodeURIComponent(id)}/download`)
  if (result.contentType !== 'application/zip' && result.contentType !== 'application/octet-stream') {
    throw new ApiError(200, 'INVALID_ARTIFACT_TYPE', '服务器返回的文件不是 ZIP 产物')
  }
  const url = browser.createObjectURL(result.blob)
  try {
    browser.clickDownload(url, safeFilename(result.filename, id))
  } finally {
    browser.revokeObjectURL(url)
  }
}
