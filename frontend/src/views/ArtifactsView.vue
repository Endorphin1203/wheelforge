<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Download, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import type { ArtifactView } from '@/api/contracts'
import { listArtifacts } from '@/api/artifacts'
import StatusBadge from '@/components/StatusBadge.vue'
import { downloadArtifact } from '@/features/artifacts/downloadArtifact'
import { formatBytes, formatDate } from '@/features/tasks/taskPresentation'

const artifacts = ref<ArtifactView[]>([])
const loading = ref(false)
const downloadingId = ref('')
const errorMessage = ref('')

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try { artifacts.value = await listArtifacts() }
  catch (error) { errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : '产物列表加载失败' }
  finally { loading.value = false }
}

async function download(item: ArtifactView): Promise<void> {
  downloadingId.value = item.id
  try { await downloadArtifact(item.id); ElMessage.success('下载已开始') }
  catch (error) { ElMessage.error(error instanceof ApiError ? error.message : '下载失败') }
  finally { downloadingId.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="page-header"><div><p class="page-eyebrow">Artifact Delivery</p><h1>产物管理</h1></div><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button></header>
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage" />
    <div v-loading="loading" class="artifact-list">
      <article v-for="item in artifacts" :key="item.id" class="artifact-row">
        <div class="artifact-main"><div class="zip-mark">ZIP</div><div><strong>{{ item.filename }}</strong><code>{{ item.sha256 }}</code></div></div>
        <dl><div><dt>大小</dt><dd>{{ formatBytes(item.sizeBytes) }}</dd></div><div><dt>有效期</dt><dd>{{ formatDate(item.expiresAt) }}</dd></div><div><dt>下载</dt><dd>{{ item.downloadCount }} 次</dd></div></dl>
        <div class="artifact-actions"><StatusBadge :status="item.buildStatus" /><el-button type="primary" :icon="Download" :loading="downloadingId === item.id" @click="download(item)">下载</el-button></div>
      </article>
      <el-empty v-if="!loading && !artifacts.length" description="暂无可下载产物" />
    </div>
  </section>
</template>

<style scoped>
.artifact-list { min-height: 240px; border-top: 1px solid var(--wf-border); }.artifact-row { display: grid; grid-template-columns: minmax(260px, 1.2fr) 1fr auto; gap: 20px; align-items: center; min-height: 92px; padding: 14px 8px; border-bottom: 1px solid var(--wf-border); }.artifact-main { display: flex; min-width: 0; align-items: center; gap: 12px; }.zip-mark { display: grid; width: 42px; height: 46px; flex: 0 0 42px; place-items: center; border: 1px solid #92a1ad; border-radius: 5px; color: #566571; font: 700 10px ui-monospace, SFMono-Regular, Menlo, monospace; }.artifact-main > div:last-child { min-width: 0; }.artifact-main strong, .artifact-main code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.artifact-main strong { font-size: 13px; }.artifact-main code { margin-top: 5px; color: var(--wf-muted); font-size: 9px; }.artifact-row dl { display: grid; grid-template-columns: repeat(3, 1fr); margin: 0; }.artifact-row dl div { padding: 0 10px; border-left: 1px solid var(--wf-border); }.artifact-row dt { color: var(--wf-muted); font-size: 10px; }.artifact-row dd { margin: 4px 0 0; font-size: 12px; font-weight: 650; }.artifact-actions { display: flex; align-items: center; gap: 12px; }
@media (max-width: 900px) { .artifact-row { grid-template-columns: 1fr auto; }.artifact-row dl { grid-column: 1 / -1; grid-row: 2; }.artifact-row dl div:first-child { border-left: 0; }.artifact-actions { grid-column: 2; grid-row: 1; } }
@media (max-width: 560px) { .artifact-row { grid-template-columns: 1fr; }.artifact-actions { grid-column: 1; grid-row: 3; justify-content: space-between; }.artifact-row dl { grid-column: 1; }.artifact-main code { max-width: 240px; } }
</style>
