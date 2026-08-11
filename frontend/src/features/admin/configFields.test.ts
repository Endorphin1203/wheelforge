import { configFieldFor } from './configFields'

describe('configFields', () => {
  it('defines exact bounds for resource and worker controls', () => {
    expect(configFieldFor('maxUploadSizeBytes')).toMatchObject({ type: 'integer', min: 1, max: 524288 })
    expect(configFieldFor('maxConcurrentBuilds')).toMatchObject({ type: 'integer', min: 1, max: 64 })
    expect(configFieldFor('taskTimeoutSeconds')).toMatchObject({ type: 'integer', min: 60, max: 86400 })
  })

  it('uses a switch only for retentionEnabled', () => {
    expect(configFieldFor('retentionEnabled')).toMatchObject({ type: 'boolean' })
    expect(configFieldFor('unknown')).toBeNull()
  })
})
