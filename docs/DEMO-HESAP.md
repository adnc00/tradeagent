# Binance Demo Trading'de tam otomatik çalışma

Bu rehberin sonunda ajan bilgisayarınız açıkken 7/24 çalışır. Binance'taki en hacimli USDT-M vadeli pariteleri her 15 dakikada bir tarar. Uygun bir kurulum bulunca **sanal parayla** demo hesapta onay beklemeden pozisyon açar ve her adımı Telegram'dan bildirir.

> Demo Trading gerçek para kullanmaz. Ajanın otomatik modu (`--demo-auto`) gerçek para hesabında **çalışmaz**: `crypto.testnet: false` ile başlatmaya çalışırsanız komut hiçbir şey yapmadan reddeder.

## Hazır olanlar

`config.yaml` bu kurulum için ayarlandı:

| Ayar | Değer | Anlamı |
|---|---|---|
| `mode` | `live` | Komutlara `--live` verilince uzak hesap kullanılır |
| `crypto.exchange` | `binanceusdm` | Binance USDⓈ-M sürekli vadeliler |
| `crypto.testnet` | `true` | **Demo Trading** (sanal para) |
| `risk.allowlist` | `'*/USDT:USDT'` | Tüm USDT-M vadeliler bilete dönüşebilir |
| `universe.top` | `100` | En yüksek hacimli 100 parite taranır |
| `universe.min_quote_volume` | `10000000` | 24s hacmi 10 milyon USDT altındakiler elenir |
| `risk.risk_pct` | `0.5` | İşlem başına özsermayenin %0,5'i riske edilir |
| `risk.daily_loss_pct` | `2.0` | Günde %2 kayıptan sonra yeni bilet açılmaz |
| `risk.max_open_risk_pct` | `3.0` | Aynı anda en fazla %3 açık risk |

Bu değerleri `config.yaml` dosyasından değiştirebilirsiniz.

## 1. Demo hesabı ve API anahtarı

1. <https://demo.binance.com/> adresinden Binance hesabınızla giriş yapın. Demo hesap sanal bakiyeyle gelir.
2. Futures ekranında hesap modunu kontrol edin: **One-way Mode** (tek yönlü) olmalı. Hedge modu reddedilir. Ayar: Futures → sağ üstteki ⚙ → Position Mode → One-way Mode.
3. <https://demo.binance.com/en/my/settings/api-management> sayfasında **Create API** deyin (System generated).
4. Yetkiler: okuma (Reading) ve vadeli işlem (Futures) yetkisi açık olsun; para çekme (Withdrawals) kapalı kalsın. Sayfadaki seçenek adları farklıysa aynı anlama gelenleri seçin.
5. API Key ve Secret Key'i kopyalayın. Secret yalnızca bir kez gösterilir.

## 2. Anahtarları `.env` dosyasına yazın

```powershell
notepad C:\Users\Dell\Desktop\trading-agent\.env
```

```dotenv
BINANCEUSDM_API_KEY=...
BINANCEUSDM_API_SECRET=...
```

Kaydedip kapatın. Anahtarları başka hiçbir yere (sohbet, ekran görüntüsü, GitHub) koymayın.

## 3. Telegram

[TELEGRAM.md](TELEGRAM.md) adımlarını uygulayın. Telegram olmadan da çalışır; o durumda yalnızca Windows bildirimi ve kayıt dosyası olur.

## 4. Bağlantıyı kontrol edin

```powershell
cd C:\Users\Dell\Desktop\trading-agent
python -m tradeagent.cli check --live
```

Beklenen satır: `Hesap bağlantısı : OK - ccxt:binanceusdm (demo trading ...), özsermaye ... USDT`. `BAŞARISIZ` görürseniz anahtarları ve One-way modunu kontrol edin.

**Saat:** `check --live` çıktısında `Saat : SORUNLU` görürseniz önce Windows saatini düzeltin: Ayarlar → Saat ve dil → Tarih ve saat → "Saati otomatik ayarla" Açık, saat dilimi "(UTC+03:00) İstanbul", ardından "Şimdi eşitle". Binance istekleri küçük farkları kendisi telafi eder, ama ajanın mum kapanışı, killzone ve günlük kayıp hesapları bilgisayar saatine dayanır.

## 5. Önce tek tur deneyin

```powershell
python -m tradeagent.cli watch --universe --live --demo-auto --once
```

Bu komut bir kez tarar, uygun bilet varsa demo hesapta gönderir ve çıkar. Yaklaşık 1-2 dakika sürer. Telegram'a "izleme başladı" mesajı gelir. Fırsat bulunursa "demo hesapta otomatik gönderildi" mesajı da gelir. Emirleri <https://demo.binance.com/> ekranında görebilirsiniz.

## 6. 7/24 başlatın

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-watch-task.ps1 -Universe -Live -DemoAuto
```

- Görev hemen başlar ve her oturum açılışında yeniden başlar. Çökerse 1 dakika sonra yeniden başlar.
- Çıktı `data\watch.log` dosyasına yazılır.
- Canlı paneli açmak için: `python -m tradeagent.cli panel --live` (tarayıcıda `http://127.0.0.1:8765`). `--live` ile paneldeki onay, iptal ve acil durdurma demo hesaba gider.
- Durdurmak ve kaldırmak için: `powershell -ExecutionPolicy Bypass -File .\scripts\install-watch-task.ps1 -Remove`
- Tüm demo pozisyonları kapatmak için: `python -m tradeagent.cli kill --live` (terminalde `ONAY` ister).

**Uyku ayarı:** Bilgisayar uykuya geçerse izleme durur. Ayarlar → Sistem → Güç ve pil → Ekran ve uyku → "Prize takılıyken cihazı uyku moduna al: Hiçbir zaman".

## 7. Değerlendirme

Gerçek parayla otomatik çalışma desteklenmez. Gerçek hesaba geçmeyi düşünmeden önce demo sonuçlarına bakın:

```powershell
python -m tradeagent.cli stats
```

En az 30, tercihen 100'den fazla kapanmış işlem olmadan kazanma oranı ve beklenti anlamlı değildir. Demo dolumları gerçek piyasadan daha iyimser olabilir (kayma ve likidite farkı).

## Bilinen sınırlamalar

- Kapanan demo işlemlerinin sonucu (net K/Z, R, stop/take-profit) Binance'ın işlem geçmişinden journal'a yazılır; panel ve `stats` bunları sayar. Hiç dolmamış girişler "iptal" olarak işaretlenir.
- Aynı sembolde aynı anda tek bilet çalışır.
- Tüm USDT-M kriptolar aynı korelasyon grubunda sayılır. Bu yüzden aynı anda açılabilecek toplam risk `max_risk_pct` (%1) ile sınırlıdır; bu, piyasa birlikte düşerken çok sayıda pozisyonun birden zarar etmesini önler.
