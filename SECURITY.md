# Güvenlik

## Anahtarlar ve gizli bilgiler

- API anahtarları, Telegram token'ı ve chat id yalnızca `.env` dosyasında durur. `.env`, `config.yaml` ve `data/` (journal, paper durumu, `watch.log`) `.gitignore` ile depoya hiç girmez. Commit atmadan önce `git status` ile kontrol edin.
- Borsa anahtarlarında **yalnızca işlem (trade)** yetkisi olsun; **para çekme (withdraw) ve transfer kapalı**, **IP beyaz listesi açık**.
- Bir anahtar sızdıysa (ekran görüntüsü, yanlış commit, paylaşılan dosya): anahtarı borsada hemen **silin** ve yenisini oluşturun. Git geçmişinden silmek yetmez; sızan anahtar iptal edilmeden güvende sayılmaz.
- Telegram token'ı sızarsa @BotFather'da `/revoke` ile yenileyin.

## Emir güvenliği

- God mode kapalıyken gerçek para emri yalnızca kullanıcının onayıyla gider: terminalde (TTY) `ONAY` yazarak ya da yerel panelde önizlemeyi görüp **Onayla**'ya basarak. God mode açıkken ajan onay beklemez; god mode yalnızca panelden, kuralları onaylayarak açılır ve korumaları (stop, ayrılan sermaye, iflas freni, kâr kilidi) kaldırılamaz. Terminalde betik veya boru onay veremez. Panel 127.0.0.1'e bağlanır; işlemler sayfaya gömülü gizli anahtar (her panel açılışında yeni), panelin kendi Origin'i ve JSON gövde olmadan reddedilir. Bilgisayarınızda çalışan kötü amaçlı bir program yine de yerel paneli kullanabilir; bilgisayarınızı güvende tutun ve gerçek para hesabında paneli yalnızca kullanırken açık tutun.
- Onaysız gönderim iki yoldan olur: `watch --demo-auto` (yalnızca demo/paper) ve panelden açılan god mode. Bu sınırı gevşeten bir değişiklik, `tests/test_cli.py` ve `tests/test_watch.py` içindeki güvenlik testlerini kırmalıdır.
- Bildirim kanalları yalnızca **gönderir**; Telegram'dan gelen mesajlar hiçbir işlem başlatmaz.

## Açık bildirme

Bir güvenlik açığı bulursanız herkese açık issue açmayın; depo sahibine GitHub üzerinden özel olarak (Security → Report a vulnerability) bildirin.
