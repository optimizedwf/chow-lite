import os
import sys

def check_token(value):
    # BUG: whitespace is truthy, so it is treated as a valid token
    return "ok" if value else "missing"

def main():
    # BUG: unset env -> KeyError traceback instead of a clean error
    token = os.environ["NINE_TEST_TOKEN"]
    if not token:
        sys.stderr.write("[error] NINE_TEST_TOKEN is empty\n")
        return 1
    sys.stdout.write(f"[ok] token accepted: {token[:4]}...\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
