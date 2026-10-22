<#
.SYNOPSIS
    将 quant-agent（A股/ETF 量化交易 Agent：人设 + 7 个 Skill + 3 个 MCP
    + risk-reviewer 风控复核子智能体）一键安装到本地 DeerFlow 实例。

.DESCRIPTION
    1. 复制 persona/SOUL.md、config/agent.config.yaml 到用户空间 agents/quant-agent/
    2. 复制 skills/*（7 个）到用户空间 skills/custom/
    3. 复制 mcp-servers/* 到 <DeerFlowRoot>\backend\.deer-flow\mcp\quant-agent\
       （复制后安装不再依赖本仓库路径，仓库可移动）
    4. 为 quant-data 建独立 venv 并安装 akshare / baostock / mcp（与 machine-db 锁定的
       mcp==1.2.1 环境隔离；此步联网且最慢，失败不阻塞后续步骤）
    5. 合并 quant-data / quant-db / trade-gateway 三个 MCP 与 7 个 skill 开关到
       deer-flow/extensions_config.json（写入本机绝对路径；trade-gateway 默认 enabled:false）
    6. 创建 quant.db（15 表）并仅在 instruments 为空时灌入演示种子
    7. 以 BEGIN/END managed block 幂等注入 risk-reviewer 子智能体
       —— 注意：本机 config.yaml 已存在 machine-problems 注入的顶层 subagents: 键，
       故本脚本不追加第二个顶层 subagents:，而是把片段插到既有 custom_agents: 之下
    8. 环境自检（uv / uvx / QMT 路径 / 8001 端口）并提示重启 Gateway
    9. 将免 key 的 web_search 后端由 tavily 切换为 DuckDuckGo（ddgs）

.PARAMETER DeerFlowRoot
    deer-flow 项目根目录（含 backend/、config.yaml）。

.PARAMETER UserId
    DeerFlow 用户 ID（backend/.deer-flow/users/ 下目录名）。不指定且只有一个用户时自动选择。

.PARAMETER EnableTrading
    是否启用 trade-gateway（实盘交易网关）。**默认不启用**。启用后仍默认 dry_run=1，
    只写本地 orders 表不报券商；要真报单需自行把 QUANT_TRADE_DRY_RUN 改为 0。

.PARAMETER QmtPath
    QMT / MiniQMT 客户端安装根目录（其下应有 bin.x64\Lib\site-packages）。仅
    -EnableTrading 时使用。xtquant 随 QMT 分发、不在 PyPI，脚本用 PYTHONPATH 注入，
    不执行任何 pip install。

.PARAMETER QmtAccount
    资金账号。仅 -EnableTrading 且传了 -QmtPath 时使用；不传会写成占位符
    「<你的资金账号>」，需事后手工改 extensions_config.json。

.PARAMETER UseDuckDuckGo
    是否把 web_search 切换到免 API key 的 DuckDuckGo（默认 $true）。

.PARAMETER SkipSubagent
    跳过 risk-reviewer 子智能体注入（仅调试用；旗舰功能默认安装）。

.PARAMETER Uninstall
    卸载：移除三个 MCP 键、7 个 skill 开关与 managed 块。默认**保留** quant.db
    与 quant-data 的 venv（如需彻底清理请手工删除对应目录）。

.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -EnableTrading -QmtPath "C:\国金QMT" -QmtAccount "你的资金账号"
    .\scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$DeerFlowRoot = "d:\ROG魔霸新锐\Documents\deepflow\deer-flow",
    [string]$UserId = "",
    [switch]$EnableTrading,
    [string]$QmtPath = "",
    [string]$QmtAccount = "",
    [bool]$UseDuckDuckGo = $true,
    [switch]$SkipSubagent,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$SkillNames = @("data-ops", "market-research", "factor-screening", "strategy-backtest", "signal-review", "live-trading", "risk-limits")
$McpNames = @("quant-data", "quant-db", "trade-gateway")
$BlockBegin = "# BEGIN quant-agent managed block"
$BlockEnd = "# END quant-agent managed block"

function Assert-Path($path, $label) {
    if (-not (Test-Path $path)) { throw "$label 不存在：$path" }
}
function Write-Utf8($path, $text) { [System.IO.File]::WriteAllText($path, $text, $Utf8NoBom) }
function Read-Utf8($path) { [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) }
function Write-Step($n, $total, $msg, $color = "Cyan") { Write-Host "[$n/$total] $msg" -ForegroundColor $color }

Assert-Path $DeerFlowRoot "DeerFlow 根目录"
Assert-Path (Join-Path $DeerFlowRoot "backend") "DeerFlow backend 目录"
$TotalSteps = 9

# --- 0. 定位用户空间与外部工具 ---
$usersDir = Join-Path $DeerFlowRoot "backend\.deer-flow\users"
Assert-Path $usersDir "用户空间目录（请确认 DeerFlow 已初始化并创建过管理员账号）"
if (-not $UserId) {
    $userDirs = Get-ChildItem $usersDir -Directory | Where-Object { Test-Path (Join-Path $_.FullName "memory.json") }
    if ($userDirs.Count -eq 1) { $UserId = $userDirs[0].Name }
    else { throw "无法自动确定用户 ID，请用 -UserId 指定。候选：$((Get-ChildItem $usersDir -Directory).Name -join ', ')" }
}
$userHome = Join-Path $usersDir $UserId
Assert-Path $userHome "用户目录"

$uvExe = $null
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($uv) { $uvExe = $uv.Source }
elseif (Test-Path (Join-Path $env:USERPROFILE ".local\bin\uv.exe")) { $uvExe = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }

$uvxCmd = $null
$uvx = Get-Command uvx.exe -ErrorAction SilentlyContinue
if ($uvx) { $uvxCmd = $uvx.Source }
elseif (Test-Path (Join-Path $env:USERPROFILE ".local\bin\uvx.exe")) { $uvxCmd = Join-Path $env:USERPROFILE ".local\bin\uvx.exe" }

$extPath = Join-Path $DeerFlowRoot "extensions_config.json"
$cfgPath = Join-Path $DeerFlowRoot "config.yaml"
$dataDir = Join-Path $DeerFlowRoot "backend\.deer-flow\data"
$dbAbsPath = Join-Path $dataDir "quant.db"
$mcpInstallDir = Join-Path $DeerFlowRoot "backend\.deer-flow\mcp\quant-agent"
$quantDataVenv = Join-Path $DeerFlowRoot "backend\.deer-flow\mcp\quant-data\.venv"
$quantDataPython = Join-Path $quantDataVenv "Scripts\python.exe"

Write-Host "" 
Write-Host "quant-agent 安装器" -ForegroundColor Green
Write-Host "  仓库根目录   : $ProjectRoot"
Write-Host "  DeerFlow 根  : $DeerFlowRoot"
Write-Host "  目标用户     : $UserId"
Write-Host "  交易网关     : $(if ($EnableTrading) { '启用（默认 dry_run=1，不报券商）' } else { '不启用（默认）' })"
Write-Host ""

# ===========================================================================
# 卸载分支
# ===========================================================================
if ($Uninstall) {
    Write-Host "进入卸载模式：移除 MCP 键 / skill 开关 / managed 块（保留 quant.db 与 venv）" -ForegroundColor Yellow
    if (Test-Path $extPath) {
        $ext = Get-Content $extPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($name in $McpNames) {
            if ($ext.mcpServers -and ($ext.mcpServers.PSObject.Properties.Name -contains $name)) {
                $ext.mcpServers.PSObject.Properties.Remove($name)
            }
        }
        foreach ($name in $SkillNames) {
            if ($ext.skills -and ($ext.skills.PSObject.Properties.Name -contains $name)) {
                $ext.skills.PSObject.Properties.Remove($name)
            }
        }
        Write-Utf8 $extPath ($ext | ConvertTo-Json -Depth 12)
        Write-Host "  extensions_config.json 已清理（$($McpNames -join ', ') 与 7 个 skill 开关）" -ForegroundColor Cyan
    }
    if (Test-Path $cfgPath) {
        $cfg = Read-Utf8 $cfgPath
        $cfg = [regex]::Replace($cfg, "(?ms)^\# BEGIN quant-agent managed block\r?\n.*?^\# END quant-agent managed block\r?\n?", "")
        Write-Utf8 $cfgPath $cfg
        Write-Host "  config.yaml 中的 $BlockBegin .. $BlockEnd 已移除" -ForegroundColor Cyan
    }
    $agentDir = Join-Path $userHome "agents\quant-agent"
    if (Test-Path $agentDir) { Remove-Item $agentDir -Recurse -Force }
    foreach ($name in $SkillNames) {
        $p = Join-Path $userHome "skills\custom\$name"
        if (Test-Path $p) { Remove-Item $p -Recurse -Force }
    }
    Write-Host "  用户空间 agents/quant-agent 与 7 个技能目录已删除" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "卸载完成。以下内容被**保留**，如需彻底清理请手工删除：" -ForegroundColor Green
    Write-Host "  - 数据库      : $dbAbsPath"
    Write-Host "  - MCP 代码副本: $mcpInstallDir"
    Write-Host "  - venv        : $quantDataVenv"
    Write-Host "最后记得重启 Gateway 使配置生效。"
    return
}

# --- 1. 复制 Agent 与 7 个 Skill ---
$agentDir = Join-Path $userHome "agents\quant-agent"
New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
Copy-Item (Join-Path $ProjectRoot "persona\SOUL.md") (Join-Path $agentDir "SOUL.md") -Force
Copy-Item (Join-Path $ProjectRoot "config\agent.config.yaml") (Join-Path $agentDir "config.yaml") -Force

$customSkillsDir = Join-Path $userHome "skills\custom"
New-Item -ItemType Directory -Force -Path $customSkillsDir | Out-Null
# 注意：复制目录“内容”用 skills\*，避免产生 skills\skills 嵌套
Copy-Item (Join-Path $ProjectRoot "skills\*") $customSkillsDir -Recurse -Force
Write-Step 1 $TotalSteps "Agent（人设 + 配置）与 7 个 Skill（$($SkillNames -join '/')）已复制"

# --- 2. 复制自建 MCP server 到用户空间 ---
New-Item -ItemType Directory -Force -Path $mcpInstallDir | Out-Null
Copy-Item (Join-Path $ProjectRoot "mcp-servers\*") $mcpInstallDir -Recurse -Force
Write-Step 2 $TotalSteps "自建 MCP server 已复制到 $mcpInstallDir"

# --- 3. quant-data 专用 venv（fail-soft，联网最慢）---
$venvOk = $false
$venvMsg = ""
if (-not $uvExe) {
    $venvMsg = "未找到 uv.exe，跳过 venv 创建；请手工 pip 安装 akshare/baostock/mcp 并改写 quant-data 的 command"
}
elseif (Test-Path $quantDataPython) {
    $venvOk = $true
    $venvMsg = "venv 已存在，跳过创建（如需重建请删除 $quantDataVenv 后重跑）"
}
else {
    $env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
    $env:UV_CACHE_DIR = Join-Path (Split-Path $DeerFlowRoot -Parent) ".uv-cache"
    # PowerShell 5.1 会把原生命令写到 stderr 的进度行当成终止错误（$ErrorActionPreference=Stop），
    # 故调用 uv 期间临时降为 Continue，改为检查 $LASTEXITCODE。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # 必须锁 Python 3.12：quant-data 的依赖链（numpy/pandas）在 3.14 上于 MCP
        # 工作线程内首次导入实测会卡 76~100s，导致工具调用超时。
        & $uvExe venv --python 3.12 $quantDataVenv 2>&1 | Out-Null
        $venvRc = $LASTEXITCODE
        # 与 machine-db 锁定的 mcp==1.2.1 环境完全隔离，互不影响
        & $uvExe pip install --python $quantDataPython akshare baostock "mcp>=1.28,<2" 2>&1 | Out-String | Write-Verbose
        $pipRc = $LASTEXITCODE
        if ($venvRc -ne 0 -or $pipRc -ne 0) {
            $venvMsg = "uv 退出码异常（venv=$venvRc, pip=$pipRc），可手工执行：uv venv --python 3.12 `"$quantDataVenv`" ; uv pip install --python `"$quantDataPython`" akshare baostock `"mcp>=1.28,<2`""
        }
        elseif (Test-Path $quantDataPython) { $venvOk = $true; $venvMsg = "quant-data venv 就绪（Python 3.12）：$quantDataPython" }
        else { $venvMsg = "venv 创建后未找到 python.exe，请检查 uv 输出" }
    }
    catch {
        $venvMsg = "创建 quant-data venv 失败：$($_.Exception.Message)。可稍后手工执行：uv venv --python 3.12 `"$quantDataVenv`" ; uv pip install --python `"$quantDataPython`" akshare baostock `"mcp>=1.28,<2`""
    }
    finally { $ErrorActionPreference = $prevEap }
}
Write-Step 3 $TotalSteps $venvMsg $(if ($venvOk) { "Cyan" } else { "Yellow" })

# --- 4. 合并三个 MCP 与 7 个 skill 开关 ---
Assert-Path $extPath "deer-flow/extensions_config.json（请先在 DeerFlow 中完成一次初始化）"
# 改动全局配置前先做带时间戳的备份，便于出错回滚
foreach ($f in @($extPath, $cfgPath)) {
    if (Test-Path $f) {
        Copy-Item $f "$f.bak_$(Get-Date -Format 'yyyyMMdd-HHmmss')" -Force
    }
}
$ext = Get-Content $extPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $ext.mcpServers) { $ext | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force }
if (-not $ext.skills) { $ext | Add-Member -NotePropertyName skills -NotePropertyValue ([pscustomobject]@{}) -Force }
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

$uvCacheDir = Join-Path (Split-Path $DeerFlowRoot -Parent) ".uv-cache"
# Windows 中文系统必须强制子进程 UTF-8，否则含中文的结果会被按 GBK 写出、按 UTF-8 解码失败，
# 表现为工具调用 60 秒超时。三个 MCP 全都需要，不是只给 sqlite 加。
$utf8Env = [ordered]@{ PYTHONUTF8 = "1"; PYTHONIOENCODING = "utf-8" }

$quantDataServer = [ordered]@{
    enabled              = $true
    type                 = "stdio"
    command              = $(if ($venvOk) { $quantDataPython } else { "python" })
    args                 = @((Join-Path $mcpInstallDir "quant-data\server.py"))
    cwd                  = (Join-Path $DeerFlowRoot "backend")
    env                  = [ordered]@{
        PYTHONUTF8        = "1"
        PYTHONIOENCODING  = "utf-8"
        PYTHONUNBUFFERED  = "1"
        QUANT_DB_PATH     = $dbAbsPath
        QUANT_DATA_TIMEOUT = "25"
        QUANT_DATA_RETRY  = "2"
    }
    tool_name_prefix     = $true
    session_init_timeout = 180
    tool_call_timeout    = 120
    description          = "A股与ETF行情/标的/基本面取数：交易日历、标的台账（含退市标的与板块）、带复权因子的日线、带公告日(pub_date)的基本面、涨跌停与停牌状态、指数成分股；本地缓存落 quant-db"
    capability           = [ordered]@{ id = "quant-data" }
    routing              = [ordered]@{ mode = "prefer"; priority = 75; keywords = @("行情", "日线", "K线", "复权", "前复权", "后复权", "收盘价", "成交额", "换手率", "标的", "代码", "ETF", "股票", "指数", "成分股", "沪深300", "中证500", "创业板", "交易日历", "基本面", "公告日", "市盈率", "ROE", "财报", "涨跌停", "涨停", "跌停", "停牌", "退市", "取数", "更新数据") }
}

$quantDbServer = [ordered]@{
    enabled              = $true
    type                 = "stdio"
    command              = $uvxCmd
    args                 = @("--with", "mcp==1.2.1", "mcp-server-sqlite", "--db-path", $dbAbsPath)
    cwd                  = (Join-Path $DeerFlowRoot "backend")
    env                  = [ordered]@{
        UV_CACHE_DIR     = $uvCacheDir
        UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
        RUST_LOG         = "error"
        PYTHONUTF8       = "1"
        PYTHONIOENCODING = "utf-8"
    }
    tool_name_prefix     = $true
    session_init_timeout = 120
    tool_call_timeout    = 60
    description          = "quant-agent 本地状态库：标的台账（含退市标的）、交易日历、日线缓存、基本面（带公告日）、因子定义与因子值、策略、回测与净值曲线、信号、风控复核凭证、下单意图与确认码、订单、成交、日终持仓，共 15 张关联表"
    capability           = [ordered]@{ id = "quant-db" }
    routing              = [ordered]@{ mode = "prefer"; priority = 80; keywords = @("数据库", "SQL", "入库", "存到数据库", "落库", "查询", "统计", "聚合", "标的池", "因子值", "回测记录", "净值曲线", "信号", "风控复核", "确认码", "订单", "成交", "持仓") }
}

$tradeEnv = [ordered]@{
    PYTHONUTF8       = "1"
    PYTHONIOENCODING = "utf-8"
    PYTHONUNBUFFERED = "1"
    QUANT_DB_PATH    = $dbAbsPath
    QUANT_TRADE_DRY_RUN = "1"
    QUANT_MAX_ORDER_AMOUNT = "100000"
    QUANT_MAX_POSITION_PCT = "0.30"
    QUANT_CANCEL_SELL_ONLY = "1"
}
if ($QmtPath) {
    # xtquant 随 QMT 客户端分发、不在 PyPI，用 PYTHONPATH 注入而不是 pip install
    $xtq = Join-Path $QmtPath "bin.x64\Lib\site-packages"
    if (-not (Test-Path (Join-Path $xtq "xtquant"))) {
        Write-Host "  [警告] $xtq 下未找到 xtquant，路径可能不对；dry_run 不受影响，真报单会失败" -ForegroundColor Yellow
    }
    $tradeEnv["PYTHONPATH"]      = $xtq
    $tradeEnv["QUANT_QMT_PATH"]  = Join-Path $QmtPath "userdata_mini"
    $tradeEnv["QUANT_QMT_ACCOUNT"] = $(if ($QmtAccount) { $QmtAccount } else { "<你的资金账号>" })
    $tradeEnv["QUANT_QMT_ACCOUNT_TYPE"] = "stock"
}

$tradeServer = [ordered]@{
    enabled              = [bool]$EnableTrading
    type                 = "stdio"
    command              = $(if ($venvOk) { $quantDataPython } else { "python" })
    args                 = @((Join-Path $mcpInstallDir "trade-gateway\server.py"))
    cwd                  = (Join-Path $DeerFlowRoot "backend")
    env                  = $tradeEnv
    tool_name_prefix     = $true
    session_init_timeout = 120
    tool_call_timeout    = 60
    description          = "实盘交易网关（xtquant/QMT，默认关闭）：交易模式与闸门状态、资金账户、持仓、委托、成交查询，以及准备下单（生成 6 位确认码）、下单、撤单；四道闸=默认关闭+风控复核凭证+人工确认码+下单前重校验"
    capability           = [ordered]@{ id = "trade-gateway" }
    routing              = [ordered]@{ mode = "prefer"; priority = 90; keywords = @("下单", "买入", "卖出", "报单", "委托", "撤单", "持仓", "可用资金", "账户", "成交回报", "实盘", "QMT", "miniQMT", "确认码", "dry_run", "交易模式") }
}

$ext.mcpServers | Add-Member -NotePropertyName "quant-data"    -NotePropertyValue ([pscustomobject]$quantDataServer) -Force
$ext.mcpServers | Add-Member -NotePropertyName "quant-db"      -NotePropertyValue ([pscustomobject]$quantDbServer)   -Force
$ext.mcpServers | Add-Member -NotePropertyName "trade-gateway" -NotePropertyValue ([pscustomobject]$tradeServer)     -Force
foreach ($s in $SkillNames) {
    $ext.skills | Add-Member -NotePropertyName $s -NotePropertyValue ([pscustomobject]@{ enabled = $true }) -Force
}
[System.IO.File]::WriteAllText($extPath, ($ext | ConvertTo-Json -Depth 12), $Utf8NoBom)
Write-Step 4 $TotalSteps "extensions_config.json 已合并 3 个 MCP（uvx=$uvxCmd；python=$($quantDataServer['command'])；trade-gateway enabled=$([bool]$EnableTrading)）与 7 个 skill 开关"

# --- 5. 建库（15 表）+ 仅空库灌种子 ---
$schemaPath = Join-Path $ProjectRoot "integrations\quant-db\schema.sql"
$seedPath = Join-Path $ProjectRoot "integrations\quant-db\seed.sql"
Assert-Path $schemaPath "integrations/quant-db/schema.sql"
Assert-Path $seedPath "integrations/quant-db/seed.sql"

$py = @"
import pathlib, sqlite3, sys
db, schema, seed = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.executescript(pathlib.Path(schema).read_text(encoding='utf-8'))
n = con.execute('SELECT COUNT(*) FROM instruments').fetchone()[0]
if n == 0:
    con.executescript(pathlib.Path(seed).read_text(encoding='utf-8'))
    print('seed loaded')
else:
    print('seed skipped (instruments already has %d rows)' % n)
con.commit()
con.close()
print('db ready:', db)
"@
$tmpPy = Join-Path $env:TEMP "_quant_agent_init_db.py"
Write-Utf8 $tmpPy $py
$dbPy = $(if ($venvOk) { $quantDataPython } elseif ($uvExe) { $null } else { "python" })
Push-Location (Join-Path $DeerFlowRoot "backend")
try {
    if ($venvOk) {
        & $quantDataPython $tmpPy $dbAbsPath $schemaPath $seedPath
    }
    elseif ($uvExe) {
        & $uvExe run --no-sync python $tmpPy $dbAbsPath $schemaPath $seedPath
    }
    else {
        python $tmpPy $dbAbsPath $schemaPath $seedPath
    }
}
finally { Pop-Location }
Write-Step 5 $TotalSteps "quant.db 15 张表就绪（$dbAbsPath）"

# --- 6. 注入 risk-reviewer 子智能体（缩进片段 + 幂等标记块）---
Assert-Path $cfgPath "全局 config.yaml"
$cfg = Read-Utf8 $cfgPath
if ($SkipSubagent) {
    Write-Step 6 $TotalSteps "已按 -SkipSubagent 跳过子智能体注入" "Yellow"
}
else {
    $subText = Read-Utf8 (Join-Path $ProjectRoot "subagents\risk-reviewer.yaml")
    $b = $subText.IndexOf($BlockBegin); $e = $subText.IndexOf($BlockEnd)
    if ($b -lt 0 -or $e -lt 0) { throw "subagents/risk-reviewer.yaml 缺少 BEGIN/END 标记" }
    $fragment = $subText.Substring($b, $e - $b + $BlockEnd.Length).TrimEnd()

    # 幂等：先删掉旧 managed 块
    $cfg = [regex]::Replace($cfg, "(?ms)^\# BEGIN quant-agent managed block\r?\n.*?^\# END quant-agent managed block\r?\n?", "")
    if (-not $cfg.EndsWith("`n")) { $cfg += "`n" }

    $lines = [System.Collections.ArrayList]@($cfg -split "`r?`n")
    $subIdx = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^subagents:\s*$') { $subIdx = $i; break }
    }
    if ($subIdx -ge 0) {
        $caIdx = -1
        for ($i = $subIdx + 1; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match '^[A-Za-z_]') { break }      # 遇到下一个顶层键即停止
            if ($lines[$i] -match '^  custom_agents:\s*$') { $caIdx = $i; break }
        }
        if ($caIdx -ge 0) {
            # 本机已有 custom_agents（machine-problems 注入的 safety-reviewer 就在这里），
            # 因此只需把 4 空格缩进的片段插到该行之后，**不新增第二个顶层 subagents:** 键。
            $lines.Insert($caIdx + 1, $fragment)
        }
        else {
            $lines.Insert($subIdx + 1, "  custom_agents:")
            $lines.Insert($subIdx + 2, $fragment)
        }
    }
    else {
        $lines.Add("subagents:")
        $lines.Add("  custom_agents:")
        $lines.Add($fragment)
    }
    $cfg = ($lines -join "`n").TrimEnd() + "`n"
    Write-Utf8 $cfgPath $cfg
    Write-Step 6 $TotalSteps "已注入 risk-reviewer 子智能体（片段挂到既有 custom_agents 下，未新增重复顶层键）"
}

# --- 7. 环境自检 ---
$issues = @()
if (-not $uvExe) { $issues += "未找到 uv.exe（quant-data venv 未创建；请安装 uv 后重跑）" }
if (-not $uvxCmd) { $issues += "未找到 uvx.exe（quant-db MCP 无法启动）" }
if (-not $venvOk) { $issues += "quant-data venv 不可用：$venvMsg" }
if ($EnableTrading) {
    if (-not $QmtPath) { $issues += "-EnableTrading 已开但未传 -QmtPath，trade-gateway 无法定位 xtquant" }
    elseif (-not (Test-Path (Join-Path $QmtPath "bin.x64\Lib\site-packages\xtquant"))) {
        $issues += "在 $QmtPath\bin.x64\Lib\site-packages\xtquant 未找到 xtquant，请核对 QMT 安装路径"
    }
}
$portBusy = $false
try {
    $conn8001 = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue
    $portBusy = ($null -ne $conn8001)
} catch { $portBusy = $false }
if ($issues.Count -eq 0) {
    Write-Step 7 $TotalSteps "环境自检通过（uv/uvx 可用，DB 就绪）"
}
else {
    Write-Step 7 $TotalSteps "环境自检发现 $($issues.Count) 项待处理：" "Yellow"
    foreach ($i in $issues) { Write-Host "      - $i" -ForegroundColor Yellow }
}

# --- 8. web_search 切换到免 key 的 DuckDuckGo ---
$cfg = Read-Utf8 $cfgPath
if ($UseDuckDuckGo) {
    if ($cfg -match 'deerflow\.community\.ddg_search\.tools:web_search_tool') {
        Write-Step 8 $TotalSteps "web_search 已指向 DuckDuckGo，无需改动"
    }
    elseif ($cfg -match 'use:\s*deerflow\.community\.tavily\.tools:web_search_tool') {
        $cfg = [regex]::Replace($cfg, 'deerflow\.community\.tavily\.tools:web_search_tool', 'deerflow.community.ddg_search.tools:web_search_tool')
        Write-Utf8 $cfgPath $cfg
        Write-Step 8 $TotalSteps "web_search 已由 tavily（需 key）切换为 DuckDuckGo（免 key）"
    }
    else {
        Write-Step 8 $TotalSteps "未找到 tavily web_search 配置行，跳过搜索后端切换" "Yellow"
    }
}
else {
    Write-Step 8 $TotalSteps "按参数保留原 web_search 后端" "Yellow"
}

# --- 9. 完成与后续步骤 ---
Write-Step 9 $TotalSteps "安装完成" "Green"
Write-Host ""
Write-Host "接下来必须做的事：" -ForegroundColor Green
Write-Host "  1) 重启 Gateway（uvicorn 无 --reload，改配置不热加载）"
if ($portBusy) { Write-Host "     · 8001 端口当前已被占用，即 Gateway 正在运行，需先停掉再启动" -ForegroundColor Yellow }
Write-Host "  2) 新会话选择「量化交易助手」，先让它跑一次 data-ops 取数（首次 akshare import 较慢属正常）"
Write-Host "  3) 配置校验（可选，PyYAML 需已安装）："
Write-Host "     uv run --with pyyaml python -c `"import yaml;d=yaml.safe_load(open(r'$cfgPath',encoding='utf-8'));print(list(d['subagents']['custom_agents'].keys()))`""
Write-Host "     应同时看到 safety-reviewer 与 risk-reviewer"
if (-not $EnableTrading) {
    Write-Host "  4) 实盘交易网关**未启用**。确认策略可用后再执行：" -ForegroundColor Yellow
    Write-Host "     .\scripts\install.ps1 -EnableTrading -QmtPath `"<你的QMT安装目录>`""
    Write-Host "     启用后仍默认 QUANT_TRADE_DRY_RUN=1（只写 orders 表不报券商），改 0 才真报单"
}
Write-Host "  5) 卸载：.\scripts\install.ps1 -Uninstall（保留 quant.db 与 venv）"
Write-Host ""
Write-Host "免责：本项目是研究工具，不构成投资建议；实盘盈亏由使用者自行承担。" -ForegroundColor DarkGray
