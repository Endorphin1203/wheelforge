<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Back, Delete, Download, Refresh, RefreshRight, VideoPause } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import type { ArtifactView, BuildLogView, BuildTaskView, ResolvedPackageView, VersionComparisonRow } from '@/api/contracts'
import { listArtifacts } from '@/api/artifacts'
import { cancelBuildTask, deleteBuildTask, getBuildTask, listBuildLogs, listResolvedPackages, listVersionComparison, retryBuildTask } from '@/api/tasks'
import StaticValidationNotice from '@/components/StaticValidationNotice.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { usePolling } from '@/composables/usePolling'
import { downloadArtifact } from '@/features/artifacts/downloadArtifact'
import { formatBytes, formatDate, isTerminalStatus, presentVersionDirection } from '@/features/tasks/taskPresentation'

const route = useRoute()
const router = useRouter()
const taskId = computed(() => String(route.params.id))
const task = ref<BuildTaskView | null>(null)
const logs = ref<BuildLogView[]>([])
const packages = ref<ResolvedPackageView[]>([])
const comparisons = ref<VersionComparisonRow[]>([])
const artifacts = ref<ArtifactView[]>([])
const activeTab = ref('overview')
const loading = ref(true)
const action = ref('')
const errorMessage = ref('')
const terminal = computed(() => task.value ? isTerminalStatus(task.value.status) : false)
const canCancel = computed(() => task.value && !terminal.value && !task.value.cancelRequested)
const canRetry = computed(() => task.value && ['FAILED', 'CANCELLED'].includes(task.value.status))
const lastSequence = computed(() => logs.value.at(-1)?.sequence ?? 0)

function setError(error: unknown, fallback: string): void {
  errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : fallback
}

async function loadLogs(): Promise<void> {
  const next = await listBuildLogs(taskId.value, lastSequence.value)
  if (next.length) logs.value.push(...next.filter((item) => !logs.value.some((existing) => existing.sequence === item.sequence)))
}

async function loadEvidence(): Promise<void> {
  const [resolved, versionRows, allArtifacts] = await Promise.all([
    listResolvedPackages(taskId.value), listVersionComparison(taskId.value), listArtifacts(),
  ])
  packages.value = resolved
  comparisons.value = versionRows
  artifacts.value = allArtifacts.filter((artifact) => artifact.buildTaskId === taskId.value)
}

const polling = usePolling(
  () => getBuildTask(taskId.value),
  {
    intervalMs: 2000,
    isTerminal: (value) => isTerminalStatus(value.status),
    onData: async (value) => {
      task.value = value
      await loadLogs()
      if (isTerminalStatus(value.status)) await loadEvidence()
    },
    onError: (error) => setError(error, '任务状态刷新失败'),
  },
)

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  polling.stop()
  try {
    task.value = await getBuildTask(taskId.value)
    await loadLogs()
    if (isTerminalStatus(task.value.status)) await loadEvidence()
    else polling.start()
  } catch (error) { setError(error, '任务详情加载失败') }
  finally { loading.value = false }
}

async function cancelTask(): Promise<void> {
  action.value = 'cancel'
  try { task.value = await cancelBuildTask(taskId.value); ElMessage.success('已请求取消任务') }
  catch (error) { setError(error, '取消任务失败') }
  finally { action.value = '' }
}

async function retryTask(): Promise<void> {
  action.value = 'retry'
  try { const next = await retryBuildTask(taskId.value); await router.push(`/tasks/${next.id}`) }
  catch (error) { setError(error, '重试任务失败') }
  finally { action.value = '' }
}

async function removeTask(): Promise<void> {
  try {
    await ElMessageBox.confirm('删除后任务记录将不再显示，是否继续？', '删除任务', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch { return }
  action.value = 'delete'
  try { await deleteBuildTask(taskId.value); await router.push('/tasks') }
  catch (error) { setError(error, '删除任务失败') }
  finally { action.value = '' }
}

async function download(id: string): Promise<void> {
  action.value = `download:${id}`
  try { await downloadArtifact(id) }
  catch (error) { setError(error, error instanceof ApiError ? error.message : '产物下载失败') }
  finally { action.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page-section task-detail">
    <header class="detail-header">
      <div class="detail-title"><el-button text :icon="Back" aria-label="返回任务列表" @click="router.push('/tasks')" /><div><p class="page-eyebrow">Build Task</p><h1>任务详情</h1><code>{{ taskId }}</code></div></div>
      <div v-if="task" class="detail-actions"><StatusBadge :status="task.status" /><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button><el-button v-if="canCancel" :icon="VideoPause" :loading="action === 'cancel'" @click="cancelTask">取消</el-button><el-button v-if="canRetry" type="primary" :icon="RefreshRight" :loading="action === 'retry'" @click="retryTask">重试</el-button><el-button v-if="terminal" type="danger" plain :icon="Delete" :loading="action === 'delete'" @click="removeTask">删除</el-button></div>
    </header>
    <el-alert v-if="errorMessage" class="detail-error" type="error" :closable="false" show-icon :title="errorMessage" />
    <div v-if="task" class="task-progress"><div><span>{{ task.currentStage || '等待处理' }}</span><strong>{{ task.progress }}%</strong></div><el-progress :percentage="task.progress" :show-text="false" :stroke-width="8" /></div>

    <el-tabs v-if="task" v-model="activeTab" class="detail-tabs">
      <el-tab-pane label="概览" name="overview">
        <div class="overview-grid">
          <section><h2>目标环境</h2><dl class="evidence-list"><div><dt>操作系统</dt><dd>{{ task.targetSnapshot.os }}</dd></div><div><dt>CPU</dt><dd>{{ task.targetSnapshot.architecture }}</dd></div><div><dt>Python</dt><dd>{{ task.targetSnapshot.pythonVersion }} ({{ task.targetSnapshot.pythonFullVersion }})</dd></div><div><dt>平台标签</dt><dd><code>{{ task.targetSnapshot.platformTag }}</code></dd></div></dl></section>
          <section><h2>执行信息</h2><dl class="evidence-list"><div><dt>创建</dt><dd>{{ formatDate(task.createdAt) }}</dd></div><div><dt>开始</dt><dd>{{ formatDate(task.startedAt) }}</dd></div><div><dt>完成</dt><dd>{{ formatDate(task.finishedAt) }}</dd></div><div><dt>求解模式</dt><dd>兼容求解</dd></div></dl></section>
        </div>
        <section class="validation-evidence"><h2>验证证据</h2><div class="validation-facts"><span><small>验证级别</small><strong>{{ task.validationLevel }}</strong></span><span><small>安装验证</small><strong>{{ task.installVerified ? '已执行' : '未执行' }}</strong></span><span><small>验证说明</small><strong>{{ task.validationMessage }}</strong></span></div><StaticValidationNotice /></section>
        <el-alert v-if="task.failureMessage" type="error" :closable="false" show-icon :title="task.failureCode || '构建失败'" :description="task.failureMessage" />
      </el-tab-pane>
      <el-tab-pane :label="`依赖包 (${packages.length})`" name="dependencies">
        <el-table :data="packages" class="result-table" size="small"><el-table-column prop="normalizedName" label="包名" min-width="150" /><el-table-column prop="finalVersion" label="最终版本" width="120" /><el-table-column prop="dependencyType" label="类型" width="100" /><el-table-column prop="wheelFilename" label="Wheel" min-width="250" /><el-table-column prop="packageSource" label="来源" width="110" /><el-table-column prop="wheelStatus" label="状态" width="120" /><el-table-column prop="errorMessage" label="错误" min-width="180" /></el-table>
      </el-tab-pane>
      <el-tab-pane label="版本对比" name="comparison">
        <el-table :data="comparisons" class="result-table comparison-table" size="small"><el-table-column prop="packageName" label="包名" min-width="140" /><el-table-column prop="originalConstraint" label="原始约束" min-width="130" /><el-table-column prop="strictVersion" label="锁定版本" width="110" /><el-table-column prop="finalVersion" label="最终版本" width="110" /><el-table-column label="变化" width="100"><template #default="scope"><span class="direction" :class="`direction--${presentVersionDirection(scope.row.changeDirection).tone}`"><b>{{ presentVersionDirection(scope.row.changeDirection).symbol }}</b>{{ presentVersionDirection(scope.row.changeDirection).label }}</span></template></el-table-column><el-table-column prop="changeReason" label="原因" min-width="230" /><el-table-column prop="packageSource" label="来源" width="110" /></el-table>
      </el-tab-pane>
      <el-tab-pane :label="`日志 (${logs.length})`" name="logs"><div class="log-view"><div v-for="entry in logs" :key="entry.sequence" class="log-line"><span>{{ entry.sequence }}</span><time>{{ formatDate(entry.createdAt) }}</time><b :class="`log-${entry.level.toLowerCase()}`">{{ entry.level }}</b><em>{{ entry.stage }}</em><p>{{ entry.message }}</p></div><div v-if="!logs.length" class="log-empty">暂无构建日志</div></div></el-tab-pane>
      <el-tab-pane :label="`产物 (${artifacts.length})`" name="artifact"><article v-for="item in artifacts" :key="item.id" class="detail-artifact"><div><strong>{{ item.filename }}</strong><span>{{ formatBytes(item.sizeBytes) }} · 有效期 {{ formatDate(item.expiresAt) }}</span><code>{{ item.sha256 }}</code></div><el-button type="primary" :icon="Download" :loading="action === `download:${item.id}`" @click="download(item.id)">下载 ZIP</el-button></article><el-empty v-if="!artifacts.length" description="该任务暂无产物" /></el-tab-pane>
    </el-tabs>
    <el-skeleton v-else-if="loading" :rows="8" animated />
  </section>
</template>

<style scoped>
.detail-header { display: flex; min-height: 72px; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 16px; }.detail-title { display: flex; align-items: flex-start; gap: 4px; }.detail-title h1 { display: inline; margin: 3px 10px 0 0; font-size: 23px; }.detail-title code { color: var(--wf-muted); font-size: 10px; }.detail-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; }.detail-actions :deep(.el-button + .el-button) { margin-left: 0; }.detail-error { margin-bottom: 14px; }.task-progress { padding: 12px 16px; border: 1px solid var(--wf-border); border-radius: 6px; background: #fff; }.task-progress > div { display: flex; justify-content: space-between; margin-bottom: 7px; font-size: 12px; }.detail-tabs { margin-top: 18px; }.detail-tabs :deep(.el-tabs__header) { margin-bottom: 20px; }.overview-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }.overview-grid section, .validation-evidence { padding-bottom: 22px; }.overview-grid h2, .validation-evidence h2 { margin: 0 0 12px; font-size: 15px; }.evidence-list { margin: 0; border-top: 1px solid var(--wf-border); }.evidence-list div { display: grid; grid-template-columns: 110px 1fr; padding: 9px 0; border-bottom: 1px solid var(--wf-border); }.evidence-list dt { color: var(--wf-muted); font-size: 12px; }.evidence-list dd { margin: 0; font-size: 12px; font-weight: 650; overflow-wrap: anywhere; }.validation-evidence { border-top: 1px solid var(--wf-border); padding-top: 20px; }.validation-facts { display: grid; grid-template-columns: 140px 140px 1fr; gap: 12px; margin-bottom: 12px; }.validation-facts span { display: grid; min-height: 58px; align-content: center; padding: 8px 12px; border: 1px solid var(--wf-border); border-radius: 6px; background: #fff; }.validation-facts small { color: var(--wf-muted); font-size: 10px; }.validation-facts strong { margin-top: 4px; font-size: 12px; }.result-table { border: 1px solid var(--wf-border); }.direction { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; font-weight: 650; }.direction b { font-size: 15px; }.direction--up { color: #0c7787; }.direction--down { color: #a15c0a; }.direction--same { color: #4e5c67; }.direction--unknown { color: #7a8490; }.log-view { overflow: auto; min-height: 320px; max-height: 560px; border: 1px solid #343e46; border-radius: 6px; background: #20262d; color: #cbd4db; font: 11px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace; }.log-line { display: grid; grid-template-columns: 48px 150px 58px 100px minmax(320px, 1fr); gap: 8px; padding: 6px 10px; border-bottom: 1px solid #303942; }.log-line span, .log-line time, .log-line em { color: #82909a; font-style: normal; }.log-line p { margin: 0; white-space: pre-wrap; }.log-error { color: #ff8f93; }.log-warn { color: #f1bd61; }.log-info { color: #68c6d2; }.log-empty { display: grid; min-height: 318px; place-items: center; color: #83909a; }.detail-artifact { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 16px; border: 1px solid var(--wf-border); border-radius: 8px; background: #fff; }.detail-artifact strong, .detail-artifact span, .detail-artifact code { display: block; }.detail-artifact span { margin-top: 5px; color: var(--wf-muted); font-size: 11px; }.detail-artifact code { margin-top: 5px; color: #6a7680; font-size: 9px; overflow-wrap: anywhere; }
@media (max-width: 850px) { .detail-header { display: block; }.detail-actions { justify-content: flex-start; margin-top: 14px; }.overview-grid { grid-template-columns: 1fr; gap: 0; }.validation-facts { grid-template-columns: 1fr 1fr; }.validation-facts span:last-child { grid-column: 1 / -1; }.comparison-table :deep(.el-table__cell:nth-child(2)), .comparison-table :deep(.el-table__cell:nth-child(3)), .comparison-table :deep(.el-table__cell:nth-child(6)), .comparison-table :deep(.el-table__cell:nth-child(7)) { display: none; } }
@media (max-width: 560px) { .detail-title code { display: block; margin-top: 4px; }.detail-title h1 { display: block; }.detail-actions :deep(.el-button span) { display: none; }.detail-actions :deep(.el-button) { width: 36px; padding: 0; }.detail-artifact { align-items: flex-start; flex-direction: column; }.detail-artifact .el-button { width: 100%; } }
</style>
