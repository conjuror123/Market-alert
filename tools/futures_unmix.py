"""One-off: drop the stretches where a stored continuous future interleaves two
contract months (jump.futures.drop_mixed), and reset its stray opens on the
series' own line (reset_stray_opens). Live cattle's history is Yahoo's
continuous series, which Dukascopy cannot replace (it has no cattle).

    python -m tools.futures_unmix LE=F
"""
from __future__ import annotations

import argparse
import sys

from jump import bars, futures
from jump.basket import load_basket


def main(argv: "list[str] | None" = None) -> int:
    continuous = {a.ticker: a for a in load_basket().instruments
                  if futures.is_continuous(a.ticker)}
    parser = argparse.ArgumentParser(
        description="Drop a stored future's interleaved stretches (rewrites its store).")
    parser.add_argument("ticker", choices=sorted(continuous))
    asset = continuous[parser.parse_args(argv).ticker]
    path = bars.store_path(bars.DEFAULT_BARS_DIR, asset.file_stem)
    stored = bars.load(path)
    kept, stretches = futures.drop_mixed(stored, asset.session_template)
    kept, stray = futures.reset_stray_opens(kept)
    print(f"{asset.ticker}: {len(stored) - len(kept)} hours dropped in "
          f"{len(stretches)} stretches: {stretches}; {stray} stray opens reset")
    bars.write(path, kept)
    return 0


if __name__ == "__main__":
    sys.exit(main())
