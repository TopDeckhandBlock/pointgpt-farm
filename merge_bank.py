#!/usr/bin/env python3
"""merge_bank.py — слить несколько банков авторега в один master_bank для pg_shim.

Usage: python merge_bank.py out.jsonl bank1.jsonl bank2.jsonl ...
       python merge_bank.py out.jsonl banks/*.jsonl pointgpt_bank.jsonl

Дедуп по token; только status==ok; считает суммарные монеты.
Stdlib only, Python 3.9+.
"""
import glob
import json
import sys


def merge(out_path, in_paths):
    seen = set()
    rows = []
    total = 0.0
    for f in in_paths:
        for p in sorted(glob.glob(f)) or [f]:
            try:
                fh = open(p, encoding='utf-8')
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get('status') != 'ok' or not d.get('token'):
                        continue
                    if d['token'] in seen:
                        continue
                    seen.add(d['token'])
                    rows.append(d)
                    total += float(d.get('balance') or 0)
    with open(out_path, 'w', encoding='utf-8') as fo:
        for d in rows:
            fo.write(json.dumps(d, ensure_ascii=False) + '\n')
    print('merged %d accounts, %.1f coins -> %s' % (len(rows), total, out_path))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2:])
