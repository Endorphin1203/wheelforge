import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'
import { defineComponent, h } from 'vue'
import type { Pinia } from 'pinia'

import AppShell from '@/layouts/AppShell.vue'
import { useAuthStore } from '@/stores/auth'
import BuildView from '@/views/BuildView.vue'
import ForbiddenView from '@/views/ForbiddenView.vue'
import LoginView from '@/views/LoginView.vue'
import NotFoundView from '@/views/NotFoundView.vue'
import PlaceholderView from '@/views/PlaceholderView.vue'

const placeholder = (title: string) => defineComponent({
  name: 'RoutePlaceholder',
  setup: () => () => h(PlaceholderView, { title }),
})

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: LoginView },
  { path: '/forbidden', name: 'forbidden', component: ForbiddenView },
  {
    path: '/',
    component: AppShell,
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/build/new' },
      { path: 'build/new', component: BuildView },
      { path: 'tasks', component: placeholder('构建任务') },
      { path: 'tasks/:id', component: placeholder('任务详情') },
      { path: 'artifacts', component: placeholder('产物管理') },
      { path: 'admin/users', component: placeholder('用户管理'), meta: { adminOnly: true } },
      { path: 'admin/sources', component: placeholder('下载源'), meta: { adminOnly: true } },
      { path: 'admin/config', component: placeholder('系统配置'), meta: { adminOnly: true } },
    ],
  },
  { path: '/:pathMatch(.*)*', component: NotFoundView },
]

export function createWheelForgeRouter(pinia: Pinia) {
  const router = createRouter({ history: createWebHashHistory(), routes })
  router.beforeEach((to) => {
    const auth = useAuthStore(pinia)
    if (to.meta.requiresAuth && !auth.isAuthenticated) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }
    if (to.meta.adminOnly && !auth.isAdmin) return '/forbidden'
    if (to.path === '/login' && auth.isAuthenticated) return '/build/new'
    return true
  })
  return router
}
