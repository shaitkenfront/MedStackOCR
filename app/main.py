from __future__ import annotations


def main() -> int:
    print("このブランチはWebhook起動のみ対応です。")
    print("起動コマンド: uvicorn app.line_webhook:app --host 0.0.0.0 --port 8000")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
