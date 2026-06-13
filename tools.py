"""工具定义与执行。

每个工具 = 一份 JSON Schema（给模型看） + 一个 Python 函数（真正执行）。
新增工具只需在 TOOL_SCHEMAS 加 schema，并在 TOOL_FUNCTIONS 注册函数。
"""

import ast
import operator
import pathlib
import subprocess
import urllib.error
import urllib.request

# ---------- 工具实现 ----------

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def _safe_eval(node):
    """只允许数字和算术运算符的安全求值（替代 eval）。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式: {ast.dump(node)}")


def calculator(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval")))
    except Exception as e:
        return f"计算出错: {e}"


def read_file(path: str) -> str:
    p = pathlib.Path(path)
    if not p.is_file():
        return f"文件不存在: {path}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > 8000:
        return text[:8000] + f"\n...[已截断，全文 {len(text)} 字符]"
    return text


def list_dir(path: str = ".") -> str:
    p = pathlib.Path(path)
    if not p.is_dir():
        return f"目录不存在: {path}"
    return "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir()))


def write_file(path: str, content: str) -> str:
    """写入文本文件（覆盖）。自动创建父目录。"""
    try:
        p = pathlib.Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字符到 {path}"
    except Exception as e:
        return f"写入出错: {e}"


def bash(command: str, timeout: int = 30) -> str:
    """在 shell 中执行命令，返回合并后的 stdout/stderr（带退出码）。"""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > 8000:
            out = out[:8000] + f"\n...[已截断，全长 {len(out)} 字符]"
        return f"[exit={proc.returncode}]\n{out}".rstrip()
    except subprocess.TimeoutExpired:
        return f"命令超时（>{timeout}s）: {command}"
    except Exception as e:
        return f"命令执行出错: {e}"


def http_get(url: str, timeout: int = 20) -> str:
    """发起 HTTP(S) GET 请求，返回响应正文（截断到 8000 字符）。"""
    if not url.startswith(("http://", "https://")):
        return f"仅支持 http/https 链接: {url}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "loop-agent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read(64000).decode(charset, errors="replace")
        if len(body) > 8000:
            body = body[:8000] + f"\n...[已截断，全长 {len(body)} 字符]"
        return body
    except urllib.error.HTTPError as e:
        return f"HTTP 错误 {e.code}: {url}"
    except Exception as e:
        return f"请求出错: {e}"


# ---------- 注册表 ----------

TOOL_FUNCTIONS = {
    "calculator": calculator,
    "read_file": read_file,
    "list_dir": list_dir,
    "write_file": write_file,
    "bash": bash,
    "http_get": http_get,
}

# OpenAI tools 格式
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个算术表达式，例如 '(27 * 453) + 19'",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "算术表达式"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取一个文本文件的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录下的文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，默认当前目录"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入文本文件（覆盖已有内容，自动创建父目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "要写入的文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "在 shell 中执行一条命令并返回输出（含退出码）。用于运行测试、grep、git 等",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 30"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "发起 HTTP(S) GET 请求并返回响应正文，用于联网抓取网页/API",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https 链接"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 20"},
                },
                "required": ["url"],
            },
        },
    },
]
