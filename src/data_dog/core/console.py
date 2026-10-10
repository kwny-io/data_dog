"""控制台编码兜底：让非 UTF-8 终端（Windows 的 cp936/cp1252 是常态）也能打印。

为什么需要它
-----------
演示脚本与 CLI 的输出里有 `▸` `✅` `⛔` 这类符号。Windows 上 Python 默认按**控制台
代码页**编码 stdout（简体中文环境即 `cp936`/GBK），这些字符不在 GBK 里，于是 `print`
直接抛 `UnicodeEncodeError`：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\u25b8'

后果不是"显示成问号"，而是**命令崩掉、整条演示链路走不完**——`python -m
data_dog.e2e.replay_demo` 在 Windows 上第一步就红，而 Linux/容器里完全正常
（CI 只跑 Linux，所以单测全绿也抓不到，属"静默失败"的近亲：**只在特定平台暴露**）。

修法
----
在**入口**（CLI / 各 `python -m` 演示脚本的 `main()`）显式把 stdout/stderr 重配为
UTF-8，`errors="replace"` 兜底（万一某些字符写不出去，退化为替代符而不是崩溃）。

为什么在入口而不是 `import data_dog` 时
--------------------------------------
库被 import 时不该有"改全局 stdout"的副作用——测试、被嵌入调用、被别的 CLI 引用时
都可能是非交互流；重配会污染调用方。故只在**进程入口**调用一次。

幂等、可失败：重复调用无副作用；流不支持 `reconfigure`（被重定向成 StringIO、
某些 IDE 包装）时静默跳过，`reconfigure` **抛任何异常也一律吞掉**，绝不因此报错——
兜底动作本身不能成为新的崩溃点。
"""
from __future__ import annotations

import sys


def ensure_utf8_console(*, errors: str = "replace") -> bool:
    """把 `sys.stdout` / `sys.stderr` 重配为 UTF-8；返回是否至少成功重配了一个流。

    参数 `errors="replace"`：宁可把个别字符写成替代符，也不要因编码异常中断输出
    （演示脚本的第一要务是"跑完并让人看懂"，不是"每个符号都完美"）。
    """
    changed = False
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # 非 TextIOWrapper（StringIO / 测试捕获 / 某些 IDE 包装）
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except Exception:
            # 刻意吞掉**所有**异常，而不是只列举几种：
            # "控制台编码兜底"是纯装饰性动作，它本身绝不能成为新的崩溃点——被重定向的
            # stdout 替身（CI、日志收集器、IDE）行为不可预测，可能抛任何东西。
            # 契约由 `tests/test_synth.py::test_synth_eval_main_tolerates_odd_stdout`
            # 与 `tests/test_console_utf8.py` 两处锁定。
            continue
        changed = True
    return changed
