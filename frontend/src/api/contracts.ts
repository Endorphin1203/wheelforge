export type UserRole = 'USER' | 'ADMIN'
export type BuildStatus =
  | 'CREATED'
  | 'PARSING'
  | 'QUEUED'
  | 'RESOLVING'
  | 'DOWNLOADING'
  | 'VALIDATING'
  | 'PACKAGING'
  | 'SUCCESS'
  | 'PARTIAL_SUCCESS'
  | 'FAILED'
  | 'CANCELLED'

export interface AuthTokenResponse {
  accessToken: string
  expiresAt: string
}

export interface ApiErrorBody {
  code?: string
  message?: string
  fieldErrors?: Record<string, string>
  traceId?: string
}

export interface RequirementFileView {
  id: string
  originalName: string
  sizeBytes: number
  sha256: string
  parseStatus: string
  parseError: string | null
  createdAt: string
}

export interface RequirementItemView {
  id: string
  lineNo: number
  normalizedName: string | null
  extras: string[]
  specifier: string | null
  marker: string | null
  originalText: string
  supported: boolean
  errorCode: string | null
  errorMessage: string | null
}

export interface TargetProfileView {
  id: string
  code: string
  os: string
  architecture: string
  pythonImplementation: string
  pythonVersion: string
  pythonFullVersion: string
  platformTag: string
  abiTags: string[]
  validationType: string
  validationPolicyVersion: string
}

export interface TargetSnapshot {
  profileId: string
  profileCode: string
  os: string
  architecture: string
  pythonImplementation: string
  pythonVersion: string
  pythonFullVersion: string
  platformTag: string
  abiTags: string[]
  validationType: string
  validationPolicyVersion: string
  profileVersion: number
}

export interface BuildTaskView {
  id: string
  requirementFileId: string
  targetProfileId: string
  sourceTaskId: string | null
  status: BuildStatus
  progress: number
  currentStage: string | null
  solveMode: 'COMPATIBLE'
  targetSnapshot: TargetSnapshot
  cancelRequested: boolean
  failureCode: string | null
  failureMessage: string | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  validationLevel: string
  installVerified: boolean
  validationMessage: string
}

export interface BuildLogView {
  sequence: number
  stage: string
  level: string
  message: string
  context: unknown
  createdAt: string
}

export interface ResolvedPackageView {
  normalizedName: string
  finalVersion: string | null
  dependencyType: string
  originalConstraint: string | null
  strictVersion: string | null
  changeDirection: string | null
  changeReason: string | null
  candidateAttempts: unknown
  wheelFilename: string | null
  wheelTags: string[] | null
  packageSource: string | null
  sha256: string | null
  wheelStatus: string
  errorMessage: string | null
}

export interface VersionComparisonRow {
  packageName: string
  dependencyType: string
  originalConstraint: string | null
  strictVersion: string | null
  finalVersion: string | null
  changeDirection: string | null
  changeReason: string | null
  wheelStatus: string
  packageSource: string | null
}

export interface ArtifactView {
  id: string
  buildTaskId: string
  artifactType: string
  filename: string
  sizeBytes: number
  sha256: string
  buildStatus: string
  expiresAt: string
  downloadCount: number
  createdAt: string
  validationLevel: string
  installVerified: boolean
  validationMessage: string
}

export interface UserView {
  id: string
  username: string
  role: UserRole
  status: 'ACTIVE' | 'DISABLED'
  createdAt: string
  initialPassword: string | null
}

export interface PackageSourceView {
  id: string
  code: string
  displayName: string
  baseUrl: string
  priorityNo: number
  enabled: boolean
  timeoutSeconds: number
  failureCount: number
  updatedAt: string
  version: number
}

export interface SystemConfigView {
  key: string
  value: unknown
  description: string
  updatedBy: string | null
  updatedAt: string
  version: number
}
