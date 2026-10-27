<script setup lang="ts">
import { computed, ref } from 'vue'
import { Search } from '@element-plus/icons-vue'
import type { Permission } from '../../../api/admin'
import { can } from '../../../stores/session'
import { moduleLabel, permissionModule } from '../permissions'

const props = defineProps<{ catalog: Permission[]; disabled?: boolean; readonly?: boolean }>()
const selected = defineModel<string[]>({ required: true })
const query = ref('')
const module = ref('')
const onlySelected = ref(false)
const selectedKeys = computed(() => new Set(selected.value))
const modules = computed(() => [...new Set(props.catalog.map(permissionModule))])
const filtered = computed(() => {
  const search = query.value.trim().toLowerCase()
  return props.catalog.filter(
    (permission) =>
      (!module.value || permissionModule(permission) === module.value) &&
      (!onlySelected.value || selectedKeys.value.has(permission.key)) &&
      `${permission.key} ${permission.name} ${permission.description} ${moduleLabel(permissionModule(permission))}`
        .toLowerCase()
        .includes(search),
  )
})
const selectable = computed(() => filtered.value.filter((permission) => can(permission.key)))
const canSelect = computed(() =>
  selectable.value.some((permission) => !selectedKeys.value.has(permission.key)),
)
const canClear = computed(() =>
  selectable.value.some((permission) => selectedKeys.value.has(permission.key)),
)

function selectFiltered() {
  if (props.disabled || props.readonly) return
  selected.value = [
    ...new Set([...selected.value, ...selectable.value.map((permission) => permission.key)]),
  ]
}
function clearFiltered() {
  if (props.disabled || props.readonly) return
  const removable = new Set(selectable.value.map((permission) => permission.key))
  selected.value = selected.value.filter((key) => !removable.has(key))
}
function toggle(permission: Permission, value: unknown) {
  if (props.disabled || props.readonly || !can(permission.key)) return
  selected.value =
    value === true
      ? [...new Set([...selected.value, permission.key])]
      : selected.value.filter((key) => key !== permission.key)
}
function reset() {
  query.value = ''
  module.value = ''
  onlySelected.value = false
}
</script>

<template>
  <section class="permission-picker" aria-label="角色权限配置" :aria-busy="disabled">
    <div class="permission-picker-heading">
      <h3>权限配置</h3>
      <el-tag effect="plain">已选 {{ selected.length }} 项权限</el-tag>
    </div>
    <div class="permission-filters">
      <el-input
        v-model="query"
        aria-label="搜索角色权限"
        placeholder="搜索权限名称、标识或说明"
        :prefix-icon="Search"
        clearable
        :disabled="disabled"
        @keydown.enter.prevent
      />
      <AppSelect
        v-model="module"
        aria-label="角色权限模块"
        placeholder="全部模块"
        :disabled="disabled"
      >
        <el-option label="全部模块" value="" />
        <el-option v-for="item in modules" :key="item" :label="moduleLabel(item)" :value="item" />
      </AppSelect>
      <el-checkbox v-model="onlySelected" :disabled="disabled">仅看已选</el-checkbox>
      <el-button
        :disabled="disabled || (!query && !module && !onlySelected)"
        @click="reset"
        native-type="button"
        text
        >清除筛选</el-button
      >
    </div>
    <div class="permission-selection-toolbar">
      <span role="status">显示 {{ filtered.length }} / {{ catalog.length }} 项权限</span>
      <div v-if="!readonly" class="row-actions">
        <el-button
          :disabled="disabled || !canSelect"
          @click="selectFiltered"
          native-type="button"
          link
          type="primary"
          >全选当前筛选</el-button
        >
        <el-button
          :disabled="disabled || !canClear"
          @click="clearFiltered"
          native-type="button"
          link
          >清除当前筛选</el-button
        >
      </div>
    </div>
    <div class="permission-table-scroll">
      <table class="permission-table" aria-label="权限列表">
        <thead>
          <tr>
            <th scope="col">授权</th>
            <th scope="col">权限名称</th>
            <th scope="col">权限标识</th>
            <th scope="col">所属模块</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="permission in filtered"
            :key="permission.key"
            :class="{ 'is-selected': selectedKeys.has(permission.key) }"
          >
            <td>
              <el-checkbox
                :model-value="selectedKeys.has(permission.key)"
                :aria-label="`${permission.name} ${permission.key}`"
                :disabled="disabled || readonly || !can(permission.key)"
                @change="toggle(permission, $event)"
              />
            </td>
            <td>
              <strong>{{ permission.name }}</strong>
              <p v-if="permission.description !== permission.name">{{ permission.description }}</p>
              <small v-if="!readonly && !can(permission.key)">当前账号不可授予</small>
            </td>
            <td>
              <code>{{ permission.key }}</code>
            </td>
            <td>
              <span class="permission-module">{{ moduleLabel(permissionModule(permission)) }}</span>
            </td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="4">
              <el-empty
                :image-size="48"
                :description="catalog.length ? '没有符合筛选条件的权限' : '暂无可配置的权限'"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="permission-picker-note">
      {{
        readonly
          ? '当前为只读查看，已勾选项为该角色拥有的权限。'
          : '仅能授予自己拥有的权限。批量操作只影响当前筛选，切换筛选会保留已选项。'
      }}
    </p>
  </section>
</template>

<style scoped>
.permission-picker {
  min-width: 0;
}
.permission-picker-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.permission-picker-heading h3 {
  margin: 0;
  font-size: 15px;
}
.permission-filters {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) 170px auto auto;
  align-items: center;
  gap: 12px;
}
.permission-filters .el-checkbox {
  margin-right: 0;
}
.permission-selection-toolbar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin: 16px 0 12px;
  color: #617084;
  font-size: 12px;
}
.permission-table-scroll {
  overflow-x: auto;
  border: 1px solid #e5eaf0;
  border-radius: 6px;
}
.permission-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 13px;
}
.permission-table th {
  background: #f6f8fa;
  color: #617084;
  font-weight: 500;
  white-space: nowrap;
}
.permission-table th,
.permission-table td {
  padding: 11px 14px;
  border-bottom: 1px solid #edf0f4;
}
.permission-table tr:last-child td {
  border-bottom: 0;
}
.permission-table th:first-child,
.permission-table td:first-child {
  width: 54px;
  text-align: center;
}
.permission-table strong {
  font-size: 13px;
  font-weight: 500;
}
.permission-table td:last-child {
  white-space: nowrap;
}
.permission-module {
  display: inline-block;
  border: 1px solid #e5eaf0;
  border-radius: 4px;
  padding: 2px 7px;
  color: #617084;
  background: #f6f8fa;
  font-size: 12px;
}
.permission-table .is-selected {
  background: #f0f7f3;
}
.permission-table code {
  color: #66768b;
}
.permission-table small {
  display: block;
  color: #9a7b48;
}
.permission-picker-note {
  margin-top: 10px;
  font-size: 12px;
}
@media (max-width: 640px) {
  .permission-filters {
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 10px;
  }
  .permission-filters > .el-input {
    grid-column: 1 / -1;
  }
  .permission-filters > .el-button {
    justify-self: start;
    margin-left: 0;
  }
  .permission-table th,
  .permission-table td {
    padding: 10px;
  }
  .permission-table {
    min-width: 540px;
  }
}
</style>
