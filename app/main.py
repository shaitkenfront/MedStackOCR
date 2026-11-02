from __future__ import annotations


def main() -> int:
    print("このブランチはAWS Lambda運用専用です。")
    print("デプロイ手順は infra/cdk/README.md を参照してください。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
