export interface ConfigField {
  label: string
  group: '上传与包限制' | '任务与求解' | '归档与保留'
  type: 'integer' | 'boolean'
  min?: number
  max?: number
  unit?: string
}

const fields: Record<string, ConfigField> = {
  maxUploadSizeBytes: { label: '上传文件上限', group: '上传与包限制', type: 'integer', min: 1, max: 524288, unit: 'bytes' },
  maxRequirementLines: { label: 'Requirements 最大行数', group: '上传与包限制', type: 'integer', min: 1, max: 2000, unit: '行' },
  maxPackageCount: { label: '单任务最大包数量', group: '上传与包限制', type: 'integer', min: 1, max: 500, unit: '个' },
  maxPackageSizeBytes: { label: '单个 Wheel 大小上限', group: '上传与包限制', type: 'integer', min: 1, max: 536870912, unit: 'bytes' },
  maxArtifactSizeBytes: { label: '产物总大小上限', group: '上传与包限制', type: 'integer', min: 1, max: 2147483648, unit: 'bytes' },
  minFreeDiskBytes: { label: '最小剩余磁盘空间', group: '上传与包限制', type: 'integer', min: 0, max: 1099511627776, unit: 'bytes' },
  taskTimeoutSeconds: { label: '任务超时时间', group: '任务与求解', type: 'integer', min: 60, max: 86400, unit: '秒' },
  maxConcurrentBuilds: { label: '并发构建数', group: '任务与求解', type: 'integer', min: 1, max: 64, unit: '个' },
  maxRetryAttempts: { label: '任务重试次数', group: '任务与求解', type: 'integer', min: 0, max: 20, unit: '次' },
  maxCandidatesPerRequirement: { label: '每项依赖候选版本数', group: '任务与求解', type: 'integer', min: 1, max: 20, unit: '个' },
  maxResolutionAttempts: { label: '依赖求解尝试次数', group: '任务与求解', type: 'integer', min: 1, max: 100, unit: '次' },
  maxArchiveEntries: { label: 'Wheel 归档最大条目数', group: '归档与保留', type: 'integer', min: 1, max: 20000, unit: '条' },
  maxArchiveExpansionRatio: { label: '归档最大展开倍数', group: '归档与保留', type: 'integer', min: 1, max: 200, unit: '倍' },
  artifactRetentionDays: { label: '产物保留天数', group: '归档与保留', type: 'integer', min: 1, max: 3650, unit: '天' },
  retentionEnabled: { label: '启用自动清理', group: '归档与保留', type: 'boolean' },
}

export function configFieldFor(key: string): ConfigField | null {
  return fields[key] ?? null
}
