<template>
  <el-alert
    class="validation-notice"
    type="warning"
    :closable="false"
    show-icon
    title="静态兼容性校验，不执行目标环境安装，不能证明目标机器一定安装成功。"
  />
</template>

<style scoped>
.validation-notice { border: 1px solid #efd59d; border-radius: 6px; }
</style>
