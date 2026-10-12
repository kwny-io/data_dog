"""配置管理：本地保存用户选的供应商与 API key。

用项目根目录下的 .env 文件保存（dotenv 读入），用户不碰代码即可配置。
"""
from __future__ import annotations

import os
from pathlib import Path

import dotenv

from .registry import get_vendor


def find_project_root(start: Path | None = None) -> Path:
    """向上找包含 pyproject.toml 的目录作为项目根；找不到则用当前目录。"""
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / "pyproject.toml").exists():
            return p
    return cur


def config_path(root: Path | None = None) -> Path:
    return find_project_root(root) / ".env"


def env_key_name(vendor_id: str) -> str:
    return get_vendor(vendor_id).requires_env_key


def load_config(root: Path | None = None) -> dict:
    """读回已保存的配置（供应商 / 模型 / key / 自定义端点 base_url）。

    规范名 @@DD_LLM_*@@ 优先，@@DATA_DOG_*@@ 作为历史别名兜底。
    """
    env, root = load_env(root)
    vendor_id = (env.get("DD_LLM_VENDOR") or env.get("DATA_DOG_VENDOR")
                 or "deepseek")
    api_key = env.get(env_key_name(vendor_id), "") or ""
    model = env.get("DD_LLM_MODEL") or env.get("DATA_DOG_MODEL") or ""
    base_url = (env.get("DD_LLM_BASE_URL") or env.get("DATA_DOG_BASE_URL") or "")
    return {
        "root": str(root),
        "vendor_id": vendor_id,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
    }


def save_config(
    vendor_id: str,
    api_key: str,
    model: str,
    root: Path | None = None,
) -> Path:
    """把接入结果写入 .env。"""
    root = find_project_root(root)
    path = root / ".env"
    dotenv.set_key(path, "DATA_DOG_VENDOR", vendor_id)
    vs = get_vendor(vendor_id)
    dotenv.set_key(path, vs.requires_env_key, api_key, quote_mode="never")
    if model:
        dotenv.set_key(path, "DATA_DOG_MODEL", model)
    # 尽力收紧权限：.env 里是**明文密钥**，别让同机其他用户可读（POSIX 0600）。
    # Windows 上 chmod 语义不同（只切只读位），失败不致命——仍应靠账户/磁盘权限。
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_env(root: Path | None = None) -> tuple[dict, Path]:
    root = find_project_root(root)
    path = root / ".env"
    env = {}
    if path.exists():
        env = dotenv.dotenv_values(path)
    return env, root


def ensure_key_from_env_or_file(root: Path | None = None) -> str:
    """优先用已存在的环境变量 key，其次读 .env，避免覆盖用户已有配置。"""
    env, _ = load_env(root)
    for v in [get_vendor(saved_vendor(root)), ]:
        key = os.environ.get(v.requires_env_key) or env.get(v.requires_env_key, "")
        if key:
            return key
    return ""


def saved_vendor(root: Path | None = None) -> str:
    env, _ = load_env(root)
    return env.get("DATA_DOG_VENDOR", "deepseek")
