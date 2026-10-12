"""无损视觉记忆（VISTA-1 的工业诊断同构）：原始帧按内容寻址、不重编码地留存，
Agent 可按需检索与裁剪放大。

对应论文
--------
VISTA: A Visual Harness for Reasoning in an Interactive World（arXiv 2610.02200，
MIT，Han / Hu / Qiu / Wu / He）。其核心判断是：**编码或压缩的那一刻，模型并不知道
后面会需要哪些细节**——所以把原始观测无损留存，让模型主动检索、按需放大，而不是
只喂压缩表征或文字摘要。本模块把这条原则落到机器人遥测调查：候选异常窗口内设备
相机的原始 PNG 帧无损留存，Agent 用 query_visual_evidence 检索、用 view_visual_frame
调取或裁剪局部细节。

诚实边界
--------
- 帧**不做任何重编码**；裁剪产生的是**新帧**（无损于裁剪结果），原帧不动。
- crop 需要 Pillow（可选依赖 `.[vision]`）；缺失时 available=False，对应工具不暴露
  （对纯文本链路零回归）。
- 存储是**本地目录**（内容寻址），不带副本与生命周期；生产应由外部对象存储承担。
- 与 VISTA 的差别：VISTA 是交互式环境里的**动作-观察闭环**；这里是**只读证据**通道，
  Agent 不控制设备相机（无动作回环）——只在"看"这一半同构。
"""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path
from typing import Any


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class VisualFrameStore:
    """内容寻址的原始帧存储 + 按需裁剪放大。

    frame_id = vf_<sha256 前 16 位>；帧文件以**全 sha256** 命名，故同内容只存一份
    （无损、天然去重）。裁剪产生**新帧**并记 parent_frame_id，原帧永不修改。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._idx: dict[str, dict[str, Any]] | None = None

    # ---- 能力 ----
    @property
    def available(self) -> bool:
        """Pillow 是否可用（crop 需要）。缺失 → 工具不暴露（零回归）。"""
        try:
            import PIL  # noqa: F401
            return True
        except Exception:                          # noqa: BLE001
            return False

    # ---- 写入 ----
    def put(self, png: bytes, *, device_sn: str, component_ref: str = "",
            observed_at: str = "", source: str = "camera", label: str = "",
            parent_frame_id: str = "", crop_box: list[int] | None = None
            ) -> dict[str, Any]:
        sha = hashlib.sha256(png).hexdigest()
        img_path = self.root / f"{sha}.png"
        meta_path = self.root / f"{sha}.json"
        if not img_path.exists():                  # 只写一次：内容相同即同一帧
            img_path.write_bytes(png)
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:                      # noqa: BLE001
                meta = {}
        if not meta:
            meta = {"frame_id": f"vf_{sha[:16]}", "sha256": sha, "mime_type": "image/png",
                    "device_sn": device_sn, "component_ref": component_ref,
                    "observed_at": observed_at, "source": source, "label": label,
                    "bytes": len(png), "created_at": _now()}
        if parent_frame_id:
            meta["parent_frame_id"] = parent_frame_id
        if crop_box:
            meta["crop_box"] = list(crop_box)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if self._idx is not None:
            self._idx[meta["frame_id"]] = meta
        return dict(meta)

    # ---- 读取 ----
    def _ensure(self) -> dict[str, dict[str, Any]]:
        if self._idx is None:
            idx: dict[str, dict[str, Any]] = {}
            for m in self.root.glob("*.json"):
                try:
                    d = json.loads(m.read_text(encoding="utf-8"))
                except Exception:                  # noqa: BLE001
                    continue
                if d.get("frame_id"):
                    idx[d["frame_id"]] = d
            self._idx = idx
        return self._idx

    def meta(self, frame_id: str) -> dict[str, Any] | None:
        return self._ensure().get(frame_id)

    def get_bytes(self, frame_id: str) -> bytes | None:
        d = self.meta(frame_id)
        if not d:
            return None
        p = self.root / f"{d['sha256']}.png"
        return p.read_bytes() if p.exists() else None

    def list_frames(self, *, device_sn: str = "", component_ref: str = "",
                    window_start: str = "", window_end: str = "",
                    limit: int = 20) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for d in self._ensure().values():
            if device_sn and d.get("device_sn") != device_sn:
                continue
            if component_ref and d.get("component_ref") != component_ref:
                continue
            ts = d.get("observed_at", "")
            if window_start and ts and ts < window_start:
                continue
            if window_end and ts and ts > window_end:
                continue
            out.append(d)
        out.sort(key=lambda d: d.get("observed_at", ""))
        return out[:limit]

    # ---- 裁剪（VISTA-1 的"放大任意区域"）----
    def crop(self, frame_id: str, box: list[int]) -> dict[str, Any] | None:
        """按像素框 [l, t, r, b] 裁剪，返回**新帧**（原帧不动）。框非法/图不存在 → None。"""
        from PIL import Image
        raw = self.get_bytes(frame_id)
        if raw is None:
            return None
        src = self.meta(frame_id) or {}
        try:
            with Image.open(io.BytesIO(raw)) as im0:
                im = im0.convert("RGB")
                left, top, right, bottom = (int(x) for x in box[:4])
                left, top = max(0, min(left, im.width)), max(0, min(top, im.height))
                right = max(left, min(right, im.width))
                bottom = max(top, min(bottom, im.height))
                if right <= left or bottom <= top:
                    return None
                buf = io.BytesIO()
                im.crop((left, top, right, bottom)).save(buf, format="PNG")
        except Exception:                          # noqa: BLE001 - 坏图/坏框不抛，返回 None
            return None
        return self.put(buf.getvalue(), device_sn=src.get("device_sn", ""),
                        component_ref=src.get("component_ref", ""),
                        observed_at=src.get("observed_at", ""),
                        source=src.get("source", "camera"),
                        label=f"{src.get('label', '')} crop={left},{top},{right},{bottom}".strip(),
                        parent_frame_id=frame_id,
                        crop_box=[left, top, right, bottom])
