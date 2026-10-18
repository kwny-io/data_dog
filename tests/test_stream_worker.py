"""stream_worker 入口（K8s 流消费者进程）的失败关闭与退出码测试。

为什么单独测这个入口
--------------------
`python -m data_dog.service.stream_worker` 是 K8s 里**流消费者**进程的入口
（设计见 docs/p4/04_k8s_helm.md §3.3）。它存在的理由是"API 与消费者是两种工作负载"，
所以它的正确性判据不是"能跑"，而是**失败关闭**：

- 没配源（DD_STREAM_ENABLED=0）→ 非零退出，不空转；
- 源不支持多副本（file + DD_STREAM_SHARDS>1）→ 非零退出，**绝不静默重复消费**；
- 算子绑定不可用（缺厂商文档）→ 非零退出。

这些退出码是 K8s（CrashLoopBackOff）与值班人**唯一能看到的信号**。若入口静默返回 0，
Pod 会显示 Running 却什么都没消费——最难排查的一类静默失败。故这里逐个钉住。

> 本文件由维护者提供**骨架**；标 [待补] 的用例留给你按同样的模式补齐。
> 参考已实现用例的写法（env 隔离 + capsys 断言 stderr + 断言退出码）。
"""
from __future__ import annotations

import json

import pytest

from data_dog import settings
from data_dog.service import stream_worker


def _raw_line(event_id: str) -> str:
    """一条最小可解析的原始日志行（file 源用）。"""
    body = {
        "event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.1Z", "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": 95, "current": 12.0, "mode": 0}),
    }
    return json.dumps(body)


# ---------------------------------------------------------------- 失败关闭

def test_disabled_stream_exits_2(monkeypatch, capsys):
    """DD_STREAM_ENABLED=0（默认）→ 退出码 2，且 stderr 说明原因。

    这个二进制是**为消费而生**的：没配源就启动 = 误用，必须显式报错而非静默空转。
    """
    monkeypatch.delenv("DD_STREAM_ENABLED", raising=False)   # 走默认 False
    code = stream_worker.main()
    err = capsys.readouterr().err
    assert code == 2
    assert "DD_STREAM_ENABLED" in err


@pytest.mark.skip(reason="[待补] 你来实现：file 源 + 多副本 → 退出码 2")
def test_source_assembly_failure_exits_2(monkeypatch, capsys, tmp_path):
    """[待补] 已启用、但源装配失败（file 源不支持多副本）→ 退出码 2。

    提示：
    - 用 monkeypatch.setenv 设 DD_STREAM_ENABLED=1 / DD_STREAM_SOURCE=file /
      DD_STREAM_SHARDS=2 / DD_STREAM_FILE_PATH=<tmp_path 下建的一个文件>。
    - file 源多副本会由 build_source() 抛 RuntimeError（见 stream/factory.py），
      worker 在**预检**阶段捕获 → return 2（不进消费循环）。
    - 断言退出码 == 2，且 stderr 含 "装配失败" 或 "多副本"。
    - 别忘了建文件：tmp_path 下写一行再传路径，否则报的是"路径无匹配文件"（也是 2，
      但不是你想测的那条分支）。
    """
    raise NotImplementedError


@pytest.mark.skip(reason="[待补] 你来实现：算子绑定不可用 → 退出码 3")
def test_unavailable_plane_exits_3(monkeypatch, capsys, tmp_path):
    """[待补] 源能装配、但算子绑定不可用 → 退出码 3。

    提示：
    - 让源装配**成功**（file 源 + shards=1 + 一个真实存在的文件）；
    - 把 ServiceState.new_plane 换成返回 None（monkeypatch.setattr），模拟"缺厂商文档"；
    - 断言退出码 == 3，且 stderr 含 "算子绑定不可用"。

    思考题（写进你的 PR 描述）：为什么"源装配失败"是 2、"绑定不可用"是 3？
    两个不同的非零码对运维排查有什么价值？
    """
    raise NotImplementedError


# ---------------------------------------------------------------- 正常工作

@pytest.mark.skip(reason="[待补] 你来实现：源+绑定都就绪 → 退出码 0")
def test_healthy_start_returns_0(monkeypatch, capsys, tmp_path):
    """[待补] 源与绑定都就绪 → 进入消费循环并正常返回 0。

    提示：
    - 环境：DD_STREAM_ENABLED=1 / DD_STREAM_SOURCE=file / DD_STREAM_SHARDS=1 /
      DD_STREAM_FILE_PATH=<tmp_path 下的文件>。
    - StreamRunner.run() 会**阻塞**，测试里必须替换掉它：monkeypatch.setattr
      把 data_dog.service.stream_worker.StreamRunner.run 换成一个立即返回的假函数
      （可选：断言它被调用过）。
    - 断言退出码 == 0，且 stderr 含 "已启动" / "已停止"。
    """
    raise NotImplementedError


@pytest.mark.skip(reason="[待补·进阶] 你来实现：SIGTERM 触发优雅停机")
def test_sigterm_triggers_graceful_stop(monkeypatch, tmp_path):
    """[待补·进阶] 收到 SIGTERM → 置 stop 事件 → runner 收尾后退出 0。

    提示：worker 在 main() 内注册 SIGTERM/SIGINT handler 置 stop 事件
    （见 stream_worker.py 的 _on_signal）。可以：
    - 替换 StreamRunner.run 为"记录传入的 stop，并主动 set() 后返回"；
    - 断言最终退出码 0。
    进阶：用 signal.raise_signal(signal.SIGTERM) 真实触发 handler（注意 handler
    只在主线程有效，pytest 默认在主线程跑，可行）。
    """
    raise NotImplementedError
