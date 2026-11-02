"""Export or check the generated reference snapshot; never used by runtime loading."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from wxbridge.config import default_document


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--write',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'config.defaults.json')
    args=parser.parse_args(argv)
    value=default_document()
    if args.check:
        try:
            actual=json.loads(args.output.read_text(encoding='utf-8-sig'))
        except (OSError,ValueError):
            actual=None
        if actual!=value:
            print('Default reference differs; run scripts/config-defaults.py --write.',file=sys.stderr)
            return 1
        print('OK: generated default reference')
    else:
        args.output.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
