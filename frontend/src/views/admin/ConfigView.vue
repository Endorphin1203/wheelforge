<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import type { SystemConfigView } from '@/api/contracts'
import { listConfig, updateConfig } from '@/api/admin'
import { configFieldFor } from '@/features/admin/configFields'

const groups = ['上传与包限制', '任务与求解', '归档与保留'] as const
const configs = ref<SystemConfigView[]>([])
const drafts = reactive<Record<string, number | boolean>>({})
const loading = ref(false)
const savingKey = ref('')
const errorMessage = ref('')

const groupedConfigs = computed(() => groups.map((group) => ({
  group,
  items: configs.value.filter((item) => configFieldFor(item.key)?.group === group),
})))

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    configs.value = await listConfig()
    for (const item of configs.value) {
      if (typeof item.value === 'number' || typeof item.value === 'boolean') drafts[item.key] = item.value
    }
  } catch (error) { errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : '系统配置加载失败' }
  finally { loading.value = false }
}

async function save(item: SystemConfigView): Promise<void> {
  savingKey.value = item.key
  try {
    const updated = await updateConfig({ [item.key]: { value: drafts[item.key], version: item.version } })
    configs.value = configs.value.map((existing) => updated.find((next) => next.key === existing.key) ?? existing)
    ElMessage.success('配置已保存')
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      await load()
      ElMessage.warning('配置已被其他管理员更新，已刷新为服务器值')
    } else ElMessage.error(error instanceof ApiError ? error.message : '配置保存失败')
  } finally { savingKey.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="page-header"><div><p class="page-eyebrow">Runtime Policy</p><h1>系统配置</h1></div><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></header>
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage" />
    <div v-loading="loading" class="config-groups">
      <section v-for="section in groupedConfigs" :key="section.group" class="config-section"><h2>{{ section.group }}</h2><div class="config-list"><article v-for="item in section.items" :key="item.key" class="config-row"><div><strong>{{ configFieldFor(item.key)?.label }}</strong><code>{{ item.key }}</code><p>{{ item.description }}</p></div><div class="config-editor"><el-switch v-if="configFieldFor(item.key)?.type === 'boolean'" v-model="drafts[item.key]" :aria-label="configFieldFor(item.key)?.label" /><el-input-number v-else v-model="drafts[item.key]" :aria-label="configFieldFor(item.key)?.label" :min="configFieldFor(item.key)?.min" :max="configFieldFor(item.key)?.max" controls-position="right" /><span>{{ configFieldFor(item.key)?.unit }}</span></div><el-button type="primary" plain :loading="savingKey === item.key" @click="save(item)">保存</el-button></article></div></section>
    </div>
  </section>
</template>

<style scoped>
.config-groups { min-height: 240px; }.config-section + .config-section { margin-top: 28px; }.config-section h2 { margin: 0 0 8px; font-size: 14px; }.config-list { border-top: 1px solid var(--wf-border); }.config-row { display: grid; grid-template-columns: minmax(260px, 1fr) 250px 72px; gap: 18px; align-items: center; min-height: 92px; padding: 12px 8px; border-bottom: 1px solid var(--wf-border); }.config-row strong, .config-row code { display: inline-block; }.config-row strong { margin-right: 8px; font-size: 13px; }.config-row code { color: #687580; font-size: 9px; }.config-row p { margin: 5px 0 0; color: var(--wf-muted); font-size: 10px; }.config-editor { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }.config-editor :deep(.el-input-number) { width: 180px; }.config-editor > span { width: 52px; color: var(--wf-muted); font-size: 10px; }
@media (max-width: 700px) { .config-row { grid-template-columns: 1fr auto; }.config-editor { grid-column: 1; justify-content: flex-start; }.config-row > .el-button { grid-column: 2; grid-row: 2; }.config-editor :deep(.el-input-number) { width: min(190px, 55vw); } }
</style>
