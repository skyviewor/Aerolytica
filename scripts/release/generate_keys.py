"""Generate a local Ed25519 release key pair for GitHub and a password manager."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate_key_pair() -> tuple[str, str, str]:
    """Return the Base64 seed, Base64 public key, and release-compatible key ID."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    return (
        base64.b64encode(key.private_bytes_raw()).decode(),
        base64.b64encode(public).decode(),
        hashlib.sha256(public).hexdigest()[:16],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地生成发行签名密钥；不上传、不写文件、不修改 GitHub 配置。"
    )
    parser.add_argument(
        "--environment",
        choices=("production", "testing"),
        default="production",
        help="仅用于标注密钥所属环境，默认 production；每次执行生成全新密钥。",
    )
    args = parser.parse_args(argv)
    if not sys.stdout.isatty():
        print("拒绝向管道或重定向输出私钥，请在本地交互式终端直接运行。", file=sys.stderr)
        return 1

    print(
        "注意：下面会显示私钥。请关闭录屏/共享屏幕，复制到 1Password 后清理终端记录。",
        file=sys.stderr,
    )
    seed, public, key_id = generate_key_pair()
    print(f"Aerolytica 发行签名密钥（{args.environment}）")
    print(f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("算法：Ed25519；私钥格式：32-byte seed 的 Base64 编码")
    print(f"密钥标识：{key_id}")
    print("\nGitHub Secret：RELEASE_SIGNING_KEY（私钥，必须保密）")
    print(seed)
    print("\n公钥（可公开，建议与私钥一起记录到 1Password）")
    print(public)
    print("\nGitHub Variable：RELEASE_PUBLIC_KEYS（复制下一行完整 JSON）")
    print(json.dumps({key_id: public}, sort_keys=True))
    print("\n请保存以上内容到 1Password；不要提交到 Git、粘贴到聊天或保存为公开附件。")
    print("本脚本不会替换已有配置。每次重跑都会生成新密钥；已发布后不要直接换私钥。")
    print("轮换时应合并新旧公钥 JSON，不要覆盖已有历史公钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
