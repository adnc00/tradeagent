# Değişiklik günlüğü

Biçim [Keep a Changelog](https://keepachangelog.com/tr-TR/1.1.0/), sürümler [SemVer](https://semver.org/lang/tr/).

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
