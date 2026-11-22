#!/usr/bin/env python3
import os
import sys


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    key = (
        "KAGEMUSHA_GIT_USERNAME"
        if prompt.lower().startswith("username")
        else "KAGEMUSHA_GIT_TOKEN"
    )
    value = os.environ.get(key)
    if value is None:
        return 1
    sys.stdout.write(value)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
