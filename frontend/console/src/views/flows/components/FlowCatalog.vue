<script setup lang="ts">
import { computed, ref } from 'vue'
import type { FlowDefinition } from '../../../api/flows'

const props = defineProps<{ definitions: FlowDefinition[]; loading: boolean; enabled: boolean }>()
const emit = defineEmits<{ start: [key: string] }>()
const query = ref('')
const filtered = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  return props.definitions.filter((definition) =>
    [definition.name, definition.key, definition.description].some((value) =>
      value.toLocaleLowerCase().includes(keyword),
    ),
  )
})
</script>

<template>
  <template v-if="enabled && definitions.length">
    <div class="module-form catalog-toolbar">
      <label
        >搜索流程<el-input v-model="query" placeholder="搜索名称、标识或说明" clearable
      /></label>
      <span>显示 {{ filtered.length }} / {{ definitions.length }} 个流程</span>
    </div>
    <div class="table-scroll">
      <el-table
        :fit="false"
        native-scrollbar
        :data="filtered"
        row-key="key"
        class="data-table"
        table-layout="auto"
      >
        <el-table-column label="流程" min-width="180">
          <template #default="{ row }"
            ><strong>{{ row.name }}</strong
            ><small>{{ row.key }}</small></template
          >
        </el-table-column>
        <el-table-column label="说明" min-width="240">
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="120">
          <template #default="{ row }">
            <el-button
              :disabled="loading"
              @click="emit('start', row.key)"
              native-type="button"
              type="primary"
              link
              >发起流程</el-button
            >
          </template>
        </el-table-column>
        <template #empty
          ><el-empty :image-size="64" description="没有匹配的流程，请调整搜索条件。"
        /></template>
      </el-table>
    </div>
  </template>
</template>

<style scoped>
.catalog-toolbar label {
  max-width: 360px;
  flex: 1;
}
</style>
