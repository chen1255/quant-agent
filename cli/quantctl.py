#!/usr/bin/env python3
"""quantctl —— quant-agent 的独立运行入口（不依赖 DeerFlow）。

它把同一套资产直接跑起来：``persona/SOUL.md`` 当系统提示词、``skills/*/SKILL.md``
按需加载、三个 MCP server 通过 stdio 挂载、``subagents/risk-reviewer.yaml`` 里的
系统提示词作为只读复核子代理。四道下单闸门仍然在 MCP 代码层，本文件不重复实现。

用法：
    python cli/quantctl.py doctor                 # 自检（MCP 连接 / API key / 工具数）
    python cli/quantctl.py tools                  # 列出可用工具（含 MCP 前缀）
    python cli/quantctl.py run "查一下 510300 的日线覆盖到哪一天"
    python cli/quantctl.py chat                   # 交互式会话（确认码可跨轮贴回）

配置：默认读 ``cli/config.json``（可从 ``cli/config.example.json`` 复制），
模型默认 ``deepseek-v4-pro``，API key 从 ``DEEPSEEK_API_KEY`` 读取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# 与 DeerFlow 常驻内置工具集对齐：describe_skill 永远可用
ALWAYS_ON = ("describe_skill", "read_file", "write_file", "task")

# risk-reviewer 只读白名单（对应 subagents/risk-reviewer.yaml 的 tools 字段）
REVIEWER_TOOLS = (
    "read_file",
    "quant-db_list_tables",
    "quant-db_describe_table",
    "quant-db_read_query",
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    cfg.setdefault("model", "deepseek-v4-pro")
    cfg.setdefault("base_url", "https://api.deepseek.com/v1")
    cfg.setdefault("api_key_env", "DEEPSEEK_API_KEY")
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("max_turns", 30)
    cfg.setdefault("timeout", 600)
    root = cfg.get("project_root") or ".."
    cfg["project_root"] = str((path.parent / root).resolve())
    return cfg


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def api_key(cfg: dict) -> str:
    key = os.environ.get(cfg["api_key_env"], "")
    if not key:
        raise SystemExit(
            f"未找到环境变量 {cfg['api_key_env']}；请先设置，"
            f"或在启动前 source 你的 .env")
    return key


# ---------------------------------------------------------------------------
# MCP 桥：一个连三个 server，工具名带 server 前缀（与 DeerFlow 的 tool_name_prefix 一致）
# ---------------------------------------------------------------------------
class MCPBridge:
    def __init__(self, servers: dict, stack: AsyncExitStack):
        self.servers = servers
        self.stack = stack
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, dict] = {}      # runtime name -> {server, real, schema}
        # MCP server 的 stderr 默认会直接打到终端（uv 的 INFO 解析日志尤其吵）；
        # 需要排障时设 QUANTCTL_DEBUG=1 才放行
        if os.environ.get("QUANTCTL_DEBUG"):
            self.errlog = sys.stderr
        else:
            self.errlog = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
            stack.callback(self.errlog.close)

    async def connect(self) -> None:
        for name, spec in self.servers.items():
            if not spec.get("enabled", True):
                continue
            spec = _expand(spec)
            # 命令行里写的是裸命令名时，按当前 PATH 解析成绝对路径；
            # Windows 的 CreateProcess 不像 shell 那样搜索 PATH，不解析会 FileNotFoundError
            command = spec["command"]
            if not Path(command).is_absolute():
                resolved = shutil.which(command)
                if resolved:
                    command = resolved
            env = {**os.environ, **spec.get("env", {}), "PYTHONUTF8": "1",
                   "PYTHONIOENCODING": "utf-8"}
            params = StdioServerParameters(
                command=command,
                args=spec.get("args", []),
                env=env,
                cwd=spec.get("cwd") or None,
            )
            read, write = await self.stack.enter_async_context(
                stdio_client(params, errlog=self.errlog))
            session = await self.stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            listed = await session.list_tools()
            for tool in listed.tools:
                runtime = f"{name}_{tool.name}"
                self.tools[runtime] = {
                    "server": name,
                    "real": tool.name,
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": runtime,
                            "description": (tool.description or "")[:1000],
                            "parameters": tool.inputSchema or {"type": "object",
                                                               "properties": {}},
                        },
                    },
                }

    def schemas(self, names: tuple[str, ...] | None = None) -> list[dict]:
        if names is None:
            return [t["schema"] for t in self.tools.values()]
        return [self.tools[n]["schema"] for n in names if n in self.tools]

    async def call(self, runtime_name: str, args: dict) -> str:
        entry = self.tools.get(runtime_name)
        if entry is None:
            return json.dumps({"ok": False, "error": f"未知工具 {runtime_name}"},
                              ensure_ascii=False)
        result = await self.sessions[entry["server"]].call_tool(entry["real"], args)
        chunks: list[str] = []
        for part in result.content:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
        if not chunks and getattr(result, "structuredContent", None):
            chunks.append(json.dumps(result.structuredContent, ensure_ascii=False))
        return "\n".join(chunks) or "(空结果)"


# ---------------------------------------------------------------------------
# 资产加载：SOUL / 技能索引 / 子代理提示词
# ---------------------------------------------------------------------------
def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block, body = text[3:end], text[end + 4:]
    meta: dict[str, Any] = {}
    current_list: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and current_list:
            item = line.strip()
            if item.startswith("- "):
                meta[current_list].append(item[2:].strip())
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if value:
                meta[key] = value
                current_list = None
            else:
                meta[key] = []
                current_list = key
    return meta, body.lstrip("\n")


def load_skills(root: Path) -> dict[str, dict]:
    skills: dict[str, dict] = {}
    base = root / "skills"
    if not base.is_dir():
        return skills
    for entry in sorted(base.iterdir()):
        md = entry / "SKILL.md"
        if not md.is_file():
            continue
        meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
        name = meta.get("name") or entry.name
        raw_tools = meta.get("allowed-tools") or []
        if isinstance(raw_tools, str):
            raw_tools = raw_tools.split()
        skills[name] = {
            "name": name,
            "description": meta.get("description", ""),
            "tools": tuple(raw_tools),
            "body": body,
            "path": str(md),
        }
    return skills


def load_reviewer(root: Path) -> tuple[str, str]:
    path = root / "subagents" / "risk-reviewer.yaml"
    if not path.is_file():
        return "", "risk-reviewer.yaml 缺失"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^\s*system_prompt:\s*\|\s*$\n(.*?)(?=^\s*[a-z_]+:\s|\Z)",
                      text, re.S | re.M)
    if not match:
        return "", "risk-reviewer.yaml 中未找到 system_prompt"
    prompt = "\n".join(line[8:] if line.startswith(" " * 8) else line.strip()
                       for line in match.group(1).splitlines())
    return prompt.strip(), "ok"


SYSTEM_TEMPLATE = """{soul}

## 运行环境（quantctl 独立模式）
你现在运行在 quantctl 上，不依赖 DeerFlow。工具分两类：
- **技能**：用 `describe_skill(name)` 读取技能全文后再按其中的流程执行；技能一旦激活，
  你的工具会被收窄到该技能的 allowed-tools，切换技能需重新 describe_skill。
- **MCP 工具**：`quant-data_*`（取数）、`quant-db_*`（本地库读写）、`trade-gateway_*`（下单闸门）。
  四道闸门在 MCP 代码层，提示词约束不能绕过它们。

可用技能：
{skills}

纪律：不编造数据；金额/数量/口径以工具返回与用户确认为准；下单确认码必须由用户贴回。
"""


def build_system_prompt(root: Path, skills: dict[str, dict]) -> str:
    soul_path = root / "persona" / "SOUL.md"
    soul = soul_path.read_text(encoding="utf-8") if soul_path.is_file() else "你是量化交易助手。"
    lines = [f"- `{s['name']}`：{s['description']}" for s in skills.values()]
    return SYSTEM_TEMPLATE.format(soul=soul.strip(), skills="\n".join(lines))


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
class LLM:
    def __init__(self, cfg: dict, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client

    async def chat(self, messages: list[dict], tools: list[dict] | None) -> dict:
        payload: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg["temperature"],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        resp = await self.client.post(
            f"{self.cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key(self.cfg)}"},
            json=payload,
            timeout=self.cfg["timeout"],
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"模型调用失败 HTTP {resp.status_code}：{resp.text[:500]}")
        return resp.json()["choices"][0]["message"]


# ---------------------------------------------------------------------------
# Agent：工具循环 + 技能收窄 + 子代理委托
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, bridge: MCPBridge, llm: LLM, cfg: dict,
                 system: str, skills: dict[str, dict], reviewer_prompt: str,
                 depth: int = 0):
        self.bridge = bridge
        self.llm = llm
        self.cfg = cfg
        self.skills = skills
        self.reviewer_prompt = reviewer_prompt
        self.depth = depth
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.active_skill: str | None = None

    # -- 工具集 --------------------------------------------------------------
    def _local_schemas(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": "describe_skill",
                "description": "读取某个技能的完整 SKILL.md 并激活它（可选：data-ops / "
                               "market-research / factor-screening / strategy-backtest / "
                               "signal-review / risk-limits / live-trading）",
                "parameters": {"type": "object", "properties": {
                    "name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "读取项目内文件（相对仓库根目录的路径）",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "write_file",
                "description": "写入项目内文件（相对仓库根目录）",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}},
                    "required": ["path", "content"]}}},
            {"type": "function", "function": {
                "name": "task",
                "description": "委托只读子代理做独立复核。当前仅有 risk-reviewer："
                               "回测交付或下单前必须调用，产出三档结论",
                "parameters": {"type": "object", "properties": {
                    "agent": {"type": "string"},
                    "instructions": {"type": "string"}},
                    "required": ["agent", "instructions"]}}},
        ]

    def current_tools(self) -> list[dict]:
        local = self._local_schemas()
        if self.active_skill:
            allowed = set(self.skills[self.active_skill]["tools"]) | {"describe_skill"}
            local = [t for t in local if t["function"]["name"] in allowed]
            return local + self.bridge.schemas(tuple(allowed))
        return local + self.bridge.schemas()

    # -- 文件与委托 ----------------------------------------------------------
    def _safe_path(self, rel: str) -> Path:
        root = Path(self.cfg["project_root"]).resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise ValueError(f"路径越界：{rel}")
        return target

    async def _delegate(self, agent_name: str, instructions: str) -> str:
        if agent_name != "risk-reviewer":
            return json.dumps({"ok": False, "error": f"未注册的子代理：{agent_name}"},
                              ensure_ascii=False)
        if not self.reviewer_prompt:
            return json.dumps({"ok": False, "error": "risk-reviewer 提示词缺失"},
                              ensure_ascii=False)
        sub = Agent(self.bridge, self.llm, self.cfg, self.reviewer_prompt,
                    self.skills, self.reviewer_prompt, depth=self.depth + 1)
        original = self.bridge.tools
        try:
            self.bridge.tools = {k: v for k, v in original.items()
                                 if k in REVIEWER_TOOLS or k == "read_file"}
            return await sub.run(instructions, max_turns=12)
        finally:
            self.bridge.tools = original

    async def _execute(self, name: str, args: dict) -> str:
        if name == "describe_skill":
            skill = self.skills.get(args.get("name", ""))
            if not skill:
                return f"未找到技能 {args.get('name')}；可用：{', '.join(self.skills)}"
            self.active_skill = skill["name"]
            return f"# 已激活技能 {skill['name']}\n\n{skill['body']}"
        if name == "read_file":
            try:
                return self._safe_path(args["path"]).read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                return f"读取失败：{exc}"
        if name == "write_file":
            try:
                target = self._safe_path(args["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args.get("content", ""), encoding="utf-8")
                return f"已写入 {target}"
            except Exception as exc:  # noqa: BLE001
                return f"写入失败：{exc}"
        if name == "task":
            return await self._delegate(args.get("agent", ""), args.get("instructions", ""))
        return await self.bridge.call(name, args)

    # -- 主循环 --------------------------------------------------------------
    async def run(self, user_text: str, max_turns: int | None = None) -> str:
        self.messages.append({"role": "user", "content": user_text})
        limit = max_turns or self.cfg["max_turns"]
        for turn in range(limit):
            tools = self.current_tools()
            message = await self.llm.chat(self.messages, tools or None)
            calls = message.get("tool_calls") or []
            clean = {"role": "assistant", "content": message.get("content")}
            if calls:
                clean["tool_calls"] = calls
            self.messages.append(clean)
            if not calls:
                return message.get("content") or "(模型未返回内容)"
            for call in calls:
                fn = call["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"  → {name}({_short(args)})", flush=True)
                output = await self._execute(name, args)
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": output})
        return f"达到最大轮数 {limit}，未收敛；可加大 max_turns 或细化任务。"


def _short(args: dict, limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
async def cmd_doctor(cfg: dict) -> None:
    async with httpx.AsyncClient() as client, AsyncExitStack() as stack:
        bridge = MCPBridge(cfg["mcp_servers"], stack)
        await bridge.connect()
        skills = load_skills(Path(cfg["project_root"]))
        reviewer, status = load_reviewer(Path(cfg["project_root"]))
        print(f"模型    : {cfg['model']} @ {cfg['base_url']}")
        print(f"API key : {'已设置' if os.environ.get(cfg['api_key_env']) else '缺失'}")
        print(f"技能    : {len(skills)} 个（{', '.join(skills)}）")
        print(f"子代理  : risk-reviewer 提示词 {len(reviewer)} 字（{status}）")
        for name, session in bridge.sessions.items():
            print(f"MCP     : {name} 已连接")
        print(f"工具总数: {len(bridge.tools)}（MCP）+ {len(ALWAYS_ON)}（本地）")
        await health_probe(cfg, client)


async def health_probe(cfg: dict, client: httpx.AsyncClient) -> None:
    try:
        resp = await client.get(f"{cfg['base_url'].rstrip('/')}/models",
                                headers={"Authorization": f"Bearer {api_key(cfg)}"},
                                timeout=20)
        ok = resp.status_code == 200
        print(f"连通性  : /models HTTP {resp.status_code}{' ✓' if ok else ' ✗'}")
    except Exception as exc:  # noqa: BLE001
        print(f"连通性  : 失败 {exc}")


async def cmd_tools(cfg: dict) -> None:
    async with AsyncExitStack() as stack:
        bridge = MCPBridge(cfg["mcp_servers"], stack)
        await bridge.connect()
        for name in sorted(bridge.tools):
            print(f"{name}\t{bridge.tools[name]['server']}")


async def cmd_run(cfg: dict, prompt: str) -> None:
    async with httpx.AsyncClient() as client, AsyncExitStack() as stack:
        bridge = MCPBridge(cfg["mcp_servers"], stack)
        await bridge.connect()
        skills = load_skills(Path(cfg["project_root"]))
        reviewer, _ = load_reviewer(Path(cfg["project_root"]))
        system = build_system_prompt(Path(cfg["project_root"]), skills)
        agent = Agent(bridge, LLM(cfg, client), cfg, system, skills, reviewer)
        print(await agent.run(prompt))


async def cmd_chat(cfg: dict) -> None:
    async with httpx.AsyncClient() as client, AsyncExitStack() as stack:
        bridge = MCPBridge(cfg["mcp_servers"], stack)
        await bridge.connect()
        skills = load_skills(Path(cfg["project_root"]))
        reviewer, _ = load_reviewer(Path(cfg["project_root"]))
        system = build_system_prompt(Path(cfg["project_root"]), skills)
        agent = Agent(bridge, LLM(cfg, client), cfg, system, skills, reviewer)
        print("quantctl 交互模式；输入 exit / Ctrl-C 退出")
        while True:
            try:
                text = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if text in ("exit", "quit"):
                return
            if not text:
                continue
            print(await agent.run(text))


def main() -> None:
    parser = argparse.ArgumentParser(description="quant-agent 独立运行入口（不依赖 DeerFlow）")
    parser.add_argument("--config", default=str(HERE / "config.json"),
                        help="配置文件路径（默认 cli/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="自检")
    sub.add_parser("tools", help="列出工具")
    run = sub.add_parser("run", help="一次性执行")
    run.add_argument("prompt")
    sub.add_parser("chat", help="交互式会话")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{config_path}（可从 config.example.json 复制）")
    cfg = load_config(config_path)

    if args.command == "doctor":
        asyncio.run(cmd_doctor(cfg))
    elif args.command == "tools":
        asyncio.run(cmd_tools(cfg))
    elif args.command == "run":
        asyncio.run(cmd_run(cfg, args.prompt))
    else:
        asyncio.run(cmd_chat(cfg))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
