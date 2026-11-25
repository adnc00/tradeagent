# Onaylı Trade Ajanı — Plan (2026-11-20)

Amaç: Kullanıcının seçtiği piyasalarda ICT/SMC kurulumlarını tarayan, risk kurallarına göre **emir taslağı** üreten ve emri **yalnızca kullanıcının terminalde açık onayıyla** gönderen Python ajanı. Bilgi tabanı: `~/.claude/skills/trading-smc-ict/`.

## Değişmez güvenlik kuralları
1. Varsayılan mod `paper`. `live` için hem `config.yaml`'da `mode: live` hem de komut satırında `--live` gerekir.
2. Her emir onay ekranından geçer; kullanıcı tam olarak `ONAY` yazmazsa emir gitmez. Otomatik (onaysız) emir yolu yoktur.
3. Risk motoru reddederse emir onaya bile gelmez: stop yok, risk% > limit, günlük kayıp limiti dolmuş, toplam açık risk > limit, likidasyon stop'a çok yakın, enstrüman izin listesinde değil.
4. API anahtarları yalnızca ortam değişkenlerinden / `.env`'den okunur; loglanmaz, yazdırılmaz. Çekim yetkisi olmayan anahtar kullanılmalı (README'de anlatılır).
5. `kill` komutu: tüm açık emirleri iptal eder, pozisyonları kapatır (onaylı).
6. Her karar ve emir `journal.sqlite`'a yazılır.

## Piyasa / bağlayıcı kapsamı
| Piyasa | Bağlayıcı | Veri | Paper | Canlı emir |
|---|---|---|---|---|
| Binance spot + USDT-M futures | ccxt `binance`, `binanceusdm` | ✔ | ✔ yerel + resmi testnet | ✔ |
| BtcTurk (spot, TRY) | ccxt `btcturk` | ✔ | ✔ yerel | ✔ |
| Kraken, Coinbase (ABD/AB kripto) | ccxt | ✔ | ✔ yerel | ✔ |
| ABD hisse (NYSE/Nasdaq) | Alpaca (`alpaca-py`) | ✔ | ✔ Alpaca paper hesabı | ✔ |
| BIST hisse | yfinance (`.IS`) | ✔ (gecikmeli) | ✔ yerel | ✘ — kurumların genel API'si yok; Faz 2: aracı kurum API'si (ör. Deniz Yatırım AlgoLab) veya Matriks IQ |
| Binance TR | — | — | — | ✘ ccxt desteklemiyor; Faz 2: resmi Binance TR Open API ile doğrudan istemci (dokümanı doğrulandıktan sonra) |

## Modüller (`tradeagent/`)
- `models.py` — `Candle`, `Setup`, `OrderTicket` (symbol, side, entry, stop, targets, qty, leverage, reason), `RiskLimits`.
- `risk.py` — pozisyon büyüklüğü, likidasyon tahmini, R:R, ön-işlem kontrolleri, günlük/haftalık kayıp ve toplam açık risk.
- `ict.py` — swing tepe/dip, FVG (BISI/SIBI + CE), EQH/EQL likidite, PDH/PDL, displacement'lı MSS, premium/discount, daily bias, killzone (NY saati).
- `scanner.py` — mumlardan kurulum adayı → `Setup` → risk motoru → `OrderTicket`.
- `brokers/` — `base.py` (arayüz), `paper.py`, `ccxt_broker.py`, `alpaca_broker.py`, `data.py` (yfinance/ccxt OHLCV).
- `journal.py` — SQLite günlük, R bazlı istatistik (beklenti, kazanma oranı, max DD).
- `cli.py` — `scan`, `plan`, `execute <ticket_id>` (onay ekranı), `positions`, `kill`, `stats`.

## Test (TDD)
pytest; ağ gerektiren testler `-m network` ile ayrı. Risk motoru ve ICT dedektörleri sentetik mumlarla birim testli.
