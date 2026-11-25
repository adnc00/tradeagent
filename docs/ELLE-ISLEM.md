# Panelden kendi işleminizi açmak (long / short)

Ajanın bulduğu biletlere ek olarak panelden kendi işleminizi tanımlayabilirsiniz. Elle açılan işlem de ajanınkiyle aynı kontrollerden geçer: risk motoru, teminat, likidasyon, bayat fiyat kontrolü ve `ONAY`.

Paneli açın (demo hesap için `--live`):

```powershell
cd C:\Users\Dell\Desktop\trading-agent
python -m tradeagent.cli panel --live
```

## Adımlar

1. Sağ üstte **Yeni işlem**'e basın.
2. **Sembol** girin:
   - Vadeli (long ve short): `ETH/USDT:USDT`, `SOL/USDT:USDT` …
   - Spot (yalnızca long): `ETH/USDT` …
3. **Yön** seçin: **Long** (fiyatın yükseleceğini düşünüyorsanız) ya da **Short** (düşeceğini düşünüyorsanız).
4. **Fiyatı getir**'e basın; giriş kutusuna güncel fiyat yazılır. Fiyatın geri çekilmesini beklemek istiyorsanız girişi değiştirin. Emir limit emirdir: fiyat girişe gelince dolar.
5. **Stop** ve **Hedef** girin:
   - Long: stop girişin **altında**, hedef **üstünde**.
   - Short: stop girişin **üstünde**, hedef **altında**.
6. İsterseniz **Risk %** (boş bırakılırsa ayardaki değer, şu an %5) ve **Kaldıraç** (boş = otomatik) girin.
7. **Önizle**'ye basın. Pencerede şunlar görünür: miktar, pozisyon değeri, kaldıraç ve teminat, stop olursa kaybedilecek tutar ve yüzdesi, hedefte kazanç ve R, likidasyon fiyatı. Bir kural ihlal edilirse (ör. R:R 2'nin altında) nedeni kırmızıyla yazılır ve gönder butonu kapalı kalır.
8. Değerleri değiştirmek için alttaki kutuları düzenleyip **Değişiklikleri uygula**'ya basın; önizleme yeniden hesaplanır.
9. Göndermek için kutuya tam olarak `ONAY` yazıp **Emri gönder**'e basın. Borsaya önce stop ve hedef, sonra limit giriş gider; giriş dolduğu an pozisyon korumalıdır.

Ajanın bulduğu biletler **Onay bekleyenler** listesinde görünür. **İncele ve onayla** aynı pencereyi açar; giriş, stop, hedef, risk ve kaldıracı aynı şekilde değiştirebilirsiniz. Düzenlediğiniz bilet sonraki taramalarda üzerine yazılmaz.

## Miktar, risk ve kaldıraç nasıl hesaplanır?

- **Risk %**, stop olursa bakiyenin ne kadarını kaybedeceğinizdir (komisyon dahil). 100 USDT'lik (sanal) hesapta %5 = 5 USDT.
- **Miktar** bu riske göre hesaplanır: `miktar = risk tutarı / (giriş ile stop arası mesafe + komisyon)`. Stop ne kadar yakınsa pozisyon o kadar büyük olur.
- **Kaldıraç** riski değiştirmez; yalnızca pozisyon için borsada ne kadar teminat bağlanacağını belirler: `teminat = pozisyon değeri / kaldıraç`. Kaldıraç arttıkça likidasyon fiyatı girişe yaklaşır.
- **Otomatik kaldıraç** (`risk.margin_per_trade_pct: 33`): kaldıraç, bir işlemin teminatı bakiyenin %33'ünü geçmeyecek kadar yükseltilir (en fazla `risk.max_leverage`, şu an 20x). Böylece aynı anda 3 işlem açılabilir. Kaldıraç kutusuna bir sayı yazarsanız bu **en az** kaldıraç olur; gerekirse yine yükseltilir.
- **Likidasyon** stop'tan önce gelemez: likidasyon mesafesi stop mesafesinin en az 2 katı olmalıdır (`risk.liq_stop_multiple`), değilse emir reddedilir.

Örnek (100 USDT sanal hesap, risk %5, komisyon %0,05): ETH long, giriş 1.700, stop 1.683 (%1 aşağıda), hedef 1.740. Birim risk 17 + 1,7 (gidiş-dönüş komisyon) = 18,7 USDT, miktar 5 / 18,7 ≈ 0,267 ETH, pozisyon ≈ 455 USDT. Teminat sınırı 33 USDT olduğu için kaldıraç 14x seçilir (teminat ≈ 32,5 USDT; likidasyon ≈ 1.596, stop'un çok altında). Stop olursa ≈ −5 USDT, hedefte ≈ +10,2 USDT (≈ 2,05R).

## Açık işlemi yönetmek

- **Girişi iptal et** (Pozisyonlar tablosu): dolmamış girişi stop ve hedefiyle birlikte iptal eder.
- **Acil durdur** (sağ üst): tüm pozisyonları piyasa emriyle kapatır ve emirleri iptal eder (`ONAY` ister).
- Dolmuş bir pozisyonun stop veya hedefini değiştirmek henüz panelden yapılamaz; bunun için Binance ekranını kullanın.

## Sınırlar (config.yaml → risk)

| Ayar | Şu an | Anlamı |
|---|---|---|
| `risk_pct` | 5 | İşlem başına varsayılan risk % |
| `max_risk_pct` | 5 | Bir işlemde izin verilen en yüksek risk % (programın üst sınırı 5) |
| `max_group_risk_pct` | 15 | Aynı yöndeki tüm kriptoların toplam açık riski % |
| `max_open_risk_pct` | 15 | Tüm açık işlemlerin toplam riski % |
| `daily_loss_pct` | 10 | Günlük kayıp sınırı; dolunca o gün yeni bilet üretilmez |
| `weekly_loss_pct` | 25 | Haftalık kayıp sınırı |
| `max_leverage` | 20 | En yüksek kaldıraç |
| `min_rr` | 2 | Hedefin komisyon dahil en az kaç R olması gerektiği |

Değiştirdikten sonra paneli ve izleme görevini yeniden başlatın. Gerçek para hesabında her işlem yine `ONAY` ister; otomatik mod orada çalışmaz.
