<script setup lang="ts">
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import AdminLayout from './layouts/AdminLayout.vue'
</script>

<template>
  <el-config-provider :locale="zhCn">
    <AdminLayout />
  </el-config-provider>
</template>
