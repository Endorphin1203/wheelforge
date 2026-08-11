<script setup lang="ts">
import { computed, ref } from 'vue'
import { Box, Close, Connection, Files, Menu, Plus, Setting, SwitchButton, Tickets, User } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const drawerOpen = ref(false)

const userItems = [
  { path: '/build/new', label: '新建构建', icon: Plus },
  { path: '/tasks', label: '构建任务', icon: Tickets },
  { path: '/artifacts', label: '产物管理', icon: Files },
]
const adminItems = [
  { path: '/admin/users', label: '用户管理', icon: User },
  { path: '/admin/sources', label: '下载源', icon: Connection },
  { path: '/admin/config', label: '系统配置', icon: Setting },
]
const accountLabel = computed(() => auth.isAdmin ? '管理员' : '构建用户')

async function navigate(path: string): Promise<void> {
  drawerOpen.value = false
  await router.push(path)
}

async function logout(): Promise<void> {
  auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <div class="app-shell">
    <aside class="nav-rail" aria-label="主导航">
      <div class="rail-brand"><span>WF</span><strong>WheelForge</strong></div>
      <nav class="rail-nav">
        <button
          v-for="item in userItems"
          :key="item.path"
          type="button"
          :class="{ active: route.path === item.path || (item.path === '/tasks' && route.path.startsWith('/tasks/')) }"
          @click="navigate(item.path)"
        >
          <el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span>
        </button>
        <p v-if="auth.isAdmin" class="rail-section-label">管理</p>
        <button
          v-for="item in auth.isAdmin ? adminItems : []"
          :key="item.path"
          type="button"
          :class="{ active: route.path === item.path }"
          @click="navigate(item.path)"
        >
          <el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="rail-account">
        <div><el-icon><Box /></el-icon><span>{{ accountLabel }}</span></div>
        <el-tooltip content="退出登录" placement="top">
          <button type="button" aria-label="退出登录" @click="logout"><el-icon><SwitchButton /></el-icon></button>
        </el-tooltip>
      </div>
    </aside>

    <header class="mobile-header">
      <el-tooltip content="打开导航" placement="bottom">
        <button type="button" class="icon-button" aria-label="打开导航" @click="drawerOpen = true"><el-icon><Menu /></el-icon></button>
      </el-tooltip>
      <div class="mobile-brand"><span>WF</span><strong>WheelForge</strong></div>
      <el-tooltip content="退出登录" placement="bottom">
        <button type="button" class="icon-button" aria-label="退出登录" @click="logout"><el-icon><SwitchButton /></el-icon></button>
      </el-tooltip>
    </header>

    <main class="app-content"><router-view /></main>

    <el-drawer v-model="drawerOpen" direction="ltr" size="min(86vw, 300px)" :show-close="false" class="mobile-drawer">
      <template #header>
        <div class="drawer-heading"><div class="mobile-brand"><span>WF</span><strong>WheelForge</strong></div><button type="button" class="icon-button" aria-label="关闭导航" @click="drawerOpen = false"><el-icon><Close /></el-icon></button></div>
      </template>
      <nav class="drawer-nav" aria-label="移动端主导航">
        <button v-for="item in userItems" :key="item.path" type="button" :class="{ active: route.path.startsWith(item.path.replace('/new', '')) }" @click="navigate(item.path)"><el-icon><component :is="item.icon" /></el-icon>{{ item.label }}</button>
        <p v-if="auth.isAdmin" class="rail-section-label">管理</p>
        <button v-for="item in auth.isAdmin ? adminItems : []" :key="item.path" type="button" :class="{ active: route.path === item.path }" @click="navigate(item.path)"><el-icon><component :is="item.icon" /></el-icon>{{ item.label }}</button>
      </nav>
    </el-drawer>
  </div>
</template>

<style scoped>
.app-shell { min-height: 100vh; background: var(--wf-canvas); }
.nav-rail { position: fixed; inset: 0 auto 0 0; z-index: 20; display: flex; width: 216px; flex-direction: column; background: var(--wf-rail); color: #dce3e8; }
.rail-brand { display: flex; height: 72px; align-items: center; gap: 10px; padding: 0 20px; border-bottom: 1px solid #39424a; }
.rail-brand span, .mobile-brand span { display: grid; width: 32px; height: 32px; place-items: center; border: 1px solid #4f5b65; border-radius: 6px; color: #62cbd7; font-size: 12px; font-weight: 800; }
.rail-brand strong, .mobile-brand strong { font-size: 16px; letter-spacing: 0; }
.rail-nav { display: flex; flex: 1; flex-direction: column; gap: 4px; padding: 18px 12px; }
.rail-nav button, .drawer-nav button { display: flex; width: 100%; min-height: 40px; align-items: center; gap: 11px; border: 0; border-radius: 6px; background: transparent; color: #b9c3ca; cursor: pointer; text-align: left; }
.rail-nav button { padding: 0 12px; }
.rail-nav button:hover { background: #29323a; color: #fff; }
.rail-nav button.active { background: #163f48; color: #74d4de; box-shadow: inset 3px 0 #36b8c9; }
.rail-section-label { margin: 18px 12px 6px; color: #788791; font-size: 11px; font-weight: 700; text-transform: uppercase; }
.rail-account { display: flex; height: 66px; align-items: center; justify-content: space-between; padding: 0 14px 0 20px; border-top: 1px solid #39424a; color: #aeb8c0; font-size: 12px; }
.rail-account div { display: flex; align-items: center; gap: 8px; }
.rail-account button, .icon-button { display: grid; width: 36px; height: 36px; place-items: center; border: 0; border-radius: 6px; background: transparent; color: inherit; cursor: pointer; }
.rail-account button:hover { background: #303942; color: #fff; }
.app-content { min-height: 100vh; margin-left: 216px; }
.mobile-header { display: none; }
.mobile-drawer { display: none; }
.drawer-heading, .mobile-brand { display: flex; align-items: center; gap: 10px; }
.drawer-heading { width: 100%; justify-content: space-between; }
.drawer-nav { display: flex; flex-direction: column; gap: 5px; }
.drawer-nav button { padding: 0 12px; color: #34424d; }
.drawer-nav button.active { background: var(--wf-cyan-soft); color: #0a7486; }
@media (max-width: 767px) {
  .nav-rail { display: none; }
  .mobile-header { position: sticky; top: 0; z-index: 15; display: flex; height: 58px; align-items: center; justify-content: space-between; padding: 0 12px; border-bottom: 1px solid var(--wf-border); background: #fff; }
  .mobile-header .icon-button { color: #34424d; }
  .app-content { margin-left: 0; }
  :global(.mobile-drawer) { display: block; }
}
</style>
