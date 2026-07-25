"""summary: emit a bounded hello line for local-script acceptance"""

def greet(name: str) -> str:
    return f"hello:{name}"

def main() -> int:
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "world"
    print(greet(target))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
