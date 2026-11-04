import type { Permission } from '../../api/admin'

const moduleNames = new Map([
  ['identity', '身份与访问'],
  ['dictionary', '业务字典'],
  ['model', '模型接入'],
  ['flow', 'Flow 流程'],
  ['audit', '操作审计'],
])

export function permissionModule(permission: Permission): string {
  const resource = permission.key.split(':')[0]!.split('.')[0]!
  if (resource === 'user' || resource === 'role') return 'identity'
  if (resource === 'model_invocation') return 'model'
  return resource
}

export function moduleLabel(module: string): string {
  return moduleNames.get(module) ?? module
}
