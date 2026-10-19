import type { App } from 'vue'
import {
  ElAlert,
  ElAvatar,
  ElBreadcrumb,
  ElBreadcrumbItem,
  ElButton,
  ElCard,
  ElCheckbox,
  ElCheckboxGroup,
  ElConfigProvider,
  ElDescriptions,
  ElDescriptionsItem,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
  ElEmpty,
  ElIcon,
  ElInput,
  ElOption,
  ElSkeleton,
  ElTable,
  ElTableColumn,
  ElTabPane,
  ElTabs,
  ElTag,
  ElTimeline,
  ElTimelineItem,
} from 'element-plus'
import AppSelect from './components/AppSelect.vue'
import StatusTag from './components/StatusTag.vue'

export const ui = {
  install(app: App) {
    for (const component of [
      ElAlert,
      ElAvatar,
      ElBreadcrumb,
      ElBreadcrumbItem,
      ElButton,
      ElCard,
      ElCheckbox,
      ElCheckboxGroup,
      ElConfigProvider,
      ElDescriptions,
      ElDescriptionsItem,
      ElDropdown,
      ElDropdownItem,
      ElDropdownMenu,
      ElEmpty,
      ElIcon,
      ElInput,
      ElOption,
      ElSkeleton,
      ElTable,
      ElTableColumn,
      ElTabPane,
      ElTabs,
      ElTag,
      ElTimeline,
      ElTimelineItem,
    ])
      app.component(component.name!, component)
    app.component('AppSelect', AppSelect)
    app.component('StatusTag', StatusTag)
  },
}
