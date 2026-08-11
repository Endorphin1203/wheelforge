<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Document, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import type { RequirementFileView, RequirementItemView, TargetProfileView } from '@/api/contracts'
import { getRequirementFile, listRequirementItems, uploadRequirement } from '@/api/requirements'
import { createBuildTask } from '@/api/tasks'
import { listTargetProfiles } from '@/api/targets'
import StaticValidationNotice from '@/components/StaticValidationNotice.vue'
import { usePolling } from '@/composables/usePolling'
import { groupTargetProfiles } from '@/features/build/targetProfiles'
import { useBuildDraftStore } from '@/stores/buildDraft'

const MAX_FILE_SIZE = 512 * 1024
const router = useRouter()
const draft = useBuildDraftStore()
const input = ref<HTMLInputElement | null>(null)
const dragActive = ref(false)
const uploading = ref(false)
const creating = ref(false)
const targetLoading = ref(true)
const errorMessage = ref('')
const traceId = ref('')
const requirementFile = ref<RequirementFileView | null>(null)
const items = ref<RequirementItemView[]>([])
const profiles = ref<TargetProfileView[]>([])
const selectedOs = ref('')
const selectedArchitecture = ref('')
const selectedPython = ref('')

const grouped = computed(() => groupTargetProfiles(profiles.value))
const architectureOptions = computed(() => grouped.value.architecturesFor(selectedOs.value))
const pythonOptions = computed(() => grouped.value.pythonVersionsFor(selectedOs.value, selectedArchitecture.value))
const selectedProfile = computed(() => grouped.value.find(selectedOs.value, selectedArchitecture.value, selectedPython.value))
const canBuild = computed(() => requirementFile.value?.parseStatus === 'PARSED' && Boolean(selectedProfile.value) && !creating.value)
const parseStatusLabel = computed(() => ({ PENDING: '等待解析', PARSING: '解析中', PARSED: '解析完成', FAILED: '解析失败' })[requirementFile.value?.parseStatus ?? ''] ?? '')

function describeError(error: unknown, fallback: string): void {
  if (error instanceof ApiError) {
    errorMessage.value = error.code === 'NETWORK_ERROR' ? error.message : fallback
    traceId.value = error.traceId ?? ''
  } else {
    errorMessage.value = fallback
    traceId.value = ''
  }
}

async function loadItems(id: string): Promise<void> {
  items.value = await listRequirementItems(id)
}

const parsePolling = usePolling(
  async () => {
    if (!requirementFile.value) throw new Error('No requirement file')
    return getRequirementFile(requirementFile.value.id)
  },
  {
    intervalMs: 2000,
    isTerminal: (file) => file.parseStatus === 'PARSED' || file.parseStatus === 'FAILED',
    onData: async (file) => {
      requirementFile.value = file
      if (file.parseStatus === 'PARSED') await loadItems(file.id)
    },
    onError: (error) => describeError(error, '无法刷新解析状态，请重试'),
  },
)

function selectDefaults(): void {
  selectedOs.value = grouped.value.operatingSystems[0] ?? ''
  selectedArchitecture.value = architectureOptions.value[0] ?? ''
  selectedPython.value = pythonOptions.value[0] ?? ''
}

function selectOs(os: string | number | boolean): void {
  selectedOs.value = String(os)
  selectedArchitecture.value = grouped.value.architecturesFor(selectedOs.value)[0] ?? ''
  selectedPython.value = grouped.value.pythonVersionsFor(selectedOs.value, selectedArchitecture.value)[0] ?? ''
}

function selectArchitecture(architecture: string | number | boolean): void {
  selectedArchitecture.value = String(architecture)
  selectedPython.value = grouped.value.pythonVersionsFor(selectedOs.value, selectedArchitecture.value)[0] ?? ''
}

function validateFile(file: File): string | null {
  if (!file.name.toLowerCase().endsWith('.txt')) return '仅支持 .txt 格式的 requirements 文件'
  if (file.size === 0) return '文件不能为空'
  if (file.size > MAX_FILE_SIZE) return '文件不能超过 512 KiB'
  return null
}

async function acceptFile(file: File): Promise<void> {
  const validationError = validateFile(file)
  errorMessage.value = validationError ?? ''
  traceId.value = ''
  if (validationError) return
  uploading.value = true
  items.value = []
  parsePolling.stop()
  try {
    const uploaded = await uploadRequirement(file)
    requirementFile.value = uploaded
    draft.setRequirementFile(uploaded.id)
    if (uploaded.parseStatus === 'PARSED') await loadItems(uploaded.id)
    else if (uploaded.parseStatus !== 'FAILED') parsePolling.start()
  } catch (error) {
    describeError(error, '上传失败，请检查文件后重试')
  } finally {
    uploading.value = false
    if (input.value) input.value.value = ''
  }
}

function onFileChange(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (file) void acceptFile(file)
}

function onDrop(event: DragEvent): void {
  dragActive.value = false
  const file = event.dataTransfer?.files[0]
  if (file) void acceptFile(file)
}

async function startBuild(): Promise<void> {
  if (!requirementFile.value || !selectedProfile.value || creating.value) return
  creating.value = true
  errorMessage.value = ''
  try {
    draft.setTargetProfile(selectedProfile.value.id)
    const task = await createBuildTask({
      requirementFileId: requirementFile.value.id,
      targetProfileId: selectedProfile.value.id,
      solveMode: 'COMPATIBLE',
    })
    draft.clear()
    await router.push(`/tasks/${task.id}`)
  } catch (error) {
    describeError(error, '构建任务创建失败，请稍后重试')
  } finally {
    creating.value = false
  }
}

onMounted(async () => {
  try {
    profiles.value = await listTargetProfiles()
    selectDefaults()
  } catch (error) {
    describeError(error, '目标平台加载失败，请刷新页面')
  } finally {
    targetLoading.value = false
  }
})
</script>

<template>
  <section class="page-section build-page">
    <header class="page-header">
      <div><p class="page-eyebrow">Build Workspace</p><h1>新建离线依赖构建</h1></div>
      <div class="build-mode"><span>求解模式</span><strong>兼容求解</strong></div>
    </header>

    <ol class="workflow-steps" aria-label="构建步骤">
      <li :class="{ active: !requirementFile, done: requirementFile }"><span>1</span><div><strong>上传依赖</strong><small>requirements.txt</small></div></li>
      <li :class="{ active: requirementFile?.parseStatus === 'PARSED' && !selectedProfile, done: selectedProfile }"><span>2</span><div><strong>选择目标</strong><small>系统 / CPU / Python</small></div></li>
      <li :class="{ active: canBuild }"><span>3</span><div><strong>创建任务</strong><small>进入构建队列</small></div></li>
    </ol>

    <el-alert v-if="errorMessage" class="page-error" type="error" :closable="false" show-icon :title="errorMessage">
      <template v-if="traceId" #default>错误跟踪号：<code>{{ traceId }}</code></template>
    </el-alert>

    <div class="build-grid">
      <div class="build-main">
        <section class="workspace-section" aria-labelledby="upload-heading">
          <div class="section-heading"><div><span class="section-number">01</span><h2 id="upload-heading">上传依赖文件</h2></div><span v-if="requirementFile" class="section-state">{{ parseStatusLabel }}</span></div>
          <input ref="input" class="visually-hidden" type="file" accept=".txt,text/plain" @change="onFileChange" />
          <button
            type="button"
            class="upload-zone"
            :class="{ 'is-dragging': dragActive }"
            :disabled="uploading"
            @click="input?.click()"
            @dragenter.prevent="dragActive = true"
            @dragover.prevent
            @dragleave.prevent="dragActive = false"
            @drop.prevent="onDrop"
          >
            <el-icon :size="30"><UploadFilled /></el-icon>
            <strong>{{ uploading ? '正在上传...' : '选择或拖入 requirements.txt' }}</strong>
            <span>UTF-8 / GBK，最大 512 KiB</span>
          </button>
          <div v-if="requirementFile" class="file-summary">
            <el-icon><Document /></el-icon>
            <div><strong>{{ requirementFile.originalName }}</strong><span>{{ requirementFile.sizeBytes }} bytes · SHA-256 {{ requirementFile.sha256.slice(0, 12) }}...</span></div>
            <el-icon v-if="parsePolling.active.value" class="is-loading"><Refresh /></el-icon>
          </div>
          <el-alert v-if="requirementFile?.parseStatus === 'FAILED'" type="error" :closable="false" show-icon title="Requirements 解析失败" :description="requirementFile.parseError ?? '请检查文件语法'" />
          <el-table v-if="items.length" :data="items" class="requirement-table" size="small">
            <el-table-column prop="lineNo" label="行" width="58" />
            <el-table-column prop="originalText" label="原始依赖" min-width="210" />
            <el-table-column prop="normalizedName" label="包名" min-width="130" />
            <el-table-column label="约束" width="120"><template #default="scope">{{ scope.row.specifier || '任意版本' }}</template></el-table-column>
            <el-table-column label="状态" width="92"><template #default="scope"><span :class="scope.row.supported ? 'text-success' : 'text-danger'">{{ scope.row.supported ? '支持' : '不支持' }}</span></template></el-table-column>
          </el-table>
        </section>

        <section class="workspace-section" aria-labelledby="target-heading">
          <div class="section-heading"><div><span class="section-number">02</span><h2 id="target-heading">选择目标运行环境</h2></div></div>
          <el-skeleton v-if="targetLoading" :rows="3" animated />
          <div v-else-if="profiles.length" class="target-controls">
            <div class="target-field"><label>操作系统</label><el-segmented :model-value="selectedOs" :options="grouped.operatingSystems.map(value => ({ label: value === 'LINUX' ? 'Linux' : 'Windows', value }))" @change="selectOs" /></div>
            <div class="target-field"><label>CPU 架构</label><el-segmented :model-value="selectedArchitecture" :options="architectureOptions.map(value => ({ label: value === 'X86_64' ? 'x86_64' : value, value }))" @change="selectArchitecture" /></div>
            <div class="target-field"><label for="python-version">Python 版本</label><el-select id="python-version" v-model="selectedPython" aria-label="Python 版本"><el-option v-for="version in pythonOptions" :key="version" :label="`Python ${version}`" :value="version" /></el-select></div>
          </div>
          <el-empty v-else description="当前没有可用的目标平台" :image-size="72" />
        </section>
      </div>

      <aside class="build-summary" aria-labelledby="summary-heading">
        <h2 id="summary-heading">构建摘要</h2>
        <dl>
          <div><dt>依赖文件</dt><dd>{{ requirementFile?.originalName ?? '尚未上传' }}</dd></div>
          <div><dt>操作系统</dt><dd>{{ selectedOs || '-' }}</dd></div>
          <div><dt>CPU</dt><dd>{{ selectedArchitecture || '-' }}</dd></div>
          <div><dt>Python</dt><dd>{{ selectedPython || '-' }}</dd></div>
          <div><dt>平台标签</dt><dd class="mono">{{ selectedProfile?.platformTag ?? '-' }}</dd></div>
        </dl>
        <div class="compatibility-note"><strong>兼容求解</strong><p>若锁定版本没有目标平台 Wheel，系统可能升级或降级版本，并在结果中列出全部变化。</p></div>
        <StaticValidationNotice />
        <el-button class="build-submit" type="primary" :disabled="!canBuild" :loading="creating" @click="startBuild">开始构建</el-button>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.build-page { padding-bottom: 56px; }
.build-mode { display: flex; min-height: 38px; align-items: center; gap: 10px; padding: 0 12px; border: 1px solid var(--wf-border); border-radius: 6px; background: #fff; font-size: 12px; }
.build-mode span { color: var(--wf-muted); }.build-mode strong { color: var(--wf-cyan); }
.workflow-steps { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0 0 24px; padding: 0; border-block: 1px solid var(--wf-border); list-style: none; }
.workflow-steps li { display: flex; min-height: 68px; align-items: center; gap: 10px; padding: 10px 18px; color: #7c8791; }
.workflow-steps li + li { border-left: 1px solid var(--wf-border); }.workflow-steps li > span { display: grid; width: 28px; height: 28px; flex: 0 0 28px; place-items: center; border: 1px solid #bdc6ce; border-radius: 50%; font-size: 12px; font-weight: 750; }
.workflow-steps strong, .workflow-steps small { display: block; }.workflow-steps strong { font-size: 13px; }.workflow-steps small { margin-top: 2px; font-size: 11px; font-weight: 400; }
.workflow-steps li.active, .workflow-steps li.done { color: #0a7486; }.workflow-steps li.active > span, .workflow-steps li.done > span { border-color: var(--wf-cyan); background: var(--wf-cyan-soft); }
.page-error { margin-bottom: 18px; }.build-grid { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 24px; align-items: start; }.build-main { min-width: 0; }
.workspace-section { padding: 0 0 28px; }.workspace-section + .workspace-section { padding-top: 26px; border-top: 1px solid var(--wf-border); }
.section-heading { display: flex; min-height: 36px; align-items: flex-start; justify-content: space-between; margin-bottom: 13px; }.section-heading > div { display: flex; align-items: baseline; gap: 10px; }.section-heading h2, .build-summary h2 { margin: 0; font-size: 16px; }.section-number { color: var(--wf-cyan); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; font-weight: 800; }.section-state { color: var(--wf-cyan); font-size: 12px; }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }.upload-zone { display: grid; width: 100%; min-height: 150px; place-items: center; align-content: center; gap: 6px; border: 1px dashed #9eabb6; border-radius: 8px; background: #fff; color: #53616c; cursor: pointer; }.upload-zone:hover, .upload-zone.is-dragging { border-color: var(--wf-cyan); background: #f2fafb; color: #0c7e90; }.upload-zone:disabled { cursor: wait; opacity: .7; }.upload-zone strong { font-size: 14px; }.upload-zone span { color: var(--wf-muted); font-size: 12px; }
.file-summary { display: flex; min-height: 56px; align-items: center; gap: 10px; margin-top: 10px; padding: 8px 12px; border: 1px solid var(--wf-border); border-radius: 6px; background: #fff; }.file-summary div { min-width: 0; flex: 1; }.file-summary strong, .file-summary span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.file-summary strong { font-size: 13px; }.file-summary span { margin-top: 3px; color: var(--wf-muted); font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; }
.requirement-table { margin-top: 12px; border: 1px solid var(--wf-border); }.text-success { color: var(--wf-green); font-weight: 650; }.text-danger { color: var(--wf-red); font-weight: 650; }
.target-controls { display: grid; grid-template-columns: 1fr 1fr 190px; gap: 16px; }.target-field { min-width: 0; }.target-field label { display: block; margin-bottom: 7px; color: #4b5863; font-size: 12px; font-weight: 650; }.target-field :deep(.el-segmented) { width: 100%; }.target-field :deep(.el-segmented__item) { flex: 1; }.target-field :deep(.el-select) { width: 100%; }
.build-summary { position: sticky; top: 24px; padding: 20px; border: 1px solid var(--wf-border); border-radius: 8px; background: #fff; }.build-summary dl { margin: 16px 0; border-top: 1px solid var(--wf-border); }.build-summary dl div { display: grid; grid-template-columns: 88px minmax(0, 1fr); gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--wf-border); }.build-summary dt { color: var(--wf-muted); font-size: 12px; }.build-summary dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: #303c46; font-size: 12px; font-weight: 650; }.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px !important; }.compatibility-note { margin: 14px 0; padding: 12px; border-left: 3px solid var(--wf-cyan); background: #f0f7f8; }.compatibility-note strong { color: #0b7080; font-size: 12px; }.compatibility-note p { margin: 5px 0 0; color: #596772; font-size: 11px; line-height: 1.6; }.build-submit { width: 100%; height: 42px; margin-top: 16px; }
@media (max-width: 1050px) { .build-grid { grid-template-columns: 1fr; }.build-summary { position: static; }.target-controls { grid-template-columns: 1fr 1fr; }.target-field:last-child { grid-column: span 2; } }
@media (max-width: 600px) { .workflow-steps li { min-height: 54px; padding: 8px; }.workflow-steps small { display: none; }.workflow-steps li > span { width: 24px; height: 24px; flex-basis: 24px; }.build-mode { display: none; }.target-controls { grid-template-columns: 1fr; }.target-field:last-child { grid-column: auto; }.build-summary { padding: 16px; }.requirement-table :deep(.el-table__cell:nth-child(3)), .requirement-table :deep(.el-table__cell:nth-child(4)) { display: none; } }
</style>
