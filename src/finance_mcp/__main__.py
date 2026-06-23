"""Console entry point: `python -m finance_mcp <command>`.

Commands:
  demo          Build and print a digest from bundled synthetic data (no bank needed).
  demo --json   Print the synthetic transaction dataset as JSON.
  analytics     Run the SQL analytical read-models (monthly cash flow, category
                mix, top merchants) — defaults to synthetic data; see queries.sql.
"""
import sys

from finance_mcp import demo
from finance_mcp.store import analytics


def main():
    args = sys.argv[1:]
    if not args or args[0] == "demo":
        demo.main(args[1:])
        return 0
    if args[0] == "analytics":
        analytics.main(args[1:])
        return 0
    sys.stderr.write(f"unknown command: {args[0]!r}\n")
    sys.stderr.write("usage: python -m finance_mcp [demo [--json] | analytics [--db F] [--owner N]]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
