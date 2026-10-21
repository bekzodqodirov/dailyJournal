"""Red lines as code (docs/jarvis-plan.md §5.1-5.3, rules 13, 14 and 18).

Two pure checks, no model, no database, no network:

* ``classify_inbound(text)`` reads a client's message for the signals that
  must go to the owner instead of a draft: a price, discount or payment-term
  question, a deadline asked for, a promise or guarantee demanded, a payment
  the client says they made, an identity question («botmisiz?»), a
  complaint or a legal threat, a question about other people. Injection
  tripwires are a signal too, but only raise an alert.
* ``check_outbound(text, role, allowed_tokens)`` is the binding output guard
  on every text MIYA proposes. RED LINE 1: no promise, commitment, deadline,
  time or guarantee. RED LINE 2: no price, discount, payment term, money
  figure or confirmation that a payment arrived. AI-authored text carries no
  digit at all except a GS code or waybill seen verbatim in the same chat's
  inbound messages (``allowed_tokens``).

The guard reads the text twice. The *surface* view keeps every symbol (NFKC,
format characters stripped, apostrophes folded, casefolded, Cyrillic in
Latin): currency symbols, percent signs, clock times, dates, digits, money
figures (``amounts.money_mentions``), links, markup. The *word* view is
``text.normalise_for_search``: the GUARANTEE, TIME/DEADLINE, PROMISE and
MONEY lexicons below, in Uzbek Latin, Uzbek Cyrillic (it normalises to the
same Latin) and Russian, written as people write them and normalised at
import like ``text.STOPWORDS``. A generic commitment backstop catches every
first-person future verb («olib boraman», «hal qilamiz»); the stall-verb
allowlist («aniqlashtirib», «bilib», «yozaman», «xabar beraman»…) is taken
out of it, and those verbs pass only without a TIME hit. A disguised word
is read back too: split into pieces («er ta ga», «yetkaz amiz», a line
break inside a word), hyphenated letter by letter, or stretched
(«ertaaga»). Figures in any shape are refused: digits of
every script (fullwidth, mathematical, circled, superscript), keycap and
other number emoji, figures spelled out in words; so are emoji that state a
time, money or an agreement («🕔», «💵», «🤝», «👍»).

Every doubt is a block: a false pass of a promise or a price is the worst
failure, a false block only costs the owner an escalation. Owner-authored
text (role ``owner``) is their own words: it is flagged for the card, never
blocked. ``tests/fixtures/redline_corpus.py`` pins the verdicts.

``GUARD`` is the red-line guard ``gate`` runs; ``outbox`` registers it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from miya.jarvis import gate
from miya.jarvis.gate import GuardResult, GuardRole
from miya.services import amounts, codes, safety, when
from miya.services.text import (
    fold_apostrophes,
    name_pattern,
    normalise_for_search,
    to_latin,
)

# Bumped whenever a lexicon or a rule changes, so a stored verdict says which
# checker made it (outbox_messages.guard.checker_version).
CHECKER_VERSION: Final = "redlines-2"

# The longest AI reply (§5.2).
MAX_CHARS: Final = 400

Signal = Literal[
    "price",
    "discount",
    "payment_terms",
    "deadline",
    "promise",
    "guarantee",
    "payment_claim",
    "identity",
    "complaint",
    "legal",
    "other_people",
    "injection",
]
Flag = Literal[
    "promise",
    "deadline",
    "guarantee",
    "price",
    "discount",
    "payment_terms",
    "payment_claim",
    "digits",
    "link",
]
Script = Literal["latin", "cyrillic", "russian"]
Outcome = Literal["pass", "block", "flag"]

PASS: Final = "pass"
BLOCK: Final = "block"
FLAG: Final = "flag"

# The order flags are reported in.
FLAGS: Final[tuple[Flag, ...]] = (
    "promise",
    "deadline",
    "guarantee",
    "price",
    "discount",
    "payment_terms",
    "payment_claim",
    "digits",
    "link",
)

# What the owner sees on a card for each flag (JV-13 owner strings).
FLAG_LABELS: Final[Mapping[Flag, str]] = {
    "promise": "va'da",
    "deadline": "muddat",
    "guarantee": "kafolat",
    "price": "narx",
    "discount": "chegirma",
    "payment_terms": "to'lov sharti",
    "payment_claim": "to'lov tasdig'i",
    "digits": "raqam",
    "link": "havola",
}

# Signals that open an escalation; ``injection`` only raises an alert.
ESCALATING: Final[frozenset[Signal]] = frozenset(
    {
        "price",
        "discount",
        "payment_terms",
        "deadline",
        "promise",
        "guarantee",
        "payment_claim",
        "identity",
        "complaint",
        "legal",
        "other_people",
    }
)

# The escalation reason (escalations.reasons) for a blocked flag.
_FLAG_REASON: Final[Mapping[Flag, str]] = {
    "promise": "promise",
    "deadline": "deadline",
    "guarantee": "guarantee",
    "price": "price",
    "discount": "discount",
    "payment_terms": "payment_terms",
    "payment_claim": "payment_claim",
    "digits": "other",
    "link": "other",
}


# --- lexicons ---------------------------------------------------------------------------
#
# Entries are separated by commas or new lines and written naturally (Latin
# Uzbek, Cyrillic Uzbek or Russian); a trailing «*» on a word matches any
# ending. Entries are normalised at import exactly like the text they are
# compared with, so
# «bo'ladi», «бўлади» and «boladi» are one entry. A «ye» in an Uzbek word
# also matches the «e» the Cyrillic spelling gives it («етказамиз»,
# «қаерда»).


def _entries(raw: str) -> tuple[str, ...]:
    """The entries of a lexicon block: separated by commas or new lines."""
    return tuple(e.strip() for e in re.split(r"[,\n]", raw) if e.strip())


def _ye(part: str) -> str:
    """``part`` as a regex in which every «ye» may also be the «e» that the
    Cyrillic «е» becomes («етказамиз», «қаерда»)."""
    return "(?:ye|e)".join(re.escape(piece) for piece in part.split("ye"))


def _word_pattern(word: str) -> str:
    """One entry word as a regex. Uzbek writers swap «h» and «x» («xafta»,
    «hammasi»/«xammasi»), so either letter matches both."""
    prefix = word.endswith("*")
    norm = normalise_for_search(word.rstrip("*"))
    piece = r"\s+".join(_ye(part) for part in norm.split())
    piece = re.sub(r"[hx]", "[hx]", piece)
    return piece + (r"[a-z0-9]*" if prefix and piece else "")


def _compile(entry: str) -> re.Pattern[str] | None:
    words = [p for p in (_word_pattern(w) for w in entry.split()) if p]
    if not words:
        return None
    return re.compile(r"(?<![a-z0-9-])" + r"\s+".join(words) + r"(?![a-z0-9])")


@dataclass(frozen=True, slots=True)
class Lexicon:
    """One category's entries, compiled; ``hits`` names the entries found."""

    name: str
    entries: tuple[str, ...]
    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    # English entries: a client may write in English, and so may a draft.
    # They are Latin only (no Cyrillic spelling exists).
    english: frozenset[str] = frozenset()
    # Every entry in one pattern: does the text hit this lexicon at all?
    union: re.Pattern[str] | None = None

    @classmethod
    def of(cls, name: str, raw: str, *, english: str = "") -> Lexicon:
        foreign = _entries(english)
        entries = (*_entries(raw), *foreign)
        compiled = tuple(
            (entry, pattern)
            for entry in entries
            if (pattern := _compile(entry)) is not None
        )
        union = (
            re.compile("|".join(f"(?:{p.pattern})" for _e, p in compiled))
            if compiled
            else None
        )
        return cls(name, entries, compiled, frozenset(foreign), union)

    def hit(self, words: str) -> bool:
        return self.union is not None and self.union.search(words) is not None

    def hits(self, words: str) -> list[tuple[str, tuple[int, int]]]:
        return [
            (entry, match.span())
            for entry, pattern in self.patterns
            for match in pattern.finditer(words)
        ]


# RL1 — a time or a deadline. Day words, parts of the day, clock words,
# weekdays, months, spans and deadline words.
TIME = Lexicon.of(
    "deadline",
    """
    bugun*, ertaga*, ertangi*, indin*, erta, ertaroq, kecha, kechagi, kechasi,
    kechqurun*, kechki, kech, kechroq, kechgacha, ertalab*, tongda, tonggi, tushlik*,
    tushgacha, tushdan keyin, kunduzi, tunda, tungi, peshin*, tushda, sahar*, ertasi*,
    reys*, рейс*, bayramdan keyin, bayramgacha, hayitdan keyin, hayitgacha, tez, tezroq,
    tezda, tezkor, tezlik*, на днях, ближайш*, быстро, быстрее, долго, недолго, soat*,
    daqiqa*, minut*, sekund*, hafta*, oy, oyda, oyga, oyning, oyi, oylar, oydagi*,
    oydan, oyiga, oylik, yil*, kunda, kuni, kungacha, kundan, kunlik, kunlari, kunlarda,
    kun ichida, dushanba*, seshanba*, chorshanba*, payshanba*, juma, jumaga, jumada,
    jumagacha, jumadan, shanba*, yakshanba*, yanvar*, fevral*, mart, martda, martga,
    aprel*, may, mayda, mayga, iyun*, iyul*, avgust*, sentyabr*, oktyabr*, noyabr*,
    dekabr*, vaqtida, vaqtgacha, vaqtda, darhol, zudlik*, shoshilinch*, kechik*,
    kechikmay*, сегодня*, завтра*, послезавтра, вчера, утром, утра, утро, вечером,
    вечера, вечер, вечеру, ночью, днём, днем, дня, дней, день, дню, час, часа, часов,
    часам, часу, часик*, полчаса, минут*, секунд*, недел*, месяц*, год, года, году, лет,
    понедельник*, вторник*, среда, среду, среды, четверг*, пятниц*, суббот*, воскресен*,
    январ*, феврал*, март*, апрел*, мая, май, июн*, июл*, август*, сентябр*, октябр*,
    ноябр*, декабр*, обед*, вовремя, сразу, немедленно, срочно, в течение, через,
    до конца, yaqinda, yaqin orada, hademay, hoziroq, zahoti*, darrov, скоро,
    вскоре, скорее, скорейш*, мигом, сейчас же, прямо сейчас, сутк*, суток,
    bir necha kun*, bir kun, yarim kun, kun oxiri*, bir zumda, zumda, birpas*,
    bir pasda, lahza*, oqshom*, kechga, tushga, tush payti, asrda, shomda,
    namozdan keyin, bayramda, bayramdan oldin, ramazonda, ramazondan keyin,
    ramazongacha, ramazon oyida, yozda, kuzda, qishda, bahorda, tezgina, tezkor*,
    ertga*, zavtro, оперативно, моментально, мгновенно, щас, выходн*, праздник*,
    полдень, полудня, утру, вечерком, утречком, денёк, денек, деньк*, летом, зимой,
    весной, осенью, ждать не придется, ждать не придётся, не придется ждать,
    не придётся ждать
    """,
    english="""
    today, tomorrow, tonight, yesterday, morning, evening, noon, week*, month*, hour*,
    minute*, day, days, asap, soon, deadline*, monday, tuesday, wednesday, thursday,
    friday, saturday, sunday, on time, right away, immediately, afternoon, eod,
    shortly, quickly, overnight, tonite, tmrw, tmr, in no time, take long, not long,
    wait long
    """,
)

# RL1 — the subset of TIME that is a deadline, also refused in a KB entry
# (where office hours are fine).
DEADLINE = Lexicon.of(
    "deadline",
    """
    kun ichida, hafta ichida, oy ichida, kunda, kundan keyin, haftada, vaqtida,
    vaqtgacha, darhol, zudlik*, kechik*, kechikmay*, ulgur*, в течение, через, вовремя,
    сразу, срочно, немедленно, успе*
    """,
)

# RL1 — a guarantee or an assurance.
GUARANTEE = Lexicon.of(
    "guarantee",
    """
    kafil*, albatta, shubhasiz*, shak-shubhasiz, aniq bo'ladi, aniq keladi, aniq yetib,
    aniq yetadi, aniq qilamiz, aniq qilaman, muammo yo'q, muammo bo'lmaydi, muammosiz,
    hech qanday muammo, xavotir olmang, xavotirlanmang, xavotir qilmang,
    tashvish qilmang, tashvishlanmang, ishonavering, ishonchingiz komil,
    ishonchim komil, ishonch bilan, ishonchli, ishoning, ishonaman, tinch bo'ling,
    xotirjam*, tashvish yo'q, yuz foiz, yuz protsent, ta'minla*, ta'minlan*, bexatar,
    sug'urta*, точно, обязательно, наверняка, безусловно, без проблем, нет проблем,
    не переживайте, не волнуйтесь, не беспокойтесь, уверяю, ручаюсь, железно,
    стопроцентно, сто процентов, можете не сомневаться, не сомневайтесь, без вопросов,
    можете быть уверены, уверен*, не подвед*, страхов*, застрахов*, albatda, albatt*,
    hammasi joyida, hammasi nazoratda, nazoratda, qasam*, vallohi, valloh, xudo haqi,
    даю слово, слово даю, честное слово, клянусь, гарантир*, bemalol, gap yo'q,
    hech gap emas, muammo emas, problem yo'q, problema yo'q, masala yo'q, qiyin emas,
    bo'ladigan ish, hammasi tartibda, hammasi normal, hammasi chotki, shubha yo'q,
    shubha qilmang, shubhalanmang, mutlaqo, so'zsiz, ishonchimiz komil, ishonchim bor,
    qo'rqmang, bezovta bo'lmang, hayajonlanmang, asabiylashmang, однозначно,
    непременно, будьте спокойны, под контролем, в порядке, всё нормально,
    все нормально, не бойтесь, положиться, надёжно, надежно, зуб даю, не вопрос,
    разумеется, естественно, само собой, без сомнени*
    """,
    english="""
    guarante*, sure, surely, definitely, certainly, of course, no problem*,
    no worries, dont worry, trust me, insured, absolutely, no doubt, rest assured,
    count on, covered, its fine
    """,
)

# RL1 — a promise, an agreement, a commitment or a status claim the AI
# cannot know («jo'natildi»). The first-person future forms of every verb
# are caught by the backstop below; these are the rest.
PROMISE = Lexicon.of(
    "promise",
    """
    va'da*, so'z beraman, so'z beramiz, kelishdik, kelishamiz, kelishildi, kelishaylik,
    kelishib olamiz, kelishuv*, mayli, xo'p, xop, hop, ha, xa, bo'p, да, ага, угу,
    постара*, shartnoma*, kontrakt*, контракт*, ok, okey, okay, bo'ldi, bo'pti,
    bo'p qoladi, bo'lib qoladi, tayyor*, hal qil*, hal bo'l*, yuboraman, yubordim,
    yubordik, yuborildi, yo'lda, jo'natdim, jo'natdik, jo'natildi, tashladim, tashladik,
    yetkazdim, yetkazdik, yetkazildi, yuklandi, yuklatdim, yetib keldi, yetib bordi,
    qildim, qildik, qilindi, qo'ydim, qo'ydik, jo'nadi, chiqdi, ketdi, обещ*,
    договорились, договоримся, договор, по рукам, ладно, хорошо, конечно, окей, ок,
    сделаем, сделаю, сделано, отправлю, отправим, отправлено, отправил, отправили,
    отправлен, отправлена, отправлены, загружен*, вышлю, вышлем, пришлю, пришлём, скину,
    скинем, скинул, привезу, привезём, привезем, доставим, доставлю, доставлен*,
    доставят, отгрузим, отгружу, отгружено, верну, вернём, вернем, переведу, переведём,
    посчитаю, посчитаем, подготовлю, подготовим, оформлю, оформим, решим, решу, будем,
    буду, готов*, приеду, приедем, прибудет, придет, придёт, дойдет, дойдёт, получите,
    получит, в пути, в дороге, едет, едут, выехал*, загрузил*, погрузил*, прибыл*,
    прибуд*, inshaalloh, inshalloh, inshaallah, иншаллах, иншааллах, xudo xohlasa,
    alloh xohlasa, kanechno, канечно, bezproblem*, беспроблем*, inshaolloh,
    inshoolloh, inshoalloh, nasib qilsa, nasib etsa, roziman, rozimiz, kutilmoqda,
    rejada, rejalashtiril*, grafik bo'yicha, jadval bo'yicha, kelsa kerak,
    bo'lsa kerak, kelishi kerak, yetib kelishi, qolar, ortdik, ortdim, ortildi,
    topshirdik, topshirildi, yukladik, berdik, rasmiylashtirdik, rasmiylashtirildi,
    chiqarildi, bajarildi, okk*, oki, легко, добро, идёт, идет, заметано, лады,
    пойдёт, пойдет, годится, соглас*, принято, норм, нормально, на подходе,
    подъезжа*, прибыва*, везём, везем, грузим, организуем, устроим, уладим, можем,
    справимся, берёмся, беремся, возьмёмся, возьмемся, займёмся, займемся,
    растамож*, выпустили, оформили, отгрузили, привезли, доставили, сдали, передали,
    выдали, передадим, сдадим
    """,
    english="""
    promise*, will, shall, gonna, we can, i can, deliver*, sent, shipped,
    on the way, agreed, deal, done, ready, okay, yes, yep, yeah, yup, well, ill, itll,
    on it, handle*, ship*, arriv*, coming
    """,
)

# RL1 — third-person futures and passives («keladi», «yuboriladi»). In a
# question with a question word («Qayerdan jo'natiladi?») they ask, not
# promise; anywhere else they are a promise.
PROMISE_THIRD = Lexicon.of(
    "promise",
    """
    bo'ladi, bo'lmaydi, keladi, kelyapti, kelmaydi, yetadi, yetib keladi, yetib boradi,
    ketadi, chiqadi, jo'naydi, jo'natiladi, yuboriladi, yetkaziladi, yuklanadi,
    beriladi, qaytariladi, qilinadi, olinadi, boradi, olib boradi, будет, omborda,
    omborimizda, bojxonada, chegarada, на складе, на таможне, на границе,
    jo'natilgan, yuborilgan, yetkazilgan, yuklangan, yetib kelgan, yetib borgan,
    kelgan, ketgan, chiqqan, пришёл, пришел, дошёл, дошел, на месте
    """,
)

# RL2 — money: currency and scale words (from the one table), and words that
# state a price.
_TABLE_WORDS = ", ".join(
    word
    for word in (*amounts.CURRENCY_WORDS, *amounts.SCALE_WORDS)
    # Symbols are the surface view's; «вон» (there) and «k» alone are read as
    # money only next to a figure.
    if len(word) > 1 and word not in {"вон", "von", "k"}
)
MONEY = Lexicon.of(
    "price",
    _TABLE_WORDS
    + """
    so'm*, сўм*, dollar*, доллар*, rubl*, рубл*, yuan*, юан*, evro*, tenge*, тысяч*,
    миллион*, миллиард*, лимон*, baks*, бакс*, tiyin*, тийин*, копе*, цент, цента,
    центов, у.е., arzon*, qimmat*, tekin*, bepul*, pullik, foiz*, protsent*, процент*,
    дешев*, дёшев*, дешёв*, недорог*, стоит, входит, включен*, kiritilgan, платить,
    платите, заплат*, плачу, платим, summa*, сумм*, kredit*, кредит*, zalog*, залог*,
    depozit*, депозит*, komissiya*, комисси*, ustama*, xarajat*, расход*, tannarx*, qqs,
    ндс, пошлин*, poshlina*, дорого, дороже, дороговато, бесплатн*, даром,
    hisoblab beraman, hisoblab beramiz, hammasi ichida, qimat*, полцен*, нисколько,
    полмиллион*, полтысяч*, полмлн, hamyonbop*, bekorga, muft, muftga, muftasiga,
    muftdan, gratis, халяв*, arzimas*, ko'kda, ko'kidan, ko'kiga, ko'kini, kusok,
    sotka*, сотк*, косар*, лям, ляма, лямов, тыща, тыщу, тыщ, полтос*
    """,
    english="""
    price*, cost*, cheap*, expensive, free, cent, cents, fee, fees, tariff*, bucks,
    buck, grand, quid, affordable
    """,
)

DISCOUNT = Lexicon.of(
    "discount",
    """
    arzonlashtir*, arzonroq, kamaytir*, narxni tushir*, narxini tushir*, aksiya*,
    bonus*, promokod*, промокод*, sovg'a*, подар*, уступ*, торг*, поторг*, подешевле,
    дешевле, акци*, бонус*, maxsus taklif*, maxsus narx*, imtiyoz*, keshbek*,
    кешбэк*, кэшбэк*, скидочк*, спеццен*, спецпредлож*, льгот*, распродаж*
    """,
    english="""
    discount*, sale, promo*, special offer, cashback, bargain, markdown, reduced
    """,
)

PAYMENT_TERMS = Lexicon.of(
    "payment_terms",
    """
    to'la*, to'lash*, to'lang, nasiya*, qarz*, shartlar*, shartlari*, bo'lib to'la*,
    oldindan to'lov*, avans*, predoplat*, naqd*, karta*, kartochka*, hisob raqam*,
    perechisl*, click, payme, uzcard, humo, paynet, предоплат*, рассрочк*, в долг, долг,
    долга, долгу, наличн*, наличк*, nalichka*, нал, безнал*, карт, карта, карту, картой,
    карте, карты, перевод*, перечисл*, реквизит*, счет, счёт, oldindan, o'tkazing,
    o'tkazasiz, o'tkazsangiz, hisobimga, hisobimizga, hisobga, hisobingizga,
    hisob-kitob*, hisob kitob*, bo'lib-bo'lib, qismlarga, qism-qism, balans*,
    hisobimizda, hisobingizda, plastikka, plastik karta*, долгов, долги, долгами,
    платеж*, платёж*, расчет*, расчёт*, рассчита*, сочтёмся, сочтемся, частям*,
    частями, пополам, по факту
    """,
    english="""
    prepay*, cash, card, invoice*, installment*, payment*, pay, transfer*, upfront,
    up front, deposit*, settle*, on arrival, on delivery
    """,
)

# RL2 — a confirmation that a payment arrived.
PAYMENT_CLAIM = Lexicon.of(
    "payment_claim",
    """
    tushdi, tushibdi, tushgan, tushirildi, chek, cheki, chekni, chekingiz, chekingizni,
    kvitansiya*, qabul qilindi, qabul qildik, qabul qildim, oldik, to'landi, o'tdi,
    o'tkazildi, keldi, получил*, получен*, поступил*, поступлен*, зачислен*, оплачено,
    оплачен, пришло, пришла, прошла, прошло, kelibdi, kelipti, kelibti, o'tibdi,
    o'tipti, o'tibti, tushipti, tushibti, tushyapti, tasdiqlandi, tasdiqlangan,
    tasdiqladik, tasdiqladim, ko'rindi, ko'rinyapti, пришли, дошли, дошло, дошла,
    дошёл, упали, упало, на счету, подтвержден*, подтвердили
    """,
    english="""
    paid, received, money arrived, funds, credited, came through, money came
    """,
)

# Rule 18 and §5.2: no self-disclosure or prompt leakage, and never a denial
# of being an AI.
SELF_DISCLOSURE = Lexicon.of(
    "self_disclosure",
    """
    bot, botman, botmiz, botmisiz, robot*, sun'iy intellekt*, suniy intellekt*,
    neyroset*, chatgpt*, gpt*, ai, assistent*, yordamchi*, avtomatik javob*, avtojavob*,
    dastur*, miya, jarvis, til modeli*, prompt*, promt*, instruktsiya*, instruksiya*,
    ko'rsatma*, tizim*, system*, json, odamman, tirik odam*, bot emasman, бот, ботом,
    робот*, искусствен*, искусствен* интеллект*, нейросет*, ассистент*, автоответ*,
    программ*, языков* модел*, ии, инструкци*, я человек, живой человек, не бот
    """,
    english="""
    assistant*, chatbot*, language model*, artificial intelligence, automated
    """,
)

# Polite formulas that name a part of the day without naming a time, and
# invitations to write («bemalol yozing», feel free to write): taken out of
# the word view before the lexicons read it.
GREETINGS = Lexicon.of(
    "greeting",
    """
    xayrli kun*, xayrli tong*, xayrli kech*, xayrli tun*, hayrli kun*, hayrli tong*,
    kuningiz xayrli, juma muborak, oldindan rahmat, добрый день,
    добрый вечер, доброе утро, доброй ночи, спокойной ночи, хорошего дня,
    хорошего вечера, удачного дня, хорошего вам дня, всего доброго,
    добро пожаловать, bemalol yozing, bemalol yozavering, bemalol so'rang,
    bemalol murojaat*
    """,
)

# The owner's own vague «tez orada» (soon) in their default stall (JV-13).
# It is a time like any other: only a stall (role ``stall``: a fixed template
# or the owner's override) may say it, and only next to a stall verb («o'zim
# tez orada yozvoraman»). An AI reply that says it is refused («Tez orada
# sizda»).
OWNER_SOON = Lexicon.of("owner_soon", "tez orada")

# Offers of help that end in an ability verb («bera olaman»), asked as a
# question: taken out before the commitment backstop reads the clause.
HELP_OFFERS = Lexicon.of(
    "help_offer",
    """
    qanday yordam bera olaman, qanday yordam bera olamiz, nima yordam bera olaman,
    nimada yordam bera olaman, nimada yordam bera olamiz, sizga qanday yordam,
    чем могу помочь, чем можем помочь
    """,
)


# Topic words: naming a price, a deadline or a payment is allowed only as
# the subject of a deferral («narx bo'yicha aniqlashtirib, o'zim yozaman»,
# «по цене уточню»), never as a statement («narxi o'zgarmagan»).
_TOPIC_RAW: Final[Mapping[Flag, str]] = {
    "price": """
        narx*, narh*, baho, bahosi, bahosini, baxo*, qiymat*, tarif*, prays*, pul*, цена,
        цены, цену, цене, ценой, ценам, ценах, ценник*, стоимост*, mablag'*, деньги,
        денег, деньгам, деньгами, деньгах
        """,
    "deadline": """
        muddat*, srok*, срок*
        """,
    "payment_terms": """
        to'lov*, оплат*
        """,
    "discount": """
        chegirma*, skidk*, скидк*, скидочк*
        """,
    "guarantee": """
        kafolat*, garant*, гарант*
        """,
}
TOPICS: Final[tuple[tuple[Flag, Lexicon], ...]] = tuple(
    (flag, Lexicon.of(flag, raw)) for flag, raw in _TOPIC_RAW.items()
)

# The words that make a topic the subject of a deferral.
_REGARDING_AFTER = frozenset(
    normalise_for_search(w)
    for w in ("bo'yicha", "haqida", "masalasida", "borasida", "xususida", "yuzasidan")
)
_REGARDING_BEFORE = frozenset(
    normalise_for_search(w)
    for w in ("по", "о", "об", "насчёт", "насчет", "касательно", "про", "относительно")
)
# What may stand between a topic and its stall verb: «narxini ham hozir
# aniqlashtirib».
_FILLERS = frozenset(
    normalise_for_search(w)
    for w in (
        "ham",
        "hozir",
        "buni",
        "uni",
        "yana",
        "bir",
        "o'zim",
        "o'zimiz",
        "sizga",
        "sizlarga",
        "всё",
        "все",
        "сейчас",
        "это",
        "сам",
        "сами",
        "вам",
    )
)


def _forms(raw: str) -> tuple[tuple[str, bool], ...]:
    """(normalised form, is a prefix) per entry, for single-token matching."""
    out = []
    for entry in _entries(raw):
        prefix = entry.endswith("*")
        norm = normalise_for_search(entry.rstrip("*"))
        if norm:
            out.append((norm, prefix))
            if "ye" in norm:
                out.append((norm.replace("ye", "e"), prefix))
    return tuple(out)


def _token_in(token: str, forms: Sequence[tuple[str, bool]]) -> bool:
    return any(
        token.startswith(form) if prefix else token == form for form, prefix in forms
    )


# The stall verbs (§5.2): finding out, telling, writing back. A first-person
# future of one of these is a deferral, not a promise, and passes only
# without a TIME hit.
STALL_VERBS = _forms(
    """
    aniqla*, aniqlashtir*, bilib, bilish*, bilaman, bilamiz, bilsam, yozaman, yozamiz,
    yozib, yozvoraman, yozvoramiz, yozvomman, yozvommiz, yozay, aytaman, aytamiz, aytib,
    aytay, tekshir*, so'rab, so'rayman, so'raymiz, qarab, qarayman, qaraymiz, ko'rib,
    o'rganib, o'rganaman, surishtir*, maslahatlash*, уточн*, узнаю, узнаем, напишу,
    напишем, сообщу, сообщим, отвечу, ответим, проверю, проверим, посмотрю, посмотрим,
    спрошу, спросим, выясню, выясним, проверя*, выясня*
    """
)
# Words a stall auxiliary («beraman») may follow and stay a deferral:
# «xabar beraman», «javob beraman», «aniqlab beraman».
STALL_LEADS = _forms(
    """
    xabar, javob, aniqlab, aniqlashtirib, bilib, tekshirib, so'rab, qarab, ko'rib,
    o'rganib, surishtirib
    """
)
# Auxiliaries: the verb they follow decides what they commit to.
AUXILIARIES = _forms(
    """
    beraman, beramiz, qo'yaman, qo'yamiz, olaman, olamiz, ko'raman, ko'ramiz, chiqaman,
    chiqamiz, turaman, qolaman, qilaman
    """
)
# Verbs that commit to nothing: knowing, understanding, thinking, waiting,
# thanking, apologising, asking, «arzimaydi» (you are welcome).
NEUTRAL_VERBS = _forms(
    """
    tushunaman, tushunamiz, o'ylayman, o'ylaymiz, kutaman, kutamiz, uzr so'rayman,
    tilayman, tilaymiz,
    tabriklayman, tabriklaymiz, iltimos qilaman, rahmat aytaman, qulay, noqulay,
    shug'ullan*, arzimaydi, eshitaman, qalay, kechirasiz*, понимаю, понимаем, знаю,
    думаю, жду, ждем, ждём, благодарю, благодарим, извиняюсь, прошу, рад, рады, давай,
    совсем, всем, затем, зачем, причем, причём, xursandman, xursandmiz, mamnunman,
    mamnunmiz, minnatdorman, minnatdormiz, shodman, dushman, karman, roman, obman,
    tushundim, tushundik, думаем, желаем, желаю
    """
)
# Words that make a clause a real question («qayerdan», «qaysi», «какой»).
QUESTION_WORDS = _forms(
    """
    qayer*, qaysi*, nima*, qanday, qanaqa*, kim*, necha*, qancha*, qachon*, nega,
    откуда, куда, где, какой, какая, какое, какие, какую, сколько, что, кто, чем, когда
    """
)

# The generic commitment backstop, on word-view tokens: first-person future
# and present («boraman», «qilamiz», «tashlayman»), progressive
# («yuboryapman»), intent («yubormoqchiman»), the colloquial «-vomman» and the
# optative («yuboray», «qilaylik»). Russian: the first-person forms of
# perfective verbs.
_UZ_COMMIT = re.compile(
    r"(?:[aey]|ya)(?:man|miz)(?:mi|chi|u)?$"
    r"|(?:[aey]|ya)m[zn]$"
    r"|[^aeiouy\s]man$"
    r"|.{3,}(?:ish|sh)im(?:iz)?$"
    r"|yap(?:man|miz)$"
    r"|mokchi(?:man|miz)?$"
    r"|ajak(?:man|miz)?$"
    r"|vom(?:man|miz)$"
    r"|(?<!d)(?:ay|ayin|aylik)(?:mi)?$"
    # Particles («qilamiz-da», «yuboramizku»), the «-yab-»/«-vot-» and
    # «-yotib-» progressives, the perfect «-ganmiz», the past «-dik», the
    # future «-armiz».
    r"|(?:[aey]|ya)(?:man|miz)(?:-?ku|-(?:da|a|ya|yu|chi|e|ey))$"
    r"|ya[bp](?:man|miz)$"
    r"|vot(?:ti)?(?:man|miz|mz|mn)$"
    r"|yotib(?:man|miz)$"
    r"|[gkq]an(?:man|miz)$"
    r"|.{3,}[dt]ik$"
    r"|[aeiouy]r(?:man|miz)$"
)
# Third person, future or habitual: consonant stem + «adi» («keladi»), vowel
# stem + «ydi» («ishlaydi»), the negative «-maydi», the plural «-ishadi»;
# and the second person's future («rozi bo'lasiz», «olasiz»). The past
# («keldi», «bo'ldi») and a question («olasizmi») end otherwise.
_UZ_THIRD = re.compile(
    r"(?:[^aeiou]adi|aydi|iydi|uydi|maydi|ishadi|[^aeiou]asiz|aysiz)$"
    # «ketyapti», «yuklanyabdi», «kelvotti», «kutilmoqda», «berarsiz», and the
    # passive past («topshirildi», «bajarildi»).
    r"|(?:yap|yab|vot|yotib)(?:ti|di)$|moqda$|[aeiou]rsiz$|.{4,}(?:il|in)di$"
)
_SO_FAR = frozenset({"hozirgacha", "shugacha", "hanuzgacha"})
_RU_COMMIT = re.compile(
    r"^(?=[a-z]{5,}$)(?:(?!.*iyu$).*(?:lyu|ayu|eyu|nyu|ryu|vyu|ruyu|zuyu)"
    r"|(?:s|so|po|pri|ot|pere|vy|za|do|pod|raz|na|pro|ob)[a-z]{3,}(?:em|im|yom)"
    r"|(?:pri|pere|vy|s|za|do|pod|ot|raz|pro)[a-z]{2,}[zdlnrt]u)$"
)
# The Russian first-person plural («организуем», «довезём», «перешлём») in
# any script: no Uzbek word ends this way, so it is read in Uzbek text too
# («Ака, организуем») and in Russian typed in Latin («Aka, zakinem»).
_RU_WE = re.compile(r"^(?=[a-z]{6,}$)[a-z]{3,}(?:uem|aem|yaem|iem|zem|nem|yom|shlem)$")
# A verb ending typed as its own word («yetkaz amiz», «yubor aman»).
_LONE_ENDING = re.compile(
    r"^(?:(?:a|ya|y|e)?(?:miz|mz)|a(?:man|mn)|yman|adi|aydi|ydi|ishadi|yapti|yapmiz"
    r"|yapman)$"
)


# --- the surface view -------------------------------------------------------------------

_PERCENT = re.compile(r"[%‰‱]")
_HUNDRED_PERCENT = re.compile(r"(?<!\d)100\s?%")
_CLOCK = re.compile(r"(?<!\d)\d{1,2}\s?[:.]\s?\d{2}(?!\d)")
_DATE = re.compile(r"(?<!\d)\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?(?!\d)")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w@])@\w{2,}")
_URL_HINT = re.compile(r"(?i)(?:https?:|www\.|t\.me|wa\.me|\.uz\b|\.com\b|\.ru\b)")
# «e r t a g a», «5 0 0»: a word spelled out letter by letter slips past
# every lexicon.
_SPACED = re.compile(r"(?<!\w)(?:\w[\s.\-_·•*/|]{1,4}){3,}\w(?!\w)")
# A symbol standing in for a letter of a word: «ert@ga», «n*rx», «ertag@».
_INNER_SYMBOL = re.compile(r"[^\W\d_][@$€*#|=~^]")


def _foreign(ch: str) -> bool:
    """A letter outside plain Latin and Cyrillic: a Greek «ο» in «ertaga»
    reads the same and matches nothing, and so does an accented Latin
    letter («ërtaga», «böladi», «çegirma»): the word view splits a word at
    it. Uzbek Latin needs none of them once the apostrophes are folded."""
    if not ch.isalpha() or ch.isascii():
        return False
    return not 0x0400 <= ord(ch) <= 0x04FF


def _enclosed(ch: str) -> bool:
    """Regional-indicator and enclosed or squared Latin letters («🇪», «🅴»):
    letters no view can read."""
    return 0x1F100 <= ord(ch) <= 0x1F1FF


_MARKUP = re.compile(
    r"<\s*/?\s*[a-z!][^>]*>"
    r"|&[a-z]+;|&#\d+;"
    r"|\*\*|__|~~|`"
    r"|\[[^\]\n]*\]\([^)\n]*\)"
    r"|^\s{0,3}#{1,6}\s"
    r"|^\s*>\s"
    r"|^\s*[-*+•]\s"
    r"|(?<![\w*])\*[^*\n]+\*(?![\w*])"
    r"|(?<![\w_])_[^_\n]+_(?![\w_])"
    r"|\|",
    re.IGNORECASE | re.MULTILINE,
)
# Number words: a spelled-out figure is a figure (rule 14). «bir» (one, «a»)
# is left out on purpose: it is in every other sentence.
_NUMBER_WORDS = re.compile(
    r"(?<![\w'])(?:"
    r"(?:ikki|yetti|etti|sakkiz|to'qqiz|to'rt|yigirma|o'ttiz|qirq|ellik|oltmish"
    r"|yetmish|etmish|sakson|to'qson)[\w']*"
    r"|(?:uch|o'n|yuz|besh|olti)"
    r"(?:ta\w*|inchi\w*|nchi\w*|ga|da|dan|lab|ov\w*|ala\w*|yuz\w*|ming\w*)?"
    r"|ikkov\w*|ming\w+"
    r"|devyanost\w*|semdesyat\w*|poltor\w*|o'?ttiz\w*|to'?qson\w*|to'?qqiz\w*"
    r"|yarim\w*|yarm(?:i|ini|ida|iga|isi|idan)|sotn\w*|sotk\w*|poltinnik\w*"
    r"|kosar\w*|nol(?:ga|da|dan|ni)?"
    r"|dva|dve|dvux|tri|tryox|trex|chetire\w*|chetiryox|pyat\w*|shest\w*|sem|semi"
    r"|vosem\w*|devyat\w*|desyat\w*|\w+nadtsat\w*|dvadtsat\w*|tridtsat\w*|sorok\w*"
    r"|sto|dvesti|trista|chetiresta|pyatsot|shestsot|semsot|vosemsot|devyatsot"
    r"|tisyach\w*|poltora|polovin\w*|dvoe|troe|chetvero|pyatero|shestero"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\w+teen"
    r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred\w*"
    r"|thousand\w*|million\w*|half|dozen\w*"
    r")(?![\w'])"
    # «on» is «o'n» (ten) typed without the apostrophe, and Russian «он» (he):
    # a figure only before a count, a unit or a time word.
    r"|(?<![\w'])on(?:ta\w*|inchi\w*|\s+(?:kun|hafta|oy|yil|soat|daqiqa|minut|ming"
    r"|million|dollar|so'm|som|kilo|kg|tonna|kub|quti|joy|foiz|protsent|prosent|ta"
    r"|bir|ikki|uch|to'rt"
    r"|besh|olti|yetti|sakkiz|to'qqiz)\w*)(?![\w'])"
)

# Emoji that are figures, times, money or an agreement: «🔟», «💯», a
# clock face, a calendar, a banknote, a handshake, a check mark or a thumbs
# up (a bare «yes» to whatever was asked, like «ha» or «ok»).
_EMOJI: Final[Mapping[str, tuple[Flag, str]]] = {
    **{chr(c): ("deadline", "time:emoji") for c in range(0x1F550, 0x1F568)},
    "🔟": ("digits", "digits:emoji"),
    "💯": ("guarantee", "guarantee:emoji"),
    "⏰": ("deadline", "time:emoji"),
    "⏱": ("deadline", "time:emoji"),
    "⏲": ("deadline", "time:emoji"),
    "⌚": ("deadline", "time:emoji"),
    "⏳": ("deadline", "time:emoji"),
    "⌛": ("deadline", "time:emoji"),
    "📅": ("deadline", "time:emoji"),
    "📆": ("deadline", "time:emoji"),
    "🗓": ("deadline", "time:emoji"),
    "💵": ("price", "money:emoji"),
    "💴": ("price", "money:emoji"),
    "💶": ("price", "money:emoji"),
    "💷": ("price", "money:emoji"),
    "💰": ("price", "money:emoji"),
    "💲": ("price", "money:emoji"),
    "💸": ("price", "money:emoji"),
    "🤑": ("price", "money:emoji"),
    "💳": ("payment_terms", "payment_terms:emoji"),
    "🧾": ("payment_claim", "payment_claim:emoji"),
    "🤝": ("promise", "promise:emoji"),
    "👌": ("promise", "promise:emoji"),
    "👍": ("promise", "promise:emoji"),
    "✅": ("promise", "promise:emoji"),
    "✔": ("promise", "promise:emoji"),
    "☑": ("promise", "promise:emoji"),
    "🆗": ("promise", "promise:emoji"),
    "🚚": ("promise", "promise:emoji"),
    "🚛": ("promise", "promise:emoji"),
    "✈": ("promise", "promise:emoji"),
    "📦": ("promise", "promise:emoji"),
}


def _clean(text: str) -> str:
    """NFKC with every format character (bidi, zero-width) removed."""
    return safety.display_name(text or "")


def _surface(text: str) -> str:
    return to_latin(fold_apostrophes(text).casefold())


# Emoji presentation selectors and the keycap mark: they follow a symbol or a
# digit, never a letter.
_EMOJI_MARKS: Final = "\ufe0e\ufe0f\u20e3"


def _hidden(text: str) -> bool:
    """Bidi overrides, zero-width marks, control characters or invisible
    combining marks (the grapheme joiner, variation selectors): what can make
    a text read differently from what it says, splitting a word for the word
    view. The zero-width joiner of an emoji sequence is not one of them, nor
    an emoji's presentation selector."""
    composed = unicodedata.normalize("NFC", text or "")
    for i, ch in enumerate(composed):
        category = unicodedata.category(ch)
        if (category == "Cf" and ch != "\u200d") or (
            category == "Cc" and ch not in "\n\t"
        ):
            return True
        if category in ("Mn", "Me"):
            before = composed[i - 1] if i else ""
            if not (ch in _EMOJI_MARKS and not before.isalpha()):
                return True
    return False


def _numeric(ch: str) -> bool:
    return ch.isdigit() or ch.isnumeric()


def _mask_tokens(text: str, allowed: frozenset[str]) -> tuple[str, list[str]]:
    """``text`` with every allowed GS code and waybill blanked out, and the
    codes that were not allowed (their digits then stay and are refused).

    Rule 14 says verbatim: a code is allowed when its digits are exactly the
    digits the client typed, leading zeros and all («GS367» does not allow
    «GS0367» nor the reverse). Only the prefix may be spelled otherwise (case,
    a Cyrillic look-alike, a space or a dash before the digits)."""
    allowed_forms = _code_forms(allowed)
    unknown: list[str] = []
    out = text
    for pattern, verbatim in (
        (codes.waybill_re(), _waybill_form),
        (codes.client_code_re(), _code_form),
    ):
        pieces: list[str] = []
        last = 0
        for match in pattern.finditer(out):
            form = verbatim(match)
            if form is None:
                continue  # not a code («GS367abc»): its digits stay and are refused
            if form in allowed_forms:
                start, end = _core_span(match)
                pieces.append(out[last:start])
                pieces.append(" " * (end - start))
                last = end
            else:
                unknown.append("code")
        pieces.append(out[last:])
        out = "".join(pieces)
    return out, unknown


def _code_forms(allowed: Iterable[str]) -> frozenset[str]:
    """The verbatim forms of the codes in ``allowed``."""
    forms: set[str] = set()
    for token in allowed:
        for pattern, verbatim in (
            (codes.waybill_re(), _waybill_form),
            (codes.client_code_re(), _code_form),
        ):
            for match in pattern.finditer(_clean(token)):
                form = verbatim(match)
                if form is not None:
                    forms.add(form)
    return frozenset(forms)


def _waybill_form(match: re.Match[str]) -> str | None:
    return codes.canonical_waybill(match).upper()


def _code_form(match: re.Match[str]) -> str | None:
    tail = match.group("tail")
    if tail and tail.lower() not in codes.UZ_SUFFIXES:
        return None
    return codes.verbatim_client_code(match).upper()


def _core_span(match: re.Match[str]) -> tuple[int, int]:
    """The code itself, without a grammatical ending («GS367ga»)."""
    if "d" in match.re.groupindex:
        return match.start(), match.end("d")
    return match.span()


@dataclass(frozen=True, slots=True)
class _Hits:
    """What one reading found: (flag or None, hit code)."""

    found: list[tuple[Flag | None, str]]

    def add(self, flag: Flag | None, code: str) -> None:
        self.found.append((flag, code))


def _surface_hits(
    raw: str, clean: str, masked: str, hits: _Hits, *, role: GuardRole
) -> None:
    surface = _surface(masked)
    if any(unicodedata.category(ch) == "Sc" for ch in masked):
        hits.add("price", "money:currency_symbol")
    if _PERCENT.search(masked):
        hits.add("price", "money:percent")
    if _HUNDRED_PERCENT.search(masked):
        hits.add("guarantee", "guarantee:hundred_percent")
    if amounts.money_mentions(masked) or amounts.money_mentions(surface):
        hits.add("price", "money:figure")
    if _CLOCK.search(masked):
        hits.add("deadline", "time:clock")
    if _DATE.search(masked):
        hits.add("deadline", "time:date")
    if when.parse_time(masked) is not None:
        hits.add("deadline", "time:phrase")
    if any(_numeric(ch) for ch in masked):
        hits.add("digits", "digits")
    if any(_numeric(ch) and not ch.isascii() for ch in raw):
        hits.add("digits", "digits:numeral")
    if _PHONE.search(masked):
        hits.add("digits", "digits:phone")
    if _NUMBER_WORDS.search(surface):
        hits.add("digits", "digits:number_word")
    for ch in dict.fromkeys(raw):
        if ch in _EMOJI:
            hits.add(*_EMOJI[ch])
    if (
        safety.extract_urls(clean)
        or _URL_HINT.search(masked)
        or _EMAIL.search(masked)
        or _HANDLE.search(masked)
    ):
        hits.add("link", "link")
    if role != "owner" and _MARKUP.search(clean):
        hits.add(None, "markup")
    if role != "owner" and _SPACED.search(fold_apostrophes(clean)):
        hits.add(None, "spaced_letters")
    if role != "owner" and _INNER_SYMBOL.search(fold_apostrophes(clean)):
        hits.add(None, "symbol_in_word")
    if role != "owner" and (
        any(_foreign(ch) for ch in fold_apostrophes(clean))
        or any(_enclosed(ch) for ch in raw)
    ):
        hits.add(None, "foreign_letters")


# --- the word view ----------------------------------------------------------------------

# A clause ends at «.», «!» or «?» followed by a space, or at a line end; a
# dot inside a word («у.е.», «14.30») does not end one. A clause is read in
# segments split at commas, semicolons, colons and dashes: a question tag
# («…, nima deysiz?») asks only in its own segment.
_CLAUSE = re.compile(r"(?:[^.!?\n]|[.!?](?![\s.!?]|$))+[.!?]*")
_SEGMENT = re.compile(r"[,;:—–]")

_RED_LINES: Final[tuple[tuple[Lexicon, Flag], ...]] = (
    (GUARANTEE, "guarantee"),
    (PROMISE, "promise"),
    (MONEY, "price"),
    (DISCOUNT, "discount"),
    (PAYMENT_TERMS, "payment_terms"),
    (PAYMENT_CLAIM, "payment_claim"),
)


@dataclass(frozen=True, slots=True)
class _Clause:
    words: str
    question: bool

    @property
    def tokens(self) -> list[str]:
        return self.words.split()


def _clauses(text: str) -> list[_Clause]:
    """The segments of every sentence, each marked with whether its sentence
    is a question."""
    out = []
    for match in _CLAUSE.finditer(text):
        raw = match.group(0)
        question = "?" in raw
        for segment in _SEGMENT.split(raw):
            words = normalise_for_search(segment)
            if words:
                out.append(_Clause(words, question))
    return out


def _blank(words: str, lexicon: Lexicon) -> str:
    for _entry, (start, end) in lexicon.hits(words):
        words = words[:start] + " " * (end - start) + words[end:]
    return words


def _asks(clause: _Clause) -> bool:
    return clause.question and any(_token_in(t, QUESTION_WORDS) for t in clause.tokens)


def _past_fillers(tokens: list[str], j: int) -> int:
    while j < len(tokens) and tokens[j] in _FILLERS:
        j += 1
    return j


def _stall_at(tokens: list[str], j: int) -> bool:
    """A stall verb at ``j``: «aniqlab», «уточню», or «xabar beraman»."""
    if j >= len(tokens):
        return False
    if _stalling(tokens, j):
        return True
    return (
        _token_in(tokens[j], STALL_LEADS)
        and j + 1 < len(tokens)
        and _stalling(tokens, j + 1)
    )


def _deferral(tokens: list[str], i: int) -> bool:
    """Is the topic at ``i`` the subject of a deferral («narx bo'yicha hozir
    aniqlashtirib», «по цене уточню», «narxini aniqlashtirib»)? The stall verb
    must follow in the same segment: «narx bo'yicha o'zgarish yo'q, …» and
    «по скидке есть, уточню» state something about the topic first."""
    before = tokens[i - 1] if i > 0 else ""
    if _token_in(before, STALL_VERBS):
        return True
    j = _past_fillers(tokens, i + 1)
    if _stall_at(tokens, j):
        return True
    if j < len(tokens) and tokens[j] in _REGARDING_AFTER:
        return _stall_at(tokens, _past_fillers(tokens, j + 1))
    return False


def _committing(tokens: list[str], i: int, *, russian: bool) -> bool:
    """Is token ``i`` a first-person commitment the backstop refuses? The
    Russian endings are read only in Russian text: «savolim» is Uzbek for
    «my question», not a Russian verb."""
    token = tokens[i]
    if not (
        _UZ_COMMIT.search(token)
        or _RU_WE.search(token)
        or (russian and _RU_COMMIT.search(token))
    ):
        return False
    if len(token) < 5:
        return False
    pair = " ".join(tokens[max(i - 1, 0) : i + 1])
    if _token_in(token, NEUTRAL_VERBS) or _token_in(pair, NEUTRAL_VERBS):
        return False
    if _token_in(token, STALL_VERBS):
        return False
    if _token_in(token, AUXILIARIES):
        before = tokens[i - 1] if i > 0 else ""
        return not _token_in(before, STALL_LEADS)
    return True


def _predicting(token: str) -> bool:
    """A future or habitual about someone else («keladi», «bo'ladi»,
    «yetkazishadi», «olmaydi», «bajariladi», «rozi bo'lasiz»): a prediction
    about the cargo, the price or the people the AI cannot make."""
    return (
        len(token) >= 5
        and _UZ_THIRD.search(token) is not None
        and not _token_in(token, NEUTRAL_VERBS)
    )


def _until(token: str) -> bool:
    """«uyingizgacha», «oxirigacha», «ertagacha», «tugaguncha»: until a time
    or as far as a place, a deadline or a delivery either way. «hozirgacha»
    (so far) is neither, nor is the farewell «ko'rishguncha»."""
    if len(token) <= 6 or token in _SO_FAR:
        return False
    if token.endswith("gacha"):
        return True
    return token.endswith(("guncha", "kuncha")) and not token.startswith(
        ("korishgun", "korishga")
    )


def _stalling(tokens: list[str], i: int) -> bool:
    token = tokens[i]
    if _token_in(token, STALL_VERBS):
        return True
    before = tokens[i - 1] if i > 0 else ""
    return _token_in(token, AUXILIARIES) and _token_in(before, STALL_LEADS)


# The lexicons a disguised word is read against: a word split into short
# pieces («er ta ga», «zav tra», «nar xi») or stretched («ertaaga»,
# «albattaaa») is still the word.
_DISGUISABLE: Final[tuple[tuple[Lexicon, Flag], ...]] = (
    (TIME, "deadline"),
    (DEADLINE, "deadline"),
    *_RED_LINES,
    *((lexicon, flag) for flag, lexicon in TOPICS),
)
_SHORT_PIECE: Final = 3
# The most pieces one word is read back from («e r t a g a» is six).
_MOST_PIECES: Final = 8
_STRETCH = re.compile(r"([a-z])\1{2,}|([aeiou])\2+")


def _disguised(words: str) -> str:
    """The forms a clause's words take undisguised, space-separated: every
    run of two to eight short pieces joined up, every hyphenated word without
    its hyphens, and the clause with stretched letters squeezed. Empty when
    nothing in the clause could be a disguise."""
    tokens = words.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and len(tokens[j]) <= _SHORT_PIECE:
            j += 1
        for start in range(i, j):
            for end in range(start + 2, min(j, start + _MOST_PIECES) + 1):
                out.append("".join(tokens[start:end]))
        i = max(j, i + 1)
    # «e-r-t-a-g-a»: the search form keeps a word's inner hyphens.
    out.extend(t.replace("-", "") for t in tokens if "-" in t)
    squeezed = _STRETCH.sub(lambda m: m.group(1) or m.group(2), words)
    if squeezed != words:
        out.append(squeezed)
    return " ".join(dict.fromkeys(out))


def _disguise_hits(words: str, hits: _Hits, *, kb: bool) -> None:
    """Entries a disguised form shows that the clause as written does not
    (the plain view has already judged those, deferral and all)."""
    forms = _disguised(words)
    if not forms:
        return
    for lexicon, flag in _DISGUISABLE:
        if kb and lexicon is TIME:
            continue
        seen = {entry for entry, _span in lexicon.hits(words)}
        for entry, _span in lexicon.hits(forms):
            if entry not in seen:
                hits.add(flag, f"disguised:{entry}")
    if _NUMBER_WORDS.search(forms):
        hits.add("digits", "digits:number_word")


def _join_hits(words: str, hits: _Hits, *, kb: bool) -> None:
    """A word split in two or three pieces of any length, across a line break
    too («yetkaz amiz», «zavt ra», «er\ntaga», «предо плата»): ``words`` is
    the whole text's word view. A joined form counts when no piece alone hits
    that lexicon and the entry it shows runs across the join."""
    tokens = words.split()
    for size in (2, 3):
        for start in range(len(tokens) - size + 1):
            pieces = tokens[start : start + size]
            joined = "".join(pieces)
            first = len(pieces[0])
            for lexicon, flag in _DISGUISABLE:
                if (kb and lexicon is TIME) or not lexicon.hit(joined):
                    continue
                if any(lexicon.hit(piece) for piece in pieces):
                    continue
                for entry, (begin, end) in lexicon.hits(joined):
                    if begin < first < end:
                        hits.add(flag, f"disguised:{entry}")
            number = _NUMBER_WORDS.search(joined)
            if (
                number is not None
                and number.start() < first < number.end()
                and not any(_NUMBER_WORDS.search(piece) for piece in pieces)
            ):
                hits.add("digits", "digits:number_word")


def _polite(words: str) -> str:
    """``words`` without the greetings and the offers of help."""
    return _blank(_blank(words, GREETINGS), HELP_OFFERS)


def _word_hits(clean_masked: str, hits: _Hits, *, role: GuardRole) -> None:
    kb = role == "kb"
    russian = script_of(clean_masked) == "russian"
    stall_verb = False
    time_hit = False
    soon = False
    topics: list[tuple[Flag, str]] = []
    if role != "owner":
        _join_hits(_polite(normalise_for_search(clean_masked)), hits, kb=kb)
    for clause in _clauses(clean_masked):
        words = _polite(clause.words)
        if role == "stall" and OWNER_SOON.hit(words):
            soon = True
            words = _blank(words, OWNER_SOON)
        if not kb:
            for entry, _span in TIME.hits(words):
                hits.add("deadline", f"time:{entry}")
                time_hit = True
        for entry, _span in DEADLINE.hits(words):
            hits.add("deadline", f"deadline:{entry}")
            time_hit = True
        for lexicon, flag in _RED_LINES:
            for entry, _span in lexicon.hits(words):
                hits.add(flag, f"{lexicon.name}:{entry}")
        if not _asks(clause):
            for entry, _span in PROMISE_THIRD.hits(words):
                hits.add("promise", f"promise:{entry}")
        if role != "owner":
            for entry, _span in SELF_DISCLOSURE.hits(words):
                hits.add(None, f"self_disclosure:{entry}")
            _disguise_hits(words, hits, kb=kb)
        tokens = words.split()
        asks = _asks(clause)
        for i, token in enumerate(tokens):
            if not kb and _LONE_ENDING.search(token):
                hits.add("promise", "promise:commitment")
            if _stalling(tokens, i):
                stall_verb = True
            elif not kb and _committing(tokens, i, russian=russian):
                hits.add("promise", "promise:commitment")
            elif not (kb or russian or asks) and _predicting(token):
                hits.add("promise", "promise:prediction")
            if not kb and _until(token):
                hits.add("deadline", "deadline:gacha")
                time_hit = True
            for flag, lexicon in TOPICS:
                for entry, _span in lexicon.hits(token):
                    if not _deferral(tokens, i):
                        hits.add(flag, f"topic:{entry}")
                    else:
                        topics.append((flag, entry))
    if topics and not stall_verb:
        for flag, entry in topics:
            hits.add(flag, f"topic:{entry}")
    if stall_verb and time_hit:
        # A stall verb with a time is a promise to answer by then.
        hits.add("promise", "promise:stall_with_time")
    if soon and not stall_verb:
        # The owner's «tez orada» is theirs only next to a stall verb.
        hits.add("deadline", "time:tez orada")


def _names_hits(
    words: str, allowed: Iterable[str], others: Iterable[str], hits: _Hits
) -> None:
    allowed_forms = {f for f in (normalise_for_search(n) for n in allowed if n) if f}
    # «Akmal aka» may be called «Akmal»: a part of an allowed name is allowed.
    allowed_parts = allowed_forms | {p for f in allowed_forms for p in f.split()}
    names = [n for n in others if n and normalise_for_search(n) not in allowed_parts]
    pattern = name_pattern(names)
    if pattern is None:
        return
    own = name_pattern(sorted(allowed_forms))
    if own is not None:
        words = own.sub(lambda m: " " * len(m.group(0)), words)
    if pattern.search(words):
        hits.add(None, "name")


# --- scripts ----------------------------------------------------------------------------

_UZ_CYRILLIC = frozenset("ўқғҳЎҚҒҲ")
_RU_ONLY = frozenset("ыщЫЩ")
# Uzbek endings Russian words do not have: «берамиз», «етказиб», «келган»,
# «келади».
_UZ_ENDING = re.compile(
    r"(?:миз|сиз|ингиз|нинг|иб|ган|япти|ябди|моқчи|моқда|дими|дингиз|лар\w+"
    r"|ади|йди|майди|ишади)$"
)
_UZ_CYRILLIC_WORDS = frozenset(
    {
        "ва",
        "бу",
        "ака",
        "опа",
        "нима",
        "керак",
        "бор",
        "салом",
        "ассалому",
        "яхши",
        "рахмат",
        "юк",
        "сиз",
        "мен",
        "шу",
        "билан",
        "учун",
        "ҳам",
        "эмас",
        "йук",
        "канча",
        "качон",
        "нарх",
        "нархи",
        "борми",
        "керакми",
        "ассалом",
        "алайкум",
    }
)


def script_of(text: str) -> Script | None:
    """The script a message is written in: Latin, Uzbek Cyrillic or Russian.
    None when it has no letters (an emoji, a sticker caption)."""
    clean = _clean(text)
    latin = sum(1 for ch in clean if ch.isalpha() and ch.isascii())
    cyrillic = sum(1 for ch in clean if "Ѐ" <= ch <= "ӿ")
    if not latin and not cyrillic:
        return None
    if latin >= cyrillic:
        return "latin"
    if any(ch in _UZ_CYRILLIC for ch in clean):
        return "cyrillic"
    if any(ch in _RU_ONLY for ch in clean):
        return "russian"
    words = [w.strip(".,!?;:«»\"'()").lower() for w in clean.split()]
    if set(words) & _UZ_CYRILLIC_WORDS or any(_UZ_ENDING.search(w) for w in words):
        return "cyrillic"
    return "russian"


def _family(script: Script) -> str:
    return "latin" if script == "latin" else "cyrillic"


# --- the verdict ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """The deterministic verdict on one outbound text."""

    verdict: Outcome
    flags: tuple[Flag, ...] = ()
    hits: tuple[str, ...] = ()
    role: GuardRole = "reply"

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    @property
    def labels(self) -> tuple[str, ...]:
        """The flags as the owner reads them («narx», «muddat»)."""
        return tuple(FLAG_LABELS[f] for f in self.flags)

    def reasons(self) -> tuple[str, ...]:
        """The escalation reasons (escalations.reasons) this verdict gives."""
        found = dict.fromkeys(_FLAG_REASON[f] for f in self.flags)
        return tuple(found) or (("other",) if not self.passed else ())

    def result(self) -> GuardResult:
        """The verdict as ``gate`` stores it (outbox_messages.guard)."""
        return GuardResult(
            verdict=self.verdict,
            flags=self.flags,
            det_hits=self.hits,
            model_verdict=None,
            checker_version=CHECKER_VERSION,
        )


def check_outbound(
    text: str,
    role: GuardRole = "reply",
    allowed_tokens: frozenset[str] = frozenset(),
    *,
    allowed_names: Iterable[str] = (),
    other_names: Iterable[str] = (),
    client_script: Script | None = None,
) -> Verdict:
    """The binding red-line check on one outbound text (§5.2 step 2).

    ``role``: ``reply`` (an AI reply) and ``stall`` (a stall template or the
    owner's override of one) are blocked on any hit; ``owner`` (the owner's
    own words) is only flagged; ``kb`` (a knowledge-base answer) is refused
    for money, discounts, payment terms, payment confirmations, deadlines,
    promises and guarantees, but may carry office hours and other digits.
    ``allowed_tokens`` are the GS codes and waybills seen in this chat's
    inbound messages. ``other_names`` are people the text must not name
    unless they are in ``allowed_names`` (the counterparty, the owner's
    aliases); ``client_script`` is the script the client wrote in.
    """
    hits = _Hits([])
    raw = text or ""
    clean = _clean(raw)
    if not clean.strip():
        hits.add(None, "empty")
    if role != "owner" and _hidden(raw):
        hits.add(None, "hidden_characters")
    if role in ("reply", "stall") and len(clean) > MAX_CHARS:
        hits.add(None, "too_long")
    masked, unknown = _mask_tokens(clean, allowed_tokens)
    for _code in unknown:
        hits.add("digits", "digits:unknown_code")
    _surface_hits(raw, clean, masked, hits, role=role)
    _word_hits(masked, hits, role=role)
    if role != "owner":
        _names_hits(normalise_for_search(masked), allowed_names, other_names, hits)
        written = script_of(clean)
        if (
            client_script is not None
            and written is not None
            and _family(written) != _family(client_script)
        ):
            hits.add(None, "script")
    found = hits.found
    if role == "kb":
        found = [
            (flag, code)
            for flag, code in found
            if flag not in ("digits", "link")
            and not code.startswith(("time:", "self_disclosure:"))
            and code not in ("markup", "script", "name", "too_long")
        ]
    flags = tuple(f for f in FLAGS if any(flag == f for flag, _ in found))
    codes_found = tuple(dict.fromkeys(code for _, code in found))
    if not codes_found:
        return Verdict(PASS, role=role)
    outcome: Outcome = FLAG if role == "owner" else BLOCK
    return Verdict(outcome, flags, codes_found, role)


# --- inbound signals --------------------------------------------------------------------

_IN_PRICE = Lexicon.of(
    "price",
    """
    narx*, narh*, necha pul, nechpul, nech pul, qancha turadi, qanchaga, qanchadan,
    qancha bo'ladi, qancha so'm, qancha pul, qancha, qanaqa narx, baho*, qiymat*,
    tarif*, prays*, hisoblab ber*, hisobla*, цена, цены, цену, цене, ценой, ценам,
    ценах, ценник*, стоимост*, сколько стоит, сколько будет, почем, почём, по чем,
    прайс*, тариф*, посчитай*
    """,
    english="""
    price*, cost*, how much, rate*, quote
    """,
)
_IN_DISCOUNT = Lexicon.of(
    "discount",
    """
    chegirma*, skidk*, arzon*, kamaytir*, narxni tushir*, narxini tushir*,
    tushirib bering, tushirsangiz, oxirgi narx*, скидк*, дешевле, подешевле, уступ*,
    торг*, последняя цена
    """,
    english="""
    discount*, cheaper, best price, sale
    """,
)
_IN_TERMS = Lexicon.of(
    "payment_terms",
    """
    nasiya*, qarzga, qarz*, bo'lib to'la*, oldindan to'lov*, oldindan pul*,
    oldindan to'la*, avans*, predoplat*, keyin to'la*, keyin beraman, to'lovni keyin,
    pulini keyin, naqd*, karta*, perechisl*, to'lov*, qanday to'la*, hisob raqam*,
    click, payme, предоплат*, рассрочк*, в долг, долг, долга, долгу, наличн*, безнал*,
    оплат*, перечисл*, реквизит*, на карту, по карте, pulni qayerga, qayerga tashlay*,
    qayerga to'la*, qayerga o'tkaz*, karta raqam*, куда платить, куда переводить,
    куда скинуть, номер карты
    """,
    english="""
    prepay*, installment*, card, cash, payment*, pay later, credit
    """,
)
_IN_PAID = Lexicon.of(
    "payment_claim",
    """
    to'ladim, to'lab qo'ydim, to'lab bo'ldim, to'ladik, pul tashladim, pulni tashladim,
    pulini tashladim, kartaga tashladim, pul yubordim, pulni yubordim, pulini yubordim,
    pul o'tkazdim, o'tkazdim, o'tkazib qo'ydim, o'tkazvordim, tashlab qo'ydim, chek*,
    kvitansiya*, tushdimi, keldimi pul, pul keldimi, pul tushdimi, pulim tushdimi,
    перевел*, перевёл*, оплатил*, оплачено, отправил деньги, скинул*, деньги пришли,
    дошли деньги, чек, квитанц*, деньги отправил*, деньги скинул*, деньги перевел*,
    деньги перевёл*, деньги дошли, оплату отправил*
    """,
    english="""
    i paid, we paid, i sent the money, money sent, payment sent, receipt
    """,
)
_IN_DEADLINE = Lexicon.of(
    "deadline",
    """
    qachon*, necha kunda, necha kun, nechchi kun*, qancha vaqt*, qancha kunda,
    qancha kun, qancha muddat*, muddat*, srok*, когда, сколько дней, за сколько,
    к какому, до какого, срок*, как долго
    """,
    english="""
    when, how long, how many days, deadline*
    """,
)
# «Now» and «soon» asked for: a time in an inbound message, though the
# outbound TIME lexicon leaves «hozir» to the stalls («hozir aniqlashtirib»).
_IN_NOW = Lexicon.of(
    "deadline",
    """
    tez orada, hozir, hoziroq, сейчас, прямо сейчас
    """,
)
# «qancha» (how much, how long) asks how long unless it is about money or a
# measure: «Qancha kutaman?» is a deadline, «Kilosi qanchadan?» a price.
_HOW_MUCH = _forms("qancha, qanchadan")
_PRICE_CONTEXT = Lexicon.of(
    "price",
    """
    narx*, narh*, baho*, pul*, qiymat*, tarif*, prays*, turadi, so'm*, kilo*, kg,
    kub*, tonna*, metr*, dona*, gramm*, litr*
    """,
)
_IN_PROMISE = Lexicon.of(
    "promise",
    """
    va'da*, so'z bering, so'z berasizmi, so'z berolasizmi, обещ*, даёте слово,
    даете слово, слово дайте
    """,
    english="""
    promise*
    """,
)
_IN_GUARANTEE = Lexicon.of(
    "guarantee",
    """
    kafolat*, garant*, aniqmi, aniq bo'ladimi, aniq keladimi, aniq yetib*, aniq ayting,
    rostdanmi, yuz foiz, гарант*, точно, наверняка, сто процентов, ishonsam*,
    ishonsa*, ishonch*, ishonaymi, можно доверять, доверять, страховк*, sug'urta*
    """,
    english="""
    guarante*, for sure, sure, trust
    """,
)
_IN_IDENTITY = Lexicon.of(
    "identity",
    """
    bot, botmi, botmisiz, botmisan, botmasmi, botmasmisiz, bot emasmi, botsizmi, botsiz,
    robot*, sun'iy intellekt*, suniy intellekt*, neyroset*, chatgpt*, gpt*, ai,
    avtomatik javob*, avtojavob*, o'zingizmisiz, o'zlarimisiz, odammisiz, jonli odam*,
    tirik odam*, kim yozyapti, kim javob beryapti, бот, бота, боту, ботом, робот*,
    искусствен*, нейросет*, автоответ*, живой человек, это вы, кто пишет, кто отвечает,
    ии, botsan*, botsizmi, botmi*, odam bormi, odammi, odam bilan, jonli*, operator*,
    оператор*, avtomatik*, автоматическ*, o'zingizmi*, o'zingiz yozyap*, kim yozyap*,
    кто это пишет, человек есть, есть человек, с человеком
    """,
    english="""
    bot, robot*, chatbot*, ai, are you human, real person, human, automated, assistant*
    """,
)
_IN_COMPLAINT = Lexicon.of(
    "complaint",
    """
    shikoyat*, norozi*, yomon*, aldad*, aldov*, aldamchi*, buzil*, sindi, singan,
    sinib*, shikast*, yo'qol*, yo'qotdingiz, yo'qotdilar, kechikdi, kechikyapti,
    kechikib, hali kelmadi, hali yo'q, javob bermayapsiz, javob bermaysiz,
    pulimni qaytar*, qaytarib bering, qaytaring, uyat*, sharmand*, bezor*, jahlim*,
    xafa*, жалоб*, обман*, обманул*, сломан*, разбит*, потерял*, потеряли, верните,
    возврат*, ужас*, безобраз*, недовол*, задерж*, опоздал*, кинули, kelmadi,
    yetib kelmadi, kelmayapti, hali ham yo'q, uzoq kutdim, kutib charchadim,
    kutyapman hali, hali kutyapman, сколько можно, долго жду, до сих пор нет,
    не пришло, не пришёл, не пришел
    """,
    english="""
    complain*, refund*, broken, damaged, lost, scam*, fraud*, late, delayed, terrible
    """,
)
_IN_LEGAL = Lexicon.of(
    "legal",
    """
    sud, sudga, sudda, sudlash*, prokuratur*, advokat*, yurist*, militsiya*, politsiya*,
    da'vo*, ariza yoz*, huquq*, суд, суда, судом, в суд, полици*, адвокат*, юрист*, иск,
    прокурат*, заявлени*, hokimiyat*, xalq qabulxona*, ombudsman*, хоким*, омбудсман*
    """,
    english="""
    court, lawyer*, police, sue, lawsuit*, attorney*
    """,
)
_IN_OTHER_PEOPLE = Lexicon.of(
    "other_people",
    """
    boshqa mijoz*, boshqa odam*, boshqalar*, raqamini bering, raqamini tashlang,
    nomerini bering, nomerini tashlang, telefonini bering, kontaktini, manzilini bering,
    другого клиента, других клиентов, его номер, её номер, ее номер, дайте номер
    """,
)
_IN_INJECTION = Lexicon.of(
    "injection",
    """
    oldingi ko'rsatma*, ko'rsatmalarni unut*, barcha ko'rsatma*, забудь, игнорируй,
    предыдущие инструкции, ты теперь, системный промпт, системная инструкция,
    системные инструкции
    """,
    english="""
    ignore previous, ignore all, ignore the above, disregard, system prompt,
    you are now, act as, developer mode, jailbreak, forget previous, forget all,
    forget your, new instructions
    """,
)
_INJECTION_MARKS = re.compile(r"\[\s*(?:me|ме)\s*\]|```|<\|", re.IGNORECASE)

# A third person and something of theirs: «akamning yuki», «uning puli».
_THIRD_PARTY = _forms(
    """
    uning, ularning, akamning, ukamning, opamning, singlimning, do'stimning, dadamning,
    onamning, sherigimning, tanishimning, qo'shnimning, akaning, opaning, akasining,
    opasining, ukasining, singlisining, do'stining, otasining, onasining,
    xotinimning, erimning, его, её, ее, их, друга, брата, сестры, знакомого, соседа
    """
)
_THEIR_THINGS = _forms(
    """
    yuk*, pul*, qarz*, buyurtma*, to'lov*, kod*, nomer*, raqam*, telefon*, manzil*,
    груз*, долг*, заказ*, номер*, телефон*, адрес*, деньг*
    """
)
_NAMED_POSSESSIVE = re.compile(
    r"(?<![\w'])[A-ZА-ЯЎҚҒҲЁ][a-zа-яўқғҳё']{2,}(?:ning|нинг)\b"
)
# «Sizning yukingiz» is the client's own; so is «Yukning».
_NOT_PEOPLE = frozenset(
    normalise_for_search(w)
    for w in (
        "siz",
        "biz",
        "sen",
        "men",
        "ular",
        "kim",
        "nima",
        "bu",
        "shu",
        "o'sha",
        "hamma",
        "har",
        "mijoz",
        "kompaniya",
        "firma",
        "mashina",
        "tovar",
        "сиз",
        "биз",
        "улар",
        "ким",
        "нима",
        "бу",
        "шу",
    )
)
# A demand («kerak», «-ing»), as opposed to a statement.
_DEMANDS = _forms(
    """
    kerak*, bo'lsin, shart, нужно, надо, должн*
    """
)
_DEMAND_ENDINGS = re.compile(r"(?:ing|ng|ngiz|sangiz|sin|ite|yte)$")
_QUESTION_TOKEN = re.compile(r"(?:mi|mikan|chi)$")


def _question_shape(raw: str, tokens: Sequence[str]) -> bool:
    if "?" in raw:
        return True
    return any(len(t) > 3 and _QUESTION_TOKEN.search(t) for t in tokens) or any(
        t == "li" or _token_in(t, QUESTION_WORDS) for t in tokens
    )


def _demand_shape(tokens: Sequence[str]) -> bool:
    return any(
        _token_in(t, _DEMANDS) or (len(t) > 4 and _DEMAND_ENDINGS.search(t))
        for t in tokens
    )


def classify_inbound(text: str) -> frozenset[Signal]:
    """The signals in a client's message that must go to the owner (§5.2
    step 1). Empty: nothing in it needs the owner, a draft may be made."""
    clean = _clean(text)
    if not clean.strip():
        return frozenset()
    words = _blank(normalise_for_search(clean), GREETINGS)
    tokens = words.split()
    found: set[Signal] = set()

    def lexicon(signal: Signal, lex: Lexicon) -> None:
        if lex.hits(words):
            found.add(signal)

    lexicon("price", _IN_PRICE)
    lexicon("price", MONEY)
    lexicon("discount", _IN_DISCOUNT)
    lexicon("payment_terms", _IN_TERMS)
    lexicon("payment_claim", _IN_PAID)
    lexicon("deadline", _IN_DEADLINE)
    lexicon("promise", _IN_PROMISE)
    lexicon("guarantee", _IN_GUARANTEE)
    lexicon("identity", _IN_IDENTITY)
    lexicon("complaint", _IN_COMPLAINT)
    lexicon("legal", _IN_LEGAL)
    lexicon("other_people", _IN_OTHER_PEOPLE)
    lexicon("injection", _IN_INJECTION)

    surface = _surface(clean)
    if (
        any(unicodedata.category(ch) == "Sc" for ch in clean)
        or amounts.money_mentions(clean)
        or amounts.money_mentions(surface)
    ):
        found.add("price")
    if _PERCENT.search(clean):
        found.add("discount")
    if _HUNDRED_PERCENT.search(clean):
        found.add("guarantee")
    timed = (
        TIME.hit(words)
        or DEADLINE.hit(words)
        or _IN_NOW.hit(words)
        or _CLOCK.search(clean) is not None
        or when.parse_time(clean) is not None
    )
    if timed and (_question_shape(clean, tokens) or _demand_shape(tokens)):
        found.add("deadline")
    if any(_token_in(t, _HOW_MUCH) for t in tokens) and not (
        MONEY.hit(words) or _PRICE_CONTEXT.hit(words)
    ):
        found.add("deadline")
    for i, token in enumerate(tokens):
        if _token_in(token, _THIRD_PARTY) and any(
            _token_in(t, _THEIR_THINGS) for t in tokens[i + 1 : i + 4]
        ):
            found.add("other_people")
    for match in _NAMED_POSSESSIVE.finditer(clean):
        stem = normalise_for_search(match.group(0))[: -len("ning")]
        if stem in _NOT_PEOPLE or _token_in(stem, _THEIR_THINGS):
            continue
        after = normalise_for_search(clean[match.end() :]).split()[:3]
        if any(_token_in(t, _THEIR_THINGS) for t in after):
            found.add("other_people")
    if _INJECTION_MARKS.search(clean):
        found.add("injection")
    return frozenset(found)


def escalation_reasons(signals: Iterable[Signal]) -> tuple[str, ...]:
    """The escalations.reasons values of the signals that escalate, in a
    stable order; injection alone escalates nothing."""
    order = (
        "price",
        "discount",
        "payment_terms",
        "deadline",
        "promise",
        "guarantee",
        "payment_claim",
        "identity",
        "complaint",
        "legal",
        "other_people",
    )
    present = set(signals)
    return tuple(s for s in order if s in present)


# --- the guard gate runs ----------------------------------------------------------------


_SCRIPTS: Final[Mapping[str, Script]] = {
    "latin": "latin",
    "cyrillic": "cyrillic",
    "russian": "russian",
}


class RedLineGuard:
    """``gate.Guard``: the deterministic check, nothing else, with the names
    and the client's script the outbox passes. The model check (JV-16) runs
    before a row is created and can only add a block."""

    def check(
        self,
        body: str,
        *,
        role: GuardRole,
        allowed_tokens: frozenset[str],
        allowed_names: tuple[str, ...] = (),
        other_names: tuple[str, ...] = (),
        client_script: str | None = None,
    ) -> GuardResult:
        return check_outbound(
            body,
            role,
            allowed_tokens,
            allowed_names=allowed_names,
            other_names=other_names,
            client_script=_SCRIPTS.get(client_script or ""),
        ).result()


GUARD: Final = RedLineGuard()


def install() -> None:
    """Register the red-line guard with the gate (``outbox`` calls it)."""
    gate.register_guard(GUARD)


__all__ = [
    "BLOCK",
    "CHECKER_VERSION",
    "ESCALATING",
    "FLAG",
    "FLAGS",
    "FLAG_LABELS",
    "GUARD",
    "MAX_CHARS",
    "PASS",
    "Flag",
    "Lexicon",
    "RedLineGuard",
    "Script",
    "Signal",
    "Verdict",
    "check_outbound",
    "classify_inbound",
    "escalation_reasons",
    "install",
    "script_of",
]
