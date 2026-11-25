# Değişiklik günlüğü

Biçim [Keep a Changelog](https://keepachangelog.com/tr-TR/1.1.0/), sürümler [SemVer](https://semver.org/lang/tr/).

## [0.8.0] - 2026-11-26

### Eklendi
- Panelde bilet düzenleme (giriş, stop, hedef, risk %, kaldıraç) ve **Yeni işlem** formu (kendi long/short işleminiz; fiyatı getir, önizle, `ONAY`). Düzenlenen ve elle açılan biletler taramada üzerine yazılmaz. Rehber: `docs/ELLE-ISLEM.md`.
- `risk.max_group_risk_pct`: aynı yöndeki korelasyonlu grubun toplam açık riski (0 = eski davranış, grup tek pozisyon).
- `risk.margin_per_trade_pct`: futures'ta kaldıraç, bir işlemin teminatı bakiyenin bu payını aşmayacak kadar otomatik yükselir (en fazla `max_leverage`).
- `--tf` birden çok zaman dilimi alır (`5m,15m`); izleme en kısa zaman diliminde uyanır.
- Binance bir coinin kaldıraç üst sınırını aşan isteği reddederse (-4028) kaldıraç o coinin sınırına indirilip bir kez daha denenir (risk aynı kalır, likidasyon uzaklaşır).

## [0.7.0] - 2026-11-26

### Eklendi
- `goal` ayarı (küçük hesap provası): demo/canlı hesap `start_capital` + `since`'ten beri kapanan işlemlerin net K/Z'si kadar sanal özsermayeyle boyutlanır ve risk sınırları buna göre ölçülür (gerçek bakiyeyi aşmaz). `watch` sanal özsermayeyi de kaydeder.
- Panelde **Hedef** kartı: sanal özsermaye, ilerleme, net K/Z, işlem sayısı, beklenti ve hedefe tahmini işlem sayısı.

### Değişti
- Örnek ayarda komisyon %0,05 (Binance USDⓈ-M taker); önceki %0,1 R'yi olduğundan kötü gösteriyordu.

## [0.6.0] - 2026-11-26

### Eklendi
- `watch` her turda demo/canlı hesabın özsermayesini kaydeder (günlük/haftalık kayıp sınırları gün başından ölçülür); panel, bağlı olduğu hesabın özsermayesini gösterir.
- Binance USDⓈ-M (demo/canlı) işlem sonuçları: kapanan biletin net K/Z'si, R'si, çıkış fiyatı ve nedeni borsanın işlem geçmişinden journal'a yazılır; hiç dolmamış girişler "iptal" olur. `watch`, sonucu bilinmeyen kapanmış biletleri her turda doldurur.

### Düzeltildi
- Panel, eski bir panel işleminin yeni sayfayı sunduğunu (sürüm uyuşmazlığı) fark eder ve "paneli yeniden başlatın" der; çizim hatası bağlantı hatası gibi gösterilmez.

## [0.5.0] - 2026-11-25

### Eklendi
- Panelden kontrol: "Onay bekleyenler" kuyruğu (İncele ve onayla: tam bilet önizlemesi, `ONAY` yazarak gönderim; Reddet), açık emirlerde "Girişi iptal et", üst çubukta "Acil durdur" (`ONAY`). Sunucu tarafında terminalle aynı kontroller; gizli sayfa anahtarı, Origin ve JSON zorunlu; işlemler sırayla.
- `panel --live`: panel işlemleri canlı/demo hesaba gider (ayrıca `mode: live`).

### Düzeltildi
- Paneldeki "Onay bekliyor" yalnızca süresi dolmamış biletlerde; eskiler "Süresi doldu". Kuyrukta R:R komisyon dahil.

## [0.4.0] - 2026-11-25

### Değişti
- Bayat bilet koruması tek yönlü: fiyat hedef yönünde girişten uzaksa limit giriş bekleyebilir; yalnızca girişin stop yönünde `ticket_max_deviation_pct`'den fazla geçilmesi bileti bayatlatır. Fiyat hedef 1'e zaten ulaştıysa bilet reddedilir.

### Eklendi
- Dolmayan girişlerin süresi: `execution.entry_expiry_minutes` (120 dk) içinde dolmayan ya da fiyat önce hedef 1'e ulaştığı için anlamını yitiren girişler `watch` tarafından iptal edilir ve bildirilir. Giriş iptal sırasında dolmuşsa koruyucu emirler yerinde bırakılır.
- Futures stop ve take-profit emirleri artık reduce-only ve bilet miktarıyla gönderilir: Binance, `closePosition` emirlerini açık pozisyon yokken reddediyordu (-4509), bu yüzden demo hesapta hiçbir bilet gönderilemiyordu (giriş de gönderilmedi; korumasız pozisyon oluşmadı).
- `universe.coins_only` (varsayılan açık): Binance'ın hisse/emtia/endeks vadelileri taranmaz (TradFi emirleri -4411 ile reddediliyordu).
- Demo/canlı hesapta dolan giriş tespit edilir ve "giriş doldu" bildirilir; başarılı otomatik gönderim, önceki bir hata bildirimi olsa da her zaman bildirilir.
- Gönderilmekte olan (`submitting`) bilet eşitleme sırasında kapatılmaz (yarım yerleştirilmiş stop korunur).
- Saat farkı telafisi (Binance -1021), `check --live` saat kontrolü, Telegram hatalarında API açıklaması, satır tamponlu kayıt.

## [0.3.0] - 2026-11-25

### Eklendi
- `--universe` (`scan`, `watch`): Binance'taki tüm pariteleri 24s hacme göre tarar; `universe` ayar bölümü (`quote`, `top`, `min_quote_volume`, `exclude`), `--top`.
- `risk.allowlist` desen kabul eder (ör. `'*/USDT:USDT'`).
- `panel`: salt okunur yerel canlı panel (izleme durumu, performans, R eğrisi, risk tavanı, pozisyonlar, bildirimler, biletler).
- `check`, `notify-test`, `telegram-chatid` komutları.
- `docs/TELEGRAM.md` ve `docs/DEMO-HESAP.md` kurulum rehberleri.
- İzleme her turda `data/watch_status.json` durum dosyasını ve `data/alerts.jsonl` bildirim kaydını yazar.
- Görev Zamanlayıcı betiğine `-Universe` ve `-Top`.

## [0.2.0] - 2026-11-25

### Eklendi
- `watch` komutu: her mum kapanışında uyanıp tarar, yeni biletleri bildirir; giriş dolduğunda ve işlem kapandığında haber verir. Hata olursa durmaz.
- Bildirim içeriği: yön, seviyeler, miktar, kaldıraç/likidasyon, risk tutarı ve özsermaye yüzdesi, hedefte kazanç (komisyon dahil), başabaş kazanma oranı, journal'dan beklenen değer, geçerlilik saati ve onay komutu.
- Bildirim kanalları: konsol, Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), Windows masaüstü bildirimi (`--no-desktop` ile kapatılır).
- `watch --demo-auto`: yalnızca demo/paper hesaplarda onaysız gönderim; gerçek para hedefinde başlamadan reddeder, gönderim anında broker'ın demo olduğunu ayrıca kontrol eder.
- `scripts/install-watch-task.ps1`: Windows Görev Zamanlayıcı kurulumu (oturum açınca başlar, çökerse yeniden başlar).
- Journal'da `notifications` tablosu (aynı bilet iki kez bildirilmez).
- LICENSE (MIT), SECURITY.md, CHANGELOG.md, GitHub Actions test iş akışı.

### Değişti
- Ayar dosyası önce bulunduğunuz klasördeki `config.yaml` olarak aranır; örnek ayarda `data_dir: data` (proje klasörüne göre).
- `scan` ve `execute` iç mantığı `run_scan` ve `send_confirmed` fonksiyonlarına ayrıldı (davranış aynı).

## [0.1.0] - 2026-11-20

### Eklendi
- İlk sürüm: ICT/SMC tarayıcı, risk motoru, paper/Binance USDⓈ-M/Alpaca broker'ları, SQLite journal, `scan`, `plan`, `execute` (ONAY), `positions`, `kill`, `stats`, `risk-calc`.
