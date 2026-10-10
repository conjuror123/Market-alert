# A new basket: proposal

A working proposal, under discussion with the user since 2026-10-10. Nothing here is in
`config/basket.yaml` yet: each change waits for the user's go, with its measured
before/after, and the whole basket changes in one cold rebuild.

## Why change it

The basket of 172 came from a paper and a round of additions. Measured on the stored
events (`data/jump/jumps.parquet`):

- **It sees the world late.** 132 instruments are New York-listed funds, so a market
  abroad is seen only in New York's hours. On 2024-08-05 the Nikkei had its worst day
  since 1987, trading 00:00-06:00 UTC; the basket saw Japan at 14:00 UTC, when EWJ
  opened, as a `noticeable` weekend gap in the note - not a push.
- **Coins crowd the channel.** Over the year to 2026-10-05 the 16 coins made 88 of the
  247 pushes; 30 of them were one small coin moving alone.
- **Look-alikes.** 62 funds fall in 21 groups that hold nearly the same thing (three gold
  funds, five mortgage-bond funds, four TIPS funds, ...). They mostly flag in the same
  hour as their twin: more lines, the same story.

## What a basket like this should watch

From the sources below, five ideas:

1. **Four connected markets** - stocks, bonds, commodities, currencies (Murphy's
   intermarket analysis). They move in known ways together; the news is often when the
   usual pattern breaks.
2. **Growth or inflation** (Bridgewater's All Weather): stocks, commodities and credit
   like growth; government bonds like a slowdown; commodities, inflation-linked bonds and
   gold like inflation. Every move answers: growth, inflation, or fear?
3. **Fear gauges and havens**: VIX (stocks), MOVE (bonds); the dollar, yen, franc,
   Treasuries and gold in a crisis - though not always (March 2026's energy shock: the
   dollar alone).
4. **The world by size**: the US about 64% of world stocks, then Japan 5%, Taiwan, the UK
   and Canada about 3% each (MSCI ACWI, 2026-08-31).
5. **A few giants**: Nvidia, Apple and Microsoft about a fifth of the S&P 500, the top 10
   about 39% (S&P Dow Jones Indices, 2026-09-30).

## The proposed basket (version 1)

| group | what it tells | instruments |
|---|---|---|
| world stocks, in their own hours (new) | how each region feels, as it happens | Nikkei 225, Hang Seng, CSI 300, KOSPI, TAIEX, Nifty 50, ASX 200, Euro Stoxx 50, DAX, FTSE 100, TSX, Bovespa, IPC, BIST 100 (14) - in place of the 19 country funds |
| US stocks | the biggest market, and who leads it | SPY, QQQ, IWM; the 11 sectors (XLK XLF XLY XLP XLE XLV XLI XLB XLU XLRE XLC); SMH, KRE, XBI, ITB, IYT (19) |
| the giants (optional, new) | a third of the S&P in a few companies | Nvidia, Apple, Microsoft, Amazon, Alphabet, Meta, Tesla (7) |
| fear (new, hourly) | panic in stocks, and in bonds | VIX, MOVE (2) |
| the price of money | the Fed; inflation; trust in debt | SHY, IEF, TLT, TIP (4) |
| credit | do lenders still trust borrowers? | LQD, HYG, BKLN, EMB, EMLC (5) |
| currencies | where money runs | the dollar index (new); EUR, JPY, CHF, GBP, CNH, AUD, CAD, NOK, MXN, BRL, INR, KRW, ZAR, TRY against the dollar (15) |
| energy | inflation and war | USO, BNO, UNG, UGA; Europe's gas, TTF (new, data to check) (5) |
| metals | fear (gold) and industry (copper) | GLD, SLV, PPLT, CPER; the LME's tin, nickel, aluminium; LIT, REMX (9) |
| food | weather, harvests, prices | CORN, WEAT, SOYB, CANE; coffee, cocoa, cotton, live cattle (8) |
| crypto | risk appetite, every hour | Bitcoin, Ethereum, Solana, XRP (4; the user's choice) |

85 instruments, 92 with the giants.

**The trim alone** (the 86 that stay of today's 172, nothing added), over the year to
2026-10-05: pushes 247 → 135, hours with a push 133 → 84, coin pushes 88 → 20. Of the 49
hours that would fall quiet, 41 pushes were small coins and 7 LMBS's.

**Data, checked 2026-10-10.** Yahoo serves about three years of hourly bars for each index,
VIX, MOVE and the giants (from late 2023); TTF, COMEX copper and the dollar index from
2024-05. The indices have no volume, and their bars start on their own minute (:00, :30,
Nifty :45). `exchange_calendars`, which builds the NYSE and B3 tables, has all 14
exchanges.

## Changes under discussion

The proposal's own critique (2026-10-10), each measured where it could be. The user
answers each.

1. **One oil, in its own hours.** USO and BNO flagged in the same hour 18 of 22 times
   (three years to 2026-10-05). And Brent's big moves come when the funds are closed:
   in Wallstreetcn's hourly Brent, 2026-04-22 to 10-09, 4 of 8 moves of 6 sd or more were
   Monday-morning gaps after a weekend (3 more look like the vendor's own glitches at its
   daily break). Change: one oil, as futures read by contract like coffee.
2. **Copper from the LME, like tin.** CPER trades in New York's hours; LME copper's two big
   moves in the same window were in London's morning. Sina and Wallstreetcn carry it as
   they carry the other three LME metals.
3. **Gold and silver stay funds.** Gold's four big moves in that window were all in New
   York's hours. No change.
4. **Drop QQQ.** The S&P 500 and the Nasdaq 100 move alike (hourly correlation 0.95; 33 of
   53 big hours together, Yahoo, 2023-11 to 2026-10). In the store, XLK flagged in each of
   QQQ's 8 hours over three years.
5. **One of Euro Stoxx 50 and DAX.** Hourly correlation 0.93 (the CAC 0.92); the FTSE is its
   own (0.75).
6. **Sectors 11 → 4.** Of 20 sector pushes in three years, 16 came in an hour when SPY, QQQ
   or IWM flagged too; 4 alone (XLP, XLE, XLV, XLF, one each). Keep tech, financials,
   energy, health care.
7. **Drop the dollar index.** It moves with EUR/USD (correlation −0.92; 76 of 139 big hours
   together), and the pairs carry the rest.
8. **Add the giants.** Their overnight gaps at the push level (8.5 sd against their own
   nights, a simple σ, not the detector's): 10 across the seven in 2.7 years, mostly
   earnings - about 4 pushes a year in all.
9. **LIT and REMX are companies, not metals** (battery makers, rare-earth miners): keep
   REMX for China's export controls, drop LIT. Not measured.
10. **BKLN looks noisy.** It flagged 23 times in three years to HYG's 12, together only 7.
    Drop it; HYG carries junk credit. Its lone flags were not checked.
11. **Nights.** Seven Asian indices ring in Europe's and the Americas' night, and there are
    no quiet hours. A question about delivery, not the basket.

The standing risk: Yahoo carries 34 instruments today and would carry about 58. Each new
instrument needs voters (Wallstreetcn has 38 index CFDs, the FT 24 days of indices).

## Sources

- StockCharts, intermarket relationships (Murphy):
  https://articles.stockcharts.com/article/articles-chartwatchers-2011-04-intermarket-picture-points-to-change-in-market-directions/
- LuxAlgo, intermarket analysis: https://www.luxalgo.com/library/concept/intermarket-analysis/
- A macro panel: https://www.thetrading.tools/macro-panel
- SSGA, the All Weather portfolio:
  https://www.ssga.com/us/en/individual/insights/the-all-weather-portfolio-built-for-any-forecast
- Banque de France, safe assets:
  https://www.banque-france.fr/en/publications-and-statistics/publications/have-us-treasuries-lost-their-momentum-evidence-new-taxonomy-safe-assets
- Advisor Perspectives, havens failing (March 2026):
  https://www.advisorperspectives.com/articles/2026/03/07/long-trusted-haven-trades-failing-gold-treasuries-fall
- MSCI ACWI factsheet: https://www.msci.com/documents/10199/255599/msci-acwi-net.pdf
- S&P 500 concentration:
  https://landmarkwealthmgmt.com/articles/the-evolution-of-sp-500-top-10-companies-how-tech-giants-captured-nearly-40-of-the-index-by-2026/
- The yen carry trade, August 2024:
  https://www.advisorperspectives.com/commentaries/2024/08/12/understanding-yen-carry-trade-impact-world-markets
- TTF's record, 2022:
  https://www.spglobal.com/energy/en/news-research/latest-news/natural-gas/081622-dutch-ttf-prices-hit-all-time-high-with-no-signs-of-slowing
