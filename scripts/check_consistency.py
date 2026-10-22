#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""quant-agent 资产包一致性校验（**离线，无网络、无第三方依赖**）。

校验的是「跨文件的引用是否自洽」——这类错误不会在运行时立刻报错，
但会在用户装上以后以「技能加载了但工具不可用」「表不存在」等形式炸掉：

  A. config/agent.config.yaml 里 skills / mcp_plugins / allowed_subagents 引用的名字
     都有对应文件（skills/<name>/SKILL.md、integrations/<name>/mcp.example.json、
     subagents/<name>.yaml）。
  B. 每个 SKILL.md 的 allowed-tools 逐项都在「已声明的 MCP 工具清单 ∪ 内置工具名」内。
  C. **除 live-trading 外，任何技能的 allowed-tools 都不得含 trade-gateway_ 前缀**
     —— 下单能力只能被一个技能持有。
  D. schema.sql 建出的表名覆盖技能与 MCP 代码里引用的表名。
  E. install.ps1 里 Join-Path $ProjectRoot "<相对路径>" 引用的文件都真实存在。
  F. trade-gateway 在 integrations 示例与 install.ps1 里都默认关闭（enabled=false / 默认 switch）。
  G. subagents/risk-reviewer.yaml 有 BEGIN/END 标记、是缩进片段（不产生第二个顶层
     subagents: 键）、且其 tools 都在已声明清单内。
  H. schema.sql 恰好 15 张表；README / README_EN / LICENSE / .gitignore 存在。

用法：python scripts/check_consistency.py
退出码：0 = 全部通过；1 = 存在失败项。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 除 live-trading 外，任何技能都不得持有交易工具
TRADING_ONLY_SKILL = "live-trading"
TRADING_PREFIX = "trade-gateway_"

# DeerFlow 运行时内置工具名（技能白名单里出现这些是合法的）
BUILTIN_TOOLS = {
    "read_file", "write_file", "web_search", "web_fetch", "task",
    "describe_skill", "read_skill_file", "review_skill_package", "tool_search",
    "list_dir", "search_in_file",
}

# uvx mcp-server-sqlite 暴露的工具（quant-db 用）
SQLITE_TOOLS = {"read_query", "write_query", "create_table", "list_tables",
                "describe_table", "append_insight"}

REQUIRED_SKILLS = ["data-ops", "market-research", "factor-screening",
                   "strategy-backtest", "signal-review", "live-trading",
                   "risk-limits"]
REQUIRED_MCPS = ["quant-data", "quant-db", "trade-gateway"]
REQUIRED_SUBAGENTS = ["risk-reviewer"]
EXPECTED_TABLE_COUNT = 15

errors: list[str] = []
warnings: list[str] = []


def check(condition: bool, message: str, warn_only: bool = False) -> None:
    if condition:
        return
    (warnings if warn_only else errors).append(message)


# ---------------------------------------------------------------------------
# 极简 YAML / Markdown 解析（不依赖 PyYAML，保证离线可跑）
# ---------------------------------------------------------------------------
def top_level_list(text: str, key: str) -> list[str]:
    """读顶层 `key:` 下的 `  - item` 列表。"""
    items: list[str] = []
    collecting = False
    for raw in text.splitlines():
        if re.match(rf"^{re.escape(key)}\s*:\s*$", raw):
            collecting = True
            continue
        if collecting:
            if re.match(r"^[A-Za-z_]", raw):
                break
            m = re.match(r"^\s+-\s*(.+?)\s*$", raw)
            if m:
                items.append(m.group(1).strip().strip('"\''))
    return items


def read_frontmatter(text: str) -> dict:
    """读 SKILL.md 的 YAML frontmatter（只取 name/description/license/allowed-tools/argument-hint）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    body = text[3:end]
    fm: dict = {}
    current = None
    for raw in body.splitlines():
        m = re.match(r"^([A-Za-z_][\w\-]*)\s*:\s*(.*)$", raw)
        if m:
            current = m.group(1)
            value = m.group(2).strip()
            if value:
                fm[current] = value.strip('"\'')
            else:
                fm[current] = []
            continue
        if current and re.match(r"^\s+-\s+", raw):
            item = raw.split("-", 1)[1].strip().strip('"\'')
            if isinstance(fm.get(current), list):
                fm[current].append(item)
    return fm


def json_no_comment(path: Path) -> dict:
    """读带 `_comment` 的 JSON 片段（正常 JSON，注释只是普通键）。"""
    return json.loads(path.read_text(encoding="utf-8"))


def mcp_tools_from_server(py_file: Path, prefix: str) -> set[str]:
    """从自建 MCP server 源码里抓 @mcp.tool() 下面的函数名，拼成运行时真名。"""
    if not py_file.exists():
        return set()
    text = py_file.read_text(encoding="utf-8")
    names = set()
    for m in re.finditer(r"@mcp\.tool\(\)\s*\ndef\s+([a-zA-Z_]\w*)\s*\(", text):
        names.add(f"{prefix}_{m.group(1)}")
    return names


# ---------------------------------------------------------------------------
# A. 名字引用完整性
# ---------------------------------------------------------------------------
def check_references() -> tuple[dict, dict]:
    cfg_path = ROOT / "config" / "agent.config.yaml"
    check(cfg_path.exists(), "缺少 config/agent.config.yaml")
    cfg_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""

    skills = top_level_list(cfg_text, "skills")
    mcps = top_level_list(cfg_text, "mcp_plugins")
    subs = top_level_list(cfg_text, "allowed_subagents")

    for name in REQUIRED_SKILLS:
        check((ROOT / "skills" / name / "SKILL.md").exists(),
              f"config 声明的技能 {name} 缺 skills/{name}/SKILL.md")
    for name in REQUIRED_MCPS:
        check((ROOT / "integrations" / name / "mcp.example.json").exists(),
              f"config 声明的 MCP {name} 缺 integrations/{name}/mcp.example.json")
    for name in REQUIRED_SUBAGENTS:
        check((ROOT / "subagents" / f"{name}.yaml").exists(),
              f"config 声明的子代理 {name} 缺 subagents/{name}.yaml")

    for name in skills:
        check(name in REQUIRED_SKILLS,
              f"config.skills 出现未在本包声明过的技能：{name}")
    for name in mcps:
        check(name in REQUIRED_MCPS,
              f"config.mcp_plugins 出现未在本包声明过的 MCP：{name}")
    for name in subs:
        check(name in REQUIRED_SUBAGENTS,
              f"config.allowed_subagents 出现未在本包声明过的子代理：{name}")

    check(set(skills) == set(REQUIRED_SKILLS),
          f"config.skills 与预期 7 个技能不一致：{skills}")
    check(set(mcps) == set(REQUIRED_MCPS),
          f"config.mcp_plugins 与预期 3 个 MCP 不一致：{mcps}")

    # 每个 skills/<name> 目录都必须被 config 声明（防止写了文件但没挂上）
    for d in sorted((ROOT / "skills").iterdir()):
        if d.is_dir():
            check(d.name in skills, f"skills/{d.name} 存在但 config.skills 未声明")

    return {"skills": skills, "mcps": mcps, "subagents": subs}, {}


# ---------------------------------------------------------------------------
# B/C. allowed-tools 白名单
# ---------------------------------------------------------------------------
def declared_tool_inventory() -> set[str]:
    inv: set[str] = set(BUILTIN_TOOLS)
    inv |= {f"quant-db_{t}" for t in SQLITE_TOOLS}
    inv |= mcp_tools_from_server(
        ROOT / "mcp-servers" / "quant-data" / "server.py", "quant-data")
    inv |= mcp_tools_from_server(
        ROOT / "mcp-servers" / "trade-gateway" / "server.py", "trade-gateway")
    return inv


def check_skills(inventory: set[str]) -> None:
    for name in REQUIRED_SKILLS:
        path = ROOT / "skills" / name / "SKILL.md"
        if not path.exists():
            continue
        fm = read_frontmatter(path.read_text(encoding="utf-8"))
        for field in ("name", "description", "license", "allowed-tools"):
            check(field in fm and fm[field], f"{path.name}({name}) 缺 frontmatter 字段 {field}")
        check(fm.get("name") == name, f"{name}/SKILL.md 的 frontmatter name 不等于目录名")

        tools = fm.get("allowed-tools") or []
        check(isinstance(tools, list) and len(tools) > 0,
              f"{name}/SKILL.md 的 allowed-tools 为空或格式不对（应为 YAML 列表）")
        for tool in tools:
            check(tool in inventory,
                  f"{name}/SKILL.md 的 allowed-tools 含未声明工具：{tool}")
            # YAML 列表形式不做别名归一化，写了别名等于运行时不可用
            check(not re.match(r"^(Read|Write|WebSearch|WebFetch|Task)$", tool),
                  f"{name}/SKILL.md 使用了会被白名单拒绝的别名：{tool}（须写运行时真名）")

        has_trading = any(str(t).startswith(TRADING_PREFIX) for t in tools)
        if name == TRADING_ONLY_SKILL:
            check(has_trading,
                  f"{TRADING_ONLY_SKILL} 是唯一持有交易工具的技能，但其 allowed-tools 里没有 {TRADING_PREFIX}*")
        else:
            check(not has_trading,
                  f"{name}/SKILL.md 出现 {TRADING_PREFIX}*：下单能力只能由 {TRADING_ONLY_SKILL} 持有")

        # 互指约定：description 必须写明边界
        desc = str(fm.get("description") or "")
        check("改用" in desc or "→" in desc,
              f"{name}/SKILL.md 的 description 未写「不触发 → 改用 XXX」边界", warn_only=True)

        tpl_dir = ROOT / "skills" / name / "templates"
        check(tpl_dir.exists() and any(tpl_dir.glob("*.md")),
              f"{name}/ 缺少 templates/*.md")


# ---------------------------------------------------------------------------
# D. 表名覆盖
# ---------------------------------------------------------------------------
def check_tables() -> None:
    schema_path = ROOT / "integrations" / "quant-db" / "schema.sql"
    check(schema_path.exists(), "缺少 integrations/quant-db/schema.sql")
    if not schema_path.exists():
        return
    schema = schema_path.read_text(encoding="utf-8")
    tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", schema))
    check(len(tables) == EXPECTED_TABLE_COUNT,
          f"schema.sql 建了 {len(tables)} 张表，预期 {EXPECTED_TABLE_COUNT} 张：{sorted(tables)}")

    referenced: dict[str, set[str]] = {}

    def collect(path: Path, label: str) -> None:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        found = set()
        for pattern in (r"\bFROM\s+(\w+)", r"\bJOIN\s+(\w+)",
                        r"\bINTO\s+(\w+)", r"\bUPDATE\s+(\w+)"):
            found |= {t.lower() for t in re.findall(pattern, text)}
        found = {t for t in found if t.startswith(
            ("instrument", "trading_", "bars_", "fundamental", "factor_",
             "strateg", "backtest", "equity_", "signal", "risk_review",
             "order_", "orders", "trades", "positions_"))}
        if found:
            referenced[label] = found

    for name in REQUIRED_SKILLS:
        collect(ROOT / "skills" / name / "SKILL.md", f"skills/{name}/SKILL.md")
    collect(ROOT / "mcp-servers" / "quant-data" / "server.py", "quant-data/server.py")
    collect(ROOT / "mcp-servers" / "trade-gateway" / "server.py", "trade-gateway/server.py")
    collect(ROOT / "mcp-servers" / "trade-gateway" / "gates.py", "trade-gateway/gates.py")

    for label, names in sorted(referenced.items()):
        for table in sorted(names):
            check(table in tables,
                  f"{label} 引用了 schema.sql 中不存在的表：{table}")


# ---------------------------------------------------------------------------
# E. install.ps1 里的仓库路径
# ---------------------------------------------------------------------------
def check_installer_paths() -> None:
    ps_path = ROOT / "scripts" / "install.ps1"
    check(ps_path.exists(), "缺少 scripts/install.ps1")
    if not ps_path.exists():
        return
    raw = ps_path.read_bytes()
    check(raw[:3] == b"\xef\xbb\xbf",
          "install.ps1 必须存为 UTF-8 带 BOM（中文系统下 PS 5.1 否则按 GBK 读，会乱码）")
    text = raw.decode("utf-8-sig")

    for rel in re.findall(r'Join-Path \$ProjectRoot "([^"]+)"', text):
        if "*" in rel:
            continue
        check((ROOT / rel.replace("\\", "/")).exists(),
              f'install.ps1 引用了仓库内不存在的路径：{rel}')

    # 默认必须不启用交易网关
    check(re.search(r"\[switch\]\$EnableTrading", text) is not None,
          "install.ps1 缺少 -EnableTrading 开关（交易网关应默认关闭）")
    check(re.search(r"\[switch\]\$Uninstall", text) is not None,
          "install.ps1 缺少 -Uninstall 开关")
    check(re.search(r"enabled\s*=\s*\[bool\]\$EnableTrading", text) is not None,
          "install.ps1 的 trade-gateway enabled 未绑定到 $EnableTrading")
    check(re.search(r"QUANT_TRADE_DRY_RUN\s*=\s*\"1\"", text) is not None,
          "install.ps1 未把 QUANT_TRADE_DRY_RUN 默认设为 1（应只写库不报单）")
    utf8_hits = text.count("PYTHONUTF8")
    check(utf8_hits >= 3,
          f"install.ps1 中 PYTHONUTF8 只出现 {utf8_hits} 次，三个 MCP 都要设（Windows 中文编码坑）")
    check("risk-reviewer managed block" in text or "quant-agent managed block" in text,
          "install.ps1 未使用 quant-agent managed block 标记")
    check("custom_agents" in text and "Insert" in text,
          "install.ps1 未实现「插入到既有 custom_agents 之下」的子代理注入写法")


def check_integration_examples() -> None:
    for name in REQUIRED_MCPS:
        path = ROOT / "integrations" / name / "mcp.example.json"
        if not path.exists():
            continue
        try:
            data = json_no_comment(path)
        except json.JSONDecodeError as exc:
            errors.append(f"integrations/{name}/mcp.example.json 不是合法 JSON：{exc}")
            continue
        server = (data.get("mcpServers") or {}).get(name)
        check(server is not None, f"integrations/{name}/mcp.example.json 缺 mcpServers.{name}")
        if server is None:
            continue
        env = server.get("env") or {}
        check(env.get("PYTHONUTF8") == "1",
              f"{name} 的 env 缺少 PYTHONUTF8=1（中文 Windows 下 MCP stdio 会 60s 超时）")
        check(env.get("PYTHONIOENCODING") == "utf-8",
              f"{name} 的 env 缺少 PYTHONIOENCODING=utf-8")
        check(server.get("tool_name_prefix") is True,
              f"{name} 未设置 tool_name_prefix=true（技能白名单依赖带前缀的真名）")
        skills_map = data.get("skills") or {}
        check(set(skills_map) == set(REQUIRED_SKILLS),
              f"integrations/{name}/mcp.example.json 的 skills 段不是 7 个技能")

    tg = ROOT / "integrations" / "trade-gateway" / "mcp.example.json"
    if tg.exists():
        server = (json_no_comment(tg).get("mcpServers") or {}).get("trade-gateway") or {}
        check(server.get("enabled") is False,
              "trade-gateway 的示例配置必须 enabled=false（默认关闭是第一道闸）")
        env = server.get("env") or {}
        check(env.get("QUANT_TRADE_DRY_RUN") == "1",
              "trade-gateway 示例的 QUANT_TRADE_DRY_RUN 必须为 1")
        check("PYTHONPATH" in env,
              "trade-gateway 示例未通过 PYTHONPATH 注入 xtquant（xtquant 不在 PyPI）")


# ---------------------------------------------------------------------------
# G. 子代理片段
# ---------------------------------------------------------------------------
def check_subagent(inventory: set[str]) -> None:
    path = ROOT / "subagents" / "risk-reviewer.yaml"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    check("# BEGIN quant-agent managed block" in text and
          "# END quant-agent managed block" in text,
          "risk-reviewer.yaml 缺 BEGIN/END managed block 标记")
    for line in text.splitlines():
        if re.match(r"^subagents\s*:", line):
            errors.append("risk-reviewer.yaml 含顶层 `subagents:` 键 —— 会与 machine-problems "
                          "已注入的同名键冲突；本文件必须是缩进片段")
        if line.strip().startswith("risk-reviewer:") and not line.startswith("    "):
            errors.append("risk-reviewer.yaml 的 risk-reviewer: 缩进不是 4 空格（须与 "
                          "safety-reviewer 同级，挂在 custom_agents 下）")

    tools_block = re.search(r"^\s{6}tools:\s*\n((?:\s{8}-.*\n)+)", text, re.M)
    check(tools_block is not None, "risk-reviewer.yaml 未找到 tools 列表")
    if tools_block:
        tools = re.findall(r"-\s*([\w\-]+)", tools_block.group(1))
        for tool in tools:
            check(tool in inventory,
                  f"risk-reviewer.yaml 的 tools 含未声明工具：{tool}")
        for forbidden in ("write_file", "task"):
            check(forbidden not in tools,
                  f"risk-reviewer 不得持有 {forbidden}（复核代理必须只读、不可再委托）")
    check("disallowed_tools" in text and "write_file" in text,
          "risk-reviewer.yaml 未在 disallowed_tools 中禁写")
    check("verdict" in text and "通过" in text,
          "risk-reviewer 的输出格式未约定 verdict 三档结论")
    check("lag_shift" in text or "后移" in text,
          "risk-reviewer 未包含「信号后移 1 日」对照测试要求")


# ---------------------------------------------------------------------------
# H. 交付物齐全
# ---------------------------------------------------------------------------
def check_deliverables() -> None:
    for rel in ("README.md", "README_EN.md", "LICENSE", ".gitignore",
                "docs/architecture.md", "scripts/check_consistency.py"):
        check((ROOT / rel).exists(), f"缺少交付物：{rel}")
    examples = ROOT / "examples"
    check(examples.exists() and len(list(examples.glob("*.md"))) >= 3,
          "examples/ 下至少需要 3 个可复现样例")
    tests = ROOT / "tests"
    for name in ("test_order_gate.py", "test_data_ops.py", "test_risk_reviewer_fixtures.py"):
        check((tests / name).exists(), f"缺少测试文件：tests/{name}")


def main() -> int:
    inventory = declared_tool_inventory()
    check_references()
    check_skills(inventory)
    check_tables()
    check_installer_paths()
    check_integration_examples()
    check_subagent(inventory)
    check_deliverables()

    print("=" * 72)
    print("quant-agent 一致性校验")
    print(f"  仓库根目录：{ROOT}")
    print(f"  已声明工具数：{len(inventory)}"
          f"（内置 {len(BUILTIN_TOOLS)} + quant-db {len(SQLITE_TOOLS)}"
          f" + 自建 {len(inventory) - len(BUILTIN_TOOLS) - len(SQLITE_TOOLS)}）")
    print("=" * 72)
    if warnings:
        print(f"\n[WARN] {len(warnings)} 项提示：")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"\n[FAIL] {len(errors)} 项错误：")
        for e in errors:
            print(f"  - {e}")
        print("\n校验未通过。")
        return 1
    print("\n[OK] 全部检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
