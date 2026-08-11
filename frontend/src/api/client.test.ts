import { ApiClient, ApiError } from './client'

describe('ApiClient', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('adds bearer authorization and parses JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([{ id: 9 }]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const client = new ApiClient(() => 'signed-token')

    await expect(client.request<Array<{ id: number }>>('/api/build-tasks')).resolves.toEqual([{ id: 9 }])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/build-tasks',
      expect.objectContaining({ headers: expect.any(Headers) }),
    )
    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer signed-token')
  })

  it('turns a JSON failure into an ApiError and announces a 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: 'UNAUTHENTICATED',
            message: 'Authentication is required',
            traceId: 'trace-1',
          }),
          { status: 401, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )
    const onUnauthorized = vi.fn()
    const client = new ApiClient(() => 'expired', onUnauthorized)

    const error = await client.request('/api/build-tasks').catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 401, code: 'UNAUTHENTICATED', traceId: 'trace-1' })
    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('keeps multipart content type under browser control', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 1 }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const body = new FormData()
    body.append('file', new File(['numpy==1.26.4'], 'requirements.txt'))

    await new ApiClient(() => null).request('/api/requirement-files', { method: 'POST', body })

    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.has('Content-Type')).toBe(false)
  })
})
