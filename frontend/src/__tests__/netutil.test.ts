import { describe, expect, it } from "vitest"
import { networkToWildcardJS } from "../netutil"

describe("networkToWildcardJS", () => {
  it("已是以 0. 开头的反掩码时原样返回", () => {
    expect(networkToWildcardJS("0.0.0.255")).toBe("0.0.0.255")
  })

  it("非法的反掩码格式返回 null", () => {
    expect(networkToWildcardJS("0.0.0")).toBeNull()
    expect(networkToWildcardJS("0.0.0.999")).toBeNull()
  })

  it("CIDR 记法精确计算", () => {
    expect(networkToWildcardJS("192.168.1.0/24")).toBe("0.0.0.255")
    expect(networkToWildcardJS("10.0.0.0/8")).toBe("0.255.255.255")
    expect(networkToWildcardJS("172.16.0.0/16")).toBe("0.0.255.255")
    expect(networkToWildcardJS("10.0.0.0/32")).toBe("0.0.0.0")
  })

  it("CIDR 越界返回 null", () => {
    expect(networkToWildcardJS("10.0.0.0/33")).toBeNull()
    expect(networkToWildcardJS("10.0.0.0/-1")).toBeNull()
  })

  it("纯 IP 按 A/B/C 类推断", () => {
    expect(networkToWildcardJS("10.1.2.3")).toBe("0.255.255.255")
    expect(networkToWildcardJS("172.16.5.5")).toBe("0.0.255.255")
    expect(networkToWildcardJS("192.168.1.1")).toBe("0.0.0.255")
  })

  it("格式非法返回 null", () => {
    expect(networkToWildcardJS("")).toBeNull()
    expect(networkToWildcardJS("abc")).toBeNull()
    expect(networkToWildcardJS("1.2.3")).toBeNull()
    expect(networkToWildcardJS("0.1.2.3")).toBe("0.1.2.3") // 0. 开头且四段合法 → 视为反掩码直返
  })
})
