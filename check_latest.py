"""Read a local/public latest.json and return quality fields evaluated now."""
import argparse
import json
from pathlib import Path
from collector import get
from quality import refresh_quality

DEFAULT_URL = 'https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/latest.json'

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--path', type=Path)
    group.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--require-usable', action='store_true')
    args = parser.parse_args()
    data = json.loads(args.path.read_text()) if args.path else get(args.url)[0]
    data = refresh_quality(data)
    print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    raise SystemExit(1 if args.require_usable and not data['monitoring_eligible'] else 0)
