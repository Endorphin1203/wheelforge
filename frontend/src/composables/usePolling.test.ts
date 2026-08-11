import { usePolling } from './usePolling'

describe('usePolling', () => {
  it('stops after a terminal response', async () => {
    const load = vi.fn().mockResolvedValueOnce({ status: 'PARSING' }).mockResolvedValueOnce({ status: 'PARSED' })
    const polling = usePolling(load, {
      intervalMs: 5,
      isTerminal: (value) => value.status !== 'PARSING',
    })

    polling.start()
    await vi.waitFor(() => expect(load).toHaveBeenCalledTimes(2))
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(load).toHaveBeenCalledTimes(2)
    expect(polling.active.value).toBe(false)
  })

  it('never overlaps requests and cancels its next timer', async () => {
    let resolveRequest!: (value: { status: string }) => void
    const load = vi.fn(() => new Promise<{ status: string }>((resolve) => { resolveRequest = resolve }))
    const polling = usePolling(load, { intervalMs: 5, isTerminal: () => false })

    polling.start()
    await vi.waitFor(() => expect(load).toHaveBeenCalledOnce())
    await new Promise((resolve) => setTimeout(resolve, 15))
    expect(load).toHaveBeenCalledOnce()

    resolveRequest({ status: 'PARSING' })
    polling.stop()
    await new Promise((resolve) => setTimeout(resolve, 15))
    expect(load).toHaveBeenCalledOnce()
  })
})
