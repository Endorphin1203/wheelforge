import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import { apiClient } from './api/client'
import { createWheelForgeRouter } from './router'
import { useAuthStore } from './stores/auth'
import './styles/base.css'

const app = createApp(App)
const pinia = createPinia()
const auth = useAuthStore(pinia)
auth.restore()
const router = createWheelForgeRouter(pinia)

apiClient.configure(
  () => auth.accessToken,
  () => {
    const redirect = router.currentRoute.value.fullPath
    auth.logout()
    void router.replace({ path: '/login', query: { redirect, reason: 'expired' } })
  },
)

app.use(pinia).use(router).use(ElementPlus).mount('#app')
