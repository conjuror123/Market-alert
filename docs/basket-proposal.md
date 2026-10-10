# A new basket: proposal

A working proposal, under discussion with the user since 2026-10-10. Nothing here is in
`config/basket.yaml` yet. Before it goes in: a source for each new instrument, then the
whole basket's measured before/after for the user's go, and one cold rebuild.

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
3. **Fear gauges and havens**: the dollar, yen, franc, Treasuries and gold in a crisis -
   though not always (March 2026's energy shock: the dollar alone).
4. **The world by size**: the US about 64% of world stocks, then Japan 5%, Taiwan, the UK
   and Canada about 3% each (MSCI ACWI, 2026-08-31).
5. **Each market in its own hours**: a price that trades round the clock, or in Tokyo's
   or London's day, is watched there, not through a New York fund.

## The basket (version 3, the user's answers of 2026-10-10)

| group | what it tells | instruments |
|---|---|---|
| stock indices, each in its own hours | how each region feels, as it happens | ASX 200, Nikkei 225, KOSPI, TAIEX, CSI 300, Hang Seng, Nifty 50; Euro Stoxx 50, FTSE 100, SMI; BIST 100, Tadawul, JSE Top 40; S&P 500 (SPY), Russell 2000 (IWM), TSX, Bovespa, IPC (18; 16 new) |
| US sectors and industries | what leads the biggest market | tech, financials, energy, health care (XLK, XLF, XLE, XLV); semiconductors, regional banks, biotech, homebuilders, transports (SMH, KRE, XBI, ITB, IYT) (9) |
| the price of money | the Fed; inflation; trust in debt | 2-, 10- and 30-year Treasury futures, read by contract (new); TIP (4) |
| credit | do lenders still trust borrowers? | LQD, HYG, EMB (3) |
| currencies | where money runs | EUR/USD, USD/JPY, GBP/USD, AUD/USD, USD/CAD, USD/CHF, USD/CNH (7) |
| energy | inflation and war | Brent futures, read by contract (new); UNG; Europe's gas, TTF (new) (3) |
| metals | fear (gold) and industry (copper) | gold, silver and platinum round the clock, XAU/USD, XAG/USD, XPT/USD (new); the LME's copper (new), tin, nickel, aluminium; REMX (8) |
| food | weather, harvests, prices | corn, wheat, soybeans and sugar futures (new, for CORN, WEAT, SOYB, CANE); coffee, cocoa, cotton, live cattle (8) |
| crypto | risk appetite, every hour | Bitcoin, Ethereum, Solana, XRP (4) |

64 instruments: 35 of today's, and 29 new. VIX stays as it is today, the daily line on the
weekly note, not an hourly instrument.

The 137 of today's that go:

| block | out |
|---|---|
| equity (50) | XLY, XLP, XLI, XLB, XLU, XLRE, XLC, QQQ, DIA, RSP, MDY, XRT, XME, XPH, IHI, VDC, VHT, VPU, SOXX, IGV, FDN, CIBR, SKYY; the country and region funds EFA, EEM, EWJ, EWG, EWU, EWQ, EWC, EWA, EWL, EWN, EZU, FXI, EWZ, EWW, INDA, EPI, EWY, EWT, EZA, TUR; real estate VNQ, IYR, RWR, SCHH, REM, VNQI, RWX |
| rates (19) | SHY, IEF, TLT, IEI, TLH, MBB, GOVT, SCHO, VGIT, VGLT, SPTL, BWX, VTIP, SCHP, STIP, VMBS, SPMB, LMBS, JMBS |
| credit (23) | EMLC, JNK, BKLN, PFF, VCIT, VCSH, IGIB, SPIB, USIG, QLTA, GIGB, SLQD, SHYG, USHY, ANGL, SRLN, FALN, VWOB, PCY, EBND, LEMB, EMHY, CEMB |
| energy (6) | USO, BNO, UGA, DBO, DBE, UNL |
| precious metals (8) | GLD, SLV, PPLT, PALL, IAU, SGOL, SIVR, GLTR |
| industrial metals (4) | DBB, CPER, LIT, SLX |
| agriculture (5) | CORN, WEAT, SOYB, CANE, DBA |
| pairs (10) | USD/NOK, USD/MXN, USD/BRL, USD/INR, USD/KRW, USD/ZAR, USD/TRY, NZD/USD, USD/SEK, USD/PLN |
| coins (12) | LTC, BCH, LINK, ADA, DOGE, AVAX, DOT, POL, UNI, ATOM, FIL, AAVE |

## The user's answers

Each was the proposal's own critique, measured where it could be.

**Round 1** (on version 1, 2026-10-10):

| | change | answer | evidence |
|---|---|---|---|
| 1 | one oil, in its own hours | yes: Brent futures, read by contract | USO and BNO flagged in the same hour 18 of 22 times (three years to 2026-10-05); 4 of Wallstreetcn Brent's 8 moves of 6 sd or more, 2026-04-22 to 10-09, were Monday-morning gaps after a weekend |
| 2 | copper from the LME | yes: LME copper for CPER; DBB and LIT go; tin, nickel, aluminium, platinum, rare earths stay | LME copper's two big moves in that window were in London's morning |
| 3 | gold and silver | round the clock, XAU/USD and XAG/USD (the user's; the proposal had kept the funds) | gold's four big moves in that window were in New York's hours |
| 4 | drop QQQ | yes | the S&P 500 and Nasdaq 100: hourly correlation 0.95, 33 of 53 big hours together (Yahoo, 2023-11 to 2026-10); XLK flagged in each of QQQ's 8 hours |
| 5 | one of Euro Stoxx 50 and DAX | yes: Euro Stoxx 50 | hourly correlation 0.93 (the CAC 0.92); the FTSE its own (0.75) |
| 6 | sectors 11 → 4 | yes: tech, financials, energy, health care | of 20 sector pushes in three years, 16 came with SPY, QQQ or IWM flagging too |
| 7 | drop the dollar index | yes | with EUR/USD: correlation −0.92, 76 of 139 big hours together |
| 8 | the giants (Nvidia, Apple, ...) | no | - |
| 9 | drop LIT | yes | battery makers, already in tech; not measured |
| 10 | drop BKLN, keep HYG | yes | BKLN 23 flags in three years to HYG's 12, together 7; its lone flags not checked |
| 11 | Asian indices ring at night; no quiet hours | fine as it is | - |

**Round 2** (on version 2, 2026-10-10; none of it measured):

| | change | answer |
|---|---|---|
| 1 | Treasuries as futures, in their own hours | yes: 2-, 10- and 30-year futures; TIP stays a fund |
| 2 | platinum round the clock | yes: XPT/USD for PPLT |
| 3 | grains and sugar as futures, like coffee | yes; sources later |
| 4 | drop gasoline (UGA) | yes; UNG stays |
| 5 | drop EMLC | yes, EMB stays; and the pairs cut to seven: EUR, JPY, GBP, AUD, CAD, CHF, CNH (the user's) |
| 6 | VIX and MOVE | MOVE dropped; VIX stays as today |
| 7 | the pairs as the loudest group | no longer, at seven |
| 8 | iron ore | no |
| 9 | Saudi Arabia's index | yes, with the index list above: Tadawul, JSE Top 40 and SMI added (the user's) |

Version 1's trim alone (the 86 of today's that it kept, nothing added) moved the year to
2026-10-05 from 247 pushes to 135. Versions 2 and 3 are not measured.

## Still to do

- **Sources** for the 29 new instruments, each with voters (to be worked out with the
  user). Seen on 2026-10-10: Yahoo serves about three years of hourly bars for the indices
  it was asked for (from late 2023) and TTF from 2024-05, without volume, each on its own
  minute (:00, :30, Nifty :45). `exchange_calendars`, which builds the NYSE and B3 tables,
  has all 16 exchanges abroad.
- **The measured before/after** of version 3, for the user's go.
- **The standing risk**: undocumented endpoints carry most of it, as they do today.

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
