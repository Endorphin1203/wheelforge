<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import type { PackageSourceView } from '@/api/contracts'
import { listSources, updateSource } from '@/api/admin'
import { formatDate } from '@/features/tasks/taskPresentation'

const sources = ref<PackageSourceView[]>([])
const loading = ref(false)
const savingId = ref('')
const errorMessage = ref('')

async function load(): Promise<void> {
  loading.value = true
  try { sources.value = (await listSources()).map((item) => ({ ...item })) }
  catch (error) { errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : '下载源加载失败' }
  finally { loading.value = false }
}

async function save(source: PackageSourceView): Promise<void> {
  savingId.value = source.id
  try {
    const updated = await updateSource(source.id, { enabled: source.enabled, priorityNo: source.priorityNo, timeoutSeconds: source.timeoutSeconds })
    sources.value = sources.value.map((item) => item.id === updated.id ? { ...updated } : item)
    ElMessage.success('下载源配置已保存')
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '下载源保存失败')
    await load()
  } finally { savingId.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="page-header"><div><p class="page-eyebrow">Package Index</p><h1>下载源</h1></div><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></header>
    <p class="page-intro">构建按优先级尝试已启用的内置源。源地址由平台固定，不接受任意 URL。</p>
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage" />
    <div v-loading="loading" class="source-list">
      <article v-for="source in sources" :key="source.id" class="source-row">
        <div class="source-identity"><div><strong>{{ source.displayName }}</strong><span>{{ source.code }}</span></div><code>{{ source.baseUrl }}</code><small>更新时间 {{ formatDate(source.updatedAt) }} · 失败计数 {{ source.failureCount }}</small></div>
        <div class="source-control"><label :for="`priority-${source.id}`">优先级</label><el-input-number :id="`priority-${source.id}`" v-model="source.priorityNo" :min="1" :max="1000" controls-position="right" /></div>
        <div class="source-control"><label :for="`timeout-${source.id}`">超时（秒）</label><el-input-number :id="`timeout-${source.id}`" v-model="source.timeoutSeconds" :min="1" :max="300" controls-position="right" /></div>
        <div class="source-enabled"><el-switch v-model="source.enabled" :aria-label="`启用${source.displayName}`" inline-prompt active-text="启用" inactive-text="停用" /><el-button type="primary" plain :loading="savingId === source.id" @click="save(source)">保存</el-button></div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.page-intro { margin: -10px 0 18px; color: var(--wf-muted); font-size: 12px; }.source-list { min-height: 220px; border-top: 1px solid var(--wf-border); }.source-row { display: grid; grid-template-columns: minmax(300px, 1fr) 140px 140px 150px; gap: 18px; align-items: center; min-height: 112px; padding: 16px 8px; border-bottom: 1px solid var(--wf-border); }.source-identity { min-width: 0; }.source-identity > div { display: flex; align-items: center; gap: 8px; }.source-identity strong { font-size: 14px; }.source-identity span { padding: 2px 5px; border-radius: 4px; background: #e9edf0; color: #5c6873; font: 9px ui-monospace, SFMono-Regular, Menlo, monospace; }.source-identity code, .source-identity small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.source-identity code { margin-top: 7px; color: #26616b; font-size: 11px; }.source-identity small { margin-top: 7px; color: var(--wf-muted); font-size: 10px; }.source-control label { display: block; margin-bottom: 6px; color: var(--wf-muted); font-size: 10px; }.source-control :deep(.el-input-number) { width: 100%; }.source-enabled { display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
@media (max-width: 950px) { .source-row { grid-template-columns: 1fr 130px 130px; }.source-enabled { grid-column: 1 / -1; justify-content: flex-end; } }
@media (max-width: 620px) { .source-row { grid-template-columns: 1fr 1fr; }.source-identity { grid-column: 1 / -1; }.source-enabled { grid-column: 1 / -1; }.source-identity code { white-space: normal; overflow-wrap: anywhere; } }
</style>
