<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import ConsoleLayout from './layouts/ConsoleLayout.vue'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <ConsoleLayout />
  </el-config-provider>
</template>
