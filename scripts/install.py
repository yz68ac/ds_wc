"""New-host bootstrap; use --dry-run to inspect the fixed installation plan."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

if __name__ == '__main__':
    from wxbridge_service.install import main
    raise SystemExit(main(source=ROOT))
