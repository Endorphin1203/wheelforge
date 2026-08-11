<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh, Search } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import type { BuildTaskView } from '@/api/contracts'
import { listBuildTasks } from '@/api/tasks'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDate } from '@/features/tasks/taskPresentation'

const router = useRouter()
const tasks = ref<BuildTaskView[]>([])
const loading = ref(false)
const errorMessage = ref('')
const search = ref('')
const status = ref('ALL')
const statusOptions = ['ALL', 'QUEUED', 'DOWNLOADING', 'SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'CANCELLED']

const filteredTasks = computed(() => {
  const query = search.value.trim().toLowerCase()
  return tasks.value.filter((task) => {
    const matchesStatus = status.value === 'ALL' || task.status === status.value
    const searchable = `${task.id} ${task.targetSnapshot.profileCode} ${task.targetSnapshot.os} ${task.targetSnapshot.architecture}`.toLowerCase()
    return matchesStatus && (!query || searchable.includes(query))
  })
})

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    tasks.value = await listBuildTasks()
  } catch (error) {
    errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : '任务列表加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="page-header"><div><p class="page-eyebrow">Build History</p><h1>构建任务</h1></div><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></header>
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage" />
    <div class="table-toolbar">
      <el-input v-model="search" aria-label="搜索任务" clearable :prefix-icon="Search" placeholder="搜索任务 ID 或目标平台" />
      <el-select v-model="status" aria-label="任务状态"><el-option v-for="item in statusOptions" :key="item" :label="item === 'ALL' ? '全部状态' : item" :value="item" /></el-select>
      <span>{{ filteredTasks.length }} 项</span>
    </div>
    <el-table v-loading="loading" :data="filteredTasks" class="data-table" row-key="id" @row-click="row => router.push(`/tasks/${row.id}`)">
      <el-table-column label="状态" width="122"><template #default="scope"><StatusBadge :status="scope.row.status" /></template></el-table-column>
      <el-table-column label="目标平台" min-width="220"><template #default="scope"><strong>{{ scope.row.targetSnapshot.os }} · {{ scope.row.targetSnapshot.architecture }}</strong><small>Python {{ scope.row.targetSnapshot.pythonVersion }} · {{ scope.row.targetSnapshot.profileCode }}</small></template></el-table-column>
      <el-table-column label="进度" width="150"><template #default="scope"><el-progress :percentage="scope.row.progress" :stroke-width="7" /></template></el-table-column>
      <el-table-column label="任务 ID" min-width="180"><template #default="scope"><code>{{ scope.row.id }}</code></template></el-table-column>
      <el-table-column label="创建时间" width="180"><template #default="scope">{{ formatDate(scope.row.createdAt) }}</template></el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.table-toolbar { display: grid; grid-template-columns: minmax(220px, 360px) 170px 1fr; gap: 10px; align-items: center; margin-bottom: 12px; }.table-toolbar > span { justify-self: end; color: var(--wf-muted); font-size: 12px; }.data-table { border: 1px solid var(--wf-border); }.data-table :deep(.el-table__row) { cursor: pointer; }.data-table strong, .data-table small { display: block; }.data-table strong { font-size: 13px; }.data-table small { margin-top: 3px; color: var(--wf-muted); font-size: 11px; }.data-table code { font-size: 11px; }
@media (max-width: 767px) { .table-toolbar { grid-template-columns: 1fr 135px; }.table-toolbar > span { display: none; }.data-table :deep(.el-table__cell:nth-child(3)), .data-table :deep(.el-table__cell:nth-child(4)), .data-table :deep(.el-table__cell:nth-child(5)) { display: none; } }
</style>
