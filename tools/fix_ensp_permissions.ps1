# fix_ensp_permissions.ps1
# 修复 eNSP 因安装目录（Program Files）不可写导致的设备启动失败：
#   VirtualBox 克隆基础镜像时要在 C:\Program Files\Huawei\eNSP\VBoxServer\AR_Base\
#   写 AR_Base.vbox-tmp，但该目录默认只有 Users:(RX)，无写权限，
#   报 "Access denied (-38)"，导致防火墙等需要新建 VM 的设备起不来。
#
# 本脚本给当前用户授予 eNSP 安装目录的"修改(Modify)"权限。需以管理员身份运行。
# 用法（管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File fix_ensp_permissions.ps1
# 或指定安装目录：
#   powershell -ExecutionPolicy Bypass -File fix_ensp_permissions.ps1 -EnspRoot "D:\eNSP"

param(
    [string]$EnspRoot = "C:\Program Files\Huawei\eNSP",
    [string]$User     = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = "Stop"

# 必须提权
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERR] 需要以管理员身份运行。请右键 PowerShell -> 以管理员身份运行，再执行本脚本。" -ForegroundColor Red
    exit 2
}

if (-not (Test-Path $EnspRoot)) {
    Write-Host "[ERR] 找不到 eNSP 目录: $EnspRoot" -ForegroundColor Red
    exit 3
}

Write-Host "[1/3] 目标目录: $EnspRoot"
Write-Host "[2/3] 授权用户: $User  (权限: Modify)"

# icacls: 授予 (OI)(CI)M = 容器/对象继承 + 修改权限
$targets = @(
    $EnspRoot,
    (Join-Path $EnspRoot "VBoxServer"),
    (Join-Path $EnspRoot "VBoxServer\devices"),
    (Join-Path $EnspRoot "vboxserver")
) | Sort-Object -Unique

foreach ($t in $targets) {
    if (Test-Path $t) {
        Write-Host ("  -> {0}" -f $t)
        & icacls $t /grant "${User}:(OI)(CI)M" /T /C | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host ("     [WARN] icacls 返回 {0}" -f $LASTEXITCODE) -ForegroundColor Yellow
        }
    }
}

Write-Host "[3/3] 验证写入权限..."
$probe = Join-Path $EnspRoot "VBoxServer\AR_Base\.write_probe"
try {
    Set-Content -Path $probe -Value "probe" -ErrorAction Stop
    Remove-Item $probe -Force
    Write-Host "[OK] AR_Base 现在可写 —— 防火墙等设备应能正常启动。" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "[FAIL] 仍不可写: $($_.Exception.Message)" -ForegroundColor Red
    exit 4
}
