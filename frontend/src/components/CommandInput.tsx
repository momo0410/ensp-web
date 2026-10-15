import { useEffect, useRef, useState } from "react"
import { Autocomplete } from "@mantine/core"
import type { KeyboardEvent } from "react"
import { api } from "../api"
import { toCommandOptions } from "../lib/commandComplete"
import type { CommandOption } from "../lib/commandComplete"
import type { CommandCompleteResult } from "../types"

interface CommandInputProps {
  value: string
  onChange: (value: string) => void
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void
  ip?: string
  view?: string
  deviceType?: string
  placeholder?: string
  label?: string
  disabled?: boolean
  size?: string
}

export function CommandInput({
  value, onChange, onKeyDown, ip, view, deviceType, placeholder, label, disabled, size,
}: CommandInputProps) {
  const [options, setOptions] = useState<CommandOption[]>([])
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(async () => {
      const params = new URLSearchParams()
      if (ip) params.set("ip", ip)
      params.set("prefix", value || "")
      if (view) params.set("view", view)
      if (deviceType) params.set("device_type", deviceType)
      try {
        const data = await api<CommandCompleteResult>(`/api/command-complete?${params.toString()}`)
        setOptions(toCommandOptions(data.candidates))
      } catch {
        setOptions([])
      }
    }, 300)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [value, ip, view, deviceType])

  return (
    <Autocomplete
      value={value}
      onChange={onChange}
      onKeyDown={onKeyDown}
      data={options}
      label={label}
      placeholder={placeholder}
      disabled={disabled}
      size={size}
      styles={{ input: { fontFamily: 'Consolas, "Courier New", monospace' } }}
    />
  )
}
