"""P4-2：算子制品签名 / 验签（真实，非字符串检查）。

目标（docs/p4/02_control_plane.md §3.2）：改一个字节就验签失败。签名对象是
**内容绑定**——`operator_id` + `version` + `dsl_hash(source)`，任一处变动即失效。

为什么是 HMAC-SHA256 起步（而非 Ed25519）
----------------------------------------
文档 §3.2 给出两种：「Ed25519（`cryptography` 库）**或 HMAC-SHA256 起步**」。
本项目坚持**零新必需依赖**（`cryptography` 未在依赖里，见 pyproject）——故：
  - **默认 HMAC-SHA256**：纯标准库（`hmac` + `hashlib`），对称密钥。
  - **Ed25519 为可选加强**：装了 `cryptography` 且用 `DD_OPERATOR_PRIVKEY_ED25519`
    时启用非对称签名（公钥验签，私钥不接触验签方）。未装则**明确拒绝**，不静默降级。

诚实边界（§6）：
  - 本模块**只提供签名机制，不提供 KMS/HSM**。私钥进环境变量；生产接 KMS 属更大工程。
  - HMAC 是对称的：验签方也持有私钥。**跨信任域分发**（验签方不该有私钥）应改用
    Ed25519——这是本节留出 Ed25519 分支的原因，不是可省的细节。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time as _t
from dataclasses import dataclass
from typing import Any

ALG_HMAC = "hmac-sha256"
ALG_ED25519 = "ed25519"


class SigningError(ValueError):
    """签名/验签的配置或格式错误（统一归类 invalid_input）。"""


@dataclass(frozen=True)
class SignedPackage:
    """一个签名后的算子制品。`sig` 绑定 (operator_id, version, dsl_hash)。"""
    operator_id: str
    version: str
    dsl_hash: str
    alg: str
    key_id: str
    sig: str
    signed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id, "version": self.version,
            "dsl_hash": self.dsl_hash, "alg": self.alg, "key_id": self.key_id,
            "sig": self.sig, "signed_at": self.signed_at,
        }


def canonical_payload(operator_id: str, version: str, dsl_hash: str) -> bytes:
    """待签名的规范字节串（内容绑定）。

    为什么用 JSON + 排序键而非简单拼接：拼接的字段边界若含分隔符即可被"移位攻击"
    （`a|x` + `y` 与 `a` + `x|y` 产出同一串）。JSON 的引号/转义给出无歧义边界，
    `sort_keys` 保证字段序确定 → 同内容恒等。
    """
    return json.dumps(
        {"operator_id": operator_id, "version": version, "dsl_hash": dsl_hash},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def source_hash(source: str) -> str:
    """算子源的 sha256（确定性 ID 的基础，也是签名绑定对象）。"""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _key_id_hmac(secret: str) -> str:
    """密钥指纹（前 12 位）：用于轮换时识别"这条签名用的是哪把钥匙"，不泄露密钥。"""
    return hashlib.sha256(("hmac:" + secret).encode("utf-8")).hexdigest()[:12]


def _hmac_sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _try_ed25519():
    """延迟导入 cryptography；未装 → None（调用方据此决定口径）。"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # type: ignore
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        return Ed25519PrivateKey, Ed25519PublicKey
    except ImportError:
        return None


def sign_package(operator_id: str, version: str, source: str, *,
                 secret: str, alg: str = ALG_HMAC,
                 signed_at: float | None = None) -> SignedPackage:
    """对一个算子源签名。`secret`：HMAC 为对称密钥；Ed25519 为私钥的 hex 编码。

    失败关闭：`secret` 为空 → 抛 `SigningError`（绝不产出"空签名当有效"）。
    """
    if not secret:
        raise SigningError("缺少签名密钥（DD_OPERATOR_SIGNING_KEY）")
    dsl_hash = source_hash(source)
    payload = canonical_payload(operator_id, version, dsl_hash)
    ts = _t.time() if signed_at is None else signed_at

    if alg == ALG_HMAC:
        return SignedPackage(operator_id, version, dsl_hash, ALG_HMAC,
                             _key_id_hmac(secret), _hmac_sign(payload, secret), ts)
    if alg == ALG_ED25519:
        mods = _try_ed25519()
        if mods is None:
            raise SigningError(
                "Ed25519 需要可选依赖 cryptography（pip install data_dog[signing]）")
        Ed25519PrivateKey, _ = mods
        try:
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret))
        except (ValueError, TypeError) as e:
            raise SigningError(f"Ed25519 私钥需为 hex 编码的 32 字节: {e}") from e
        sig = priv.sign(payload).hex()
        return SignedPackage(operator_id, version, dsl_hash, ALG_ED25519,
                             hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:12],
                             sig, ts)
    raise SigningError(f"未知签名算法: {alg!r}")


def verify_package(pkg: dict[str, Any] | SignedPackage, *,
                   secret: str | None = None,
                   pubkey: str | None = None,
                   source: str | None = None) -> bool:
    """验签。返回 True/False（**不抛**——验签失败是预期内的判定结果）。

    内容绑定：若给了 `source`，先重算 `dsl_hash` 并比对——**改一个字节即 False**。
    `secret`（HMAC）/ `pubkey`（Ed25519）缺失 → False（失败关闭，不因"没钥匙"而放行）。
    """
    d = pkg.to_dict() if isinstance(pkg, SignedPackage) else dict(pkg)
    alg = d.get("alg")
    try:
        dsl_hash = d["dsl_hash"]
        if source is not None and source_hash(source) != dsl_hash:
            return False  # 源内容与签名时不一致（被改过）
        payload = canonical_payload(d["operator_id"], d["version"], dsl_hash)

        if alg == ALG_HMAC:
            if not secret:
                return False
            expect = _hmac_sign(payload, secret)
            return hmac.compare_digest(expect, str(d.get("sig", "")))
        if alg == ALG_ED25519:
            if not pubkey:
                return False
            mods = _try_ed25519()
            if mods is None:
                return False  # 未装 cryptography → 无法验签，失败关闭
            _, Ed25519PublicKey = mods
            try:
                pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey))
                pk.verify(bytes.fromhex(str(d.get("sig", ""))), payload)
                return True
            except Exception:  # noqa: BLE001 — InvalidSignature/格式错 一律 False
                return False
        return False  # 未知算法 → 不认
    except (KeyError, ValueError, TypeError):
        return False


def verify_or_raise(pkg: dict[str, Any] | SignedPackage, *,
                    secret: str | None = None, pubkey: str | None = None,
                    source: str | None = None) -> None:
    """验签，失败即抛 `SigningError`。注册路径用这个（失败关闭 → 拒绝注册）。"""
    if not verify_package(pkg, secret=secret, pubkey=pubkey, source=source):
        raise SigningError("验签失败：签名与内容不符或密钥缺失")
