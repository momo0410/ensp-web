// 网络地址 → 反掩码（wildcard mask）推导，与旧版 networkToWildcardJS 保持一致
export function networkToWildcardJS(network: string | null | undefined): string | null {
  if (!network) return null
  network = network.trim()

  // 已经是反掩码格式
  if (network.startsWith("0.")) {
    const parts = network.split(".")
    if (parts.length === 4 && parts.every((p) => !isNaN(+p) && +p >= 0 && +p <= 255)) {
      return network
    }
  }

  const wildcardBits = (prefix: number): string => {
    const mask = (0xFFFFFFFF << (32 - prefix)) >>> 0
    const wildcard = (~mask) >>> 0
    return [(wildcard >>> 24) & 0xFF, (wildcard >>> 16) & 0xFF, (wildcard >>> 8) & 0xFF, wildcard & 0xFF].join(".")
  }

  // CIDR 格式
  if (network.includes("/")) {
    const [addr, prefixStr] = network.split("/")
    const prefix = parseInt(prefixStr, 10)
    if (isNaN(prefix) || prefix < 0 || prefix > 32) return null
    return wildcardBits(prefix)
  }

  // 纯 IP 格式，按类推断
  const octets = network.split(".")
  if (octets.length !== 4) return null
  const first = parseInt(octets[0], 10)
  if (isNaN(first) || first === 0) return null
  const prefix = first <= 127 ? 8 : first <= 191 ? 16 : 24
  return wildcardBits(prefix)
}
