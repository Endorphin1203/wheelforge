<script setup lang="ts">
import { ref } from 'vue'
import { Lock, User } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref(route.query.reason === 'expired' ? '登录已过期，请重新登录' : '')
const traceId = ref('')

async function submit(): Promise<void> {
  if (!username.value.trim() || !password.value) {
    errorMessage.value = '请输入用户名和密码'
    return
  }
  submitting.value = true
  errorMessage.value = ''
  traceId.value = ''
  try {
    await auth.login(username.value.trim(), password.value)
    const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/')
      ? route.query.redirect
      : '/build/new'
    await router.replace(redirect)
  } catch (error) {
    if (error instanceof ApiError && error.code === 'AUTHENTICATION_FAILED') {
      errorMessage.value = '用户名或密码错误'
    } else if (error instanceof ApiError) {
      errorMessage.value = error.code === 'NETWORK_ERROR' ? error.message : '登录失败，请稍后重试'
      traceId.value = error.traceId ?? ''
    } else {
      errorMessage.value = '登录响应无效，请联系管理员'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand" aria-labelledby="login-product-name">
      <div class="login-brand__content">
        <div class="login-brand__mark" aria-hidden="true">WF</div>
        <p>多架构 Python 离线依赖构建平台</p>
        <h1 id="login-product-name">WheelForge</h1>
        <dl class="login-facts">
          <div><dt>目标系统</dt><dd>Linux / Windows</dd></div>
          <div><dt>CPU 架构</dt><dd>x86_64 / ARM64</dd></div>
          <div><dt>Python</dt><dd>3.9 - 3.13</dd></div>
        </dl>
      </div>
    </section>
    <section class="login-form-section" aria-labelledby="login-title">
      <form class="login-form" @submit.prevent="submit">
        <div class="login-form__heading">
          <p class="page-eyebrow">Build Console</p>
          <h2 id="login-title">登录构建控制台</h2>
          <p>使用平台账号继续。</p>
        </div>
        <label class="field-label" for="username">用户名</label>
        <el-input id="username" v-model="username" aria-label="用户名" autocomplete="username" :prefix-icon="User" />
        <label class="field-label" for="password">密码</label>
        <el-input
          id="password"
          v-model="password"
          aria-label="密码"
          type="password"
          autocomplete="current-password"
          show-password
          :prefix-icon="Lock"
          @keyup.enter="submit"
        />
        <el-alert v-if="errorMessage" type="error" :closable="false" show-icon :title="errorMessage">
          <template v-if="traceId" #default>错误跟踪号：<code>{{ traceId }}</code></template>
        </el-alert>
        <el-button class="login-submit" type="primary" native-type="submit" :loading="submitting">登录</el-button>
      </form>
    </section>
  </main>
</template>

<style scoped>
.login-page { display: grid; min-height: 100vh; grid-template-columns: minmax(320px, 44%) 1fr; background: #fff; }
.login-brand { display: flex; align-items: center; padding: clamp(36px, 6vw, 88px); background: #20262d; color: #fff; }
.login-brand__content { width: min(100%, 520px); }
.login-brand__mark { display: grid; width: 54px; height: 54px; place-items: center; border: 1px solid #4b5964; border-radius: 8px; color: #62cbd7; font-weight: 800; }
.login-brand p { margin: 24px 0 6px; color: #aeb8c0; font-size: 14px; }
.login-brand h1 { margin: 0; font-size: 42px; color: #fff; }
.login-facts { display: grid; gap: 0; margin: 48px 0 0; border-top: 1px solid #404a53; }
.login-facts div { display: grid; grid-template-columns: 110px 1fr; padding: 14px 0; border-bottom: 1px solid #404a53; }
.login-facts dt { color: #88959f; font-size: 13px; }
.login-facts dd { margin: 0; color: #e7ecef; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
.login-form-section { display: grid; place-items: center; padding: 32px; background: #f7f8f9; }
.login-form { display: grid; width: min(100%, 400px); gap: 10px; padding: 32px; border: 1px solid var(--wf-border); border-radius: 8px; background: #fff; box-shadow: 0 16px 44px rgb(29 39 48 / 8%); }
.login-form__heading { margin-bottom: 12px; }
.login-form__heading h2 { margin: 5px 0 6px; font-size: 24px; }
.login-form__heading p:last-child { margin: 0; color: var(--wf-muted); font-size: 14px; }
.field-label { margin-top: 4px; color: #35424d; font-size: 13px; font-weight: 650; }
.login-submit { width: 100%; height: 40px; margin-top: 8px; }
code { font-size: 11px; }
@media (max-width: 767px) {
  .login-page { grid-template-columns: 1fr; background: #f7f8f9; }
  .login-brand { min-height: 220px; align-items: flex-start; padding: 28px 24px; }
  .login-brand h1 { font-size: 34px; }
  .login-brand p { margin-top: 18px; }
  .login-facts { display: none; }
  .login-form-section { place-items: start stretch; padding: 0 16px 24px; }
  .login-form { max-width: 500px; margin: -44px auto 0; padding: 24px; }
}
</style>
