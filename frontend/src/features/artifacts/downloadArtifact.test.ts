import type { ApiClient } from '@/api/client'
import { ApiError } from '@/api/client'
import { downloadArtifact, type DownloadBrowser } from './downloadArtifact'

describe('downloadArtifact', () => {
  it('clicks a safe ZIP filename and revokes the object URL', async () => {
    const client = {
      download: vi.fn().mockResolvedValue({
        blob: new Blob(['zip']), filename: '../../wheelhouse.zip', contentType: 'application/zip',
      }),
    } as unknown as ApiClient
    const browser: DownloadBrowser = {
      createObjectURL: vi.fn().mockReturnValue('blob:test'),
      revokeObjectURL: vi.fn(),
      clickDownload: vi.fn(),
    }

    await downloadArtifact('artifact-7', client, browser)

    expect(browser.clickDownload).toHaveBeenCalledWith('blob:test', 'wheelhouse.zip')
    expect(browser.revokeObjectURL).toHaveBeenCalledWith('blob:test')
  })

  it('rejects a non-ZIP response without creating an object URL', async () => {
    const client = {
      download: vi.fn().mockResolvedValue({ blob: new Blob(['html']), filename: 'error.html', contentType: 'text/html' }),
    } as unknown as ApiClient
    const browser: DownloadBrowser = {
      createObjectURL: vi.fn(), revokeObjectURL: vi.fn(), clickDownload: vi.fn(),
    }

    await expect(downloadArtifact('artifact-7', client, browser)).rejects.toBeInstanceOf(ApiError)
    expect(browser.createObjectURL).not.toHaveBeenCalled()
  })
})
