"""Plain-language explanation of a ticket for someone new to charts.

Built only from the setup's measured levels (no guesses): the daily direction,
the liquidity sweep, the break of structure (MSS), the fair value gap (FVG)
used for the entry, why the stop and the target sit where they do, the
reward-to-risk and what can go wrong.
"""

from tradeagent.models import OrderTicket, Setup
from tradeagent.notify import num
from tradeagent.risk import rr_after_fees

_LEVEL_TR = {
    "swing_low": "önceki dip",
    "swing_high": "önceki tepe",
    "PDL": "dünün en düşük fiyatı",
    "PDH": "dünün en yüksek fiyatı",
    "EQL": "birbirine eşit iki dip",
    "EQH": "birbirine eşit iki tepe",
}


def tr(value: float | None) -> str:
    """Turkish number: 60.097,5 (dot for thousands, comma for decimals)."""
    if value is None:
        return "-"
    return num(float(f"{float(value):.8g}")).replace(",", "\0").replace(".", ",").replace("\0", ".")


def _reward_lines(ticket: OrderTicket) -> list[tuple[str, str]]:
    target = ticket.take_profit
    if ticket.stop is None or target is None:
        return []
    try:
        ratio = rr_after_fees(ticket.entry, ticket.stop, target, ticket.fee_rate)
    except ValueError:
        return []
    break_even = 100.0 / (1.0 + ratio) if ratio > 0 else 100.0
    return [(
        "Kazanç / risk",
        f"Hedefe ulaşılırsa kazanç, stop olursa kaybın yaklaşık {tr(round(ratio, 2))} katı ({tr(round(ratio, 2))}R; "
        f"R = bu işlemde riske edilen tutar). Uzun vadede başabaş için bu tür işlemlerin yaklaşık "
        f"%{tr(round(break_even))}'ini kazanmak yeterli.",
    )]


def _risks() -> tuple[str, str]:
    return (
        "Ne ters gidebilir?",
        "Fiyat girişe hiç gelmeyebilir (emir 2 saat sonra ya da fiyat önce hedefe giderse iptal edilir) veya giriş "
        "dolduktan sonra stop'a gidebilir; o zaman zarar yukarıdaki risk tutarıyla sınırlı kalır. Bu bir olasılık "
        "oyunudur: tek bir işlemin sonucu garanti değildir, sonuç ancak çok sayıda işlemde ortaya çıkar.",
    )


def explain(ticket: OrderTicket, setup: Setup | None) -> list[tuple[str, str]]:
    """(başlık, açıklama) satırları, yeni başlayan biri için."""
    long = ticket.side.value == "long"
    action = "alış (long: fiyatın yükselmesinden kazanç)" if long else "açığa satış (short: fiyatın düşmesinden kazanç)"
    if setup is None:
        return [
            ("Bu işlem", f"Elle oluşturulan bir {action} işlemi; {tr(ticket.entry)} fiyatından giriş."),
            ("Stop", f"Fiyat {tr(ticket.stop)} seviyesine {'inerse' if long else 'çıkarsa'} pozisyon zararla kapanır; "
                     "zarar bu noktada sınırlanır."),
            ("Hedef", f"Fiyat {tr(ticket.take_profit)} seviyesine {'çıkarsa' if long else 'inerse'} pozisyon kârla kapanır."),
            *_reward_lines(ticket),
            _risks(),
        ]
    up = setup.bias == "bullish"
    sweep_name = _LEVEL_TR.get(setup.sweep_kind, setup.sweep_kind)
    target_name = _LEVEL_TR.get(setup.target_kinds[0], setup.target_kinds[0]) if setup.target_kinds else "seviye"
    lines = [
        ("Önerilen işlem", f"{ticket.symbol} için {action}. Giriş {tr(ticket.entry)}, stop {tr(ticket.stop)}, "
                           f"hedef {tr(ticket.take_profit)} ({setup.timeframe} grafik)."),
        ("1. Genel yön", (
            "Günlük grafikte fiyat yukarı yönlü: dün, önceki günün tepesinin üstünde kapandı (ya da önceki günün "
            "dibini kısa süre kırıp geri döndü). Bu yüzden yalnızca alış fırsatı aranıyor."
            if up else
            "Günlük grafikte fiyat aşağı yönlü: dün, önceki günün dibinin altında kapandı (ya da önceki günün "
            "tepesini kısa süre aşıp geri döndü). Bu yüzden yalnızca satış fırsatı aranıyor."
        )),
        ("2. Tuzak: likidite süpürmesi", (
            f"Fiyat {tr(setup.sweep_level)} seviyesindeki {sweep_name} {'altına' if long else 'üstüne'} kısa süre "
            f"{'indi' if long else 'çıktı'} ({tr(setup.sweep_extreme)}'e kadar) ve hemen geri döndü. Bu seviyenin "
            f"{'altında' if long else 'üstünde'} çok sayıda stop emri birikir; fiyat onları tetikleyip ('likiditeyi alıp') "
            f"ters yöne dönebilir. Kısa süreli bu {'düşüş' if long else 'yükseliş'} çoğu zaman bir tuzaktır."
        )),
        ("3. Yön değişimi onayı (MSS)", (
            f"Ardından güçlü bir mum, son {'tepe' if long else 'dip'} olan {tr(setup.mss_level)} seviyesini "
            f"{'yukarı' if long else 'aşağı'} kırdı. Bu, {'satıcıların' if long else 'alıcıların'} gücünü kaybettiğine ve "
            f"{'alıcıların' if long else 'satıcıların'} kontrolü aldığına dair bir işarettir."
        )),
        ("4. Neden bu fiyattan giriş? (FVG)", (
            f"Bu hızlı hareket, {tr(setup.fvg_bottom)} ile {tr(setup.fvg_top)} arasında fiyatın neredeyse hiç işlem "
            "görmeden geçtiği bir boşluk bıraktı (FVG: adil değer boşluğu). Fiyat çoğu zaman bu boşluğa geri döner; "
            f"giriş, geri dönüşte boşluğun ortası olan {tr(ticket.entry)}'den yapılır. Böylece "
            f"{'yükselişin peşinden pahalıdan alınmaz' if long else 'düşüşün peşinden ucuzdan satılmaz'}; giriş, son hareketin "
            f"{'ucuz (alt)' if long else 'pahalı (üst)'} yarısında kalır."
        )),
        ("5. Stop neden orada?", (
            f"Stop {tr(ticket.stop)}: süpürülen {'dibin' if long else 'tepenin'} ({tr(setup.sweep_extreme)}) biraz "
            f"{'altında' if long else 'üstünde'}. Fiyat oraya {'inerse' if long else 'çıkarsa'} tuzak fikri yanlış çıkmış "
            "demektir; pozisyon kapanır ve zarar baştan belirlenen tutarda kalır."
        )),
        ("6. Hedef neden orada?", (
            f"Hedef {tr(ticket.take_profit)}: {'yukarıdaki' if long else 'aşağıdaki'} ilk dokunulmamış {target_name}. "
            f"Bu seviyede bekleyen emirler fiyatı kendine çeker ve fiyat orada çoğu zaman tepki verir; kâr orada alınır."
        )),
        *_reward_lines(ticket),
        _risks(),
    ]
    return lines
