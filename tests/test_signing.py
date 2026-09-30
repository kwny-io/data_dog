"""P4-2 签名 / 验签测试（对齐 docs/p4/02_control_plane.md §5.1）。

核心不变量：**内容绑定**——改一个字节即验签失败。
"""
from __future__ import annotations

import pytest

from data_dog.operator_factory import signing
from data_dog.operator_factory.signing import (
    ALG_HMAC,
    SigningError,
    canonical_payload,
    sign_package,
    source_hash,
    verify_or_raise,
    verify_package,
)

SECRET = "test-secret-key"


# ---------------------------------------------------------------- 往返

def test_sign_verify_roundtrip():
    pkg = sign_package("op1", "1.0.0", "temperature >= 75", secret=SECRET)
    assert verify_package(pkg, secret=SECRET, source="temperature >= 75") is True
    assert pkg.alg == ALG_HMAC
    assert pkg.dsl_hash == source_hash("temperature >= 75")


def test_wrong_secret_rejected():
    pkg = sign_package("op1", "1.0.0", "x >= 1", secret=SECRET)
    assert verify_package(pkg, secret="other-key") is False


# ---------------------------------------------------------------- 内容绑定（§5.1）

def test_tamper_one_byte_rejected():
    """改一个字节 → dsl_hash 变 → 验签 False（这是本项的核心安全属性）。"""
    pkg = sign_package("op1", "1.0.0", "temperature >= 75", secret=SECRET)
    assert verify_package(pkg, secret=SECRET, source="temperature >= 76") is False
    assert verify_package(pkg, secret=SECRET, source="temperature >= 75 ") is False


def test_tamper_id_or_version_rejected():
    pkg = sign_package("op1", "1.0.0", "x >= 1", secret=SECRET)
    d = pkg.to_dict()
    d["operator_id"] = "op2"          # 改 id
    assert verify_package(d, secret=SECRET) is False
    d2 = pkg.to_dict()
    d2["version"] = "2.0.0"           # 改版本
    assert verify_package(d2, secret=SECRET) is False


def test_tamper_sig_rejected():
    pkg = sign_package("op1", "1.0.0", "x >= 1", secret=SECRET)
    d = pkg.to_dict()
    d["sig"] = "0" * len(d["sig"])
    assert verify_package(d, secret=SECRET) is False


# ---------------------------------------------------------------- 失败关闭

def test_missing_key_is_fail_closed():
    """没密钥 → False（不因"没钥匙"而放行）。"""
    pkg = sign_package("op1", "1.0.0", "x >= 1", secret=SECRET)
    assert verify_package(pkg) is False


def test_sign_empty_secret_raises():
    with pytest.raises(SigningError):
        sign_package("op1", "1.0.0", "x >= 1", secret="")


def test_verify_or_raise_raises_on_fail():
    pkg = sign_package("op1", "1.0.0", "x >= 1", secret=SECRET)
    with pytest.raises(SigningError):
        verify_or_raise(pkg, secret="wrong")
    verify_or_raise(pkg, secret=SECRET, source="x >= 1")  # 通过则不抛


def test_unknown_alg_rejected():
    d = {"operator_id": "op", "version": "1", "dsl_hash": "x",
         "alg": "rot13", "sig": "abc"}
    assert verify_package(d, secret=SECRET) is False


def test_malformed_package_rejected():
    assert verify_package({}, secret=SECRET) is False
    assert verify_package({"alg": "hmac-sha256"}, secret=SECRET) is False


# ---------------------------------------------------------------- 规范编码

def test_canonical_payload_is_boundary_safe():
    """移位攻击防护：不同 (id, version) 组合不得产出同一签名串。"""
    a = canonical_payload("a", "b|c", "h")
    b = canonical_payload("a|b", "c", "h")
    assert a != b


def test_canonical_payload_deterministic():
    assert canonical_payload("op", "1", "h") == canonical_payload("op", "1", "h")


def test_ed25519_optional_unavailable_is_honest():
    """未装 cryptography 时：Ed25519 签名必须**明确报错**，不静默降级成 HMAC。"""
    if signing._try_ed25519() is not None:
        pytest.skip("本环境已装 cryptography，跳过'未装'分支")
    with pytest.raises(SigningError, match="cryptography"):
        sign_package("op1", "1.0.0", "x >= 1", secret="00" * 32,
                     alg=signing.ALG_ED25519)
