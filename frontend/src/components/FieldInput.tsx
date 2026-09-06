import React from "react"
import { Checkbox, Select, Stack, Text, TextInput, UnstyledButton } from "@mantine/core"
import { IconChevronDown, IconChevronRight } from "@tabler/icons-react"
import { networkToWildcardJS } from "../netutil"
import type { CapInterface, ConfigTemplate, TemplateField } from "../types"

const INTERFACE_FIELDS = ["interface", "access_interface", "outside_interface"]
const COMMON_WILDCARDS = ["0.0.0.255", "0.0.255.255", "0.255.255.255"]

export type TemplateParams = Record<string, any>
export type SetTemplateParams = React.Dispatch<React.SetStateAction<TemplateParams>>

interface FieldInputProps {
  field: TemplateField
  params: TemplateParams
  setParams: SetTemplateParams
  interfaces: CapInterface[] | null
}

/**
 * 单个模板字段（受控），对应旧版 renderFormField。
 * params/setParams 由父级表单统一持有，自动填充（network → wildcard）在此处联动。
 */
export function FieldInput({ field, params, setParams, interfaces }: FieldInputProps) {
  const value = params[field.name]
  const setValue = (v: any) => setParams((prev) => ({ ...prev, [field.name]: v }))

  const handleChange = (v: any) => {
    setValue(v)
    // 自动填充：network 字段输入时推导反掩码写入目标字段（目标为空或常见默认通配值时）
    if (field.auto_fill_target && (field.auto_fill_type || "network_to_wildcard") === "network_to_wildcard" && v) {
      const current = String(params[field.auto_fill_target] ?? "").trim()
      if (!current || COMMON_WILDCARDS.includes(current)) {
        const wc = networkToWildcardJS(String(v))
        if (wc) setParams((prev) => ({ ...prev, [field.auto_fill_target as string]: wc }))
      }
    }
  }

  let control
  if (INTERFACE_FIELDS.includes(field.name) && interfaces && interfaces.length > 0) {
    control = (
      <Select
        data={interfaces.map((intf) => ({
          value: intf.name,
          label: `${intf.name}${intf.ip ? ` (${intf.ip})` : ""}${intf.status ? ` [${intf.status}]` : ""}`,
        }))}
        placeholder="-- 选择接口 --"
        value={(value as string) || ""}
        onChange={handleChange}
        clearable
        searchable
      />
    )
  } else if (field.type === "select") {
    control = (
      <Select
        data={(field.options || []).map((opt) => ({ value: opt.value, label: opt.label }))}
        value={(value ?? field.default ?? "") as string}
        onChange={handleChange}
      />
    )
  } else if (field.type === "checkbox") {
    control = (
      <Checkbox
        checked={!!value}
        onChange={(e) => setValue(e.currentTarget.checked)}
      />
    )
  } else {
    control = (
      <TextInput
        type={field.type === "number" ? "number" : "text"}
        value={(value ?? field.default ?? "") as string}
        onChange={(e) => handleChange(e.currentTarget.value)}
        placeholder={field.placeholder || ""}
        required={!!field.required}
      />
    )
  }

  return (
    <div>
      <Text size="sm" fw={500} mb={4}>
        {field.label}
        {field.required && <span style={{ color: "var(--mantine-color-red-6)" }}> *</span>}
      </Text>
      {control}
    </div>
  )
}

// 提交前汇总参数：未改动的字段回填模板默认值（等价旧版 getModalFormParams 读取全部输入）
export function collectParams(tpl: ConfigTemplate, params: TemplateParams): TemplateParams {
  const out: TemplateParams = {}
  for (const f of tpl.fields || []) {
    const v = params[f.name]
    if (f.type === "checkbox") {
      out[f.name] = v === undefined ? !!f.default : !!v
    } else {
      out[f.name] = v === undefined || v === null ? (f.default ?? "") : v
    }
  }
  return out
}

interface TemplateFieldsProps {
  tpl: ConfigTemplate
  params: TemplateParams
  setParams: SetTemplateParams
  interfaces: CapInterface[] | null
}

/**
 * 模板字段区：有 field_groups 时分组渲染（basic/network 默认展开），
 * 否则扁平渲染。对应旧版 selectConfigTemplate 内的渲染逻辑。
 */
export function TemplateFields({ tpl, params, setParams, interfaces }: TemplateFieldsProps) {
  if (tpl.field_groups && tpl.field_groups.length > 0) {
    const groups = tpl.field_groups
      .map((group) => ({
        group,
        fields: (tpl.fields || []).filter((f) => f.group === group.id),
      }))
      .filter(({ fields }) => fields.length > 0)
    return (
      <Stack gap="xs">
        {groups.map(({ group, fields }) => (
          <FieldGroup key={group.id} group={group} fields={fields} params={params} setParams={setParams} interfaces={interfaces} />
        ))}
      </Stack>
    )
  }
  return (
    <Stack gap="sm">
      {(tpl.fields || []).map((field) => (
        <FieldInput key={field.name} field={field} params={params} setParams={setParams} interfaces={interfaces} />
      ))}
    </Stack>
  )
}

interface FieldGroupProps {
  group: { id: string; name: string; icon?: string }
  fields: TemplateField[]
  params: TemplateParams
  setParams: SetTemplateParams
  interfaces: CapInterface[] | null
}

function FieldGroup({ group, fields, params, setParams, interfaces }: FieldGroupProps) {
  // 基础配置默认展开，其他折叠
  const isBasic = group.id === "basic" || group.id === "network"
  const [open, setOpen] = React.useState(isBasic)
  return (
    <div style={{ border: "1px solid var(--mantine-color-default-border)", borderRadius: 8 }}>
      <UnstyledButton onClick={() => setOpen(!open)} p="xs" w="100%"
        style={{ display: "flex", alignItems: "center", gap: 6 }}>
        {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
        <Text size="sm" fw={500}>{group.icon || ""} {group.name}</Text>
      </UnstyledButton>
      {open && (
        <Stack gap="sm" p="xs" pt={0}>
          {fields.map((field) => (
            <FieldInput key={field.name} field={field} params={params} setParams={setParams} interfaces={interfaces} />
          ))}
        </Stack>
      )}
    </div>
  )
}
