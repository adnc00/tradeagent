# Güvenlik

## Anahtarlar ve gizli bilgiler

- API anahtarları, Telegram token'ı ve chat id yalnızca `.env` dosyasında durur. `.env`, `config.yaml` ve `data/` (journal, paper durumu, `watch.log`) `.gitignore` ile depoya hiç girmez. Commit atmadan önce `git status` ile kontrol edin.
- Borsa anahtarlarında **yalnızca işlem (trade)** yetkisi olsun; **para çekme (withdraw) ve transfer kapalı**, **IP beyaz listesi açık**.
- Bir anahtar sızdıysa (ekran görüntüsü, yanlış commit, paylaşılan dosya): anahtarı borsada hemen **silin** ve yenisini oluşturun. Git geçmişinden silmek yetmez; sızan anahtar iptal edilmeden güvende sayılmaz.
- Telegram token'ı sızarsa @BotFather'da `/revoke` ile yenileyin.

## Emir güvenliği

- Gerçek para emri yalnızca kullanıcı tam olarak `ONAY` yazınca gider: etkileşimli terminalde (TTY) ya da yerel panelin onay penceresinde. Terminalde betik veya boru onay veremez. Panel 127.0.0.1'e bağlanır; işlemler sayfaya gömülü gizli anahtar (her panel açılışında yeni), panelin kendi Origin'i ve JSON gövde olmadan reddedilir. Bilgisayarınızda çalışan kötü amaçlı bir program yine de yerel paneli kullanabilir; bilgisayarınızı güvende tutun ve gerçek para hesabında paneli yalnızca kullanırken açık tutun.
- Onaysız gönderim yalnızca `watch --demo-auto` ile ve yalnızca demo/paper hesaplarda mümkündür. Bu sınırı gevşeten bir değişiklik, `tests/test_cli.py` ve `tests/test_watch.py` içindeki güvenlik testlerini kırmalıdır.
- Bildirim kanalları yalnızca **gönderir**; Telegram'dan gelen mesajlar hiçbir işlem başlatmaz.

## Açık bildirme

Bir güvenlik açığı bulursanız herkese açık issue açmayın; depo sahibine GitHub üzerinden özel olarak (Security → Report a vulnerability) bildirin.
