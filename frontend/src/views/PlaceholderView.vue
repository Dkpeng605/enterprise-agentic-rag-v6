<script setup lang="ts">
defineProps<{ eyebrow: string; title: string; description: string }>()
</script>

<template>
  <section class="placeholder-panel">
    <p class="section-kicker">{{ eyebrow }}</p>
    <h1>{{ title }}</h1>
    <p>{{ description }}</p>
    <div class="placeholder-grid" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div>
  </section>
</template>
