# -*- coding: utf-8 -*-
"""ASCII 文件名的启动器。

存在的理由：Windows 的 cmd.exe 按系统 OEM 代码页（简体中文机器上是 936/GBK）
读取 .cmd 脚本的字节，`chcp 65001` 只改控制台输出编码，改不了这一点。
所以 .cmd 里一旦出现 UTF-8 编码的中文（包括中文文件名），就会被当成 GBK
解析成乱码，报 "'xxx' is not recognized as an internal or external command"。

把 .cmd 做成纯 ASCII、只调用本文件，中文全部留在 Python 侧（Python 按
UTF-8 读源码，不受代码页影响），这个坑就彻底绕开了。
"""
import os
import runpy
import sys

# 控制台按 UTF-8 输出，避免中文打印时 UnicodeEncodeError。
# 用 reconfigure 而不是新建 TextIOWrapper：后者会和被调脚本里的同类包装
# 叠成两层，任一层被回收就把底层 buffer 关掉，报 "I/O operation on closed file"。
for name in ("stdout", "stderr"):
    stream = getattr(sys, name, None)
    if stream is not None and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "一键导入.py")

if not os.path.exists(TARGET):
    print("找不到 一键导入.py，请确认它和本文件在同一个目录：")
    print("  " + HERE)
    sys.exit(1)

os.chdir(HERE)
sys.argv = [TARGET] + sys.argv[1:]
runpy.run_path(TARGET, run_name="__main__")
