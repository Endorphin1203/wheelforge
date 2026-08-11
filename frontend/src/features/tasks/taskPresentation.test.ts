import { isTerminalStatus, presentVersionDirection } from './taskPresentation'

describe('taskPresentation', () => {
  it.each([
    ['UPGRADE', '升级', 'up'],
    ['DOWNGRADE', '降级', 'down'],
    ['UNCHANGED', '未变化', 'same'],
    [null, '未确定', 'unknown'],
  ])('presents %s using text and a stable tone', (direction, label, tone) => {
    expect(presentVersionDirection(direction)).toMatchObject({ label, tone })
  })

  it('recognizes every terminal state', () => {
    expect(['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED'].every(isTerminalStatus)).toBe(true)
    expect(isTerminalStatus('DOWNLOADING')).toBe(false)
  })
})
