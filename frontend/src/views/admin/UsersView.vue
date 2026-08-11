<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { CopyDocument, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import type { UserRole, UserView } from '@/api/contracts'
import { createUser, listUsers, setUserStatus } from '@/api/admin'
import StatusBadge from '@/components/StatusBadge.vue'
import { formatDate } from '@/features/tasks/taskPresentation'

const users = ref<UserView[]>([])
const loading = ref(false)
const rowLoading = ref('')
const createDialog = ref(false)
const passwordDialog = ref(false)
const creating = ref(false)
const generatedPassword = ref('')
const errorMessage = ref('')
const form = reactive<{ username: string; role: UserRole; initialPassword: string }>({ username: '', role: 'USER', initialPassword: '' })

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try { users.value = await listUsers() }
  catch (error) { errorMessage.value = error instanceof ApiError && error.code === 'NETWORK_ERROR' ? error.message : '用户列表加载失败' }
  finally { loading.value = false }
}

function openCreate(): void {
  Object.assign(form, { username: '', role: 'USER', initialPassword: '' })
  createDialog.value = true
}

async function submitCreate(): Promise<void> {
  if (!form.username.trim()) { ElMessage.warning('请输入用户名'); return }
  creating.value = true
  try {
    const created = await createUser({ username: form.username.trim(), role: form.role, initialPassword: form.initialPassword || null })
    users.value.unshift({ ...created, initialPassword: null })
    generatedPassword.value = created.initialPassword ?? form.initialPassword
    form.initialPassword = ''
    createDialog.value = false
    passwordDialog.value = true
  } catch (error) { ElMessage.error(error instanceof ApiError ? error.message : '用户创建失败') }
  finally { creating.value = false }
}

function closePassword(): void {
  passwordDialog.value = false
  generatedPassword.value = ''
}

async function copyPassword(): Promise<void> {
  try { await navigator.clipboard.writeText(generatedPassword.value); ElMessage.success('已复制') }
  catch { ElMessage.warning('无法访问剪贴板，请手动记录') }
}

async function toggleStatus(user: UserView): Promise<void> {
  rowLoading.value = user.id
  const next = user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE'
  try {
    const updated = await setUserStatus(user.id, next)
    users.value = users.value.map((item) => item.id === updated.id ? updated : item)
  } catch (error) { ElMessage.error(error instanceof ApiError ? error.message : '账号状态更新失败') }
  finally { rowLoading.value = '' }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="page-header"><div><p class="page-eyebrow">Access Control</p><h1>用户管理</h1></div><div class="header-actions"><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button><el-button type="primary" :icon="Plus" @click="openCreate">创建用户</el-button></div></header>
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage" />
    <el-table v-loading="loading" :data="users" class="admin-table">
      <el-table-column prop="username" label="用户名" min-width="180" />
      <el-table-column label="角色" width="120"><template #default="scope"><strong>{{ scope.row.role === 'ADMIN' ? '管理员' : '构建用户' }}</strong></template></el-table-column>
      <el-table-column label="状态" width="130"><template #default="scope"><StatusBadge :status="scope.row.status" /></template></el-table-column>
      <el-table-column label="创建时间" min-width="190"><template #default="scope">{{ formatDate(scope.row.createdAt) }}</template></el-table-column>
      <el-table-column label="操作" width="130" align="right"><template #default="scope"><el-button :type="scope.row.status === 'ACTIVE' ? 'danger' : 'success'" plain size="small" :loading="rowLoading === scope.row.id" @click="toggleStatus(scope.row)">{{ scope.row.status === 'ACTIVE' ? '停用' : '启用' }}</el-button></template></el-table-column>
    </el-table>

    <el-dialog v-model="createDialog" title="创建用户" width="min(92vw, 480px)" destroy-on-close>
      <div class="dialog-form"><label for="new-username">用户名</label><el-input id="new-username" v-model="form.username" aria-label="用户名" autocomplete="off" maxlength="80" /><label for="new-role">角色</label><el-select id="new-role" v-model="form.role" aria-label="角色"><el-option label="构建用户" value="USER" /><el-option label="管理员" value="ADMIN" /></el-select><label for="new-password">初始密码（可选）</label><el-input id="new-password" v-model="form.initialPassword" aria-label="初始密码" type="password" show-password autocomplete="new-password" placeholder="留空由系统生成" /><small>自定义密码需 12-128 位，并包含大小写字母、数字和特殊字符。</small></div>
      <template #footer><el-button @click="createDialog = false">取消</el-button><el-button type="primary" :loading="creating" @click="submitCreate">确认创建</el-button></template>
    </el-dialog>

    <el-dialog v-model="passwordDialog" title="记录初始密码" width="min(92vw, 520px)" :close-on-click-modal="false" :show-close="false" @closed="generatedPassword = ''">
      <el-alert type="warning" :closable="false" show-icon title="此密码只显示一次，关闭后无法再次查看。" />
      <div class="password-result"><code>{{ generatedPassword }}</code><el-tooltip content="复制密码"><el-button :icon="CopyDocument" aria-label="复制密码" @click="copyPassword" /></el-tooltip></div>
      <template #footer><el-button type="primary" @click="closePassword">我已记录</el-button></template>
    </el-dialog>
  </section>
</template>

<style scoped>
.header-actions { display: flex; gap: 8px; }.admin-table { border: 1px solid var(--wf-border); }.dialog-form { display: grid; gap: 8px; }.dialog-form label { margin-top: 6px; color: #45525d; font-size: 12px; font-weight: 650; }.dialog-form :deep(.el-select) { width: 100%; }.dialog-form small { color: var(--wf-muted); font-size: 11px; line-height: 1.5; }.password-result { display: flex; align-items: center; gap: 10px; margin-top: 16px; padding: 12px; border: 1px solid var(--wf-border); border-radius: 6px; background: #f5f7f8; }.password-result code { min-width: 0; flex: 1; overflow-wrap: anywhere; color: #1c3e46; font-size: 13px; font-weight: 700; }
@media (max-width: 600px) { .header-actions .el-button:first-child { display: none; }.admin-table :deep(.el-table__cell:nth-child(4)) { display: none; } }
</style>
