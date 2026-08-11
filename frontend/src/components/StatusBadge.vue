<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheckFilled, CircleCloseFilled, Loading, RemoveFilled, WarningFilled } from '@element-plus/icons-vue'

const props = defineProps<{ status: string }>()

const presentation = computed(() => {
  const labels: Record<string, string> = {
    CREATED: '已创建',
    PARSING: '解析中',
    QUEUED: '排队中',
    RESOLVING: '解析依赖',
    DOWNLOADING: '下载中',
    VALIDATING: '校验中',
    PACKAGING: '打包中',
    SUCCESS: '成功',
    PARTIAL_SUCCESS: '部分成功',
    FAILED: '失败',
    CANCELLED: '已取消',
    ACTIVE: '启用',
    DISABLED: '停用',
  }
  if (props.status === 'SUCCESS' || props.status === 'ACTIVE') return { label: labels[props.status], tone: 'success', icon: CircleCheckFilled }
  if (props.status === 'PARTIAL_SUCCESS') return { label: labels[props.status], tone: 'warning', icon: WarningFilled }
  if (props.status === 'FAILED' || props.status === 'DISABLED') return { label: labels[props.status], tone: 'danger', icon: CircleCloseFilled }
  if (props.status === 'CANCELLED') return { label: labels[props.status], tone: 'muted', icon: RemoveFilled }
  return { label: labels[props.status] ?? props.status, tone: 'running', icon: Loading }
})
</script>

<template>
  <span class="status-badge" :class="`status-badge--${presentation.tone}`">
    <el-icon :class="{ 'is-loading': presentation.tone === 'running' }"><component :is="presentation.icon" /></el-icon>
    {{ presentation.label }}
  </span>
</template>

<style scoped>
.status-badge { display: inline-flex; min-height: 24px; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 650; white-space: nowrap; }
.status-badge--success { color: #176a42; background: #e4f4eb; }
.status-badge--warning { color: #8a560d; background: #fff1d6; }
.status-badge--danger { color: #a82f35; background: #fde8e8; }
.status-badge--muted { color: #5f6973; background: #edf0f2; }
.status-badge--running { color: #0d7283; background: #e3f3f5; }
</style>
