// 后端 API 载荷的领域类型定义（与 services/ 层返回结构对齐）

export interface DeviceInfo {
  name: string
  ip: string
  username?: string
  password?: string
  device_type?: string
  console_port?: number | null
}

export interface InterfaceInfo {
  name: string
  description?: string
  ipv4?: string
  netmask?: string
  mac?: string
  status: string
  subnet?: string
  is_recommended?: boolean
  is_ensp?: boolean
  recommendation_reason?: string
}

export interface CommandOutput {
  command: string
  success: boolean
  output?: string
  error?: string
}

export interface CollectionResult {
  ip?: string
  name?: string
  online?: boolean
  ssh_ok?: boolean
  ssh_error?: string
  info?: {
    hostname?: string
    model?: string
    version?: string
    cpu_usage?: string
    memory_usage?: string
    protocols?: string[]
    config_sections?: Record<string, { name?: string; config?: string }>
  }
  commands?: Record<string, { success: boolean; output?: string; error?: string }>
}

export interface CapabilityItem {
  supported: boolean
  configured?: boolean
  running?: boolean
  evidence?: string
  note?: string
  display_cmd?: string
}

export interface MatrixInfo {
  family?: string
  family_label?: string
  image?: string
  confidence?: string | number
}

export interface CapInterface {
  name: string
  ip?: string
  status?: string
}

export interface CapabilityResult {
  ip: string
  hostname?: string
  model?: string
  device_role?: string
  ssh_status?: boolean
  capabilities?: Record<string, CapabilityItem>
  matrix?: MatrixInfo
  errors?: string[]
  interfaces?: CapInterface[]
}

export interface RoleEntry {
  role: string
  detail?: string
  interface?: string
}

export interface RoleItem {
  name: string
  status: string
  roles?: RoleEntry[]
  config_key?: string
  presets?: Record<string, string>
  preset_hints?: string[]
}

export interface RoleResult {
  ip: string
  hostname?: string
  device_role?: string
  protocols?: Record<string, RoleItem>
}

export interface TopologyLink {
  source: string
  source_ip?: string
  local_interface?: string
  target: string
  target_ip?: string
  remote_interface?: string
}

export interface TemplateFieldOption {
  value: string
  label: string
}

export interface TemplateField {
  name: string
  label: string
  type: string
  required?: boolean
  default?: string | boolean
  placeholder?: string
  options?: TemplateFieldOption[]
  group?: string
  auto_fill_target?: string
  auto_fill_type?: string
}

export interface TemplateGroup {
  id: string
  name: string
  icon?: string
}

export interface ConfigTemplate {
  name: string
  icon?: string
  category?: string
  fields?: TemplateField[]
  field_groups?: TemplateGroup[]
}

export interface DeployOutcome {
  success?: boolean
  error?: string
  warning?: string
  backup_file?: string
  commands?: string[]
  command_outputs?: CommandOutput[]
  verify_outputs?: CommandOutput[]
  save_result?: string
}

export interface CatalogEntry {
  key: string
  name: string
  icon?: string
  category: string
  roles?: string[]
  device_types?: string[]
  fields?: TemplateField[]
}

export interface ProbeResult {
  supported: string[]
  unsupported: string[]
  device_type?: string
  model?: string
  errors?: string[]
}

export interface ConflictItem {
  message?: string
}

export interface WorkbenchConflicts {
  errors?: ConflictItem[]
  warnings?: ConflictItem[]
  suggestions?: ConflictItem[]
}

export interface WorkbenchPreviewResult {
  success: boolean
  errors?: string[]
  warnings?: string[]
  conflicts?: WorkbenchConflicts
  preview?: Record<string, { device_name?: string; commands?: string[] }>
}

export interface WorkbenchDeviceResult {
  success: boolean
  device_name?: string
  errors?: string[]
  command_output?: string
  verify_outputs?: Record<string, string>
  backup_file?: string
  duration?: number
}

export interface WorkbenchDeployResult {
  success: boolean
  task_id?: string
  task_name?: string
  summary?: { success?: number; total?: number }
  warnings?: string[]
  results?: Record<string, WorkbenchDeviceResult>
}

export interface LastDeploy {
  ip: string
  success: boolean
  error: string
  outputs: string
}

export interface AIStatus {
  success?: boolean
  configured: boolean
  api_base?: string
  model?: string
  mode?: string
  key_masked?: string
}

export interface LabExperiment {
  id: string
  name: string
  goal: string
  check_count: number
  applicable_roles?: string[]
}

export interface LabCheck {
  desc: string
  passed: boolean
  required: number
  passed_devices: string[]
}

export interface LabProgress {
  experiment_name: string
  goal: string
  progress: number
  checks?: LabCheck[]
}

export interface DevicePingResult {
  success: boolean
  command?: string
  transmitted?: number
  received?: number
  loss_rate?: number | null
  avg_ms?: number | null
  raw_output?: string
  error?: string
}

export interface ChatMessage {
  id?: string
  role: "user" | "assistant"
  content: string
  done?: boolean
  error?: string
}

export interface ProtocolTreeResult {
  success?: boolean
  model?: string
  image?: string
  interface?: string
  views?: Record<string, string[]>
  protocols?: Record<string, { name?: string; supported?: boolean; evidence?: string }>
}

export interface ClonePreview {
  success: boolean
  preview_id?: string
  warnings?: string[]
  conflicts?: WorkbenchConflicts
  targets?: Record<string, {
    device_name?: string
    commands?: string[]
    resolutions?: { message?: string }[]
    skipped?: { type?: string; message?: string }[]
  }>
}

export interface CloneDeployResult {
  success?: boolean
  error?: string
  summary?: { success?: number; total?: number }
  results?: Record<string, { device_name?: string; success?: boolean; errors?: string[] }>
}

export interface BackupFile {
  filename: string
  size: number
  created: string
}
