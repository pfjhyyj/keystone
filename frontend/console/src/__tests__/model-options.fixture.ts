import type { ChannelDescriptor, LegacySite, ModelOptions } from '../api/models'

const descriptors: ChannelDescriptor[] = [
  {
    id: 'openrouter',
    label: 'OpenRouter',
    display_as: 'openrouter',
    creatable: true,
    default_api_base: 'https://openrouter.ai/api/v1',
    custom_base: false,
    balance: true,
    bills: false,
    billing_credentials: 'key',
    fields: [],
    hint: '推理使用普通 API Key；账户余额查询可使用同一账户的 Management Key。',
  },
  {
    id: 'newapi',
    label: 'NewAPI',
    display_as: 'newapi',
    creatable: true,
    default_api_base: 'https://newapiai.com/v1',
    custom_base: true,
    balance: true,
    bills: false,
    billing_credentials: 'none',
    fields: [],
    hint: '填写标准 NewAPI 的 HTTPS API 地址，可包含路径前缀。普通 Key 用于模型列表、费用核对和额度查询。',
  },
  {
    id: 'yunwu',
    label: 'Yunwu',
    display_as: 'yunwu',
    creatable: true,
    default_api_base: 'https://yunwu.ai/v1',
    custom_base: true,
    balance: false,
    bills: false,
    billing_credentials: 'none',
    fields: [],
    hint: '兼容站点使用 Key 查询参数和分页日志，暂不支持账户余额查询。',
  },
  {
    id: 'openlux',
    label: 'Openlux',
    display_as: 'yunwu',
    creatable: false,
    default_api_base: 'https://api.openlux.ai/v1',
    custom_base: false,
    balance: false,
    bills: false,
    billing_credentials: 'none',
    fields: [],
    hint: '历史兼容渠道。',
  },
  {
    id: 'aliyun_bailian',
    label: '阿里云百炼（DashScope）',
    display_as: 'aliyun_bailian',
    creatable: true,
    default_api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    custom_base: false,
    balance: true,
    bills: true,
    billing_credentials: 'cloud',
    fields: [
      {
        name: 'workspace_id',
        label: '百炼业务空间 ID（可选）',
        pattern: '[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?',
        placeholder: '例如 llm-xxxxxxxx',
        hint: '留空使用共享域名；填写后使用业务空间专属域名查询模型，不改变模型调用地址。',
      },
    ],
    hint: '推理 Key 与云账务凭据相互独立。',
  },
  {
    id: 'volcengine',
    label: '火山方舟（豆包）',
    display_as: 'volcengine',
    creatable: true,
    default_api_base: 'https://ark.cn-beijing.volces.com/api/v3',
    custom_base: false,
    balance: true,
    bills: true,
    billing_credentials: 'cloud',
    fields: [],
    hint: '推理 Key 与云账务凭据相互独立。',
  },
]

// Test-only conveniences: select or reshape descriptors without the removed
// flat provider lists that the API used to return.
type OptionsOverrides = {
  creatable?: string[]
  balance?: string[]
  bills?: string[]
  api_bases?: Record<string, string>
  channels?: ChannelDescriptor[]
  legacy_sites?: LegacySite[]
}

export function modelOptionsFixture(overrides: OptionsOverrides = {}): ModelOptions {
  const creatable = overrides.creatable ?? [
    'openrouter',
    'newapi',
    'yunwu',
    'aliyun_bailian',
    'volcengine',
  ]
  const channels =
    overrides.channels ??
    descriptors
      .filter((item) => creatable.includes(item.id) || !item.creatable)
      .map((item) => ({
        ...item,
        default_api_base: overrides.api_bases?.[item.id] ?? item.default_api_base,
        balance: overrides.balance?.includes(item.id) ?? item.balance,
        bills: overrides.bills?.includes(item.id) ?? item.bills,
      }))
  return {
    channels,
    legacy_sites:
      overrides.legacy_sites ??
      ['https://api.openlux.ai/v1', 'https://yunwu.ai/v1'].map((api_base) => ({
        providers: ['newapi', 'yunwu'],
        api_base,
        display_as: 'yunwu',
        balance: false,
        bills: false,
        billing_credentials: 'none',
      })),
  }
}
