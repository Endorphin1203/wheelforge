import { onBeforeUnmount, ref, type Ref } from 'vue'

interface PollingOptions<T> {
  intervalMs: number
  isTerminal: (value: T) => boolean
  onData?: (value: T) => void | Promise<void>
  onError?: (error: unknown) => void
}

export interface PollingController {
  active: Ref<boolean>
  inFlight: Ref<boolean>
  start: () => void
  stop: () => void
}

export function usePolling<T>(load: () => Promise<T>, options: PollingOptions<T>): PollingController {
  const active = ref(false)
  const inFlight = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null
  let generation = 0

  function clearTimer(): void {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  function stop(): void {
    generation += 1
    active.value = false
    clearTimer()
  }

  async function tick(run: number): Promise<void> {
    if (!active.value || run !== generation || inFlight.value) return
    inFlight.value = true
    try {
      const value = await load()
      if (!active.value || run !== generation) return
      await options.onData?.(value)
      if (options.isTerminal(value)) {
        stop()
        return
      }
    } catch (error) {
      if (active.value && run === generation) options.onError?.(error)
    } finally {
      inFlight.value = false
    }
    if (active.value && run === generation) timer = setTimeout(() => void tick(run), options.intervalMs)
  }

  function start(): void {
    stop()
    active.value = true
    const run = generation
    void tick(run)
  }

  onBeforeUnmount(stop)
  return { active, inFlight, start, stop }
}
