#!/usr/bin/env python3
"""Set per-model `auto_compact_token_limit` in a Codex model catalog so auto-compaction
fires at ~75% of each model's usable context window.

Background (see NOTES.md): Codex's auto-compaction trigger is driven by the per-model
`auto_compact_token_limit` field in the catalog referenced by `model_catalog_json`. If that
field is null, Codex falls back to a low default and compacts very early (~17%). And the trigger
is `live_context + output_reserve >= limit`, so to land the REAL trigger at 75% the limit must be
set to `0.75 * usable_window + reserve` (reserve ~34k at Extra-High reasoning effort).

Usage:
    python3 set-auto-compact-limits.py [--catalog PATH] [--percent 0.75] [--reserve 34000] [--dry-run]

Then verify with:  codex debug models
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_CATALOG = Path.home() / ".codex" / "models_catalog_patched.json"


def usable_window(model):
    cw = model.get("context_window")
    if not isinstance(cw, int) or cw <= 0:
        return None
    pct = model.get("effective_context_window_percent")
    pct = pct if isinstance(pct, (int, float)) and pct > 0 else 100
    return int(cw * pct / 100)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                    help=f"path to the model catalog JSON (default: {DEFAULT_CATALOG})")
    ap.add_argument("--percent", type=float, default=0.75,
                    help="fraction of the usable window to compact at (default: 0.75)")
    ap.add_argument("--reserve", type=int, default=34000,
                    help="output/reasoning reserve added on top so the REAL trigger lands at --percent "
                         "(default: 34000, measured at Extra-High effort)")
    ap.add_argument("--dry-run", action="store_true", help="print changes without writing")
    args = ap.parse_args()

    if not args.catalog.exists():
        sys.exit(f"catalog not found: {args.catalog}\n"
                 "Point --catalog at the file your config.toml `model_catalog_json` references.")

    data = json.loads(args.catalog.read_text(encoding="utf-8"))
    models = data.get("models", data)
    items = models if isinstance(models, list) else list(models.values())

    print(f"{'model':20} {'usable window':>14} {f'{int(args.percent*100)}% trigger':>12} {'-> limit':>10}")
    changed = 0
    for model in items:
        if not isinstance(model, dict):
            continue
        name = model.get("slug") or model.get("id") or model.get("name") or "?"
        window = usable_window(model)
        if window is None:
            continue
        trigger = int(window * args.percent)
        limit = trigger + args.reserve
        model["auto_compact_token_limit"] = limit
        changed += 1
        print(f"{str(name):20} {window:>14,} {trigger:>12,} {limit:>10,}")

    if not changed:
        sys.exit("no models with a context_window found in the catalog.")

    if args.dry_run:
        print("\n[dry-run] no changes written.")
        return

    backup = args.catalog.with_suffix(
        args.catalog.suffix + ".bak." + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(args.catalog, backup)
    args.catalog.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"\nwrote {args.catalog}  (backup: {backup.name})")
    print("verify with:  codex debug models")
    print("note: takes effect on a NEW Codex thread.")


if __name__ == "__main__":
    main()
