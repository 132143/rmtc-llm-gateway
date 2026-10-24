"""全盘扫描：语法、密钥泄漏、死代码、未使用导入、测试与目录完整性。

这不是"再跑一遍测试"，而是补三类静态风险：
  1) 令牌/密码被写进仓库（规则 §11.2 令牌不得公开，交付物更要干净）；
  2) 语法能过但永远执行不到的代码（封存后没人敢删、答辩时说不清）；
  3) 模块导入关系与实际使用不一致（未使用导入是最常见的复制粘贴残留）。
"""

from __future__ import annotations

import ast
import os
import re
import sys

try:                          # Windows 控制台默认 GBK，标记符号会炸；统一成 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", ".git", "docs", "examples"}
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+sk-"),
    re.compile(r"asdf1234"),
)


def py_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


SELF = os.path.abspath(__file__)


def check_secrets(paths):
    hits = []
    for path in paths:
        if os.path.abspath(path) == SELF:
            continue          # 扫描脚本自己保存了模式串，跳过它

        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                hits.append((os.path.relpath(path, ROOT), match.group(0)[:8] + "..."))
    return hits


def check_unused_imports(paths):
    problems = []
    for path in paths:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as exc:
            problems.append((path, "语法错误：{0}".format(exc)))
            continue
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported[(alias.asname or alias.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported[alias.asname or alias.name] = node.lineno
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        text = open(path, encoding="utf-8").read()
        for name, line in imported.items():
            if name in ("annotations",):
                continue
            if name in used:
                continue
            if re.search(r"\b{0}\b".format(re.escape(name)), text.split("\n", line)[-1]):
                continue
            problems.append((path, "第 {0} 行导入了 {1} 但没用".format(line, name)))
    return problems


def check_dead_functions(paths):
    """被定义但全仓库没有任何调用的私有函数（下划线开头、非测试）。"""
    defined = {}
    called = set()
    for path in paths:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_") \
                    and not node.name.startswith("__"):
                defined.setdefault(node.name, []).append(os.path.relpath(path, ROOT))
            elif isinstance(node, ast.Attribute) and isinstance(node.attr, str):
                called.add(node.attr)
        text = open(path, encoding="utf-8").read()
        for name in list(defined):
            if text.count(name) > 1:
                called.add(name)
    dead = []
    for name, where in defined.items():
        if name in called:
            continue
        dead.append((name, where[0]))
    return dead


def main():
    paths = sorted(py_files())
    print("扫描 {0} 个 .py".format(len(paths)))
    failures = 0

    secrets = check_secrets(paths)
    if secrets:
        failures += len(secrets)
        print("[BAD] 发现疑似密钥：")
        for path, sample in secrets:
            print("   {0} → {1}".format(path, sample))
    else:
        print("[OK] 没有令牌/密码泄漏")

    syntax = []
    for path in paths:
        try:
            compile(open(path, encoding="utf-8").read(), path, "exec")
        except SyntaxError as exc:
            syntax.append((path, exc))
    if syntax:
        failures += len(syntax)
        print("[BAD] 语法错误：")
        for path, exc in syntax:
            print("   {0}: {1}".format(path, exc))
    else:
        print("[OK] 全部文件语法通过")

    unused = check_unused_imports(paths)
    if unused:
        print("[WARN] 未使用导入 {0} 处：".format(len(unused)))
        for path, message in unused:
            print("   {0}: {1}".format(os.path.relpath(path, ROOT), message))
    else:
        print("[OK] 没有未使用导入")

    dead = check_dead_functions(paths)
    if dead:
        print("[WARN] 疑似未被调用的私有函数 {0} 个：".format(len(dead)))
        for name, where in dead:
            print("   {0}（{1}）".format(name, where))
    else:
        print("[OK] 没有明显的死函数")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())