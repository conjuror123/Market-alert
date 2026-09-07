"""Tremor - how unusual was this move, for this instrument (spec 5.1).

A cluster detector for synchronous basket anomalies (SI-Index) and a module for
single idiosyncratic moves (SAED). Built alongside price_monitor/ and will
replace its detection layer entirely in phase 8; until then the existing hourly
monitoring keeps running and sending alerts, while Tremor accumulates data and
metrics without sending anything.

Reused from price_monitor: the source clients (coinbase, twelvedata), Telegram,
the economic calendar, news and the LLM. The mathematics, the storage and the
event model are here.
"""
