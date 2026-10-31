"""Human-facing host controls; the service's private entry remains service.py."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

if __name__ == '__main__':
    from wxbridge_service.cli import main
    raise SystemExit(main(root=ROOT))
