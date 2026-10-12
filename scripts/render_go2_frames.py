"""渲染 Go2 画面 + 注入**外观故障**（半真实视觉证据）。

为什么要有它
-----------
上一次视觉 A/B 是**负结果**，根因是"图里只有和文本同源的曲线"——**没有文本拿不到的信息**。
本脚本用 MuJoCo **真渲染** Go2（第三人称相机），再叠加**外观故障**（漏油 / 形变 / 异物）：
这类故障**遥测看不出来**，只有画面看得到——这才是视觉该有作用的场景。

诚实边界
--------
- 外观故障是**画面叠加**（PIL 绘制），不是物理仿真出的形变 / 漏液；但它**定义明确、
  ground truth 清楚**，足以回答"视觉是否带来文本拿不到的信息"。
- Windows 用 `wgl` 离屏渲染；无 GL 时**明确报错**（不静默出空白图）。
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from data_dog.core.console import ensure_utf8_console  # noqa: E402

_SCENE = _ROOT / "third_party/mujoco_menagerie/unitree_go2/scene.xml"
DEFECTS = [None, "oil_leak", "deformation", "foreign_object"]


def _raw_render(width: int, height: int, seed: int):
    import mujoco
    from PIL import Image
    m = mujoco.MjModel.from_xml_path(str(_SCENE))
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(cam)
    cam.distance = 2.1
    cam.azimuth = 130 + (seed % 7) * 3          # 轻微视角抖动（别每帧一模一样）
    cam.elevation = -18
    cam.lookat[:] = [0.0, 0.0, 0.30]
    r = mujoco.Renderer(m, height=height, width=width)
    r.update_scene(d, camera=cam)
    img = r.render()
    r.close()
    return Image.fromarray(img)


def _overlay(img, defect: str, seed: int):
    """在渲染图上叠加外观故障（PIL 绘制，非物理）。"""
    from PIL import ImageDraw
    w, h = img.size
    d = ImageDraw.Draw(img, "RGBA")
    if defect == "oil_leak":
        # 深色油渍：机身下方一片不规则椭圆 + 几滴
        d.ellipse([w * 0.28, h * 0.74, w * 0.62, h * 0.86], fill=(25, 22, 20, 220))
        d.ellipse([w * 0.34, h * 0.72, w * 0.44, h * 0.78], fill=(15, 13, 12, 200))
        for i in range(3):
            x = w * (0.30 + 0.06 * i + (seed % 3) * 0.01)
            d.ellipse([x, h * 0.87, x + w * 0.03, h * 0.90], fill=(30, 26, 24, 190))
    elif defect == "deformation":
        # 前腿"形变"：一块不对称凹痕 + 裂纹
        d.polygon([(w * 0.40, h * 0.44), (w * 0.52, h * 0.40), (w * 0.55, h * 0.58),
                   (w * 0.43, h * 0.62)], fill=(120, 60, 55, 170))
        d.line([(w * 0.42, h * 0.45), (w * 0.50, h * 0.52)], fill=(20, 20, 20, 230), width=2)
        d.line([(w * 0.50, h * 0.52), (w * 0.45, h * 0.60)], fill=(20, 20, 20, 230), width=2)
    elif defect == "foreign_object":
        # 机身上一个异物（亮色小块 + 阴影）
        d.rectangle([w * 0.54, h * 0.40, w * 0.62, h * 0.47], fill=(220, 180, 40, 235))
        d.ellipse([w * 0.53, h * 0.47, w * 0.63, h * 0.50], fill=(0, 0, 0, 90))
    return img


def render_go2(*, width: int = 320, height: int = 240, defect: str | None = None,
               seed: int = 0) -> bytes:
    """渲染一帧 Go2（可选叠加外观故障），返回 PNG bytes。"""
    img = _raw_render(width, height, seed)
    if defect:
        img = _overlay(img, defect, seed)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ascii(img, cols: int = 64, rows: int = 26) -> str:
    small = img.convert("L").resize((cols, rows))
    chars = " .:-=+*#%@"
    out = []
    for y in range(rows):
        out.append("".join(chars[min(9, small.getpixel((x, y)) * 10 // 256)] for x in range(cols)))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 Go2 画面（可注入外观故障）")
    ap.add_argument("--out", help="把 4 类图各存一张到该目录（便于人工看）")
    ap.add_argument("--check", action="store_true", help="打印 ASCII 预览（确认不是空白图）")
    args = ap.parse_args()
    ensure_utf8_console()
    from PIL import Image
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for defect in DEFECTS:
            png = render_go2(defect=defect, seed=0)
            (out / f"{defect or 'clean'}.png").write_bytes(png)
            print("wrote", out / f"{defect or 'clean'}.png", len(png), "bytes")
    if args.check or not args.out:
        for defect in DEFECTS:
            png = render_go2(defect=defect, seed=0)
            img = Image.open(io.BytesIO(png))
            print(f"\n--- {defect or 'clean'} ({img.size[0]}x{img.size[1]}, {len(png)}B) ---")
            print(_ascii(img))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
