import type { ApiErrorBody } from './contracts'

export interface DownloadResult {
  blob: Blob
  filename: string | null
  contentType: string
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly traceId?: string,
    public readonly fieldErrors: Record<string, string> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

function responseFilename(value: string | null): string | null {
  if (!value) return null
  const encoded = value.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded)
    } catch {
      return null
    }
  }
  return value.match(/filename="?([^";]+)"?/i)?.[1] ?? null
}

export class ApiClient {
  constructor(
    private getAccessToken: () => string | null,
    private onUnauthorized: () => void = () => undefined,
  ) {}

  configure(getAccessToken: () => string | null, onUnauthorized: () => void): void {
    this.getAccessToken = getAccessToken
    this.onUnauthorized = onUnauthorized
  }

  async request<T>(path: string, init: RequestInit & { body?: BodyInit | object | null } = {}): Promise<T> {
    const headers = new Headers(init.headers)
    const token = this.getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)

    let body = init.body
    if (body && !(body instanceof FormData) && !(body instanceof Blob) && typeof body === 'object') {
      headers.set('Content-Type', 'application/json')
      body = JSON.stringify(body)
    }
    headers.set('Accept', 'application/json')

    let response: Response
    try {
      response = await fetch(path, { ...init, headers, body: body as BodyInit | null | undefined })
    } catch {
      throw new ApiError(0, 'NETWORK_ERROR', '无法连接服务器，请检查网络后重试')
    }
    if (!response.ok) throw await this.toError(response)
    if (response.status === 204) return undefined as T
    const contentType = response.headers.get('Content-Type') ?? ''
    if (!contentType.includes('application/json')) {
      throw new ApiError(response.status, 'INVALID_RESPONSE', '服务器返回了无法识别的数据')
    }
    return (await response.json()) as T
  }

  async download(path: string): Promise<DownloadResult> {
    const headers = new Headers({ Accept: 'application/zip, application/octet-stream' })
    const token = this.getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    let response: Response
    try {
      response = await fetch(path, { headers })
    } catch {
      throw new ApiError(0, 'NETWORK_ERROR', '无法连接服务器，请检查网络后重试')
    }
    if (!response.ok) throw await this.toError(response)
    return {
      blob: await response.blob(),
      filename: responseFilename(response.headers.get('Content-Disposition')),
      contentType: response.headers.get('Content-Type')?.split(';')[0] ?? '',
    }
  }

  private async toError(response: Response): Promise<ApiError> {
    let body: ApiErrorBody = {}
    if ((response.headers.get('Content-Type') ?? '').includes('application/json')) {
      try {
        body = (await response.json()) as ApiErrorBody
      } catch {
        body = {}
      }
    }
    if (response.status === 401 && this.getAccessToken()) this.onUnauthorized()
    return new ApiError(
      response.status,
      body.code ?? `HTTP_${response.status}`,
      body.message ?? '请求失败，请稍后重试',
      body.traceId,
      body.fieldErrors,
    )
  }
}

export const apiClient = new ApiClient(() => null)
