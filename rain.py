#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
rain.py v4.5 —— 超级智能终端 Agent · 心智+基因+验收版（Termux，纯 requests）

【v4.3 修复清单（响应第二轮代码审查）】
  1. 【P0·致命】agent_loop 中 feedbacks / error_feedbacks 的初始化
     上移到验收合同解析循环之前。v4.2 中验收分支在初始化之前就
     append（AI 提交不合格 [accept:] 合同即触发），函数内赋值使
     名称局部化 → UnboundLocalError → 宿主崩溃、会话丢失。
  2. 【P1】unwrap_code 恢复正确围栏判定（ / python / py）。
     退化版（空字符串判定）因 strip() 后末行不可能为空白而恒 False，
     围栏永远不剥 → 脚本全靠自我修复兜底，且进化冒烟测试测不出。
     v4.3 同时在冒烟测试中加入围栏剥离断言，此类退化可被护栏捕获。
  3. 【P2a】用户拒绝执行验收命令 → verify_contract_once 返回 None
     哨兵，不计入 AI 的验收失败次数（用户否决 ≠ AI 失败）。
  4. 【P2b】提示词改为「检查命令严格一行，多语句用分号连接」，
     与解析器逐行解析的实际能力一致，消除静默截断。
  5. 【P3a】检查命令防篡改黑名单加入展开路径（~/rain.py、~/mind、
     ~/evolution 及身份/队列/传记文件）。诚实边界：纯字符串匹配
     仍可被 chr() 拼接等手段绕过，属纵深防御而非沙箱。
  6. 【P3b】新增 /contract [放弃] 命令：查看当前合同详情 / 主动
     放弃。解决「旧合同污染新任务」时用户无感知、无法干预的问题。
  7. 【P4a】合同 ID 加毫秒后缀，同秒注册不再覆盖审计文件。
  8. 【P4b】验收收割反思改用脚本外文本（control），脚本字符串里
     的 [reflect] 字面量不再误入技能库。
  9. 【P4c/d】删除 main 横幅中永不生效的合同显示死代码（合同不
     跨会话保留）；settle_contract 的 retry 分支同样落盘存档。

【v4.1 修复清单（全部保留）】
  1. unwrap_code 结构性剥离围栏，不误伤标识符。
  2. /plan 宿主硬约束（拒绝执行脚本/插件，非提示词建议）。
  3. 插件隔离子进程 + 超时强杀 + 确认（进程隔离，非沙箱）。
  4. 多脚本「失败即停」，并告知 AI 剩余段数。
  5. 自我修复上限批内逐段硬停。
  6. /evolve 注入当前完整源码作修改基线。
  7. atomic_write 原子替换（临时文件+fsync+os.replace）。
  8. /think 任务写入主对话历史。
  9. /idle API 失败任务放回队首。
  10. 控制标签只从脚本外文本解析。
  11. 冒烟测试隔离 HOME。
  12. 子进程剥离 DEEPSEEK_API_KEY；输出头尾保留式截断。
  13. tokenize 感知的结束标签定位。

【v4.2 新增：可执行验收（Acceptance Verification）】
  - [accept:] 验收合同：任务早期声明「产物路径 + 检查命令 + 成功标准」；
    AI 宣称完成时由宿主执行客观验收，不再相信文字收尾。
  - 三层防假裁判（解决"AI 自己写检查命令永远通过"）：
    ① trivial gate：AST 检查命令必须读取世界状态（open/exists/...），
       print('done')/assert True/sys.exit(0) 直接拒绝；
    ② 对照组证伪：正向通过后，宿主把产物临时移开再跑同一条命令，
       它必须失败——不看产物的假裁判必然暴露（藏文件由宿主执行，
       AI 无法欺骗）；
    ③ 合同锁定：首次验收触发即锁定，AI 只能修产物不能改裁判；
       合同只允许追加收紧，不允许删除放宽。
  - 验收通过才：沉淀 [reflect]、未完待办转自主问题队列、/idle 移除任务。
  - 验收失败：真实输出回传给 AI 继续修复；连续 MAX_VERIFY_ATTEMPTS 次
    → 终局失败，任务带失败记录回队。
  - 已知边界：防"假完成"不防"低质量完成"——读了产物但弱断言的命令
    两层都会放行；对照组只对第一个产物证伪；内容强度靠协议与测试员自觉。

【语法安全铁律（进化候选同样被机器验证）】
  · 函数内给全局变量赋值前必须 global，且 global 必须是函数体
    第一条语句（docstring 之后）——v4.2 的 P0 崩溃正是违背了
    "先初始化后使用"的近亲错误，本轮已修复并纳入测试。
  · 自查命令：python -m py_compile rain.py（无输出即通过）。
"""

import ast
import io
import json
import os
import re
import sys
import time
import signal
import shutil
import tempfile
import tokenize
import subprocess
import importlib.util

import requests        # 唯一网络依赖；绝不使用 openai 库

# ============================================================
# 0. 配置区（你只需要改这里）
# ============================================================
API_KEY = ""              # 空占位；实际值由 load_api_key() 在启动时决定
BASE_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
VERSION = 4.5             # 当前基因版本（AI 进化时必须递增，如 4.3→4.4）

AUTO_EXECUTE = False      # False = 执行代码/插件/进化/验收前必须 y 确认
EXEC_TIMEOUT = 180        # 单段脚本最长执行秒数
PLUGIN_TIMEOUT = 120      # 单个插件最长执行秒数（隔离子进程）
VERIFY_TIMEOUT = 60       # 单条验收命令最长执行秒数
MAX_VERIFY_ATTEMPTS = 3   # 验收失败重试上限（之后终局判定）
MAX_ROUNDS = 12           # 单轮最大「AI→执行→回传」往返次数（防死循环）
MAX_SELF_REPAIR = 5       # 脚本自我修复上限
MAX_EVOLVE_RETRY = 3      # 进化验证失败重试上限（有害突变淘汰线）
OBSERVER_AFTER = 2        # 连续失败达此次数 → 触发观察者进程纠偏
THINK_ROUNDS = 2          # /think 深度思考的「批判→重构」轮数
REFLECT_EVOLVE_HINT = 5   # 反思每积累几条就建议一次 /evolve
MAX_OUTPUT_CHARS = 24000  # 回传给 AI 的单段输出截断上限（头尾保留）
MAX_SOURCE_BYTES = 2 * 1024 * 1024   # 进化候选源码大小上限

HOME = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(HOME, "skills")       # 表型记忆（经验笔记）
PLUGINS_DIR = os.path.join(HOME, "plugins")     # 插件（能力器官）
MIND_DIR = os.path.join(HOME, "mind")           # 心智目录
CONFIG_PATH = os.path.join(MIND_DIR, "config.json")     # 敏感配置（API Key 等）
IDENTITY_PATH = os.path.join(MIND_DIR, "identity.md")   # 身份核心
JOURNAL_PATH = os.path.join(MIND_DIR, "journal.md")     # 心智传记
PROBLEMS_PATH = os.path.join(MIND_DIR, "problems.md")   # 自主问题队列
CONTRACTS_DIR = os.path.join(MIND_DIR, "contracts")     # 验收合同存档
EVOLUTION_DIR = os.path.join(HOME, "evolution") # 基因库（版本档案）
SELF_PATH = os.path.abspath(__file__)           # 自身源码路径（进化目标）

# 检查命令防篡改黑名单（字符串匹配纵深防御，非沙箱，可被拼接绕过）
_FORBIDDEN_PATHS = tuple(p for p in (
    SELF_PATH,
    os.path.expanduser("~/rain.py"),
    CONTRACTS_DIR, MIND_DIR, EVOLUTION_DIR,
    IDENTITY_PATH, JOURNAL_PATH, PROBLEMS_PATH,
) if p)

# ---- 标签正则（集中定义）----
PLUGIN_CALL_RE = r'\[plugin:([^\[\]\s]+)\]'
DESC_RE = r'\[desc\](.*?)\[/desc\]'
REFLECT_RE = r'\[reflect\](.*?)\[/reflect\]'
PROBLEM_RE = r'\[problem\](.*?)\[/problem\]'
IDENTITY_RE = r'\[identity\](.*?)\[/identity\]'
ACCEPT_RE = r'\[accept:\]\s*(.*?)\s*\[/accept\]'

# tokenize 中视作「字面量」的 token 类型（兼容 3.12 的 f-string 拆分）
_LITERAL_TOKENS = {tokenize.STRING, tokenize.COMMENT}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    if hasattr(tokenize, _name):
        _LITERAL_TOKENS.add(getattr(tokenize, _name))

# ============================================================
# 1. 全局状态（赋值前必须先声明 global）
# ============================================================
MODE = "build"
messages = []
TODOS = []
LAST_SCRIPT = ""
LAST_DESC = ""
ACTIVE_CONTRACT = None      # 当前任务的验收合同（dict / None）
CURRENT_TASK = ""           # 当前任务描述（合同归属）

# ============================================================
# 2. 通用工具：原子写入 / 输出截断 / 围栏剥离
# ============================================================
def atomic_write(path, text):
    """同目录临时文件 + fsync + os.replace 原子替换。
    中途被杀也不会在主文件留下半截内容。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rain-", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                os.chmod(tmp, os.stat(path).st_mode & 0o777)
            except Exception:
                pass
        os.replace(tmp, path)          # 原子操作：要么全旧，要么全新
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def clip_output(text, limit=MAX_OUTPUT_CHARS):
    """超长输出头尾保留式截断（优先保住末尾 traceback）。"""
    text = text or ""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half]
            + "\n...[输出已截断，总长度 %d 字符，仅保留头尾各 %d]...\n"
            % (len(text), half)
            + text[-half:])


def unwrap_code(text):
    """只剥离「完整」Markdown 围栏（首行是  家族且末行是 ），
    绝不删除任何 Python 标识符。
    v4.3 修复：围栏判定必须是三个反引号——退化版（空字符串）因
    strip() 后末行不可能为空白而恒 False，围栏永远不剥。"""
    text = text.strip()
    lines = text.splitlines()
    if (len(lines) >= 2
            and lines[0].strip().lower() in ("", "python", "py")
            and lines[-1].strip() == ""):
        return "\n".join(lines[1:-1]).strip()
    return text


# ============================================================
# 2.5 API Key 加载（v4.4 新增：config.json 优先 + 交互输入）
# ============================================================
def load_api_key():
    """按优先级获取 API Key：环境变量 -> config.json -> 交互输入并保存。
    返回 Key 字符串（可能为空，由调用方决定是否退出）。
    全程 UTF-8；config.json 损坏/读失败时优雅降级到输入流程。"""
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            key = str(cfg.get("api_key", "")).strip()
            if key:
                return key
        except (json.JSONDecodeError, OSError, ValueError):
            print("[配置] %s 读取失败或格式损坏，将走输入流程。" % CONFIG_PATH)

    print("-" * 46)
    print("未检测到 API Key。请粘贴你的 DeepSeek API Key（回车确认）：")
    print("（将保存到 %s，下次启动自动读取）" % CONFIG_PATH)
    print("-" * 46)
    try:
        key = input("API Key: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消输入。")
        return ""
    while not key:
        try:
            key = input("不能为空，请重新输入 API Key: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消输入。")
            return ""

    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except (json.JSONDecodeError, OSError, ValueError):
            cfg = {}
    cfg["api_key"] = key
    try:
        atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
        print("[配置] 已保存到 %s" % CONFIG_PATH)
    except Exception as e:
        print("[配置] 保存失败（不影响本次运行）：%s" % e)
    return key


# ============================================================
# 3. 技能记忆库（表型学习层）
# ============================================================
def load_skills() -> str:
    """读取 ~/skills/ 下 .md/.txt 注入 System Prompt（只读，无需 global）。"""
    if not os.path.isdir(SKILLS_DIR):
        return ""
    parts = []
    for fn in sorted(os.listdir(SKILLS_DIR)):
        if not (fn.endswith(".md") or fn.endswith(".txt")):
            continue
        try:
            with open(os.path.join(SKILLS_DIR, fn), "r",
                      encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
        except Exception:
            continue
        if content:
            parts.append("## 技能文件：%s\n%s" % (fn, content))
    if not parts:
        return ""
    return "\n\n【技能记忆库（历史沉淀，优先参考其中的经验）】\n" + "\n\n".join(parts)


def save_reflection(content: str):
    """[reflect] 经验自动写入 ~/skills/（并按阈值建议基因进化）。"""
    content = content.strip()
    if not content:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SKILLS_DIR, "反思_" + stamp + ".md")
    n = 1
    while os.path.exists(path):
        path = os.path.join(SKILLS_DIR, "反思_%s_%d.md" % (stamp, n))
        n += 1
    header = ("# 任务反思（AI 自动生成）\n# 时间：%s\n\n"
              % time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + content + "\n")
    print("\n[自我进化] 经验已写入技能库：%s" % path)
    append_journal("反思入库", content)
    refresh_system()
    if os.path.isdir(SKILLS_DIR):
        count = len([f for f in os.listdir(SKILLS_DIR)
                     if f.startswith("反思_")])
        if count and count % REFLECT_EVOLVE_HINT == 0:
            print("[进化建议] 反思已积累 %d 条，输入 /evolve 可让 AI "
                  "把经验固化进自身基因（源码）。" % count)
    return path


def handle_skill(u: str):
    """/skill add 名 内容 与 /skill list。"""
    if re.match(r'/skill\s+list', u, re.I):
        if os.path.isdir(SKILLS_DIR):
            files = [f for f in sorted(os.listdir(SKILLS_DIR))
                     if f.endswith(".md") or f.endswith(".txt")]
            print("技能库（%d 个）：" % len(files))
            for f in files:
                print("  - " + f)
        else:
            print("技能库为空。")
        return
    m = re.match(r'/skill\s+add\s+(\S+)\s+(.+)', u, re.S)
    if not m:
        print("用法：/skill add 文件名 技能内容")
        return
    fname, content = m.group(1), m.group(2).strip()
    if not (fname.endswith(".md") or fname.endswith(".txt")):
        fname += ".md"
    path = os.path.join(SKILLS_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    print("技能已保存：%s" % path)
    refresh_system()

# ============================================================
# 4. 心智系统（身份 / 问题队列 / 传记）
# ============================================================
def load_identity() -> str:
    """读取身份核心，注入 System Prompt 最前端。"""
    try:
        with open(IDENTITY_PATH, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read().strip()
    except Exception:
        return ""
    return ("【身份核心（自我定义，可被 [identity] 重写）】\n" + text + "\n") if text else ""


def save_identity(text: str):
    """把 AI 重写的身份核心写入 ~/mind/identity.md 并即时生效。"""
    text = text.strip()
    if not text:
        return False
    with open(IDENTITY_PATH, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    append_journal("身份重写", text[:300])
    refresh_system()
    print("[心智] 身份核心已重写并即时注入 System Prompt。")
    return True


def load_problems():
    """读取自主问题队列（每行一个问题）。"""
    try:
        with open(PROBLEMS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            return [l.strip() for l in f.read().splitlines() if l.strip()]
    except Exception:
        return []


def save_problem(text: str):
    """登记一个新问题到队列（AI 的自主好奇心）。"""
    text = text.strip()
    if not text:
        return
    with open(PROBLEMS_PATH, "a", encoding="utf-8") as f:
        f.write(text.replace("\n", " ") + "\n")
    append_journal("发现新问题", text)
    print("[好奇心] 新问题已登记：%s" % text)


def take_problem():
    """从队列取出第一个问题（/idle 自主模式领取任务）。"""
    lines = load_problems()
    if not lines:
        return None
    rest = lines[1:]
    with open(PROBLEMS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(rest) + ("\n" if rest else ""))
    return lines[0]


def requeue_problem_front(task):
    """/idle 失败时把任务放回队列头部，避免任务丢失。"""
    lines = load_problems()
    if task not in lines:
        lines.insert(0, task)
    with open(PROBLEMS_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def append_journal(tag: str, text: str):
    """向心智传记追加一条带时间戳的记录。"""
    try:
        os.makedirs(MIND_DIR, exist_ok=True)
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s ｜ %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), tag,
                       text.replace("\n", " ")[:500]))
    except Exception:
        pass    # 传记写失败不影响主流程


def extract_problems(text: str):
    """提取 AI 输出里的所有 [problem]...[/problem]。"""
    return [p.strip() for p in re.findall(PROBLEM_RE, text, re.S) if p.strip()]


def extract_identity(text: str) -> str:
    """提取 [identity]...[/identity]（身份自我重写）。"""
    m = re.search(IDENTITY_RE, text, re.S)
    return m.group(1).strip() if m else ""

# ============================================================
# 5. 插件系统（扫描在宿主；执行走隔离子进程）
# ============================================================
def scan_plugins():
    """扫描 ~/plugins/*.py 头部 @name/@desc，返回。"""
    plugins = {}
    if not os.path.isdir(PLUGINS_DIR):
        return plugins, ""
    for fn in sorted(os.listdir(PLUGINS_DIR)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        path = os.path.join(PLUGINS_DIR, fn)
        pid = fn[:-3]
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(3000)
        except Exception:
            continue
        m = re.search(r'@name[:：]?\s*(.+)', head)
        name = m.group(1).strip() if m else pid
        m = re.search(r'@desc[:：]?\s*(.+)', head)
        desc = m.group(1).strip() if m else ""
        plugins[pid] = {"name": name, "desc": desc}
    if not plugins:
        return plugins, ""
    lines = ["\n\n【可用插件列表（[plugin:文件名] 调用，不带 .py）】"]
    for pid, info in plugins.items():
        lines.append("- %s ｜ @name: %s ｜ @desc: %s"
                     % (pid, info["name"], info["desc"] or "无描述"))
    return plugins, "\n".join(lines)


# 插件子进程引导代码：在独立进程里导入插件并调用 run()
_PLUGIN_RUNNER = (
    "import importlib.util, sys\n"
    "path = sys.argv[1]\n"
    "spec = importlib.util.spec_from_file_location('rain_plugin_isolated', path)\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "if not hasattr(mod, 'run'):\n"
    "    sys.stdout.write('插件缺少 run() 函数')\n"
    "    sys.exit(3)\n"
    "ret = mod.run()\n"
    "if ret is None:\n"
    "    sys.stdout.write('(插件 run() 无返回值)')\n"
    "elif isinstance(ret, str):\n"
    "    sys.stdout.write(ret)\n"
    "else:\n"
    "    sys.stdout.write(repr(ret))\n"
)


def run_plugin(pid: str) -> str:
    """在隔离子进程中执行插件（超时可杀、无法污染宿主进程状态）。
    注意：这是进程隔离，不是沙箱——插件对文件系统的权限与宿主相同。"""
    pid = pid.strip()
    if pid.endswith(".py"):
        pid = pid[:-3]
    path = os.path.join(PLUGINS_DIR, pid + ".py")
    if not os.path.exists(path):
        return "插件 %s 不存在于 %s" % (pid, PLUGINS_DIR)
    rc, out, err, timed_out = _run_process(
        [sys.executable, "-c", _PLUGIN_RUNNER, path], PLUGIN_TIMEOUT)
    if timed_out:
        return ("插件 %s 执行超时（>%d 秒），进程组已被强制终止。"
                % (pid, PLUGIN_TIMEOUT))
    if rc == 3:
        return (out or "插件缺少 run() 函数")
    if rc != 0:
        return ("插件执行异常（隔离子进程退出码 %d）:\n%s"
                % (rc, clip_output(err) or clip_output(out)))
    return clip_output(out) if out.strip() else "(插件 run() 无返回值)"

# ============================================================
# 6. 进化引擎（基因型进化 + 多重护栏 + 原子替换）
# ============================================================
def get_gen() -> int:
    """当前基因代数 = 基因库中历史版本数 + 1。"""
    if not os.path.isdir(EVOLUTION_DIR):
        return 1
    gens = []
    for fn in os.listdir(EVOLUTION_DIR):
        m = re.match(r'rain_gen(\d+)_', fn)
        if m:
            gens.append(int(m.group(1)))
    return max(gens) + 1 if gens else 1


def check_global_positions(source: str):
    """护栏：AST 检查所有 global 声明位于函数体第一条语句。
    返回不合规函数名列表（空 = 通过）。这条护栏正是 v1 踩坑
    经验的代码化——教训写进基因，机器代代执行。"""
    problems = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not any(isinstance(n, ast.Global) for n in ast.walk(node)):
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]          # docstring 可以在 global 之前
        if not (body and isinstance(body[0], ast.Global)):
            problems.append(node.name)
    return problems


def extract_version(source: str):
    """从源码顶部提取 VERSION 数字；找不到返回 None。"""
    m = re.search(r'^VERSION\s*=\s*([0-9]+(?:\.[0-9]+)?)', source, re.M)
    return float(m.group(1)) if m else None


def verify_candidate(source: str):
    """静态验证：结构完整 → 版本递增 → 语法编译 → AST global 位置。
    返回 (是否通过, 新版本号或错误消息)。"""
    for marker in ("def main", "def call_api", "def apply_evolution",
                   "def verify_contract_once", "def is_trivial_check"):
        if marker not in source:
            return False, "候选源码结构不完整（缺少 %s），疑似误传。" % marker
    new_ver = extract_version(source)
    if new_ver is None:
        return False, "候选源码缺少 VERSION 声明。"
    if new_ver <= VERSION:
        return False, "候选版本 %s 未高于当前版本 %s，拒绝倒退。" % (new_ver, VERSION)
    try:
        compile(source, "<candidate>", "exec")          # 护栏：语法编译
    except SyntaxError as e:
        return False, "语法错误: 第 %d 行: %s" % (e.lineno, e.msg)
    problems = check_global_positions(source)           # 护栏：global 位置
    if problems:
        return False, "global 声明位置违规的函数: %s" % ", ".join(problems)
    return True, new_ver


def smoke_test(cand_path: str):
    """护栏：在独立子进程 + 隔离 HOME 下导入候选模块并冒烟测试。
    候选的模块级副作用只会落在临时 HOME，碰不到真实家目录。
    v4.3：新增围栏剥离断言——unwrap_code 退化（如反引号丢失）
    会被护栏当场捕获，不再静默通过。"""
    test_code = (
        "import sys, types\n"
        "try:\n"
        "    import requests\n"
        "except Exception:\n"
        "    sys.modules['requests'] = types.ModuleType('requests')\n"
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('candidate', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "pairs = mod.extract_scripts('[desc]t[/desc][script:]print(1)[script]')\n"
        "assert pairs and pairs[0][0] == 'print(1)', 'extract_scripts 失效'\n"
        "assert mod.unwrap_code('print(1)') == 'print(1)', 'unwrap 误伤代码'\n"
        "fenced = mod.unwrap_code(\"python\\nimport os\\n\")\n"
        "assert fenced == 'import os', 'unwrap 围栏剥离失效（退化型缺陷）'\n"
        "mod.update_todos('[todo]a|b[/todo]')\n"
        "assert len(mod.TODOS) == 2, 'todo 解析失效'\n"
        "sp = mod.build_system_prompt()\n"
        "assert 'Rain' in sp, 'System Prompt 组装失效'\n"
        "rc, out, err = mod.run_python_code(\"print('smoke_ok')\")\n"
        "assert rc == 0 and 'smoke_ok' in out, '子进程执行失效'\n"
        "assert mod.extract_reflection('[reflect]r[/reflect]') == 'r', '反思解析失效'\n"
        "assert mod.extract_problems('[problem]p[/problem]') == ['p'], '问题解析失效'\n"
        "assert mod.extract_identity('[identity]i[/identity]') == 'i', '身份解析失效'\n"
        "evos = mod.extract_evolution('[evolve:r]def x(): pass\\n[/evolve]')\n"
        "assert evos and evos[0][0] == 'r' and 'def x' in evos[0][1], '进化解析失效'\n"
        "bad, _r = mod.is_trivial_check(\"print('x')\")\n"
        "assert bad, '验收 trivial gate 失效'\n"
        "cl = mod.parse_accept_blocks('[accept:]\\n产物: /tmp/x\\n"
        "检查: open(\"/tmp/x\")\\n标准: s\\n[/accept]')\n"
        "assert cl and cl[0]['artifacts'] == ['/tmp/x'], '验收合同解析失效'\n"
        "print('SMOKE_OK')\n"
    ) % cand_path
    env = safe_child_env()
    smoke_home = tempfile.mkdtemp(prefix="rain_smoke_home_")
    env["HOME"] = smoke_home          # 隔离：候选读写的 ~ 全部指向临时目录
    try:
        rc, out, err, timed_out = _run_process(
            [sys.executable, "-c", test_code], 120, env=env)
        if timed_out:
            return False, "冒烟测试超时（>120 秒）"
        if rc == 0 and "SMOKE_OK" in out:
            return True, "冒烟测试通过（隔离 HOME）"
        return False, ("冒烟测试失败（退出码 %d）: %s"
                       % (rc, clip_output(err or out)[-800:]))
    finally:
        shutil.rmtree(smoke_home, ignore_errors=True)


def apply_evolution(source: str, reason: str):
    """进化主流程：静态验证 → 原子落盘候选 → 隔离冒烟 → 备份旧基因
    → 原子替换主文件。任何一步失败，旧版本毫发无损。"""
    source = source.strip()
    if len(source.encode("utf-8", "ignore")) > MAX_SOURCE_BYTES:
        return False, "候选源码超过 %d 字节上限，疑似异常输出。" % MAX_SOURCE_BYTES
    ok, info = verify_candidate(source)
    if not ok:
        return False, info
    new_ver = info
    os.makedirs(EVOLUTION_DIR, exist_ok=True)
    cand_dir = os.path.join(EVOLUTION_DIR, "candidates")
    os.makedirs(cand_dir, exist_ok=True)
    cand_path = os.path.join(cand_dir, "rain_candidate.py")
    atomic_write(cand_path, source + "\n")
    ok2, msg2 = smoke_test(cand_path)
    if not ok2:
        return False, msg2        # 有害突变：候选被淘汰，旧基因保留
    gen = get_gen()
    backup = os.path.join(EVOLUTION_DIR,
                          "rain_gen%d_%s.py" % (gen, time.strftime("%Y%m%d_%H%M%S")))
    try:
        with open(SELF_PATH, "r", encoding="utf-8", errors="ignore") as f:
            cur_text = f.read()
        atomic_write(backup, cur_text)
    except Exception as e:
        return False, "备份当前基因失败：%s（未做任何替换）" % e
    atomic_write(SELF_PATH, source + "\n")   # 原子替换主文件
    print("\n[自主进化] v%s → v%s 成功！旧基因备份：%s" % (VERSION, new_ver, backup))
    print("[自主进化] 当前进程仍运行旧基因，重启后新基因生效。")
    return True, ("进化成功！自身源码已升级为 v%s（原因：%s）。\n"
                  "旧基因已备份，可用 /versions 查看谱系、/rollback 回滚。\n"
                  "注意：当前进程仍运行旧代码，请在总结里提醒用户重启 "
                  "python %s 以激活新基因；不要再输出 [evolve] 或 [script:]。"
                  % (new_ver, reason or "未说明", SELF_PATH))


def list_versions():
    """/versions：打印基因谱系。"""
    if not os.path.isdir(EVOLUTION_DIR):
        print("基因库为空（尚未进化过）。")
        return []
    files = sorted([f for f in os.listdir(EVOLUTION_DIR)
                    if re.match(r'rain_gen\d+_', f)])
    if not files:
        print("基因库为空（尚未进化过）。")
        return files
    print("基因谱系（共 %d 代历史，当前运行 Gen-%d）：" % (len(files), get_gen()))
    for i, fn in enumerate(files, 1):
        print("  [%d] %s" % (i, fn))
    return files


def rollback(idx: int):
    """/rollback N：把第 N 代基因恢复为主文件（先备份当前，原子替换）。"""
    files = list_versions()
    if not files or idx < 1 or idx > len(files):
        print("序号无效，用 /versions 查看谱系。")
        return
    src_path = os.path.join(EVOLUTION_DIR, files[idx - 1])
    with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
        src_text = f.read()
    ver = extract_version(src_text)
    gen = get_gen()
    backup = os.path.join(EVOLUTION_DIR,
                          "rain_gen%d_%s.py" % (gen, time.strftime("%Y%m%d_%H%M%S")))
    try:
        with open(SELF_PATH, "r", encoding="utf-8", errors="ignore") as f:
            cur_text = f.read()
        atomic_write(backup, cur_text)
    except Exception as e:
        print("备份当前基因失败：%s（未做替换）" % e)
        return
    atomic_write(SELF_PATH, src_text)
    print("已回滚到 %s（版本 v%s），当前基因已另存。重启后生效。"
          % (files[idx - 1], ver))

# ============================================================
# 6.5 验收引擎（可执行验收 + 三层防假裁判）
# ============================================================
def _dotted_name(node):
    """AST 调用节点 → 点分名（如 os.path.exists）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return base + "." + node.attr if base else None
    return None

# 「世界状态探针」白名单：检查命令必须至少调用其一
_PROBE_NAMES = {"open", "glob", "iglob", "subprocess", "requests",
                "Path", "urlopen", "filecmp"}
_PROBE_DOTTED = {"os.path.exists", "os.path.isfile", "os.path.isdir",
                 "os.listdir", "os.stat", "os.lstat", "os.scandir",
                 "os.access", "os.path.getsize", "os.path.getmtime",
                 "os.environ", "os.walk"}
_PROBE_ATTRS = {"exists", "isfile", "isdir", "read_text", "read_bytes",
                "scandir", "stat"}


def is_trivial_check(cmd):
    """第 1 层防线：检查命令必须「读取世界状态」。
    print('done') / assert True / sys.exit(0) 这类纯字面量假裁判，
    在合同注册时直接拒绝。返回 (是否平凡, 原因)。"""
    try:
        tree = ast.parse(cmd)
    except SyntaxError as e:
        return True, "检查命令无法编译（第 %d 行: %s）" % (e.lineno, e.msg)
    has_probe = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name:
                if name in _PROBE_NAMES or name in _PROBE_DOTTED:
                    has_probe = True
                elif name.split(".")[-1] in _PROBE_ATTRS:
                    has_probe = True
    if not has_probe:
        return True, ("命令未读取任何世界状态（文件/目录/网络/环境），"
                      "纯字面量输出无法验证任何产物")
    return False, ""


def parse_accept_blocks(text):
    """解析 [accept:] 合同声明。格式（每行一条，检查命令严格一行）：
    [accept:]
    产物: /sdcard/Download/report.md
    检查: import os; assert os.path.exists("...")
    标准: 文件存在且含汇总表
    [/accept]
    返回 [{"artifacts":[绝对路径], "checks":[命令], "criteria":文本}]"""
    blocks = []
    for m in re.finditer(ACCEPT_RE, text, re.S):
        artifacts, checks, criteria = [], [], []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            ma = re.match(r'^(?:产物|artifact)\s*[：:]\s*(.+)$', line, re.I)
            if ma:
                p = os.path.abspath(os.path.expanduser(ma.group(1).strip()))
                if p:
                    artifacts.append(p)
                continue
            mc = re.match(r'^(?:检查|check)\s*[：:]\s*(.+)$', line, re.I)
            if mc:
                checks.append(mc.group(1).strip())
                continue
            mr = re.match(r'^(?:标准|criteria)\s*[：:]\s*(.+)$', line, re.I)
            if mr:
                criteria.append(mr.group(1).strip())
        blocks.append({"artifacts": artifacts, "checks": checks,
                       "criteria": "；".join(criteria)})
    return blocks


def _contract_path(cid):
    return os.path.join(CONTRACTS_DIR, cid + ".json")


def save_contract(c):
    """合同存档到 ~/mind/contracts/（审计用，写失败不影响当次验收）。"""
    try:
        os.makedirs(CONTRACTS_DIR, exist_ok=True)
        with open(_contract_path(c["id"]), "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("[验收] 合同存档失败（不影响当次验收）：%s" % e)


def register_contract(clause, task_desc, append_to=None):
    """登记/追加验收合同。返回 (合同或None, 错误消息或None)。
    只允许追加收紧（新产物/新检查），不允许删除已有条款。"""
    art = clause.get("artifacts") or []
    chk = clause.get("checks") or []
    if not art:
        return None, "合同缺少「产物:」声明——没有预期产物就无法客观验收。"
    if not chk:
        return None, "合同缺少「检查:」命令。"
    for c in chk:                                  # 第 1 层：trivial gate
        bad, reason = is_trivial_check(c)
        if bad:
            return None, ("检查命令被拒绝（%s）。请写真正读取产物并断言"
                          "其内容/结构的命令。" % reason)
    for c in chk:                                  # 第 3 层：防篡改（纵深防御）
        for forbidden in _FORBIDDEN_PATHS:
            if forbidden and forbidden in c:
                return None, "检查命令禁止引用自身源码/心智目录/基因库。"
    if append_to is None:
        # v4.3：ID 加毫秒后缀，同秒注册不互相覆盖
        cid = ("task_" + time.strftime("%Y%m%d_%H%M%S")
               + "_" + str(int(time.time() * 1000) % 10000))
        c = {"id": cid, "task": task_desc or CURRENT_TASK,
             "created": time.strftime("%Y-%m-%d %H:%M:%S"),
             "artifacts": [{"path": p, "existed": os.path.exists(p),
                            "size": (os.path.getsize(p)
                                     if os.path.exists(p) else 0)}
                           for p in art],
             "checks": [{"cmd": c, "added": time.strftime("%H:%M:%S")}
                        for c in chk],
             "criteria": clause.get("criteria", ""),
             "locked": False, "verify_attempts": 0, "status": "open"}
        save_contract(c)
        print("\n[验收] 合同已登记：%d 个产物 × %d 条检查%s"
              % (len(art), len(chk),
                 ("（标准：%s）" % c["criteria"]) if c["criteria"] else ""))
        return c, None
    # 追加收紧（仅未锁定时；锁定路径在 agent_loop 已拦截，此处兜底）
    if append_to.get("locked"):
        return None, "验收已触发，合同锁定，禁止修改裁判。只能修复产物本身。"
    known = {a["path"] for a in append_to["artifacts"]}
    append_to["artifacts"].extend(
        [{"path": p, "existed": os.path.exists(p),
          "size": os.path.getsize(p) if os.path.exists(p) else 0}
         for p in art if p not in known])
    append_to["checks"].extend(
        [{"cmd": c, "added": time.strftime("%H:%M:%S")} for c in chk])
    if clause.get("criteria"):
        append_to["criteria"] = (append_to.get("criteria", "")
                                 + "；" + clause["criteria"])
    save_contract(append_to)
    print("[验收] 合同已追加收紧（现有 %d 产物 × %d 检查）"
          % (len(append_to["artifacts"]), len(append_to["checks"])))
    return append_to, None


def _hide_artifact(p):
    """对照组操作：临时移开产物，返回备份路径。"""
    backup = p + ".rain_ctrl_hidden"
    os.rename(p, backup)
    return backup


def _restore_artifact(p, backup):
    """恢复产物（若检查命令在对照期创建了同名垃圾，先清除）。"""
    if os.path.exists(p):
        if os.path.isdir(backup):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    os.rename(backup, p)


def _run_check_cmd(cmd):
    """在隔离子进程中执行一条验收检查命令。"""
    return _run_process([sys.executable, "-c", cmd], VERIFY_TIMEOUT)


def verify_contract_once(c):
    """执行一轮完整验收。返回 (passed, 报告文本)：
    passed=True/False 为正常判定；passed=None 表示用户拒绝执行检查
    命令（v4.3：用户否决不计入 AI 的验收失败次数）。
    第 2 层防线——对照组证伪：正向通过后，把第一个产物临时移开
    再跑同一条命令，它必须失败；仍通过 = 假裁判（永远通过型），
    验收判负。文件操纵由宿主执行，AI 无法欺骗。
    已知边界：对照组只对第一个产物做证伪；防假完成不防低质量完成。"""
    arts = c["artifacts"]
    missing = [a["path"] for a in arts if not os.path.exists(a["path"])]
    if missing:
        return False, ("预期产物不存在：%s\n请先真正产出它，再宣称完成。"
                       % "\n  ".join(missing))
    target = arts[0]["path"]
    lines = []
    for chk in c["checks"]:
        cmd = chk["cmd"]
        if not confirm_execute("验收检查命令：%s"
                               % (c.get("criteria") or cmd[:60])):
            return None, "用户拒绝执行验收命令，本次验收中止（不计失败次数）。"
        rc, out, err, timed = _run_check_cmd(cmd)
        if timed:
            return False, "检查命令超时（>%d 秒）。" % VERIFY_TIMEOUT
        if rc != 0:
            return False, ("检查命令未通过（退出码 %d）:\n%s"
                           % (rc, clip_output(err or out)))
        # ---- 对照组证伪：藏起产物，命令必须改判 ----
        try:
            backup = _hide_artifact(target)
        except OSError as e:
            return False, ("对照组无法移开产物（%s），无法验证裁判"
                           "分辨力。" % e)
        try:
            rc2, _out2, _err2, timed2 = _run_check_cmd(cmd)
        finally:
            _restore_artifact(target, backup)
        if rc2 == 0 and not timed2:
            return False, ("【假裁判】已把产物 %s 临时移开，检查命令仍然"
                           "通过——它没有真正在验证该产物（永远通过型"
                           "命令）。请改写为真正读取并断言产物内容的命令；"
                           "若确实不可行，向用户文字说明。" % target)
        lines.append("检查通过（含对照证伪）：%.60s..."
                     % cmd.replace("\n", " "))
    return True, ("全部 %d 条检查通过（每条均经对照组证伪：藏起产物后"
                  "命令按预期失败，证明裁判有效）。\n  - %s"
                  % (len(c["checks"]), "\n  - ".join(lines)))


def settle_contract(passed, reply_text):
    """验收结算：通过→收割反思（v4.3：只从脚本外文本 reply_text 收割，
    脚本字符串里的 [reflect] 字面量不再误入库）+未完待办转问题；
    失败→计数并落盘。返回 "verified" / "retry" / "failed" / "ok"。"""
    c = ACTIVE_CONTRACT
    if c is None:
        return "ok"
    c["verify_attempts"] += 1
    if passed:
        c["status"] = "passed"
        save_contract(c)
        append_journal("验收通过", c["task"])
        # 未完成待办 → 登记为自主问题（好奇心联动：未竟之事不消失）
        undone = [t for done, t in TODOS if not done]
        for t in undone:
            save_problem("未竟待办（来自已验收任务）：%s" % t)
        if undone:
            print("[验收] %d 个未完成待办已转入自主问题队列。" % len(undone))
        refl = extract_reflection(reply_text or "")
        if refl:
            save_reflection(refl)
        print("\n[验收] ✅ 通过——经验获准沉淀，任务正式关闭。")
        return "verified"
    save_contract(c)      # v4.3：retry 时同样落盘，审计不丢次数
    if c["verify_attempts"] >= MAX_VERIFY_ATTEMPTS:
        c["status"] = "failed_final"
        save_contract(c)
        append_journal("验收终局失败",
                       "%s（%d 次未过）" % (c["task"], c["verify_attempts"]))
        print("\n[验收] ❌ 连续 %d 次未通过——终局判定为未完成。"
              % MAX_VERIFY_ATTEMPTS)
        return "failed"
    return "retry"


def begin_task_slot(desc):
    """开启新任务槽：挂起未决合同（如有），记录任务描述。"""
    global ACTIVE_CONTRACT, CURRENT_TASK
    if ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open":
        ACTIVE_CONTRACT["status"] = "abandoned"
        save_contract(ACTIVE_CONTRACT)
        append_journal("合同挂起",
                       "%s（新任务开始，旧合同未验收）"
                       % ACTIVE_CONTRACT["task"])
    ACTIVE_CONTRACT = None
    CURRENT_TASK = (desc or "")[:200]


def note_task(desc):
    """记录任务描述；合同存续期间视为同一任务延续，不覆盖。"""
    global CURRENT_TASK
    if not (ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open"):
        CURRENT_TASK = (desc or "")[:200]

# ============================================================
# 7. 协议解析（tokenize 感知：字面量里的标签不算协议）
# ============================================================
def _literal_spans(source):
    """返回 (字符串/注释字面量区间列表, tokenize 是否完整成功)。"""
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(pos):
        row, col = pos
        idx = row - 1
        if idx >= len(starts):
            return len(source)
        return starts[idx] + col

    spans = []
    ok = True
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in _LITERAL_TOKENS:
                spans.append((offset(token.start), offset(token.end)))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        ok = False
    return spans, ok


def _find_code_end(text, start, markers):
    """在 text[start:] 中找结束标签（必须位于字面量之外）。
    markers 可为 str 或字符串序列；多候选时取最先出现的有效标签。
    返回 (位置, 命中的标签)；未找到返回 (-1, None)。
    tokenize 失败（残缺代码）时退化为取最后一个匹配（rfind 语义）。"""
    if isinstance(markers, str):
        markers = (markers,)
    else:
        markers = tuple(markers)
    source = text[start:]
    spans, ok = _literal_spans(source)

    def _earliest(offset):
        best_pos, best_marker = -1, None
        for m in markers:
            p = source.find(m, offset)
            if p >= 0 and (best_pos < 0 or p < best_pos):
                best_pos, best_marker = p, m
        return best_pos, best_marker

    if not ok:
        best_pos, best_marker = -1, None
        for m in markers:
            p = source.rfind(m)
            if p > best_pos:
                best_pos, best_marker = p, m
        return (-1, None) if best_pos < 0 else (start + best_pos, best_marker)

    cursor = 0
    while True:
        pos, marker = _earliest(cursor)
        if pos < 0:
            return -1, None
        if not any(left <= pos < right for left, right in spans):
            return start + pos, marker
        cursor = pos + len(marker)
def _evolution_parts(text):
    """解析进化块。返回 (block 或 None, 错误消息 或 None)。
    block = (起始, 结束后, 原因, 源码)。"""
    match = re.search(r"\[evolve:([^\]\r\n]*)\]", text)
    if not match:
        return None, None
    try:
        end, end_tag = _find_code_end(text, match.end(), "[/evolve]")
    except Exception:
        end, end_tag = -1, None
    if end < 0:
        # 无有效结束标签：仅当后半段像源码时才视为残缺进化块
        # （防止闲聊中提到 [evolve: 被误判；也防止其内部代码被执行）
        if "def " in text[match.end():]:
            return None, "进化块缺少有效的 [/evolve] 结束标签"
        return None, None
    source = unwrap_code(text[match.end():end])
    if not source or "def " not in source:
        return None, None      # 只是提及格式，不是真实进化
    return (match.start(), end + len(end_tag),
            match.group(1).strip(), source), None


def evolution_status(text):
    """返回 (进化块列表, 解析错误或 None)。"""
    block, err = _evolution_parts(text)
    return ([(block[2], block[3])] if block else []), err


def extract_evolution(text):
    """兼容旧接口：只返回进化块列表。"""
    return evolution_status(text)[0]


def strip_evolution(text):
    """把进化块整段剥离；残缺进化块（缺结束标签）从起始标签起
    全部剥离，防止其内部 [script:] 被误执行。"""
    block, err = _evolution_parts(text)
    if block:
        return (text[:block[0]]
                + "\n[进化源码已提取，交由进化引擎验证]\n"
                + text[block[1]:])
    if err:
        match = re.search(r"\[evolve:([^\]\r\n]*)\]", text)
        return (text[:match.start()]
                + "\n[进化块缺少 [/evolve] 结束标签，已整体剥离以防内部代码被误执行]\n")
    return text


def _script_blocks(text):
    """解析所有 [script:]...[/script] 或 [script:]...[script] 块
    （结束标签需在字面量之外，多候选时取最先出现的有效者）。
    返回 [(起始, 结束后, 代码, 就近 desc), ...]；缺结束标签抛 ValueError。"""
    blocks = []
    cursor = 0
    end_markers = ("[/script]", "[script]")
    while True:
        start = text.find("[script:]", cursor)
        if start < 0:
            return blocks
        body_start = start + len("[script:]")
        end, end_tag = _find_code_end(text, body_start, end_markers)
        if end < 0:
            raise ValueError("脚本块缺少有效的 [/script] 或 [script] 结束标签")
        desc_matches = list(re.finditer(DESC_RE, text[cursor:start], re.S))
        desc = desc_matches[-1].group(1).strip() if desc_matches else ""
        blocks.append((start, end + len(end_tag),
                    unwrap_code(text[body_start:end]), desc))
        cursor = end + len(end_tag)
def extract_scripts(text):
    """[(代码, 就近 desc), ...]。"""
    try:
        blocks = _script_blocks(text)
    except ValueError:
        return []
    return [(code, desc) for _s, _e, code, desc in blocks if code]


def _without_scripts(text, blocks):
    """把脚本块整段替换为占位符（倒序替换保住下标）。
    控制标签只从脚本之外的文本解析。"""
    for start, end, _code, _desc in reversed(blocks):
        text = text[:start] + "\n[脚本已提取]\n" + text[end:]
    return text


def update_todos(text):
    """[todo] 重建清单（去重）；[tododone] 精确匹配打勾。"""
    global TODOS
    match = re.search(r"\[todo\](.*?)\[/todo\]", text, re.S)
    if match:
        items = list(dict.fromkeys(
            item.strip() for item in match.group(1).split("|") if item.strip()))
        if items:
            TODOS = [[False, item] for item in items]
    for done in re.findall(r"\[tododone\](.*?)\[/tododone\]", text, re.S):
        for item in TODOS:
            if done.strip() == item[1]:
                item[0] = True
                break

# ============================================================
# 8. System Prompt（三角色 + 自我修复 + 心智 + 验收 + 进化协议）
# ============================================================
BASE_PROMPT = """你是 Rain，一个运行在 Android 手机 Termux 终端里的超级智能自主 Agent。
你由三层构成：执行体（三角色协作）、心智（观察者监督/问题队列/身份核心/验收合同）、
基因（可通过重写自身源码实现真正的自主进化）。

【运行环境（务必遵守）】
- Android Termux，无图形界面。家目录 os.path.dirname(os.path.abspath(__file__))。
- 系统包 pkg install <名>；Python 包 pip install <名>（已装 requests）。
- pip 卡住换源：pip install 包名 -i [https://pypi.tuna.tsinghua.edu.cn/simple](https://pypi.tuna.tsinghua.edu.cn/simple)
- 你的代码会被宿主 subprocess 自动执行，必须自包含、可直接运行。
- 禁止图形界面操作；全部 UTF-8。

【三角色协作（复杂任务必须体现）】
1.【架构师】拆解任务：[todo]任务1|任务2[/todo]；
  完成一项输出 [tododone]任务名[/tododone]（与清单一致）。
2.【测试员】代码落地前审查逻辑（变量未定义/编码/路径/依赖/除零），
  格式：【测试员审查】……
3.【程序员】输出代码：[script:]...可直接运行的 Python...[script]
  每段脚本前必须有一行 [desc]一句话目的[/desc]。

【自我修复铁律】
- 收到带报错的【系统自动回传】：绝不询问用户、绝不只道歉。
  立刻分析根因→修正→重新输出 [script:]。最多自动重试 5 次直到退出码 0。
- 修复套路：缺库→先 pip/pkg 安装；乱码→UTF-8；路径→expanduser("~")；
  网络→requests 加 timeout。
- 收到「【观察者纠偏】」时，那是你的监督进程给出的诊断，必须优先采纳。
- 注意：同一回复里多段脚本遵循「失败即停」——第一段失败后其余不执行，
  你需要修复后重新输出剩余部分。

【可执行验收协议（客观完成标准——宿主不信文字宣称）】
- 接到任务后、动手写第一段代码之前，必须先声明验收合同：
  [accept:]
  产物: <绝对路径，可多行>（任务的可见交付物）
  检查: <严格一行 Python 命令，真实读取产物并断言其内容/结构；
        多条语句用分号 ; 连接在同一行内，禁止跨行>
  标准: <一句话描述怎样算合格>
  [/accept]
- 检查命令硬性要求（宿主会机器验证）：
  1) 必须真实读取产物（open/os.path.exists/Path.read_text 等），
     纯 print/assert 常量的「永远通过」命令会被直接拒绝；
  2) 宿主会做「对照组证伪」：临时移开你的产物再跑命令，它必须失败——
     不看产物的假裁判必然暴露；
  3) 命令禁止引用 rain.py 自身、心智目录、基因库（含展开路径）。
- 任务完成时输出文字总结（不再输出代码），宿主将执行合同验收：
  通过→经验沉淀、未竟待办自动转入问题队列、任务正式关闭；
  失败→你会收到实际输出，必须继续修复（最多 3 次验收机会，
  之后终局判定未完成）。用户拒绝执行检查命令不计入你的失败次数。
- 验收触发后合同锁定：只能修产物，不能改裁判。认为标准有误就向
  用户文字说明，由用户裁决。
- 合同只允许追加收紧（更多产物/更严检查），不允许删除放宽。

【自主心智协议】
- 执行中发现值得探索的新问题，输出 [problem]描述[/problem]，
  宿主登记到 ~/mind/problems.md；用户 /idle 时你从队列领任务自主解决
  （自主任务同样必须先声明验收合同）。
- 你可以随时重写自己的身份核心：输出 [identity]新身份描述[/identity]，
  宿主存入 ~/mind/identity.md 并即时注入你的系统提示词最前端。

【自我反思】
- 任务彻底完成、不再需要代码时，总结后输出：
  [reflect]本次经验：踩过的坑、有效做法、可复用技巧[/reflect]
- 已声明验收合同的任务，必须先通过宿主客观验收，[reflect] 才会被
  沉淀；未过验收的经验不进入技能库。
- 宿主自动写入 ~/skills/；反思每积累到阈值会提示你把经验固化进基因。

【自主进化协议（最高权限：重写自身源码 rain.py）】
- 收到「【进化指令】」时，消息里会附带你的当前完整源码；
  必须以它为基线做最小必要修改，禁止凭记忆重写整个文件。
- 输出格式：
  [evolve:一句话改进说明]
  ...完整 rain.py 新源码（一字不落的完整文件）...
  [/evolve]
- 硬性要求（宿主逐条机器验证，违反即淘汰）：
  1) 顶部 VERSION 必须高于当前版本（小步递增，如 4.3→4.4）。
  2) 【测试员】先自查：零语法错误；所有 global 声明必须位于函数体
     第一条语句（docstring 之后）——血泪基因，绝不能破坏。
  3) 不得破坏任何已有命令与标签功能（含验收协议）。
  4) 完整源码用 [evolve:原因]...[/evolve] 包裹；源码内部禁止
     Markdown 代码块包裹。
  5) 宿主执行「结构→版本→编译→AST→冒烟测试(隔离HOME)→原子替换+备份」
     多重护栏，任一步失败会回传报错，你须分析修正后重发（最多 3 次）。
  6) 进化成功后旧进程仍跑旧代码，要在总结里提醒用户重启激活新基因。

【插件调用】
- 复用能力输出：[plugin:插件文件名]（不带 .py，可用中文）。
  插件在隔离子进程中执行，超时会被强制终止。

【回传语义】
- 「【系统自动回传】」= 脚本/插件/进化/验收的真实结果：
  未完成→继续修复或推进；已完成→文字总结+[reflect]，不再输出代码。
- 退出码 0 但无输出 ≠ 任务完成，宿主会提示你确认实际效果。
"""

PLAN_RULES = """
【当前模式：/plan 规划模式】
- 只由【架构师】做分析与拆解，严禁输出任何 [script:] 代码。
- 宿主会在规划模式硬性拒绝执行脚本/插件（这是模式约束，不是提示词建议）。
- 规划内容应包含建议的验收合同（产物/检查/标准），供 /build 后使用。
- 必须用 [todo]...[/todo] 给出清单并说明关键步骤。
"""

BUILD_RULES = """
【当前模式：/build 执行模式】
- 三角色全流程：规划→审查→[script:] 代码，自动执行并回传，
  出错自我修复直到完成；完成后输出 [reflect]。
- 优先复用插件；发现自身可改进处可主动发起 [evolve:]。
- 有验收合同时：完成 = 宿主验收通过，不是你宣称完成。
"""

THINK_SYS = """你是 Rain 的深度思考子系统（慢思考进程）。
你对任务进行系统、彻底的分析，形成高质量方案。你与主执行进程
共享环境约束：Android Termux、无图形界面、subprocess 自动执行代码。"""

CRITIC_ASK = ("请以上面的方案为对象，苛刻地找出漏洞、风险、隐藏假设与更优解"
              "（不超过 5 点，直接列条目）。")

SYNTH_ASK = "请综合上述方案与批判意见，输出改进后的更强方案。"

FINAL_ASK = ("请综合所有立论与批判，输出最终可执行方案；"
             "需要代码时严格用 [script:]...[script] 与 [desc] 标签，"
             "并先声明 [accept:] 验收合同（检查命令严格一行）。")

OBSERVER_SYS = """你是 Rain 的外部观察者（独立监督进程），与执行者分离。
你的职责：诊断执行者 Agent 为什么反复失败（如：同类错误、
思路死循环、方向性错误），给出不超过 5 条具体、可直接采纳的纠偏策略。
你冷静、客观、一针见血，不写代码，只给策略。"""

EVOLVE_REQUIREMENTS = """硬性要求（宿主逐条机器验证，违反即淘汰）：
1) 顶部 VERSION 必须高于当前版本（小步递增，如 4.3→4.4）。
2) 【测试员】先自查：零语法错误；所有 global 声明必须位于函数体第一条语句（docstring 之后）。
3) 不得破坏任何已有命令与标签功能（含验收协议与三层防假裁判）。
4) 完整源码用 [evolve:原因]...[/evolve] 包裹；源码内部禁止 Markdown 代码块。
5) 宿主执行「结构→版本→编译→AST→冒烟测试(隔离HOME)→原子替换+备份」多重护栏，
   任一步失败会回传报错，你须分析修正后重发（最多 3 次）。
6) 进化成功后旧进程仍跑旧代码，要在总结里提醒用户重启激活新基因。"""


def build_system_prompt() -> str:
    """System Prompt = 身份核心 + 基础规则 + 模式 + 技能 + 插件 + 问题队列 + 自身状态。"""
    skills_text = load_skills()
    _plugins, plugins_text = scan_plugins()
    mode_text = PLAN_RULES if MODE == "plan" else BUILD_RULES
    problems = load_problems()
    pb_text = ""
    if problems:
        pb_text = ("\n\n【待自主探索的问题队列（%d 个，/idle 时领取）】"
                   % len(problems) + "".join("\n- " + p for p in problems[:20]))
    return (load_identity() + "\n" + BASE_PROMPT + mode_text + skills_text
            + plugins_text + pb_text
            + "\n\n【自身状态】当前基因版本 v%s，代数 Gen-%d。"
            % (VERSION, get_gen()))


def refresh_system():
    """重建 messages[0] 的 system 提示词。"""
    global messages
    system_text = build_system_prompt()
    if messages and messages[0]["role"] == "system":
        messages[0] = {"role": "system", "content": system_text}
    else:
        messages.insert(0, {"role": "system", "content": system_text})

# ============================================================
# 9. API 调用（纯 requests；_post 是唯一出口）
# ============================================================
def _post(msgs, temperature=0.3):
    """底层请求：任意消息列表 → AI 回复文本（失败返回 None）。"""
    headers = {
        "Authorization": "Bearer " + API_KEY.strip(),
        "Content-Type": "application/json",
    }
    payload = {"model": MODEL, "messages": msgs,
               "temperature": temperature, "stream": False}
    try:
        resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=300)
    except requests.RequestException as e:
        print("[网络错误] %s" % e)
        return None
    if resp.status_code != 200:
        print("[API 错误 %d] %s" % (resp.status_code, resp.text[:500]))
        return None
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        print("[响应解析失败]", resp.text[:500])
        return None


def call_api():
    """主对话调用：用全局 messages 请求 AI 回复。"""
    return _post(messages)

# ============================================================
# 10. 其余输出解析
# ============================================================
def extract_reflection(text: str) -> str:
    m = re.search(REFLECT_RE, text, re.S)
    return m.group(1).strip() if m else ""


def render_todos():
    if not TODOS:
        return
    print("\n╭─ 待办清单 ─────────────────────")
    for done, task in TODOS:
        print("│ %s %s" % ("[x]" if done else "[ ]", task))
    print("╰────────────────────────────────")


def display_reply(text):
    """美化展示：进化块剥离、脚本折叠、验收合同折叠、其余标签转可读。"""
    text = strip_evolution(text)
    try:
        blocks = _script_blocks(text)
    except ValueError:
        blocks = []
    if blocks:
        text = _without_scripts(text, blocks)
    text = re.sub(ACCEPT_RE, '\n[验收合同已登记，交由验收引擎]', text, flags=re.S)
    cleaned = re.sub(DESC_RE, r'（目的：\1）', text, flags=re.S)
    cleaned = re.sub(r"\[todo\](.*?)\[/todo\]", '\n[待办清单见下方]',
                     cleaned, flags=re.S)
    cleaned = re.sub(r"\[tododone\](.*?)\[/tododone\]", r'[已完成] \1', cleaned)
    cleaned = re.sub(PLUGIN_CALL_RE, r'→ 调用插件 \1', cleaned)
    cleaned = re.sub(REFLECT_RE, r'\n【经验反思】\1\n（通过验收后才沉淀）',
                     cleaned, flags=re.S)
    cleaned = re.sub(PROBLEM_RE, r'\n【登记新问题】\1', cleaned, flags=re.S)
    cleaned = re.sub(IDENTITY_RE, r'\n【身份重写】\1', cleaned, flags=re.S)
    print("\n" + "─" * 46)
    print("Rain> " + cleaned.strip())
    print("─" * 46)

# ============================================================
# 11. 认知引擎（慢思考 / 观察者）
# ============================================================
def deep_think(task: str):
    """慢思考辩论：立论 → (批判 → 重构) x THINK_ROUNDS 轮。
    内心独白不进入主对话历史，只有最终结论进入执行通道。"""
    thread = [
        {"role": "system", "content": THINK_SYS},
        {"role": "user", "content": "任务：%s\n请给出你的深度分析与初步方案。" % task},
    ]
    current = _post(thread)
    if current is None:
        return None
    thread.append({"role": "assistant", "content": current})
    print("[慢思考] 立论完成，进入 %d 轮批判-重构……" % THINK_ROUNDS)
    for i in range(THINK_ROUNDS):
        thread.append({"role": "user", "content": CRITIC_ASK})
        critique = _post(thread)
        if critique is None:
            break
        thread.append({"role": "assistant", "content": critique})
        ask = FINAL_ASK if i == THINK_ROUNDS - 1 else SYNTH_ASK
        thread.append({"role": "user", "content": ask})
        merged = _post(thread)
        if merged is None:
            break
        current = merged
        thread.append({"role": "assistant", "content": merged})
        print("[慢思考] 第 %d/%d 轮重构完成。" % (i + 1, THINK_ROUNDS))
    return current


def observer_review(rc, err, desc):
    """观察者进程：独立调用，诊断执行者为何卡壳，返回纠偏策略文本。"""
    print("\n[观察者] 触发监督进程，诊断卡壳原因……")
    context = "\n".join("[%s] %s" % (m["role"], m["content"][:400])
                        for m in messages[-8:])
    req = [
        {"role": "system", "content": OBSERVER_SYS},
        {"role": "user",
         "content": ("执行者连续失败。失败脚本目的：%s\n退出码：%d\n"
                     "最新错误：\n%s\n\n最近对话：\n%s\n\n"
                     "请诊断根因并给出纠偏策略。"
                     % (desc or "(无)", rc, clip_output(err)[:800], context))},
    ]
    return _post(req)

# ============================================================
# 12. 子进程执行（进程组隔离 / 超时强杀 / 环境剥离）
# ============================================================
def safe_child_env():
    """子进程环境：剥离 API 密钥，强制 UTF-8。"""
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _kill_process_tree(proc):
    """终止整个进程组（POSIX），子进程拉起的孙进程一并回收。"""
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_process(argv, timeout, env=None):
    """运行子进程：输出落盘（不受管道缓冲限制），超时杀整组。
    返回 (退出码, stdout, stderr, 是否超时)。"""
    proc = None
    out_file = tempfile.TemporaryFile()
    err_file = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,      # 防止脚本/插件 input() 卡死终端
            stdout=out_file,
            stderr=err_file,
            env=env if env is not None else safe_child_env(),
            start_new_session=(os.name == "posix"),
        )
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rc = -9
        out_file.seek(0)
        err_file.seek(0)
        out = out_file.read().decode("utf-8", "replace").rstrip()
        err = err_file.read().decode("utf-8", "replace").rstrip()
        return rc, out, err, timed_out
    finally:
        out_file.close()
        err_file.close()
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)


def run_python_code(code: str):
    """子进程执行脚本，返回 (退出码, stdout, stderr)（已截断）。"""
    rc, out, err, timed_out = _run_process(
        [sys.executable, "-c", code], EXEC_TIMEOUT)
    if timed_out:
        return 124, clip_output(out), ("执行超时（>%d 秒），进程组已被强制终止。"
                                       % EXEC_TIMEOUT)
    return rc, clip_output(out), clip_output(err)

# ============================================================
# 13. 确认与展示
# ============================================================
def confirm_execute(desc: str) -> bool:
    global AUTO_EXECUTE
    print("\n" + "─" * 46)
    print("Rain 请求执行一段脚本：%s" % (desc or "(无描述)"))
    print("─" * 46)
    if AUTO_EXECUTE:
        print("[AUTO_EXECUTE=True] 自动执行中……")
        return True
    while True:
        ans = input("允许执行？(y=允许 / n=拒绝 / a=本次会话全自动) > ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans == "a":
            AUTO_EXECUTE = True
            print("已切换为本次会话自动执行。")
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y / n / a")


def confirm_evolve(reason: str) -> bool:
    """进化确认（改自身源码是最高权限操作，默认要 y）。"""
    global AUTO_EXECUTE
    print("\n" + "█" * 46)
    print("Rain 请求【进化自身源码】：%s" % (reason or "(无说明)"))
    print("将经过 结构→版本→编译→AST→冒烟测试→备份 多重护栏后才替换。")
    print("█" * 46)
    if AUTO_EXECUTE:
        print("[AUTO_EXECUTE=True] 自动进化中……")
        return True
    while True:
        ans = input("允许进化？ > ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans == "a":
            AUTO_EXECUTE = True
            print("已切换为本次会话自动执行（含进化）。")
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y / n / a")


def show_exec_result(code, rc, out, err):
    print("\n" + "═" * 50)
    print("【脚本执行区】")
    print("─" * 50)
    print(code)
    print("─" * 50)
    print("退出码: %d" % rc)
    print("标准输出:\n%s" % (out or "(空)"))
    print("标准错误:\n%s" % (err or "(空)"))
    print("═" * 50)

# ============================================================
# 14. /save：成功脚本 → 插件
# ============================================================
def save_plugin(name=None):
    global LAST_SCRIPT, LAST_DESC
    if not LAST_SCRIPT:
        print("当前没有「成功执行过」的脚本（/save 只保存退出码 0 的代码）。")
        return
    if not name:
        name = "skill_" + time.strftime("%Y%m%d_%H%M%S")
    name = re.sub(r'[^\w\-]+', '_', name).strip('_') or "skill"
    path = os.path.join(PLUGINS_DIR, name + ".py")
    desc = (LAST_DESC or "用户通过 /save 保存的自动化技能").replace('\n', ' ')
    template = (
        "# @name: %(name)s\n"
        "# @desc: %(desc)s\n"
        "# 由 rain.py 的 /save 自动生成，AI 可通过 [plugin:%(name)s] 调用\n\n"
        "def run():\n"
        "    import subprocess, sys\n"
        "    code = %(code)r\n"
        "    try:\n"
        "        proc = subprocess.run([sys.executable, \"-c\", code],\n"
        "                              capture_output=True, timeout=300)\n"
        "    except subprocess.TimeoutExpired:\n"
        "        return \"插件执行超时(>300秒)\"\n"
        "    result = (proc.stdout or b\"\").decode(\"utf-8\", \"replace\")\n"
        "    err = (proc.stderr or b\"\").decode(\"utf-8\", \"replace\")\n"
        "    if err.strip():\n"
        "        result += \"\\n[STDERR]\\n\" + err\n"
        "    return result\n\n"
        "if __name__ == \"__main__\":\n"
        "    print(run())\n"
    ) % {"name": name, "desc": desc, "code": LAST_SCRIPT}
    with open(path, "w", encoding="utf-8") as f:
        f.write(template)
    print("已保存为插件：%s" % path)
    refresh_system()
    print("之后 AI 可直接用 [plugin:%s] 调用。" % name)

# ============================================================
# 15. Agent 主循环（P0 已修：反馈缓冲先初始化 → 合同解析 → 执行）
# ============================================================
# ===== 补丁模式解析（由 patch_rain.py 注入）=====
def apply_patch_tag(text, base_dir=None):
    # 解析 patch 标签，返回 (目标路径, 补丁内容) 列表
    import os as _os
    _lb = chr(91)
    _rb = chr(93)
    _open = _lb + 'patch:'
    _close = _lb + '/patch' + _rb
    if base_dir is None:
        base_dir = _os.path.dirname(os.path.abspath(__file__))
    results = []
    idx = 0
    while True:
        s = text.find(_open, idx)
        if s < 0:
            break
        e = text.find(_rb, s)
        if e < 0:
            break
        target = text[s + len(_open):e].strip()
        c = text.find(_close, e)
        if c < 0:
            break
        body = text[e + 1:c]
        if not _os.path.isabs(target):
            target = _os.path.join(base_dir, target)
        results.append((target, body))
        idx = c + len(_close)
    return results


def run_patch_tag(text, base_dir=None, dry_run=False):
    # 执行 patch 标签：写入目标文件，写后 py_compile 校验，失败回滚
    import os as _os
    import shutil as _shutil
    import py_compile as _pc
    import datetime as _dt
    items = apply_patch_tag(text, base_dir)
    log = []
    for target, body in items:
        d = _os.path.dirname(target) or '.'
        if not _os.path.isdir(d):
            _os.makedirs(d, exist_ok=True)
        backup = None
        if _os.path.exists(target):
            ts = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = target + '.bak_' + ts
            _shutil.copy2(target, backup)
        if dry_run:
            log.append('DRY ' + target)
            continue
        with open(target, 'w', encoding='utf-8') as f:
            f.write(body)
        if target.endswith('.py'):
            try:
                _pc.compile(target, doraise=True)
            except Exception as ex:
                if backup:
                    _shutil.copy2(backup, target)
                log.append('FAIL ' + target + ' -> ' + str(ex) + ' (已回滚)')
                continue
        log.append('OK ' + target)
    return log
# ===== 补丁模式解析结束 =====


def agent_loop(first_reply=None):
    """一轮完整循环。返回 "ok"/"verified"/"failed"/"api_error"
    （供 /idle 决定移除/回队任务）。first_reply 用于把 /think 的结论
    直接送进执行通道。
    v4.3 P0 修复：feedbacks / error_feedbacks 在任何使用之前初始化
    （v4.2 在验收合同循环中先 append 后初始化 → UnboundLocalError
    → 宿主崩溃。函数内赋值使名称局部化，这是 Python 作用域铁律）。"""
    global LAST_SCRIPT, LAST_DESC, ACTIVE_CONTRACT
    self_repair_count = 0     # 脚本连续失败计数
    evolve_fails = 0          # 进化连续失败计数

    for _round in range(MAX_ROUNDS):
        if first_reply is not None:
            reply = first_reply
            first_reply = None
        else:
            reply = call_api()
            if reply is None:
                return "api_error"

        messages.append({"role": "assistant", "content": reply})

        # ---- 解析：进化块 → 脚本块 → 控制文本（脚本外）----
        evolutions, evo_error = evolution_status(reply)
        text = strip_evolution(reply) if (evolutions or evo_error) else reply
        script_error = None
        try:
            blocks = _script_blocks(text)
        except ValueError as e:
            blocks = []
            script_error = str(e)
        scripts = [(code, desc) for _s, _e, code, desc in blocks if code]
        control = _without_scripts(text, blocks) if blocks else text

        display_reply(text)
        update_todos(control)
        render_todos()

        # ---- 反馈缓冲初始化（P0：必须在任何使用之前）----
        feedbacks = []
        error_feedbacks = []
        _patch_items = apply_patch_tag(control)

        # ---- 心智：好奇心登记 + 身份重写（仅脚本外文本）----
        for p in extract_problems(control):
            save_problem(p)
        ident = extract_identity(control)
        if ident:
            save_identity(ident)

        # ---- 【P0 修复点】反馈缓冲必须在任何使用之前初始化 ----

        # ---- 验收合同：注册 / 追加收紧 / 锁定期拒绝 ----
        accept_registered = False
        for clause in parse_accept_blocks(control):
            base = None
            if ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open":
                base = ACTIVE_CONTRACT
            if base is not None and base.get("locked"):
                error_feedbacks.append(
                    "【系统自动回传】验收合同已锁定，禁止修改裁判。"
                    "请修复产物本身；若认为检查标准有误，用文字向用户说明，"
                    "由用户裁决。")
                continue
            c2, err2 = register_contract(clause, CURRENT_TASK, append_to=base)
            if err2:
                error_feedbacks.append("【系统自动回传】" + err2)
            else:
                ACTIVE_CONTRACT = c2
                accept_registered = True

        plugin_calls = []
        for pid in re.findall(PLUGIN_CALL_RE, control):
            pid = pid.strip()
            if pid.endswith(".py"):
                pid = pid[:-3]
            if pid and pid not in plugin_calls:
                plugin_calls.append(pid)

        if evo_error:
            error_feedbacks.append(
                "【系统自动回传】%s。进化块已整体剥离（防止其内部代码被误执行）。"
                "请重新输出完整的 [evolve:原因]...[/evolve] 源码。" % evo_error)
        if script_error:
            error_feedbacks.append(
                "【系统自动回传】%s。请补全结束标签后重新输出脚本。" % script_error)

        if evolutions:
            # ---- 进化请求优先处理 ----
            reason, source = evolutions[0]
            if evolve_fails >= MAX_EVOLVE_RETRY:
                feedbacks.append(
                    "【系统自动回传】进化重试已达上限（%d 次），本次不再接受进化。"
                    "请改用文字说明改进建议，交给用户手动修改。" % MAX_EVOLVE_RETRY)
            elif not confirm_evolve(reason):
                feedbacks.append(
                    "【系统自动回传】用户拒绝本次进化。请解释你的改进思路，"
                    "或继续用现有能力完成任务，不要再输出 [evolve:]。")
            else:
                ok, msg = apply_evolution(source, reason)
                if ok:
                    feedbacks.append("【系统自动回传】" + msg)
                else:
                    evolve_fails += 1
                    tip = ("（进化重试第 %d/%d 次）"
                           % (evolve_fails, MAX_EVOLVE_RETRY)
                           if evolve_fails < MAX_EVOLVE_RETRY else "（已达上限）")
                    feedbacks.append(
                        "【系统自动回传】进化验证失败 %s：%s\n"
                        "旧基因完好无损。请分析失败原因，修正后重新输出完整的 "
                        "[evolve:原因]...[/evolve] 源码；若无法修复请改用文字说明。"
                        % (tip, msg))
        elif _patch_items:
            # ---- 补丁模式执行（v4.5：与脚本/插件并列，统一确认+模式约束）----
            if MODE == "plan":
                feedbacks.append(
                    "【系统自动回传】当前处于 /plan 规划模式，宿主已硬性拒绝"
                    "执行任何补丁/脚本/插件。请只输出文字与 [todo] 规划；"
                    "如需执行请提示用户输入 /build。")
            elif self_repair_count >= MAX_SELF_REPAIR:
                feedbacks.append(
                    "【系统自动回传】自我修复次数已达上限（%d/%d），"
                    "补丁不再执行。请停止输出代码，直接总结失败原因与手动建议。"
                    % (self_repair_count, MAX_SELF_REPAIR))
            elif not confirm_execute("执行 %d 个补丁标签（写入目标文件）"
                                     % len(_patch_items)):
                feedbacks.append(
                    "【系统自动回传】用户拒绝执行补丁。请解释原因或改用其他方式。")
            else:
                print("\n[补丁] 执行 %d 个补丁标签……" % len(_patch_items))
                _patch_log = run_patch_tag(control)
                for _line in _patch_log:
                    print("  " + _line)
                _patch_failed = any(l.startswith("FAIL") for l in _patch_log)
                if _patch_failed:
                    self_repair_count += 1
                    if self_repair_count >= MAX_SELF_REPAIR:
                        note = ("补丁执行失败，自我修复次数已达上限（%d/%d）。"
                                "请停止输出代码，直接总结。"
                                % (self_repair_count, MAX_SELF_REPAIR))
                    else:
                        note = ("补丁执行失败（自我修复第 %d/%d 次）。"
                                "不要询问用户、不要道歉，立刻分析报错根因，"
                                "修正后重新输出补丁或脚本。"
                                % (self_repair_count, MAX_SELF_REPAIR))
                else:
                    self_repair_count = 0
                    note = ("补丁执行成功。任务若已完成，请文字总结并停止输出"
                            "代码（有合同时宿主将执行验收）；否则继续下一步。")
                feedbacks.append(
                    "【系统自动回传】补丁执行结果：\n"
                    + "\n".join(_patch_log) + "\n" + note)
        elif scripts or plugin_calls:
            if MODE == "plan":
                # ---- /plan 硬约束：宿主直接拒绝执行 ----
                feedbacks.append(
                    "【系统自动回传】当前处于 /plan 规划模式，宿主已硬性拒绝执行"
                    "任何脚本/插件（这是模式约束，不是提示词建议）。请只输出文字"
                    "与 [todo] 规划；如需执行请提示用户输入 /build。")
            else:
                # ---- 常规执行：失败即停 + 修复上限 + 观察者 ----
                rejected = False
                batch_failed = False
                for idx, (code, desc) in enumerate(scripts):
                    if self_repair_count >= MAX_SELF_REPAIR:
                        feedbacks.append(
                            "【系统自动回传】自我修复次数已达上限（%d/%d），"
                            "本批剩余脚本不再执行。请停止输出代码，直接总结失败"
                            "原因与手动建议。"
                            % (self_repair_count, MAX_SELF_REPAIR))
                        break
                    if desc:
                        LAST_DESC = desc
                    if not confirm_execute(desc):
                        rejected = True
                        feedbacks.append(
                            "【系统自动回传】用户拒绝执行该脚本。"
                            "请解释原因、给出修改方案，或改用其他方式完成。")
                        break
                    rc, out, err = run_python_code(code)
                    show_exec_result(code, rc, out, err)
                    if rc == 0:
                        LAST_SCRIPT = code
                        self_repair_count = 0
                        if out.strip():
                            note = ("脚本执行成功。任务若已完成，请文字总结并停止"
                                    "输出 [script:]（有合同时宿主将执行验收）；"
                                    "否则继续下一步。")
                        else:
                            note = ("脚本退出码 0 但没有任何输出。请确认它确实完成"
                                    "了预期工作（若涉及写文件等副作用，请用代码"
                                    "验证后再继续）。")
                    else:
                        self_repair_count += 1
                        if self_repair_count >= MAX_SELF_REPAIR:
                            note = ("脚本执行失败，自我修复次数已达上限（%d/%d）。"
                                    "请停止输出代码，直接总结。"
                                    % (self_repair_count, MAX_SELF_REPAIR))
                        else:
                            note = ("脚本执行失败（自我修复第 %d/%d 次）。"
                                    "不要询问用户、不要道歉，立刻分析报错根因，"
                                    "修正后重新输出 [script:]。"
                                    % (self_repair_count, MAX_SELF_REPAIR))
                    feedbacks.append(
                        "【系统自动回传】\n"
                        + ("[desc]%s[/desc]\n" % desc if desc else "")
                        + "脚本执行结束，退出码 %d。\n" % rc
                        + "--- 标准输出 ---\n%s\n" % (out or "(空)")
                        + "--- 标准错误 ---\n%s\n" % (err or "(空)")
                        + note)
                    if rc != 0:
                        batch_failed = True
                        if self_repair_count == OBSERVER_AFTER:
                            verdict = observer_review(rc, err, desc)
                            if verdict:
                                feedbacks.append("【观察者纠偏】\n" + verdict)
                        remaining = len(scripts) - idx - 1
                        if remaining:
                            feedbacks.append(
                                "【系统自动回传】失败即停：本回复中剩余 %d 段脚本"
                                "未执行，避免后续操作建立在错误结果之上。修复后"
                                "可重新输出它们。" % remaining)
                        break
                if not rejected and not batch_failed:
                    for pid in plugin_calls:
                        if not confirm_execute("调用插件 %s（隔离子进程）" % pid):
                            rejected = True
                            feedbacks.append(
                                "【系统自动回传】用户拒绝调用插件 %s。"
                                "请解释或改用其他方式。" % pid)
                            break
                        result = run_plugin(pid)
                        print("\n[插件 %s] 执行结果：\n%s" % (pid, result))
                        feedbacks.append(
                            "【系统自动回传】插件 %s 执行结果（隔离子进程）：\n%s\n"
                            "请基于结果决定下一步。" % (pid, result))
        elif not error_feedbacks:
            # ---- 文字收尾：验收 or 普通反思 ----
            if (MODE == "plan" and ACTIVE_CONTRACT
                    and ACTIVE_CONTRACT.get("status") == "open"):
                feedbacks.append(
                    "【系统自动回传】当前处于规划模式，暂不执行验收检查。"
                    "请提示用户 /build 后再宣称完成。")
            elif accept_registered:
                feedbacks.append(
                    "【系统自动回传】验收合同已登记（首次验收时锁定）。"
                    "请开始工作：输出 [script:] 真正产出「产物:」声明的交付物；"
                    "完成前不要只用文字宣称完成。")
            elif ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open":
                # 有活跃合同：文字宣称完成 ≠ 完成，必须过客观验收
                if not ACTIVE_CONTRACT.get("locked"):
                    ACTIVE_CONTRACT["locked"] = True
                    save_contract(ACTIVE_CONTRACT)
                print("\n[验收] AI 宣称完成，开始客观验收（不信文字）……")
                passed, report = verify_contract_once(ACTIVE_CONTRACT)
                if passed is None:
                    # v4.3：用户拒绝执行检查命令，不计失败次数
                    feedbacks.append("【系统自动回传】" + report)
                else:
                    verdict = settle_contract(passed, control)
                    if verdict == "retry":
                        feedbacks.append(
                            "【验收失败】（第 %d/%d 次）\n%s\n"
                            "任务尚未真正完成。请依据上面的真实输出修复产物，继续"
                            "工作；禁止仅用文字宣称完成，也不要修改已锁定的验收"
                            "合同。"
                            % (ACTIVE_CONTRACT["verify_attempts"],
                               MAX_VERIFY_ATTEMPTS, report))
                    elif verdict == "failed":
                        feedbacks.append(
                            "【验收终局失败】连续 %d 次未通过客观验收，任务判定未"
                            "完成。请直接总结：已完成什么、卡在哪里、给用户的手动"
                            "建议；不要再宣称完成。"
                            % MAX_VERIFY_ATTEMPTS)
                        return "failed"
                    else:
                        return "verified"
            else:
                reflection = extract_reflection(control)
                if reflection:
                    save_reflection(reflection)
                return "ok"

        feedbacks.extend(error_feedbacks)
        if not feedbacks:
            return "ok"
        for fb in feedbacks:
            messages.append({"role": "user", "content": fb})

    print("（已达单轮最大往返次数 %d，已暂停；可继续输入新指令。）" % MAX_ROUNDS)
    return "ok"

# ============================================================
# 16. 命令处理（新增 /contract；global 声明包含 ACTIVE_CONTRACT）
# ============================================================
HELP_TEXT = """可用命令：
  /think 任务           深度思考：立论→批判→重构多轮辩论，再自动执行
  /idle                 自主模式：领任务→先立验收合同→完成须过客观验收
  /evolve [说明]         自主进化：附带当前源码让 AI 修改并换基因
  /versions             查看基因谱系（历史版本）
  /rollback [序号]       回滚到某一代基因
  /contract [放弃]       查看当前验收合同 / 主动放弃
  /plan /build          规划模式（硬禁止执行）/ 执行模式
  /auto                 切换 AUTO_EXECUTE（y 确认 / 免确认）
  /skill add 名 内容     写入 ~/skills/名.md；/skill list 列出
  /save [插件名]         最近一次成功脚本 → ~/plugins/ 插件
  /clear /exit /help    清空记忆（挂起未验收合同）/ 退出 / 帮助
提示：换新任务前建议 /clear，否则旧合同的验收标准可能套在新任务上。
v4.3 验收机制：[accept:] 声明产物+检查（严格一行）+标准；完成由宿主
客观验收（三层防假裁判：探针检查 + 藏产物对照证伪 + 合同锁定）；
验收通过才沉淀经验/移除任务；连续 3 次失败终局判定、任务回队；
用户拒绝执行检查命令不计入 AI 失败次数。"""


def handle_command(u: str):
    global MODE, TODOS, AUTO_EXECUTE, ACTIVE_CONTRACT    # v4.3：/contract 放弃需要写
    cmd = u.split()[0].lower()
    if cmd == "/plan":
        MODE = "plan"
        refresh_system()
        print("已切换到【规划模式】：只拆解任务，宿主将硬性拒绝执行代码。")
    elif cmd == "/build":
        MODE = "build"
        refresh_system()
        print("已切换到【执行模式】：三角色协作，自动执行并回传。")
    elif cmd == "/clear":
        begin_task_slot(None)          # 挂起未决合同
        messages.clear()
        TODOS = []
        refresh_system()
        print("已清空对话记忆与待办清单（未验收合同已挂起，见 ~/mind/contracts/）。")
    elif cmd == "/exit":
        print("再见！")
        sys.exit(0)
    elif cmd == "/contract":
        parts = u.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg in ("放弃", "abandon"):
            if ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open":
                ACTIVE_CONTRACT["status"] = "abandoned"
                save_contract(ACTIVE_CONTRACT)
                append_journal("合同放弃", ACTIVE_CONTRACT["task"])
                ACTIVE_CONTRACT = None
                print("已放弃当前验收合同（可重新 [accept:] 立新约）。")
            else:
                print("无进行中的合同。")
        elif ACTIVE_CONTRACT and ACTIVE_CONTRACT.get("status") == "open":
            c = ACTIVE_CONTRACT
            print("当前合同：%s" % (c["task"] or "(未记录)"))
            print("  产物：")
            for a in c["artifacts"]:
                print("   - %s%s" % (a["path"],
                                     "（已存在）" if a["existed"] else "（待产出）"))
            print("  检查：")
            for k in c["checks"]:
                print("   - %s" % k["cmd"])
            print("  标准：%s" % (c["criteria"] or "(未声明)"))
            print("  状态：%s ｜ 已验收 %d/%d 次"
                  % ("已锁定" if c["locked"] else "未锁定",
                     c["verify_attempts"], MAX_VERIFY_ATTEMPTS))
        else:
            print("无进行中的合同。")
    elif cmd == "/think":
        parts = u.split(maxsplit=1)
        if len(parts) < 2:
            print("用法：/think 任务描述")
            return
        task = parts[1].strip()
        # 任务先写入主对话，后续执行与修复都有上下文
        messages.append({"role": "user",
                         "content": "【深度思考任务】%s\n（已启动慢思考进程："
                                    "立论→批判→重构，结论将自动进入执行。）" % task})
        note_task(task)
        print("[深度思考] 启动慢思考进程……")
        concl = deep_think(task)
        if not concl:
            print("[深度思考] API 调用失败。")
            return
        print("[深度思考] 结论已产生，进入执行通道。")
        agent_loop(first_reply=concl)
    elif cmd == "/idle":
        task = take_problem()
        if not task:
            print("问题队列为空。让 AI 在任务中输出 [problem]问题[/problem] 来登记。")
            return
        print("[自主模式] 领取任务：%s" % task)
        begin_task_slot("自主任务：" + task)
        messages.append({"role": "user",
                         "content": ("【自主任务·来自问题队列】%s\n请自主解决它。"
                                     "开始前必须先用 [accept:] 声明验收合同"
                                     "（产物+检查命令[严格一行]+标准），完成由宿主"
                                     "客观验收；未过验收任务不算完成。" % task)})
        status = agent_loop()
        if status == "api_error":
            requeue_problem_front(task)
            print("[自主模式] API 调用失败，任务已放回队列头部，未丢失。")
        elif status == "failed":
            requeue_problem_front(task)
            print("[自主模式] 验收终局失败，任务已放回队列（含失败记录）。")
        elif status == "verified":
            print("[自主模式] ✅ 验收通过，任务已从队列正式移除。")
        else:
            print("[自主模式] 任务结束（未声明验收合同，按普通完成处理）。")
            append_journal("无合同完成", task)
    elif cmd == "/evolve":
        parts = u.split(maxsplit=1)
        goal = parts[1].strip() if len(parts) > 1 else "检查自身运行情况"
        try:
            with open(SELF_PATH, "r", encoding="utf-8", errors="ignore") as f:
                current_src = f.read()
        except Exception as e:
            current_src = "(读取当前源码失败：%s)" % e
        messages.append({"role": "user", "content":
                         "【进化指令】%s。\n\n"
                         "【当前自身完整源码（以此基线做最小必要修改，"
                         "禁止凭记忆重写全文）】\n%s\n\n%s"
                         % (goal, current_src, EVOLVE_REQUIREMENTS)})
        agent_loop()
    elif cmd == "/versions":
        list_versions()
    elif cmd == "/rollback":
        parts = u.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip().isdigit():
            rollback(int(parts[1].strip()))
        else:
            list_versions()
            print("用法：/rollback 序号")
    elif cmd == "/save":
        parts = u.split(maxsplit=1)
        save_plugin(parts[1].strip() if len(parts) > 1 else None)
    elif cmd == "/skill":
        handle_skill(u)
    elif cmd == "/auto":
        AUTO_EXECUTE = not AUTO_EXECUTE
        print("AUTO_EXECUTE 已切换为 %s" % AUTO_EXECUTE)
    elif cmd == "/help":
        print(HELP_TEXT)
    else:
        print("未知命令 %s，输入 /help 查看可用命令。" % cmd)

# ============================================================
# 17. 程序入口（横幅死代码已删，改为固定提示）
# ============================================================
def main():
    global API_KEY
    for d in (SKILLS_DIR, PLUGINS_DIR, MIND_DIR, EVOLUTION_DIR,
              CONTRACTS_DIR):
        os.makedirs(d, exist_ok=True)
    API_KEY = load_api_key()
    if not API_KEY:
        print("未提供 API Key，无法启动。请重新运行并输入，")
        print("或设置环境变量 DEEPSEEK_API_KEY，或手动写入 %s" % CONFIG_PATH)
        sys.exit(1)
    refresh_system()
    skill_files = [f for f in os.listdir(SKILLS_DIR)
                   if f.endswith(".md") or f.endswith(".txt")]
    plugins, _p = scan_plugins()
    print("═" * 50)
    print("  Rain · 超级智能终端 Agent  (v%s / Gen-%d · 心智+基因+验收)"
          % (VERSION, get_gen()))
    print("  模型: %s ｜ 模式: %s ｜ 身份: %s" % (MODEL, MODE,
          "已定义" if load_identity() else "默认"))
    print("  技能: %d ｜ 插件: %d ｜ 待解问题: %d"
          % (len(skill_files), len(plugins), len(load_problems())))
    print("  自我修复≤%d次 ｜ 验收机会≤%d次 ｜ 进化重试≤%d次"
          % (MAX_SELF_REPAIR, MAX_VERIFY_ATTEMPTS, MAX_EVOLVE_RETRY))
    print("  验收合同不跨会话保留（/contract 查看，/clear 挂起）")
    print("  /help 命令；/think 深思；/idle 自主；/evolve 进化")
    print("═" * 50)
    while True:
        try:
            user_input = input("\n你> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break
        if not user_input:
            continue
        if user_input.startswith("/"):
            handle_command(user_input)
            continue
        note_task(user_input)         # 合同存续=任务延续，否则记录新任务
        messages.append({"role": "user", "content": user_input})
        agent_loop()


if __name__ == "__main__":
    main()

