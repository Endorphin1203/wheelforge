<script setup lang="ts">
defineProps<{ title?: string }>()
</script>

<template>
  <section class="page-section">
    <header class="page-header">
      <div>
        <p class="page-eyebrow">WheelForge Console</p>
        <h1>{{ title ?? '功能准备中' }}</h1>
      </div>
    </header>
    <div class="empty-state">该工作区将在下一阶段接入现有 API。</div>
  </section>
</template>
