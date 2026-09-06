# -*- coding: utf-8 -*-
"""配置管理：本地保存用户选的供应商与 API key。

用项目根目录下的 .env 文件保存（dotenv 读入），用户不碰代码即可配置。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import dotenv

from .registry import VendorSpec, get_vendor


def find_project_root(start: Optional[Path] = None) -> Path:
    """向上找包含 pyproject.toml 的目录作为项目根；找不到则用当前目录。"""
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if (p / "pyproject.toml").exists():
            return p
    return cur


def config_path(root: Optional[Path] = None) -> Path:
    return find_project_root(root) / ".env"


def env_key_name(vendor_id: str) -> str:
    return get_vendor(vendor_id).requires_env_key


def load_config(root: Optional[Path] = None) -> dict:
    """读回已保存的配置（含选中的供应商、模型、key）。"""
    env, root = load_env(root)
    vendor_id = env.get("DATA_DOG_VENDOR", "deepseek")
    api_key = env.get(env_key_name(vendor_id), "") or ""
    model = env.get("DATA_DOG_MODEL", "") or ""
    return {
        "root": str(root),
        "vendor_id": vendor_id,
        "model": model,
        "api_key": api_key,
    }


def save_config(
    vendor_id: str,
    api_key: str,
    model: str,
    root: Optional[Path] = None,
) -> Path:
    """把接入结果写入 .env。"""
    root = find_project_root(root)
    dotenv.set_key(root / ".env", "DATA_DOG_VENDOR", vendor_id)
    vs = get_vendor(vendor_id)
    dotenv.set_key(root / ".env", vs.requires_env_key, api_key, quote_mode="never")
    if model:
        dotenv.set_key(root / ".env", "DATA_DOG_MODEL", model)
    return root / ".env"


def load_env(root: Optional[Path] = None) -> tuple[dict, Path]:
    root = find_project_root(root)
    path = root / ".env"
    env = {}
    if path.exists():
        env = dotenv.dotenv_values(path)
    return env, root


def ensure_key_from_env_or_file(root: Optional[Path] = None) -> str:
    """优先用已存在的环境变量 key，其次读 .env，避免覆盖用户已有配置。"""
    env, _ = load_env(root)
    for v in [get_vendor(saved_vendor(root)), ]:
        key = os.environ.get(v.requires_env_key) or env.get(v.requires_env_key, "")
        if key:
            return key
    return ""


def saved_vendor(root: Optional[Path] = None) -> str:
    env, _ = load_env(root)
    return env.get("DATA_DOG_VENDOR", "deepseek")
