# Değişiklik günlüğü

Biçim [Keep a Changelog](https://keepachangelog.com/tr-TR/1.1.0/), sürümler [SemVer](https://semver.org/lang/tr/).

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
