# tradeagent: onaylı ICT/SMC trade ajanı

[![testler](https://github.com/adnc00/tradeagent/actions/workflows/tests.yml/badge.svg)](https://github.com/adnc00/tradeagent/actions/workflows/tests.yml)
[![lisans: MIT](https://img.shields.io/badge/lisans-MIT-blue.svg)](LICENSE)

`tradeagent`, seçtiğiniz piyasalarda ICT/SMC kurulumlarını tarar ve risk kurallarına göre **emir taslağı (bilet)** üretir. Gerçek parayla emri **yalnızca siz terminalde tam olarak `ONAY` yazarsanız** gönderir. `watch` komutu 7/24 açık kalır, belirli aralıklarla uyanıp tarar ve her yeni bileti size bildirir (Telegram + Windows bildirimi): ne yapılacağı, risk tutarı ve yüzdesi, hedefte kazanç, beklenen değer. Onaysız (otomatik) emir **yalnızca demo/paper hesapta** ve yalnızca `watch --demo-auto` ile mümkündür; gerçek para hesabında bu mod açılmaz.

| Klasör / dosya | Görevi |
|---|---|
| `tradeagent/cli.py` | Komutlar: `scan`, `plan`, `execute`, `positions`, `kill`, `stats`, `risk-calc`, `watch`, `panel`, `check`, `notify-test`, `telegram-chatid` |
| `tradeagent/notify.py` | Bildirim metni ve kanallar: konsol, Telegram, Windows bildirimi, panel kaydı |
| `tradeagent/universe.py` | `--universe`: borsadaki tüm pariteler, hacme göre |
| `tradeagent/panel.py`, `panel.html` | Salt okunur canlı panel (yerel web sunucusu) |
| `docs/` | Telegram ve Binance Demo Trading kurulum rehberleri |
| `tradeagent/ict.py` | ICT dedektörleri: swing, FVG, likidite, MSS, daily bias, killzone |
| `tradeagent/scanner.py` | Mumlardan kurulum → risk motoru → bilet |
| `tradeagent/risk.py` | Pozisyon büyüklüğü, likidasyon, ön-işlem kontrolleri |
| `tradeagent/journal.py` | SQLite günlük, bildirim kaydı, R istatistikleri |
| `tradeagent/brokers/` | `paper.py` (yerel), `ccxt_broker.py` (Binance futures), `alpaca_broker.py` (ABD hisse), `data.py` (veri) |
| `scripts/install-watch-task.ps1` | `watch`'u Windows Görev Zamanlayıcı'ya ekler (oturum açınca başlar) |
| `config.example.yaml`, `.env.example` | Ayar ve anahtar şablonları (gerçekleri GitHub'a gitmez) |
| `tests/` | pytest; ağ gerektirmeyen testler `-m "not network"` |

> **Yatırım tavsiyesi değildir.** Tarayıcıdaki ICT kuralları (likidite süpürmesi, MSS, FVG, premium/discount, killzone) kural tabanlı **sezgisel** tanımlardır. Grafikte belirli bir yapıyı tarif ederler, fiyatı tahmin etmezler ve kâr garantisi vermezler. Bütün kararlar ve sorumluluk kullanıcıya aittir. Önce paper modda en az 30-50, tercihen 100'den fazla işlemle sistemi sınayın.

## Güvenlik kuralları (değiştirilemez)

1. Varsayılan mod `paper`, yani yerel simülasyon. Canlı (gerçek veya uzak) broker **ancak iki koşul birlikte sağlanırsa** kurulur: `config.yaml` içinde `mode: live` **ve** komutta `--live`. Biri eksikse, API anahtarları tanımlı olsa bile paper kullanılır.
2. `execute` fiyatları ve miktarı borsaya gidecekleri hâliyle yuvarlar (stop ve hedefler riski azaltan yöne). Ardından miktarı emri gönderecek hesaba göre yeniden hesaplar ve risk motorunu güncel hesap durumuyla çalıştırır. Bileti eksiksiz gösterir: sembol, yön, miktar, pozisyon değeri, giriş, stop, hedefler, risk tutarı ve yüzdesi, komisyon dahil R:R, kaldıraç, likidasyon, broker ve mod. Emir yalnızca tam olarak `ONAY` yazılırsa gider. `onay`, `ONAY ` (sonda boşluk), `evet`, boş satır gibi başka her girdi işlemi iptal eder.
3. **Bayat bilet koruması**: güncel fiyat stop'u geçmişse, hedef 1'e zaten ulaşmışsa, girişi stop yönünde `execution.ticket_max_deviation_pct` (varsayılan %0.5) fazla geçmişse ya da bilet `execution.ticket_max_age_minutes` (varsayılan 60 dk) dakikadan eskiyse bilet onaya sunulmaz. Fiyatın hedef yönünde girişten uzak olması sorun değildir: limit giriş geri çekilmeyi bekler. **Gönderilmiş ama dolmamış giriş**, `execution.entry_expiry_minutes` (varsayılan 120 dk) içinde dolmazsa ya da fiyat girişe gelmeden hedef 1'e ulaşırsa `watch` tarafından iptal edilir (koruyucu stop/TP emirleriyle birlikte; giriş iptal sırasında dolduysa koruma yerinde bırakılır). Bu kontrol ve risk motoru, `ONAY` yazıldıktan hemen sonra da gönderilecek biletin kendisi üzerinde bir kez daha çalışır.
4. **Canlı onay yalnızca etkileşimli terminalden (TTY)** alınır. Girdi bir borudan, dosyadan ya da başka bir programdan (betik, yapay zekâ ajanı) geliyorsa canlı `execute` ve `kill` hiçbir şey göndermeden durur. Paper modda borudan gelen `ONAY` kabul edilir (demo ve testler için).
5. Gerçek para için onayı atlatan bir bayrak, ortam değişkeni veya ayar **yoktur**. Tek istisna `watch --demo-auto`'dur ve yalnızca para hareket etmeyen hesaplarda çalışır: yerel paper, `crypto.testnet: true` (Binance Demo Trading) ve `alpaca.paper: true`. Hedeflerden biri gerçek para hesabıysa komut başlamadan reddeder; gönderim anında da broker'ın demo olduğu ayrıca kontrol edilir. Testler bunu kaynak kodu tarayarak ve senaryolarla doğrular. `kill` komutu da `ONAY` ister.
6. **Çift gönderim yok**: `ONAY`'dan sonra bilet tek bir SQLite işlemiyle `submitting` durumuna alınır; aynı bileti başka bir işlem gönderemez. Gönderim sonucu doğrulanamazsa bilet `unknown` olur, açık risk sayılmaya devam eder ve büyük bir uyarı basılır. Gönderimden sonra journal yazılamazsa bilet `new`'e dönmez.
7. Risk motoru reddederse bilet onay ekranına bile gelmez. Ret nedenleri şunlardır: stop yok, stop yanlış tarafta, spot piyasada short, risk üst sınırın üstünde, gereken teminat özsermayeyi aşıyor, günlük veya haftalık kayıp limiti dolmuş, toplam açık risk sınırı aşılıyor, korelasyonlu grup riski sınırı aşılıyor, likidasyon stop mesafesinin iki katından yakın ya da girişin yanlış tarafında (yalnızca futures), sembol izin listesinde değil, kaldıraç sınırın üstünde, komisyon oranı geçersiz, R:R (komisyon dahil) düşük.
8. API anahtarları yalnızca ortam değişkenlerinden veya `.env` dosyasından okunur. Hiçbir yere loglanmaz ve ekrana yazdırılmaz.
9. Kurulumlar, biletler, emirler, durum değişiklikleri ve notlar `journal.sqlite` dosyasına yazılır. **Gerçekleşmeler (fill) ve R sonucu yalnızca paper modda otomatik journal'a girer**; canlı ve testnet biletlerinde borsadaki fill'ler çekilmez (bkz. sınırlamalar).
10. `kill` önce pozisyonları reduce-only piyasa emriyle kapatır, kapandıklarını doğrular, ancak ondan sonra kalan emirleri iptal eder. Kapatılamayan bir pozisyonun stop emirleri korunur; komut sıfırdan farklı bir kodla çıkar ve neyin açık kaldığını tek tek yazar.

## Kurulum

Python 3.14 gerekir.

```powershell
git clone https://github.com/adnc00/tradeagent.git
cd tradeagent
python -m pip install -e ".[dev]"            # bağımlılıklar: ccxt, alpaca-py, yfinance, pandas, pyyaml
Copy-Item config.example.yaml config.yaml    # ayarları düzenleyin (özellikle risk.allowlist)
Copy-Item .env.example .env                  # Telegram ve canlı/demo anahtarları için
python -m pytest -m "not network" -q         # ağ gerektirmeyen testler
```

Kurulumdan sonra `tradeagent` komutu kullanılabilir. Kurmadan çalıştırmak için `python -m tradeagent.cli ...` yazabilirsiniz.

Ayar dosyası önce bulunduğunuz klasördeki `config.yaml`, yoksa `~/trading-agent/config.yaml` olarak aranır. `data_dir: data` göreli yolu ayar dosyasının klasörüne göre çözülür; `.env` de ayar dosyasının yanında aranır. Başka bir dosya için `--config yol` verin. Bilinmeyen bir anahtar hata verir; böylece bir yazım hatası bir kuralı sessizce devre dışı bırakamaz. Risk varsayılanları: `risk_pct 0.5`, `max_risk_pct 1.0`, `daily_loss_pct 2.0`, `weekly_loss_pct 5.0`, `max_open_risk_pct 3.0`, `max_leverage 5`, `min_rr 2.0`.

## API anahtarı oluşturma (güvenli ayarlar)

Bu araç için açacağınız her anahtarda şunlara uyun:

- **Yalnızca işlem (trade) yetkisi** verin. **Para çekme (withdraw) yetkisi kesinlikle kapalı** olsun. Transfer ve evrensel transfer yetkilerini de kapatın.
- **IP beyaz listesi** tanımlayın, yani anahtarı yalnızca bu bilgisayarın sabit IP adresi kullanabilsin.
- Mümkünse ayrı bir **alt hesap (sub-account)** açın ve bu hesapta yalnızca kaybetmeyi göze aldığınız tutarı tutun. Spot `kill` komutu, izin listesindeki sembollerin coin bakiyesini piyasa emriyle satar.
- Anahtarları yalnızca `.env` dosyasına yazın. Koda, sohbete veya ekran görüntüsüne koymayın ve düzenli olarak yenileyin (rotasyon).
- Ortam değişkeni adları borsa kimliğinden türetilir: `<BORSA>_API_KEY` ve `<BORSA>_API_SECRET`. Örnekler: `BINANCE_API_KEY`, `BINANCEUSDM_API_KEY`, `BTCTURK_API_KEY`. Alpaca için `ALPACA_API_KEY` ve `ALPACA_API_SECRET` kullanılır.

### Binance test ortamları

- **Emir denemesi yalnızca USDⓈ-M futures demo ortamında mümkündür.** Binance'ın eski futures testnet'i ccxt 4.5.x ile artık kullanılamıyor (ccxt bu ortamdaki özel çağrıları reddediyor). Yerine **Demo Trading** kullanılır: <https://demo.binance.com/> adresinde demo hesabı açın ve anahtarı <https://demo.binance.com/en/my/settings/api-management> sayfasından oluşturun. Ayarlar: `crypto.exchange: binanceusdm`, `crypto.testnet: true`. Futures sembolleri ccxt biçiminde yazılır, örneğin `BTC/USDT:USDT`. Hesap tek yönlü (one-way) modda olmalıdır; hedge modu reddedilir.
- **Spot testnet** (<https://testnet.binance.vision/>) bu araçla emir için kullanılamaz: canlı kripto emri yalnızca `binanceusdm` ile verilir (aşağıdaki "Spot canlı emir neden reddediliyor?" bölümü). Spot semboller tarama ve paper için kullanılabilir.
- Gerçek hesap anahtarları: <https://www.binance.com/en/my/settings/api-management>. Gerçek parayla işlem için `testnet: false`, `mode: live` ve `--live` üçü birden gerekir.

### Alpaca paper hesabı (ABD hisseleri)

<https://alpaca.markets> adresinden ücretsiz hesap açın. Panelde **Paper Trading** hesabına geçin ve oradan API anahtarı oluşturun. Paper anahtarları gerçek parayla işlem yapamaz. `alpaca.paper: true` varsayılandır; bu ayarla `mode: live` ve `--live` verilse bile emirler Alpaca'nın paper ortamına gider. Gerçek hesap için ayrıca `alpaca.paper: false` gerekir. Alpaca bracket emirleri tam hisse ister, bu yüzden miktar aşağı yuvarlanır.

## Piyasa kapsamı

| Piyasa | Veri | Paper | Canlı emir |
|---|---|---|---|
| Binance USDⓈ-M futures (`binanceusdm`) | ccxt | evet (yerel ve demo trading) | **evet**: önce reduce-only stop-market ve take-profit-market (bilet miktarıyla), sonra limit giriş (giriş hiçbir an stopsuz kalmaz) |
| Binance spot, BtcTurk, Kraken, Coinbase (spot) | ccxt | evet (yerel) | **hayır, reddedilir** (ayrıntı aşağıda) |
| ABD hisseleri | yfinance (tarama), Alpaca | evet (yerel ve Alpaca paper) | evet: Alpaca'nın kendi bracket emri |
| BIST (`.IS`) | yfinance (gecikmeli) | evet (yerel) | hayır |
| Binance TR | - | - | planlanıyor |

**Spot canlı emir neden reddediliyor?** Canlı kripto emri açıkça yalnızca `binanceusdm` ile verilir; başka her borsa kimliği reddedilir. Spot piyasada stop-satış emri ancak coin elde olduğunda verilebilir. Bu yüzden dolmamış bir limit girişini borsa tarafındaki bir stop ile koruma altına almak mümkün değil. Yüklü ccxt sürümünde (4.5.93) binance, btcturk, kraken ve coinbase için girişe bağlı stop/TP (`createOrderWithTakeProfitAndStopLoss`) ya da birleşik bir OTO/OTOCO emri yok; btcturk'ün `create_order` fonksiyonu yalnızca market ve limit emir tanıyor. Program korumasız giriş açmaz. Bu durumda Türkçe bir hata verir ve hiçbir emir göndermez. Spot semboller taranabilir ve paper'da işlenebilir. Canlı kripto için futures (`binanceusdm`) kullanın.

**BIST canlı emir neden yok?** Türk aracı kurumlarının herkese açık, belgelenmiş bir emir API'si yok. BIST şimdilik yalnızca yfinance verisi (gecikmeli) ve paper ile çalışır. Aracı kurum API'si (ör. kurumların algoritmik işlem arayüzleri) ileride değerlendirilecek.

**Binance TR:** planlanıyor. ccxt Binance TR'yi desteklemiyor; resmi Binance TR API'si için doğrudan bir istemci, dokümanı doğrulandıktan sonra eklenecek.

## Kullanım

```bash
# Tarama: kurulum varsa bilet üretir (veya risk motorunun ret nedenlerini gösterir)
tradeagent scan --symbols BTC/USDT,ETH/USDT,THYAO.IS,AAPL --tf 15m --htf 1d

# Bir kurulumun planı: anlatı, seviyeler, bilet veya ret nedenleri
tradeagent plan s-5d42285f

# Bileti onay ekranıyla gönder (paper)
tradeagent execute t-5d42285f

# Canlı (config.yaml'da mode: live olmalı)
tradeagent execute t-5d42285f --live

# Pozisyonlar ve açık emirler (paper; canlı hesap için --live)
tradeagent positions

# Acil durdurma: tüm emirleri iptal et, pozisyonları kapat (ONAY ister)
tradeagent kill
tradeagent kill --live

# R bazlı istatistik: kazanma oranı, ortalama R, beklenti, profit factor, maks. drawdown
tradeagent stats

# Pozisyon büyüklüğü ve likidasyon hesabı
tradeagent risk-calc --equity 10000 --risk 1 --entry 60000 --stop 59400 --leverage 5 --side long
```

Ağ olmadan uçtan uca paper demosu için sentetik veri kullanılır: tarama, bilet, iki kez `execute` (biri yanlış cevapla, biri `ONAY` ile), mum besleme, `positions` ve `stats`.

```bash
python examples/paper_demo.py
```

## 7/24 izleme ve bildirimler (`watch`)

`watch` her mum kapanışından ~20 sn sonra uyanır (varsayılan aralık `--tf` süresi, ör. 15 dk), tarar ve:

- **Yeni bilet** bulunca bildirim gönderir: yön, giriş/stop/hedef, miktar, pozisyon değeri, kaldıraç ve likidasyon, **risk tutarı ve özsermaye yüzdesi**, **hedefte kazanç** (tutar, yüzde ve R, komisyon dahil), başabaş kazanma oranı, **beklenen kazanç** (journal'da en az 30 kapanmış işlem varsa geçmiş beklentiden; yoksa "hesaplanamaz" der), kurulum gerekçesi, killzone, biletin geçerlilik saati (Türkiye saati) ve onay komutu.
- Siz bilgisayarda `tradeagent execute <bilet> [--live]` çalıştırıp son kontrol ekranında `ONAY` yazınca emir gider. Bilet `execution.ticket_max_age_minutes` (60 dk) sonra bayatlar.
- Bildirilen biletin girişi dolunca ve işlem kapanınca (R sonucu, K/Z, toplam istatistik) yine haber verir.
- Aynı bileti iki kez bildirmez (journal'daki `notifications` tablosu). Bir tarama hata verirse izleme durmaz; hatayı bir kez bildirir ve sonraki uyanışta yeniden dener.

**Hızlı başlangıç:** Binance Demo Trading'de tam otomatik kurulum için [docs/DEMO-HESAP.md](docs/DEMO-HESAP.md), telefona bildirim için [docs/TELEGRAM.md](docs/TELEGRAM.md).

### Tüm Binance coinleri (`--universe`)

`--symbols` yerine `--universe` verilirse borsadaki bütün pariteler 24 saatlik işlem hacmine göre sıralanır ve en hacimli `universe.top` tanesi (varsayılan 100) taranır. `crypto.exchange: binanceusdm` ise USDT-M sürekli vadeliler (`BTC/USDT:USDT` …), `binance` ise spot pariteler gelir. Stabil coinler (USDC, FDUSD …), pasif pariteler, `universe.min_quote_volume` (10 milyon USDT) altı hacimliler ve `universe.exclude` listesi elenir. Bilet yalnızca `risk.allowlist`'e uyan sembollerde üretilir; allowlist desen kabul eder: `'*/USDT:USDT'` tüm USDT-M vadelileri kapsar. Liste her uyanışta yenilenir. 100 parite yaklaşık 1-1,5 dakikada taranır.

Tüm USDT kriptolar aynı korelasyon grubunda sayılır: aynı anda açık olabilecek toplam kripto riski `risk.max_risk_pct` (%1) ile sınırlıdır. Yani çok sayıda coinde aynı anda fırsat çıksa bile ajan ancak birkaçına girer; piyasa birlikte düşerken çok sayıda pozisyonun birden zarar etmesi böyle önlenir.

```powershell
# Bildirim modu: hiçbir emir göndermez, yalnızca haber verir
tradeagent watch --symbols BTC/USDT,ETH/USDT --tf 15m
tradeagent watch --universe                  # en hacimli 100 parite
tradeagent watch --universe --top 0          # hepsi (~400 parite, tarama uzar)

# Gerçek/demo hesabın özsermayesine göre boyutlandırılmış bildirim (onay komutu --live ile gelir)
tradeagent watch --symbols BTC/USDT:USDT --live

# DEMO hesapta tam otomatik: uygun bilet onaysız gönderilir, sonuç bildirilir
tradeagent watch --universe --live --demo-auto                              # Binance Demo Trading, tüm coinler
tradeagent watch --symbols BTC/USDT:USDT,ETH/USDT:USDT --live --demo-auto   # Binance Demo Trading
tradeagent watch --symbols BTC/USDT,ETH/USDT --demo-auto                    # yerel paper

# Tek tarama (deneme veya zamanlayıcı)
tradeagent watch --symbols BTC/USDT --once
```

`--demo-auto` tüm risk motoru ve bayat bilet kontrollerinden geçer; yalnızca `ONAY` sorusu yoktur. Demo hesap için `config.yaml`: `mode: live`, `crypto.exchange: binanceusdm`, `crypto.testnet: true`; `.env`: `BINANCEUSDM_API_KEY` / `BINANCEUSDM_API_SECRET` (Demo Trading anahtarları).

### Telegram bildirimi (telefona)

Adım adım rehber: [docs/TELEGRAM.md](docs/TELEGRAM.md). Kısaca: @BotFather'da `/newbot` ile token alın ve `.env` dosyasına `TELEGRAM_BOT_TOKEN` olarak yazın. Bota bir mesaj gönderip `tradeagent telegram-chatid` komutuyla chat id'nizi bulun ve `TELEGRAM_CHAT_ID` olarak yazın. Son olarak `tradeagent notify-test` ile deneme mesajı gönderin.

Bot yalnızca **mesaj gönderir**; Telegram'dan gelen hiçbir mesaj emir onaylayamaz. Token'ı kimseyle paylaşmayın. Windows masaüstü bildirimi kendiliğinden açıktır; kapatmak için `--no-desktop`.

### Bilgisayar açılınca kendiliğinden başlasın (Görev Zamanlayıcı)

```powershell
# Bildirim modu
powershell -ExecutionPolicy Bypass -File .\scripts\install-watch-task.ps1 -Symbols "BTC/USDT,ETH/USDT"

# Demo hesapta tam otomatik, tüm coinler (en hacimli 100)
powershell -ExecutionPolicy Bypass -File .\scripts\install-watch-task.ps1 -Universe -Live -DemoAuto

# Kaldır
powershell -ExecutionPolicy Bypass -File .\scripts\install-watch-task.ps1 -Remove
```

Görev oturum açıldığında başlar, çökerse 1 dakika sonra yeniden başlar ve çıktıyı `data/watch.log` dosyasına yazar. Bilgisayar **uykuya geçerse izleme durur**: Ayarlar → Sistem → Güç bölümünden prize takılıyken uykuyu "Hiçbir zaman" yapın. 7/24 kesintisiz çalışma için sabit IP'li bir VPS daha güvenilirdir (Binance API anahtarına o IP'yi beyaz liste olarak ekleyin).

### Canlı panel (`panel`)

```powershell
tradeagent panel            # tarayıcıda http://127.0.0.1:8765 açılır
```

Tek ekranda: izlemenin çalışıp çalışmadığı (son ve sonraki tarama, son hata), mod ve hesap, performans (toplam R, beklenti, kazanma oranı, profit factor, en büyük düşüş, özsermaye), kümülatif R eğrisi, **risk tavanı** (açık risk ve bugünkü kayıp, sınırlarına göre), açık pozisyonlar ve bekleyen emirler, bildirim akışı ve son biletler. 10 saniyede bir yenilenir.

Panel **salt okunurdur**: yalnızca bu bilgisayardan (127.0.0.1) açılır, journal'ı salt okunur modda okur, borsaya bağlanmaz ve hiçbir emir veremez. Dış kaynak (yazı tipi, betik) yüklemez.

### Kurulum kontrolü ve yardımcı komutlar

```powershell
tradeagent check            # ayarlar, anahtarlar (değerleri gösterilmez), Telegram, yapılacaklar listesi
tradeagent check --live     # ayrıca demo/canlı hesaba bağlanıp bakiyeyi okur
tradeagent notify-test      # tüm kanallara örnek bildirim
tradeagent telegram-chatid  # botunuza yazan sohbetlerin chat id'leri
```

### Tarayıcı kuralı (long; short bunun aynasıdır)

1. Günlük bias bullish olmalı. Bunun için önceki gün ya PDH'nin üstünde kapanmış olmalı ya da PDL'yi süpürüp altında kapanmamış olmalı.
2. Sell-side süpürme: fiyat onaylı bir swing low'un veya PDL'nin altına iner ve `reclaim_bars` mum içinde seviyenin üstünde kapanır.
3. Ardından displacement'lı bir bullish MSS gelir: son swing high'ın üstünde kapanış, gövde en az `displacement_k` x ATR.
4. Displacement bacağında bir bullish FVG oluşur ve doldurulmamış olarak kalır.
5. Giriş (FVG CE ya da `entry: top`) dealing range'in discount yarısında olmalı. Dealing range, süpürme ucundan bacağın tepesine kadar olan aralıktır.
6. Stop, süpürme ucunun `stop_buffer_pct` ötesine konur. Hedef 1 en yakın alınmamış buy-side likiditedir (swing high, EQH veya PDH); hedef 2 bir sonrakidir. Broker'a hedef 1 take-profit olarak gönderilir.
7. İsteğe bağlı killzone filtresi (New York saatiyle): asia 20-00, london 02-05, ny_am 07-10, london_close 10-12, ny_pm 13:30-16.

Tarama, mumu kapanmış veriyle yapılır; son mum oluşmaya devam ettiği için yok sayılır. Bilet, risk motoru hiçbir ret nedeni bulmazsa üretilir. `execute` anında miktar, emri gönderecek hesabın özsermayesine göre yeniden hesaplanır.

## Bilinen sınırlamalar

- Futures koruyucu emirleri reduce-only'dir (yalnızca pozisyonu küçültür, asla yeni pozisyon açmaz). Binance `closePosition` emirlerini pozisyon yokken reddettiği için (hata -4509) bu yol seçildi. Aynı sembolde aynı anda tek bilet çalışır; o sembolde açık pozisyon veya emir varken yeni bilet reddedilir. Hedef veya stop dolduktan sonra kalan bacak, bir sonraki `positions`/`execute`/`scan` çalıştırmasında bilet kimliğiyle bulunup iptal edilir.
- Canlı connector'lar yalnızca sahte borsa nesneleriyle test edildi; demo ortamında önce küçük miktarla deneyin.
- Kripto için günlük bias ve PDH/PDL, borsanın UTC günlük mumlarıyla hesaplanır (New York gece yarısı değil).

- Günlük ve haftalık kayıp, `journal.sqlite` içindeki özsermaye anlık görüntülerine göre ölçülür. Dönemin başlangıç değeri, araç o dönemde ilk çalıştırıldığında alınır. Araç çalıştırılmadan önce oluşan kayıplar görülmez; para yatırma ve çekme işlemleri de kâr veya zarar gibi görünür.
- Açık risk, journal'da `submitted` veya `open` durumundaki biletlerden hesaplanır. Canlı modda borsada pozisyonu ya da açık emri kalmayan biletler kapanmış sayılır; bunların R sonucu journal'a otomatik çekilmez.
- Paper hesabı tek bir sayısal özsermaye kullanır ve kur çevrimi yapmaz (USDT, TRY ve USD aynı sayı üzerinden boyutlandırılır).
- Likidasyon fiyatı izole marjinli lineer perp için yaklaşık bir hesaptır. `risk.mmr` değerini borsanın kademesine göre ayarlayın.
- yfinance verisi gecikmelidir. 15 dakikalık veri yalnızca son 60 gün için alınabilir.
- Strateji geçmiş veriyle (backtest) test edilmedi. "Beklenen kazanç" yalnızca sizin journal'ınızdaki kapanmış işlemlerden hesaplanır; gelecek sonuç garantisi değildir.
- `watch --demo-auto` demo ortamında gerçek bir borsaya karşı henüz denenmedi; ilk günlerde `data/watch.log` ve bildirimleri yakından izleyin.

## Lisans

[MIT](LICENSE). Yazılım "olduğu gibi" sunulur; finansal kayıplardan yazar sorumlu değildir. Güvenlik bildirimi için [SECURITY.md](SECURITY.md), sürüm notları için [CHANGELOG.md](CHANGELOG.md).
