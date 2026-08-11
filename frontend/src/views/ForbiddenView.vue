<template>
  <section class="route-state">
    <p class="route-state__code">403</p>
    <h1>无权访问</h1>
    <p>当前账号没有访问此管理页面的权限。</p>
    <el-button type="primary" @click="$router.push('/build/new')">返回构建工作台</el-button>
  </section>
</template>
