import { request } from './client'

export interface FlowModelDefault {
  definition_key: string
  definition_name: string
  role: string
  role_label: string
  operation: string
  delivery: string
  model_key: string | null
}
export interface ModelChoice {
  key: string
  name: string
}
const role = (definitionKey: string, roleKey: string) =>
  `/flow-model-defaults/${encodeURIComponent(definitionKey)}/${encodeURIComponent(roleKey)}`
export const flowModelDefaultsApi = {
  list: () => request<FlowModelDefault[]>('/flow-model-defaults'),
  models: (definitionKey: string, roleKey: string) =>
    request<ModelChoice[]>(`${role(definitionKey, roleKey)}/models`),
  save: (definitionKey: string, roleKey: string, modelKey: string) =>
    request<FlowModelDefault>(role(definitionKey, roleKey), 'PUT', { model_key: modelKey }),
}
