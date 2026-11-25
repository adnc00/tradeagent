# Telegram bildirim kurulumu

Bu kurulumdan sonra ajan her yeni fırsatı, otomatik gönderilen demo emirlerini, dolan girişleri, kapanan işlemleri ve hataları telefonunuza mesaj olarak gönderir. Kurulum yaklaşık 5 dakika sürer.

> Bot yalnızca **mesaj gönderir**. Telegram'dan bota yazdığınız hiçbir şey emir açamaz, kapatamaz veya onaylayamaz. Gerçek para emri her zaman bilgisayardaki terminalde `ONAY` yazılarak verilir.

## 1. Botu oluşturun (@BotFather)

1. Telegram'da arama kutusuna **@BotFather** yazın. Adın yanında mavi onay işareti olan resmi hesabı açın.
2. **Başlat** (`/start`) deyin, ardından `/newbot` yazın.
3. Bota görünen bir ad verin, ör. `Trade Ajanım`.
4. Kullanıcı adı verin. Kullanıcı adı `bot` ile bitmeli ve benzersiz olmalı, ör. `adnc_trade_ajani_bot`.
5. BotFather size şu biçimde bir **token** verir: `1234567890:AAH...`. Bu token botun şifresidir; kimseyle paylaşmayın, ekran görüntüsü almayın.

## 2. Token'ı `.env` dosyasına yazın

Proje klasöründeki `.env` dosyasını Not Defteri ile açın:

```powershell
notepad C:\Users\Dell\Desktop\trading-agent\.env
```

En alttaki satırı doldurup kaydedin:

```dotenv
TELEGRAM_BOT_TOKEN=1234567890:AAH...
```

## 3. Bota bir mesaj gönderin

Telegram'da yeni botunuzu açın (BotFather'ın verdiği `t.me/...` bağlantısı), **Başlat**'a basın ve `merhaba` yazın. Bot cevap vermez; bu normal. Bu mesaj chat id'nizi bulmak için gerekiyor.

## 4. Chat id'nizi bulun

```powershell
cd C:\Users\Dell\Desktop\trading-agent
python -m tradeagent.cli telegram-chatid
```

Çıktı şöyle olur:

```
Bota yazan sohbetler (TELEGRAM_CHAT_ID olarak birini .env'e yazın):
  123456789        Ada (private)
```

Sayıyı `.env` dosyasına yazın ve kaydedin:

```dotenv
TELEGRAM_CHAT_ID=123456789
```

"Henüz mesaj yok" derse 3. adımı tekrarlayıp bir dakika sonra yeniden deneyin.

## 5. Deneme mesajı

```powershell
python -m tradeagent.cli notify-test
```

Telefonunuza "LONG BTC/USDT:USDT - TEST bildirimi" başlıklı örnek bir mesaj gelmeli. Ekranda `Gönderildi: konsol, Telegram, Windows bildirimi` yazar. `HATA: Telegram: ...` görürseniz aşağıdaki tabloya bakın.

## 6. Genel kontrol

```powershell
python -m tradeagent.cli check
```

`Telegram : tanımlı` görmelisiniz. Bu komut anahtarların değerlerini hiçbir zaman ekrana yazmaz.

## Sorun giderme

| Belirti | Neden ve çözüm |
|---|---|
| `API reddetti: Unauthorized` | Token yanlış veya eksik kopyalandı. BotFather'da `/mybots` → bot → **API Token** ile tekrar kopyalayın. |
| `API reddetti: Bad Request: chat not found` | Chat id yanlış ya da bota hiç `Başlat` denmedi. 3. ve 4. adımları tekrarlayın. |
| `Forbidden: the bot can't send messages to the bot` | `TELEGRAM_CHAT_ID` olarak botun kendi numarası (token'daki `:` işaretinden önceki sayı) yazılmış. 4. adımdaki `telegram-chatid` çıktısındaki sizin numaranızı yazın. |
| `Forbidden: bot was blocked by the user` | Botu engellemişsiniz. Sohbeti açıp **Engeli kaldır** deyin. |
| `gönderilemedi (URLError ...)` | İnternet bağlantısı veya güvenlik duvarı `api.telegram.org` adresini engelliyor. |
| Mesaj gelmiyor ama hata da yok | `.env` dosyası `config.yaml` ile aynı klasörde mi? Dosyayı kaydettiniz mi? `check` ile kontrol edin. |

## Güvenlik

- Token sızdıysa BotFather'da `/revoke` ile hemen yenileyin ve `.env` dosyasını güncelleyin.
- `.env` dosyası `.gitignore` sayesinde GitHub'a gönderilmez.
- Bildirimler fiyat, miktar ve bakiye bilgisi içerir. Botu bir gruba eklemeyin; yalnızca kendi özel sohbetinizi kullanın.
