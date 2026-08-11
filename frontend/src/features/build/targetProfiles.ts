import type { TargetProfileView } from '@/api/contracts'

function unique(values: string[]): string[] {
  return [...new Set(values)]
}

function comparePython(left: string, right: string): number {
  const [leftMajor, leftMinor] = left.split('.').map(Number)
  const [rightMajor, rightMinor] = right.split('.').map(Number)
  return leftMajor - rightMajor || leftMinor - rightMinor
}

export interface GroupedTargetProfiles {
  operatingSystems: string[]
  architecturesFor: (os: string) => string[]
  pythonVersionsFor: (os: string, architecture: string) => string[]
  find: (os: string, architecture: string, pythonVersion: string) => TargetProfileView | undefined
}

export function groupTargetProfiles(profiles: TargetProfileView[]): GroupedTargetProfiles {
  return {
    operatingSystems: unique(profiles.map((profile) => profile.os)),
    architecturesFor: (os) => unique(
      profiles.filter((profile) => profile.os === os).map((profile) => profile.architecture),
    ),
    pythonVersionsFor: (os, architecture) => unique(
      profiles
        .filter((profile) => profile.os === os && profile.architecture === architecture)
        .map((profile) => profile.pythonVersion),
    ).sort(comparePython),
    find: (os, architecture, pythonVersion) => profiles.find(
      (profile) => profile.os === os && profile.architecture === architecture && profile.pythonVersion === pythonVersion,
    ),
  }
}
