<template>
  <section class="route-state">
    <p class="route-state__code">404</p>
    <h1>页面不存在</h1>
    <p>当前地址没有对应的 WheelForge 页面。</p>
    <el-button type="primary" @click="$router.push('/build/new')">返回构建工作台</el-button>
  </section>
</template>
