"""Rebuild, audit, and validate all hospitals from raw repository inputs."""

from pathlib import Path
import argparse
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run(command):
    subprocess.run([sys.executable, "-B", *command], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluate-h1", action="store_true",
                        help="Evaluate Hospital 1 predictions against its labels")
    args = parser.parse_args()

    run(["src/extract_rules.py", "--all"])
    run(["src/match_services.py", "--all"])
    audit = ["src/audit_invoices.py", "--all"]
    if args.evaluate_h1:
        audit.append("--evaluate")
    run(audit)
    run(["src/validate_all.py"])


if __name__ == "__main__":
    main()
