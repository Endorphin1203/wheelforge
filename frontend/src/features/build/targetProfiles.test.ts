import type { TargetProfileView } from '@/api/contracts'
import { groupTargetProfiles } from './targetProfiles'

const profiles: TargetProfileView[] = [
  {
    id: 'linux-arm-311', code: 'linux-arm-311', os: 'LINUX', architecture: 'ARM64',
    pythonImplementation: 'CPYTHON', pythonVersion: '3.11', pythonFullVersion: '3.11.9',
    platformTag: 'manylinux2014_aarch64', abiTags: ['cp311'], validationType: 'STATIC',
    validationPolicyVersion: '1',
  },
  {
    id: 'windows-x64-312', code: 'windows-x64-312', os: 'WINDOWS', architecture: 'X86_64',
    pythonImplementation: 'CPYTHON', pythonVersion: '3.12', pythonFullVersion: '3.12.4',
    platformTag: 'win_amd64', abiTags: ['cp312'], validationType: 'STATIC',
    validationPolicyVersion: '1',
  },
]

describe('groupTargetProfiles', () => {
  it('offers only combinations represented by a real profile', () => {
    const grouped = groupTargetProfiles(profiles)

    expect(grouped.operatingSystems).toEqual(['LINUX', 'WINDOWS'])
    expect(grouped.architecturesFor('LINUX')).toEqual(['ARM64'])
    expect(grouped.pythonVersionsFor('LINUX', 'ARM64')).toEqual(['3.11'])
    expect(grouped.pythonVersionsFor('LINUX', 'X86_64')).toEqual([])
    expect(grouped.find('LINUX', 'ARM64', '3.11')?.id).toBe('linux-arm-311')
  })

  it('deduplicates and sorts Python versions numerically', () => {
    const grouped = groupTargetProfiles([
      { ...profiles[0], id: 'py313', pythonVersion: '3.13' },
      { ...profiles[0], id: 'py39', pythonVersion: '3.9' },
      profiles[0],
    ])

    expect(grouped.pythonVersionsFor('LINUX', 'ARM64')).toEqual(['3.9', '3.11', '3.13'])
  })
})
