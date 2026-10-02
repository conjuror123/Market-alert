"""One-off: drop the stretches where a stored continuous future interleaves two
contract months (tremor.futures.drop_mixed), and reset its stray opens on the
series' own line (reset_stray_opens). Live cattle's history is Yahoo's
continuous series, which Dukascopy cannot replace (it has no cattle).

    python -m tools.futures_unmix LE=F
"""
from __future__ import annotations

import sys

from tremor import bars, futures
from tremor.basket import load_basket


def main() -> int:
    asset = next(a for a in load_basket().instruments if a.ticker == sys.argv[1])
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
