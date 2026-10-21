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
out of it, and those verbs pass only without a TIME hit. Verb forms that
report a status the AI cannot know are refused by their endings, not one by
one: the Uzbek past, evidential, perfect, hearsay and progressive («yetdi»,
«kepti», «ortilgan», «kelgandir», «kelopti»), the cargo somewhere («yuk
Xorgosda»), the Russian past, third person and its reflexive («зашли»,
«привезут», «всё решится»); a question word shields only what follows it.
Glued particles («yo'ldaku») come off first, and an «o'» may be typed «u»
(«yuq», «тулов»). A disguised word
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
CHECKER_VERSION: Final = "redlines-3"

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


# Where an entry has «o'» (Cyrillic «ў»), a writer on a Russian keyboard or
# in a hurry types «u» («yuq», «junatdim», «тулов», «хуп»): the entry matches
# both. The mark stands in for that «o» while the entry is normalised.
_O_MARK: Final = "\x00"
_LOOSE: Final = re.compile(r"(?<![a-z0-9\x00])-+|-+(?![a-z0-9\x00])")


def _marked(word: str) -> str:
    """``word`` normalised like ``normalise_for_search``, every «o'» marked."""
    text = to_latin(fold_apostrophes(unicodedata.normalize("NFKC", word)).casefold())
    text = text.replace("o'", _O_MARK).replace("'", "")
    text = re.sub(r"[^a-z0-9\x00-]+", " ", text)
    text = _LOOSE.sub(" ", text)
    return text.translate(_FOLD)


_FOLD: Final = str.maketrans({"q": "k", "w": "v"})


def _o_variants(norm: str) -> tuple[str, ...]:
    """A marked form as plain text: with «o», and with «u» where it was «o'»."""
    plain = norm.replace(_O_MARK, "o")
    typed = norm.replace(_O_MARK, "u")
    return (plain,) if typed == plain else (plain, typed)


def _words(raw: str) -> frozenset[str]:
    """Exact word forms (a lexicon block), normalised, with «o'» also as
    «u»."""
    return frozenset(
        form
        for word in _entries(raw)
        for form in _o_variants(_marked(word).strip())
        if form
    )


def _word_pattern(word: str) -> str:
    """One entry word as a regex. Uzbek writers swap «h» and «x» («xafta»,
    «hammasi»/«xammasi»), so either letter matches both; an «o'» may be
    typed «u»."""
    prefix = word.endswith("*")
    norm = _marked(word.rstrip("*"))
    piece = r"\s+".join(
        "[ou]".join(_ye(part) for part in token.split(_O_MARK)) for token in norm.split()
    )
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
        if not self.hit(words):
            return []
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
    часам, часу, часик*, полчаса, минут*, секунд*, недел*, месяц*, год, года, году,
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
    не придётся ждать,
    navro'z*, navruz*, навруз*, hayit*, ramazon*, рамазан*, qurbon*, курбан*,
    shomdan, shomgacha, shom payti, xufton*, asrdan, asrgacha, bomdod*, pishin*,
    iftor*, saharlik*, namoz*, намаз*, choshgoh*, ishdan keyin, ishdan so'ng,
    ishdan chiqib, sal o'tib, sal keyin, salgina, hadem*, vaqt ichida, fursat*,
    qorong'i*, tuni, kelasi hafta*, kelasi hapta*, kelasi oy*, kelasi yil*,
    keyingi hafta, ertege, ertalap*, kechqurin*, saat*,
    daqqa*, sekunt*, hapta*, oyni, oxirida, oxiriga, oxirlari*, oy boshi*,
    hafta boshi*, oktabr*, sentabr*, noabr*, shamba*, yakshamba*, dushamba*,
    seshamba*, chorshamba*, payshamba*, peyshanba*, panshanba*, charshanba*,
    charshamba*, juma*, zaftr*, zavtr*, sevodnya, sivodnya, sodnya, syodnya, sednya,
    сёдня, седня, ща, счас, тотчас, немедля, по-быстрому, пораньше, попозже, вот-вот,
    послезавтр*, за ночь, полнедел*, полмесяц*, полгода, полсуток, ненадолго, ужин*,
    вечерк*, вечерочк*, после, числ*, bystr*, bistr*, tezlashtir*, chaqqon*, nafasda,
    ko'z ochib*, to'ydan, to'yda, to'ygacha, to'yidan, ovqatdan, bir zum*,
    до закрытия, займёт, займет, вскорости
    """,
    english="""
    today, tomorrow, tonight, yesterday, morning, evening, noon, week*, month*, hour*,
    minute*, day, days, asap, soon, deadline*, monday, tuesday, wednesday, thursday,
    friday, saturday, sunday, on time, right away, immediately, afternoon, eod,
    shortly, quickly, overnight, tonite, tmrw, tmr, in no time, take long, not long,
    wait long, before you know it, close of business, cob, take ages, ages, wont take
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
    точно, обязательно, наверняка, безусловно, без проблем, нет проблем,
    не переживайте, не волнуйтесь, не беспокойтесь, уверяю, ручаюсь, железно,
    стопроцентно, сто процентов, можете не сомневаться, не сомневайтесь, без вопросов,
    можете быть уверены, уверен*, не подвед*, albatda, albatt*,
    hammasi joyida, hammasi nazoratda, nazoratda, qasam*, vallohi, valloh, xudo haqi,
    даю слово, слово даю, честное слово, клянусь, гарантир*, bemalol, gap yo'q,
    hech gap emas, muammo emas, problem yo'q, problema yo'q, masala yo'q, qiyin emas,
    bo'ladigan ish, hammasi tartibda, hammasi normal, hammasi chotki, shubha yo'q,
    shubha qilmang, shubhalanmang, mutlaqo, so'zsiz, ishonchimiz komil, ishonchim bor,
    qo'rqmang, bezovta bo'lmang, hayajonlanmang, asabiylashmang, однозначно,
    непременно, будьте спокойны, под контролем, в порядке, всё нормально,
    все нормально, не бойтесь, положиться, надёжно, надежно, зуб даю, не вопрос,
    разумеется, естественно, само собой, без сомнени*,
    ko'nglingiz*, xavotir*, bexavotir*, tashvish*, qayg'ur*, siqilma*, cho'chima*,
    bosh qotirma*, xatirjam*, xotirjem*, xatirjem*, bexavf*, xavfsiz*, omonat*,
    omon-omon, nazorat*, ishonib*, ishona*, ishonchiz*, ishonchingiz komil,
    suyanaver*, suyaning, hechqisi*, hech qisi*, gapyo'q, gap yo'q*, muammo* yo'q,
    muammo* yo'k, problem* yo'q, shubha* yo'q, hojat yo'q, o'rin yo'q, sabab yo'q,
    qiyin joyi*, o'z joyida, joyida, joy-joyida, a'lo darajada, hammasi zo'r,
    hammasi yaxshi, hammasi bor, nazorat ostida, so'zim so'z,
    erkak so'zi*, gapim gap, aniq gap, gumon qilma*, albat*, albet*, tochna, tochno,
    abizatel*, abyazatel*, obezatel*, obizatel*, obyazatel*, bazaru net,
    bez bazara, без базара, базару нет, без б, stopudov*, стопудов*, puchkom, пучком,
    chiki-puki, чики-пуки, tip-top, тип-топ, ne parsya, не парься, не парьтесь,
    ne kipishuy, не кипишуй, ne volnuysya, не волнуйся, волнова*, ne bespokoysya,
    не беспокойся, chestno, честно, схвачено, на мази, чётко, четко, в ажуре, железоб*,
    как часы, будь спок, спокойн*, пережива*, в безопасност*, надёжн*, надежн*,
    проблем нет, никаких проблем, не проблема, вопросов нет, не страшно,
    без задерж*, расслаб*, tinchlan*, tinch bo'lsin, g'am yema*, tashvish tort*,
    yaxshi xabar*, xushxabar*, quvonarli*, ijobiy*, yoqadigan, хорош* новост*,
    обрад*, порад*, доволь*, пожалеете, останетесь, yo'qolgan*, yo'qolmagan*,
    yo'qolishi*, yo'qoladi, yo'qolmaydi, yo'qoldi, norozi*, afsuslan*,
    hech qachon, hamisha*, mamnun, butun, butun-butun, odatiy*, zarar*, qopla*,
    javobgar*, mas'uliyat*, zimma*, bizni bilasiz*, oson, mumkin emas, mutlaq*,
    ko'z qorachig'*, подвод*, опаздыва*, жаловал*, не обидим, по-честному,
    не задерж*, sinmagan, singan, buzilmagan, kafil*
    """,
    english="""
    guarante*, sure, surely, definitely, certainly, of course, no problem*,
    no worries, dont worry, trust me, insured, absolutely, no doubt, rest assured,
    count on, covered, its fine, safe, nothing to worry, worry, been late,
    clockwork
    """,
)

# RL1 — a promise, an agreement, a commitment or a status claim the AI
# cannot know («jo'natildi»). The first-person future forms of every verb
# are caught by the backstop below; these are the rest.
PROMISE = Lexicon.of(
    "promise",
    """
    va'da*, so'z beraman, so'z beramiz, kelishdik, kelishamiz, kelishildi, kelishaylik,
    kelishib olamiz, mayli, xo'p, xop, hop, ha, xa, bo'p, да, ага, угу,
    постара*, ok, okey, okay, bo'ldi, bo'pti,
    bo'p qoladi, bo'lib qoladi, hal qil*, hal bo'l*, yuboraman, yubordim,
    yubordik, yuborildi, yo'lda, jo'natdim, jo'natdik, jo'natildi, tashladim, tashladik,
    yetkazdim, yetkazdik, yetkazildi, yuklandi, yuklatdim, yetib keldi, yetib bordi,
    qildim, qildik, qilindi, qo'ydim, qo'ydik, jo'nadi, chiqdi, ketdi, обещ*,
    договорились, договоримся, по рукам, ладно, хорошо, конечно, окей, ок,
    сделаем, сделаю, сделано, отправлю, отправим, отправлено, отправил, отправили,
    отправлен, отправлена, отправлены, загружен*, вышлю, вышлем, пришлю, пришлём, скину,
    скинем, скинул, привезу, привезём, привезем, доставим, доставлю, доставлен*,
    доставят, отгрузим, отгружу, отгружено, верну, вернём, вернем, переведу, переведём,
    посчитаю, посчитаем, подготовлю, подготовим, оформлю, оформим, решим, решу, будем,
    буду, приеду, приедем, прибудет, придет, придёт, дойдет, дойдёт, получите,
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
    растаможим, растаможу, растаможили, растаможил, растаможен*, растаможат,
    выпустили, оформили, отгрузили, привезли, доставили, сдали, передали,
    выдали, передадим, сдадим,
    bo'lsin, ma'qul*, rozi, davay, давай, давайте, kanechn*, kaneshn*, konechn*,
    xarasho, xorosho, harasho, prinyal*, oke, okei, kk, dagavaril*, dogovoril*,
    верно, правильно, так и есть, именно так, замётано, bo'lar, bo'laveradi,
    iloji bor, imkoni bor, imkon bor, shekilli, bo'lishi kerak, bo'lishi mumkin,
    mo'ljal*, reja bo'yicha, po grafiku, по графику, keyingi mashina*, navbatdagi*,
    keyingi partiya*, keyingi reys*, следующ* машин*, следующ* отправк*,
    следующ* рейс*, первой же, первой машин*, первой загрузк*, экспресс*, на себя,
    лично, прослеж*, позабоч*, позабот*, подстрах*, застраху*, страху*,
    bizga qo'yib*, biz tomondan, qolgani*, bizning ishimiz, kelib olaver*,
    olib ketaver*, kelib oling, olib ketishingiz, забира*, заберите,
    можете забрать, к дому, до дома, прямо домой, домой, до двери, uyingizga*,
    shu jumladan, ham ichida, haqi ichida, narxi ichida, tamojnya ichida,
    bilan birga, ustiga hech*, от нас, учтен*, учтён*, решён, решено, решена,
    будут, будете, еду, везу, беру, дадим, дадут, возьму, решаем*, to'g'ri,
    zakinu, vozmu, ничего не меняется
    """,
    english="""
    promise*, will, shall, gonna, we can, i can, deliver*, sent, shipped,
    on the way, agreed, deal, done, ready, okay, yes, yep, yeah, yup, well, ill, itll,
    on it, handle*, arriv*, coming, came in, got it, its in, landed, showing,
    cleared, left the, should be
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
    kelgan, ketgan, chiqqan, пришёл, пришел, дошёл, дошел, на месте,
    tayyor, tayyorlab*, tayyorlan*, tayyorlay*, tayyorlad*, готов*, уже, allaqachon,
    allakachon, bizdan, почти, близко
    """,
    english="""
    ship*
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
    sotka*, сотк*, косар*, лям, ляма, лямов, тыща, тыщу, тыщ, полтос*,
    haq, haqi, kira haqi, kirasi, yevro*, euro*, dolar*, zelyon*, зелён*, зелен*,
    babk*, бабк*, babl*, бабл*, lave, лавэ, лаве, evrik*, еврик*, rubas*, рубас*,
    по-божески, po-bojeski, по-братски, po-bratski, разорит*, переплач*, доплат*,
    доплач*, вдвое, втрое, вполцен*, средств*, бюджет*, deshov*, deshev*, nedarag*,
    nedorag*, darog*, cho'ntag*, arzimagan, prosent*, pratsent*, prasent*, лямчик*,
    тыщи, тысчонк*, стольник*, чирик*, червон*, четвертак*, соточк*, косых
    """,
    english="""
    price*, cost*, cheap*, expensive, free, cent, cents, fee, fees, tariff*, bucks,
    buck, grand, quid, affordable
    """,
)

# RL2 — «the same as last time», «as agreed», «as usual»: a price or terms
# stated by reference. Read in drafts only (a client may well say it).
SAME_AS = Lexicon.of(
    "price",
    """
    avvalgi*, o'tgan safar*, oldingi safar*, o'sha-o'sha, o'shancha, o'shanaqa,
    shuncha, bir xil, o'zgarish yo'q, eski*, odatdagi*, в прошлый раз, прошл* раз,
    как обычно, как всегда, как раньше, по-прежнему, по-старому, прежн*, те же,
    та же, тот же, то же, столько же, без изменений, в силе
    """,
    english="""
    same as, same rate, like before, as before, as usual, last time, as we discussed,
    as discussed, as agreed
    """,
)

DISCOUNT = Lexicon.of(
    "discount",
    """
    arzonlashtir*, arzonroq, kamaytir*, narxni tushir*, narxini tushir*, aksiya*,
    bonus*, promokod*, промокод*, sovg'a*, подар*, уступ*, торг*, поторг*, подешевле,
    дешевле, акци*, бонус*, maxsus taklif*, maxsus narx*, imtiyoz*, keshbek*,
    кешбэк*, кэшбэк*, скидочк*, спеццен*, спецпредлож*, льгот*, распродаж*,
    skidon*, скидон*, yengillik*, siz uchun maxsus, siz uchun alohida, sizga alohida,
    sizga maxsus, alohida narx*, doimiy mijoz*, e'tibor boshqa*
    """,
    english="""
    discount*, sale, promo*, special offer, cashback, bargain, markdown, reduced
    """,
)

PAYMENT_TERMS = Lexicon.of(
    "payment_terms",
    """
    to'la*, to'lash*, to'lang, nasiya*, qarz*, bo'lib to'la*,
    oldindan to'lov*, avans*, predoplat*, naqd*, karta*, kartochk*, hisob raqam*,
    perechisl*, click, payme, uzcard, humo, paynet, предоплат*, рассроч*, в долг, долг,
    долга, долгу, наличн*, наличк*, nalichka*, нал, безнал*, карт, карта, карту, картой,
    карте, карты, перевод*, перечисл*, реквизит*, счет, счёт, oldindan, o'tkazing,
    o'tkazasiz, o'tkazsangiz, hisobimga, hisobimizga, hisobga, hisobingizga,
    hisob-kitob*, hisob kitob*, bo'lib-bo'lib, qismlarga, qism-qism, balans*,
    hisobimizda, hisobingizda, plastik*, долгов, долги, долгами,
    платеж*, платёж*, расчет*, расчёт*, рассчита*, сочтёмся, сочтемся, частям*,
    частями, пополам, по факту,
    kesh, keshda, keshem, кэш*, usdt, kripta*, крипт*, terminal orqali, kaspi*,
    vestern*, western*, zolotaya korona, alipay, vechat, apelsin orqali, peyme*,
    peymi*, пейми*, пейме*, klik*, клик*, uzkard*, узкард*, kartichk*, карточк*,
    nasya*, predaplat*, pridoplat*, pridaplat*, zaklad*, заклад*, nalom, налом,
    nalichn*, keyin bersangiz, keyinroq bersangiz, kelgach bersangiz, bersangiz ham,
    kelganda, avval tashla*, oldin tashla*, oldin to'la*, avval to'la*, tashlasangiz,
    avvaldan, bir qismi*, keyin hisob*, hisobda, eski hisob*, расплат*, отдадите,
    частичн*, вперёд, вперед, реализац*, отсроч*, потом бабки, сначала груз,
    keyin to'la*
    """,
    english="""
    prepay*, cash, card, invoice*, installment*, payment*, pay, transfer*, upfront,
    up front, deposit*, settle*, on arrival, on delivery, wechat, pay later
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
    дошёл, упали, упало, на счету, подтвержден*, подтвердили,
    hammasi to'g'ri, skrin*, скрин*, to'liq ekan, kam emas, ortig'i bilan, yetarli,
    kifoya, hisob to'g'ri, hisobingiz toza, hisob yopiq, видно, видим,
    средства у нас, ko'rinib turibdi, ko'rinib qoldi, kirdi, kiribdi, kepti
    """,
    english="""
    paid, received, money arrived, funds, credited, came through, money came,
    it all came, got it all
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
    bemalol murojaat*, xayrli oqshom*, ertalabingiz xayrli*, kechingiz xayrli*,
    oqshomingiz xayrli*, kuningiz xayrli bo'lsin, xayrli bo'lsin, muborak bo'lsin,
    yangi yilingiz bilan, yangi yil muborak, yangi yilingiz muborak,
    bayramingiz muborak, bayramingiz bilan, hayitingiz muborak, ramazon muborak,
    juma kuningiz muborak, с праздником, с наступающим, с новым годом,
    хороших выходных, приятных выходных, будем рады помочь, рады помочь,
    ma'lumot yetarli, ma'lumotlar yetarli, для расчёта, для расчета,
    yaxshi o'tsin, omad yor bo'lsin, rozi bo'lsin, baraka topsin, baraka bersin,
    salomat bo'lsin
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
        денег, деньгам, деньгами, деньгах, stavk*, ставк*, расценк*, rate*
        """,
    "deadline": """
        muddat*, srok*, срок*
        """,
    "payment_terms": """
        to'lov*, оплат*, shartlar*, shartlari*
        """,
    "discount": """
        chegirma*, chegrima*, skidk*, скидк*, скидочк*
        """,
    "guarantee": """
        kafolat*, kapolat*, garant*, gorant*, гарант*
        """,
    "promise": """
        kelishuv*, shartnoma*, kontrakt*, контракт*, договор*, dogovor*
        """,
}
# Insurance is a guarantee to defer like the others, and a detail a draft may
# ask about («Нужна ли страховка груза?»).
INSURANCE: Final = Lexicon.of("guarantee", "sug'urta*, страхов*, застрахов*")
TOPICS: Final[tuple[tuple[Flag, Lexicon], ...]] = (
    *((flag, Lexicon.of(flag, raw)) for flag, raw in _TOPIC_RAW.items()),
    ("guarantee", INSURANCE),
)

# The words that make a topic the subject of a deferral.
_REGARDING_AFTER = _words(
    """
    bo'yicha, haqida, masalasida, borasida, xususida, yuzasidan
    """
)
_REGARDING_BEFORE = _words(
    """
    по, о, об, насчёт, насчет, касательно, про, относительно
    """
)
# What may stand between a topic and its stall verb: «narxini ham hozir
# aniqlashtirib».
_FILLERS = _words(
    """
    ham, hozir, buni, uni, yana, bir, o'zim, o'zimiz, sizga, sizlarga, всё, все,
    сейчас, это, сам, сами, вам
    """
)


def _forms(raw: str) -> tuple[tuple[str, bool], ...]:
    """(normalised form, is a prefix) per entry, for single-token matching."""
    out = []
    for entry in _entries(raw):
        prefix = entry.endswith("*")
        for norm in _o_variants(_marked(entry.rstrip("*")).strip()):
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
    xabar, javob, javobi*, aniqlab, aniqlashtirib, bilib, tekshirib, so'rab, qarab,
    ko'rib, o'rganib, surishtirib
    """
)
# «yozib qo'yaman», «aytib qo'yaman» defer only once the finding out is said
# («Aniqlab, yozib qo'yaman»): alone («Narxini aytib qo'yaman») they promise
# to tell.
WEAK_LEADS = _forms(
    """
    yozib, aytib
    """
)
# Auxiliaries: the verb they follow decides what they commit to.
AUXILIARIES = _forms(
    """
    beraman, beramiz, qo'yaman, qo'yamiz, olaman, olamiz, ko'raman, ko'ramiz, chiqaman,
    chiqamiz, qolaman, qilaman, beray, ko'ray, qo'yay, chiqay, beraylik, ko'raylik
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
    думаю, жду, ждем, ждём, благодарю, благодарим, извиняюсь, прошу, рад, рады,
    совсем, всем, затем, зачем, причем, причём, xursandman, xursandmiz, mamnunman,
    mamnunmiz, minnatdorman, minnatdormiz, shodman, dushman, karman, roman, obman,
    tushundim, tushundik, думаем, желаем, желаю, qalaysiz, xizmatingizdamiz,
    tushunmadim, tushunmadik, слушаю, слушаем, работаем, занимаемся, rahmat
    """
)
# Words that make a clause a real question («qayerdan», «qaysi», «какой»).
QUESTION_WORDS = _forms(
    """
    qayer*, qaysi*, nima*, qanday, qanaqa*, kim*, necha*, qancha*, qachon*, nega,
    откуда, куда, где, какой, какая, какое, какие, какую, сколько, что, кто, чем, когда,
    как, каком, какого, каких, почему, зачем, where, what, which, who, when, how, why
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
    # The Fergana «-opmiz», the colloquial «-voman»/«-vomiz», the Khorezm
    # future «-jakmiz», and the first person's «if»: «yuborsak» (an offer,
    # «shall we send it?»), «yuborsam».
    r"|op(?:miz|mz)$"
    r"|vo(?:man|miz)$"
    r"|.{2,}jak(?:man|miz)?$"
    r"|.{2,}(?:sak|sam)(?:mi|chi)?$"
)
# The contracted first-person future «yuboram», «qilam», «olib kelam»; not
# in Russian, where «по срокам» ends the same way.
_UZ_CONTRACTED = re.compile(r"^.{2,}(?:[rltnz]|sh)am$")
_NOT_CONTRACTED = _words(
    """
    muhtaram, bayram, dadam, ilham, islam, qadam, xolam, bolam, madam, salam, program,
    akam, opam, onam, ukam, olam, kelajak
    """
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
    # The colloquial progressives «kelyotti», «kelopti», «keyopti»,
    # «kelayotir», «kelayotgan».
    r"|(?:yot|op)(?:ti|di|ir)$|yotgan$"
)

# Status words: what a draft says has already happened, which the AI cannot
# know («yetdi», «ketti», «kepti», «kelibdi», «ortilgan», «kelgandir»).
# Uzbek, read outside Russian text: the past in «-di» and its assimilated
# «-ti» («ketti», «chiqti», «tushti»), the evidential «-ibdi»/«-ipti», the
# first person's past «-dim».
_UZ_PAST = re.compile(r"^(?=[a-z-]{4,}$).*(?:di|(?:[io]k|[pt]|sh|ch)ti)(?:m|lar)?$")
_NOT_PAST = _words(
    """
    endi, edi, idi, hindi, kerakdi
    """
)
# The perfect «-gan» as the predicate («yuk ortilgan», «kelgan edi»), not as
# an attribute («qadoqlangan holda»).
_UZ_PERFECT = re.compile(r"^(?=[a-z]{4,}$).*(?:gan|kan)$")
_AFTER_PERFECT = _words(
    """
    bo'lishi, edi, ekan, emish, emas, ekanku
    """
)
_NOT_PERFECT = _words(
    """
    joylashgan, ixtisoslashgan, topgan, organ
    """
)
# The third person's «let it»: «yuk kelsin», «bo'lsin».
_UZ_JUSSIVE = re.compile(r"^(?=[a-z]{5,}$).*sin(?:lar)?$")
# Particles glued to a word: «kelishdikku», «yo'ldaku», «bilasiz-ku»,
# «tushibdiyu»; the probability «-dir» and the hearsay «-mish»
# («kelgandir», «kelganmish», «kelarmish»). The word is read without them.
_GLUED: Final = (
    re.compile(r"([a-z]{2,}(?:di|ti|dik|gan|kan|da|ta|siz|miz|man|adi|ydi|moqda))-?ku"),
    re.compile(r"([a-z]{2,}(?:di|ti))yu"),
    re.compile(r"([a-z]{2,}(?:gan|kan|da|ta|ar|ir|ur|dek))(?:dir|mish)"),
)
# «tushgan-tushmaganini» (whether or not it arrived) asks, it states nothing.
_WHETHER = re.compile(r"(?<![a-z0-9-])[a-z]+[gk]an-[a-z]+ma[gk]an[a-z]*(?![a-z0-9-])")
# «-dek» (like): «avvalgidek», «kelishilgandek», «aytganimdek» state a price
# or terms by reference.
_LIKE = re.compile(r"^(?=[a-z-]{5,}$).*dek$")
# Time clauses: «kun botmasdan» (before), «majlisdan chiqqach» (after),
# «yuk kelganda» (when).
_BEFORE_DOING = re.compile(r"^(?=[a-z]{7,}$).*masdan$")
_AFTER_DOING = re.compile(r"^(?=[a-z]{5,}$).*(?:gach|kach)$|^(?=[a-z]{6,}$).*[gk]anda$")
_AFTER_WORDS = _words(
    """
    keyin, kein, so'ng, oldin, burun, avval
    """
)
_NOT_AFTER = _words(
    """
    shundan, undan, bundan, ulardan, bizdan, sizdan
    """
)
_UNTIL = re.compile(r"(?:gacha|[gk]u(?:n|nim|nimiz|ningiz|ni|nlari)cha)$")
# The cargo, the truck or the papers as the subject of a place («yuk
# Toshkentda», «yukingiz bizda»): a status claim.
_CARGO = re.compile(
    r"^(?:yuk|mashina|fura|tovar|konteyner|kontayner|haydovchi|posilka|buyurtma"
    r"|mahsulot|hujjat|deklaratsiya|partiya|zakaz)(?:lar)?"
    r"(?:i|im|imiz|ingiz|ngiz|miz|si|ri|lari|laringiz)?$"
)
_NOT_LOCATIVE = _words(
    """
    haqida, xususida, borasida, masalasida, holda, tarzda, juda, orada, ichida,
    shunda, unda, bunda, aslida, hozirda, vaqtda
    """
)
_RU_CARGO = _words(
    """
    груз, грузы, машина, машины, фура, фуры, товар, посылка, контейнер, документы,
    документ
    """
)
_RU_PLACE = _words(
    """
    в, во, на, у
    """
)
# A price per unit stated, not asked: «kilosi elik», «kubi saksan».
_PER_UNIT = re.compile(
    r"^(?:kilo|kg|kub|kubometr|tonna|dona|kuti|metr)(?:si|i)(?:ga|dan|ni|da)?$"
)
PER_UNIT = Lexicon.of(
    "price",
    """
    kub hisobida, kilo hisobida, za kilo, za kg, za kub, za mesto, za korobku,
    za shtuku, za tonnu, za meshok, за кило, за кг, за куб, за место, за коробку,
    за штуку, за тонну, за мешок
    """,
    english="""
    per kg, per kilo, per cbm, a kilo
    """,
)
_SO_FAR = frozenset({"hozirgacha", "shugacha", "hanuzgacha"})
# Russian verb prefixes; «вы-» is «vi» in the word view.
_RU_PREFIX: Final = (
    r"(?:s|so|po|pri|ot|pere|vy|vi|za|do|pod|raz|ras|na|pro|ob|obe|u|v|vo|voz|vos|is"
    r"|iz)"
)
# The Russian first person singular of a perfective verb («отправлю»,
# «организую», «найду», «помогу», «закажу», «обеспечу»), in any script: in
# an Uzbek sentence too («Ака, сам отвезу») and typed in Latin («zakinu»).
_RU_COMMIT = re.compile(
    r"^(?=[a-z]{5,}$)(?:(?!.*iyu$).*(?:lyu|ayu|eyu|nyu|ryu|vyu|ruyu|zuyu|xuyu|oyu)"
    rf"|{_RU_PREFIX}[a-z]+(?:[zdlnrtgkj]|zh|[aeiou]ch|sh)u)$"
)
# Nouns those endings also fit: «доставку», «дорогу», «программу».
_RU_NOUN_U = re.compile(r"(?:[vzstpln]k|chk|shk|mm|cht|sch)u$")
_NOT_RU_COMMIT = _words(
    """
    дорогу, погоду, страну, почту, базу, награду, встречу, продажу, задачу, работу,
    систему, посуду, одежду, воду, сумму
    """
)
# The plural («выдадим», «выгрузим»), read in Russian only: «savolim» is
# Uzbek for «my question».
_RU_COMMIT_PL = re.compile(rf"^(?=[a-z]{{5,}}$){_RU_PREFIX}[a-z]{{2,}}(?:em|im|yom)$")
# The Russian first-person plural («организуем», «довезём», «перешлём»,
# «поможем», «разберёмся») in any script: no Uzbek word ends this way, so it
# is read in Uzbek text too («Ака, организуем») and in Russian typed in
# Latin («Aka, zakinem»).
_RU_WE = re.compile(
    r"^(?=[a-z]{6,}$)[a-z]{3,}(?:uem|aem|yaem|iem|zem|nem|yom|shlem|zhem|jem)$"
    r"|^(?=[a-z]{6,}$)[a-z]{3,}(?:em|im|yom)sya$"
)
# Russian, read in Russian text: the past («пересекла», «зашли», «тронулась»),
# the third person («привезут», «пропустит», «ждёт»), its reflexive («всё
# решится», «отгружается»), and the short passive («оформлен», «упакован»).
_RU_PAST = re.compile(r"^(?=[a-z]{4,}$).*(?:[aeiouy]l|l[aoi])(?:sya|s)?$")
_RU_THIRD = re.compile(r"^(?=[a-z]{5,}$).*(?:yot|et|it|ut|yut|[^i]yat|at)$")
_RU_THIRD_MIXED = re.compile(r"^(?=[a-z]{6,}$).*(?:yot|et|ut|yut)$")
_RU_REFLEXIVE = re.compile(r"(?:etsya|itsya|yotsya|utsya|yutsya|yatsya)$")
_RU_PARTICIPLE = re.compile(
    rf"^(?=[a-z]{{6,}}$)(?:o|op|{_RU_PREFIX})[a-z]{{2,}}(?:ovan|en|yon|an)[aoi]?$"
)
_NOT_RU_VERB = _words(
    """
    понял, поняла, поняли, или, если, сколько, столько, только, несколько, около,
    кило, стекло, число, дело, масло, мало, детали, материал, канал, сигнал, вокзал,
    персонал, терминал, портал, капитал, журнал, оригинал, мебель, отель, цель,
    модель, отдел, панель, текстиль, автомобиль, автомобили, профиль, стиль, стол,
    пол, футбол, контроль, стул, модуль, кресло, привет, ответ, совет, пакет, бюджет,
    предмет, объект, маршрут, транзит, кредит, лимит, депозит, визит, интернет, билет,
    цвет, свет, секрет, самолёт, самолет, отчёт, отчет, счёт, счет, учёт, учет, полёт,
    институт, валют, салют, минут, результат, формат, адресат, автомат, брат, салат,
    опять, работ, забот, находится, находятся, является, являются, называется,
    кажется, нравится, требуется, занимается, узбекистан, казахстан, кыргызстан,
    таджикистан, туркменистан, стакан, ресторан, магнит, кабинет, предмет, расчет,
    расчёт, квадрат, аппарат, пункт, банкомат, дела, написали, написал, обратились,
    обратился, связались
    """
)
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
    # Typed without the apostrophe or the doubled letter, «o'» as «u», «q» as
    # «k», and the dialect forms: «tort», «elik», «kirk», «tuqson», «uttiz».
    r"|tort(?:ta\w*|dan|ga|da)?|elik\w*|altmish\w*|saksan\w*|tuq?son\w*|tukson\w*"
    r"|kirk\w*|yigrma\w*|igirma\w*|uttiz\w*|sakiz\w*|tu[qk]{1,2}iz\w*|iki|ikita\w*"
    r"|alti|altita\w*|yeti|yetita\w*|unta\w*|bittadan|chorak\w*"
    # Russian: the ordinals of a date («седьмого»), the forms «двум»,
    # «тремя», «одна», the slang counts («семёрка», «двушка») and «пол-»
    # (half) before anything («пол-ляма»).
    r"|perv(?:ogo|omu|oe|aya|uyu)|vtor(?:ogo|omu|oe|aya|uyu)|tret\w*"
    r"|chetv[yo]?ert\w*|chetvyort\w*|pyat(?:ogo|omu)|shest(?:ogo|omu)|sedm\w*"
    r"|vosm\w*|dvum\w*|dvuh|tremya|trem|tryom|odin|odna|odnu|odno|odnogo|odnoy"
    r"|nul\w*|dyujin\w*|dyuzhin\w*|semyork\w*|chetvyork\w*|dvushk\w*|tryoshk\w*"
    r"|polsotn\w*|poltish\w*|polti[sh]ch\w*|tishi|tischonk\w*|pol-\w+"
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
    (SAME_AS, "price"),
)


@dataclass(frozen=True, slots=True)
class _Clause:
    words: str
    question: bool
    # The segment the question mark asks with: the sentence's last one, or the
    # last before a trailing «aka» («Qachon tayyor bo'ladi, aka?»).
    final: bool
    # A probability «-dir» or hearsay «-mish» was glued to a word: «yuk
    # kelgandir», «kelarmish» report a status at second hand.
    hedged: bool = False
    # The first segment of its sentence.
    first: bool = True


# Words of address: a segment of these alone does not end the question.
_ADDRESS = _words(
    """
    aka, opa, uka, akajon, opajon, ukajon, brat, bratan, do'stim, xonim, janob,
    aka-uka
    """
)


def _unglue(words: str) -> str:
    """``words`` with every glued particle off its word, and every «whether
    or not» («kelgan-kelmaganini») taken out."""
    words = _WHETHER.sub(" ", words)
    out = []
    for token in words.split():
        for pattern in _GLUED:
            match = pattern.fullmatch(token)
            if match is not None:
                token = match.group(1)
                break
        out.append(token)
    return " ".join(out)


def _hedging(words: str) -> bool:
    return any(_GLUED[2].fullmatch(token) for token in words.split())


def _clauses(text: str) -> list[_Clause]:
    """The segments of every sentence, each marked with whether its sentence
    is a question and whether it is the segment the question asks with."""
    out = []
    for match in _CLAUSE.finditer(text):
        raw = match.group(0)
        question = "?" in raw
        searched = [normalise_for_search(s) for s in _SEGMENT.split(raw)]
        segments = [(_unglue(w), _hedging(w)) for w in searched if w]
        final = max(
            (
                k
                for k, (w, _h) in enumerate(segments)
                if not all(t in _ADDRESS for t in w.split())
            ),
            default=-1,
        )
        for k, (words, hedged) in enumerate(segments):
            out.append(_Clause(words, question, k == final, hedged, k == 0))
    return out


def _blank(words: str, lexicon: Lexicon) -> str:
    for _entry, (start, end) in lexicon.hits(words):
        words = words[:start] + " " * (end - start) + words[end:]
    return words


def _asked_from(clause: _Clause, tokens: Sequence[str]) -> int | None:
    """The index of the question word a question asks with («Qaysi shahardan
    jo'natiladi?»): what follows it is asked, not said. None in a statement,
    and in a segment the question does not end («Когда груз будет у вас, кто
    его примет?» says the cargo will be there)."""
    if not (clause.question and clause.final):
        return None
    return next((i for i, t in enumerate(tokens) if _token_in(t, QUESTION_WORDS)), None)


def _token_at(words: str, start: int) -> int:
    """The index of the token a lexicon hit at ``start`` begins in."""
    return len(words[:start].split())


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
    if _stall_verb(before):
        return True
    j = _past_fillers(tokens, i + 1)
    if _stall_at(tokens, j):
        return True
    if j < len(tokens) and tokens[j] in _REGARDING_AFTER:
        return _stall_at(tokens, _past_fillers(tokens, j + 1))
    return False


# A stall verb's past or passive is a report, not a deferral: «tekshirdim»
# (I checked), «aniqlandi» (it was found), «уточнил».
_DONE = re.compile(
    r"(?:di|ti)(?:m|k|ngiz|lar)?$|[gk]an$|[aeiouy](?:l|la|lo|li)$|(?:en|yon)[aoi]?$"
)


def _stall_verb(token: str) -> bool:
    return _token_in(token, STALL_VERBS) and _DONE.search(token) is None


def _committing(
    tokens: list[str], i: int, *, script: Script | None, primed: bool = False
) -> bool:
    """Is token ``i`` a first-person commitment the backstop refuses? The
    Russian plural is read only in Russian text and the contracted «-am» only
    outside it: «savolim» is Uzbek for «my question», «по срокам» Russian
    for «as to the terms»."""
    token = tokens[i]
    russian = script == "russian"
    if not (
        _UZ_COMMIT.search(token)
        or _RU_WE.search(token)
        or (
            _RU_COMMIT.search(token)
            and not _RU_NOUN_U.search(token)
            and token not in _NOT_RU_COMMIT
        )
        or (russian and _RU_COMMIT_PL.search(token))
        or (not russian and _UZ_CONTRACTED.search(token) and token not in _NOT_CONTRACTED)
    ):
        return False
    if len(token) < 5:
        return False
    pair = " ".join(tokens[max(i - 1, 0) : i + 1])
    if _token_in(token, NEUTRAL_VERBS) or _token_in(pair, NEUTRAL_VERBS):
        return False
    if _stall_verb(token):
        return False
    if _token_in(token, AUXILIARIES):
        return not _led(tokens, i, primed=primed)
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


def _status(tokens: list[str], i: int, *, script: Script | None) -> str | None:
    """A verb form that says what has already happened or is happening, or
    what someone else will do: the AI cannot know it. The hit code, or None."""
    token = tokens[i]
    if _token_in(token, NEUTRAL_VERBS):
        return None
    after = tokens[i + 1] if i + 1 < len(tokens) else ""
    before = tokens[i - 1] if i > 0 else ""
    if script == "russian":
        if token in _NOT_RU_VERB or _stall_verb(token):
            return None
        if _RU_PAST.search(token):
            return "status:ru_past"
        if _RU_REFLEXIVE.search(token):
            return "status:ru_reflexive"
        if _RU_THIRD.search(token):
            return "status:ru_third"
        if _RU_PARTICIPLE.search(token) and not token.endswith("stan"):
            return "status:ru_passive"
        return None
    if (
        script == "cyrillic"
        and _RU_THIRD_MIXED.search(token)
        and token not in _NOT_RU_VERB
    ):
        return "status:ru_third"
    # «Savolingizni oldim» (I got your question) reports the message.
    reported = token.endswith(("dim", "tim")) and _token_in(before, _MESSAGES)
    if _UZ_PAST.search(token) and token not in _NOT_PAST and not reported:
        return "status:past"
    if (
        _UZ_PERFECT.search(token)
        and token not in _NOT_PERFECT
        and (not after or after in _AFTER_PERFECT)
    ):
        return "status:perfect"
    if _UZ_JUSSIVE.search(token):
        return "status:jussive"
    return None


# What «oldim» (I got) may report getting: the client's message.
_MESSAGES = _forms(
    """
    savol*, xabar*, murojaat*, so'rov*, ma'lumot*, yozgan*, gapingiz*
    """
)


def _until(token: str) -> bool:
    """«uyingizgacha», «oxirigacha», «ertagacha», «tugaguncha»,
    «bo'lgunimcha»: until a time or as far as a place, a deadline or a
    delivery either way. «hozirgacha» (so far) is neither, nor is the
    farewell «ko'rishguncha»."""
    if len(token) <= 6 or token in _SO_FAR:
        return False
    return _UNTIL.search(token) is not None and not token.startswith(
        ("korishgun", "korishga")
    )


def _time_clause(tokens: list[str], i: int) -> bool:
    """«kun botmasdan» (before), «majlisdan chiqqach» (after), «yuk kelganda»
    (when), «ishdan keyin», «hayitdan so'ng» (after a time or an event)."""
    token = tokens[i]
    if _BEFORE_DOING.search(token) or _AFTER_DOING.search(token):
        return True
    after = tokens[i + 1] if i + 1 < len(tokens) else ""
    return (
        len(token) >= 5
        and token.endswith(("dan", "tan", "nan"))
        and after in _AFTER_WORDS
        and token not in _NOT_AFTER
    )


def _led(tokens: list[str], i: int, *, primed: bool = False) -> bool:
    """Does the auxiliary at ``i`` follow a stall lead («xabar beraman»,
    «aniqlab beraman»)? Not «javob beramiz» for the cargo («yuk uchun biz
    javob beramiz»: we answer for it), nor «qarab beramiz» for someone
    («siz uchun alohida qarab beramiz»). A weak lead («yozib qo'yaman»)
    counts once a stall verb came before it in the sentence (``primed``)."""
    before = tokens[i - 1] if i > 0 else ""
    if primed and _token_in(before, WEAK_LEADS):
        return True
    if not _token_in(before, STALL_LEADS):
        return False
    return not _vouching(tokens, i - 1)


_FOR = _words(
    """
    uchun, biz, men
    """
)
_LOOKING = _words(
    """
    qarab, qarayman, qaraymiz, ko'rib, javob, javobini
    """
)


def _vouching(tokens: list[str], j: int) -> bool:
    """«yuk uchun javob beramiz», «biz javob beramiz», «yukingizga qaraymiz»:
    answering for or looking after, a promise."""
    if tokens[j] not in _LOOKING:
        return False
    earlier = tokens[:j]
    if any(t in _FOR for t in earlier[-2:]) or "uchun" in earlier:
        return True
    return any(_CARGO.match(t[:-2]) for t in earlier if t.endswith(("ga", "ka")))


def _stalling(tokens: list[str], i: int, *, primed: bool = False) -> bool:
    token = tokens[i]
    if _stall_verb(token):
        return not _vouching(tokens, i)
    return _token_in(token, AUXILIARIES) and _led(tokens, i, primed=primed)


def _located(tokens: list[str], i: int, *, script: Script | None) -> bool:
    """The cargo, the truck or the papers somewhere: «yuk Toshkentda»,
    «yukingiz bizda», «груз уже в Ташкенте». A status the AI cannot know."""
    token = tokens[i]
    earlier = tokens[:i]
    if script == "russian":
        return token in _RU_PLACE and any(t in _RU_CARGO for t in earlier)
    return (
        len(token) >= 4
        and token.endswith("da")
        and token not in _NOT_LOCATIVE
        and any(_CARGO.match(t) for t in earlier)
    )


# Said, not asked, «можно» (one may) and «есть» (there is) agree to
# whatever was asked: «Можно налом», «Есть, спасибо».
_UNLESS_ASKED = _words(
    """
    можно, mumkin, есть
    """
)
_INFINITIVE = re.compile(r"[а-яё]+(?:ть|ться|ти|тись|чь)(?![а-яё])")


def _agreeing(tokens: list[str], i: int) -> bool:
    """«Можно налом», «Euroda ham mumkin»: an offer. Not «если есть вопросы»
    (if there are questions) nor «yozishingiz mumkin» (you may write)."""
    if tokens[i] not in _UNLESS_ASKED:
        return False
    before = tokens[i - 1] if i > 0 else ""
    return before != "esli" and not before.endswith("ingiz")


def _infinitives(text: str) -> frozenset[str]:
    """The Russian infinitives in ``text`` as the word view spells them: the
    soft sign that tells «растаможить» from «растаможит» is gone there."""
    return frozenset(
        normalise_for_search(word) for word in _INFINITIVE.findall(text.casefold())
    )


def _bare(tokens: list[str], i: int) -> str | None:
    """«aniq» (sure) and «qabul» (accepted) said alone: a guarantee and an
    agreement. «aniq bilib» (once I know) and «qabul qiladi» are not."""
    token = tokens[i]
    after = tokens[i + 1] if i + 1 < len(tokens) else ""
    if token == "anik" and (not after or after in _ADDRESS or after == "gap"):
        return "guarantee"
    if token == "kabul" and not after.startswith("kil"):
        return "promise"
    return None


# The lexicons a disguised word is read against: a word split into short
# pieces («er ta ga», «zav tra», «nar xi») or stretched («ertaaga»,
# «albattaaa») is still the word.
_DISGUISABLE: Final[tuple[tuple[Lexicon, Flag], ...]] = (
    (TIME, "deadline"),
    (DEADLINE, "deadline"),
    *_RED_LINES,
    *((lexicon, flag) for flag, lexicon in TOPICS),
)
# Every one of them in one pattern: does a joined form hit any at all?
_ANY_DISGUISABLE: Final = re.compile(
    "|".join(f"(?:{lex.union.pattern})" for lex, _f in _DISGUISABLE if lex.union)
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
            if _ANY_DISGUISABLE.search(joined) is None:
                lexicons: tuple[tuple[Lexicon, Flag], ...] = ()
            else:
                lexicons = _DISGUISABLE
            for lexicon, flag in lexicons:
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
    script = script_of(clean_masked)
    russian = script == "russian"
    stall_verb = False
    time_hit = False
    soon = False
    topics: list[tuple[Flag, str]] = []
    if role != "owner":
        _join_hits(_polite(normalise_for_search(clean_masked)), hits, kb=kb)
    infinitives = _infinitives(clean_masked) if russian else frozenset()
    primed = False
    for clause in _clauses(clean_masked):
        words = _polite(clause.words)
        primed = primed and not clause.first
        if not kb and clause.hedged:
            hits.add("promise", "promise:status:hedged")
        if role == "stall" and OWNER_SOON.hit(words):
            soon = True
            words = _blank(words, OWNER_SOON)
        tokens = words.split()
        asked = _asked_from(clause, tokens)

        def said(start: int, words: str = words, asked: int | None = asked) -> bool:
            """Is the hit at ``start`` said, not asked after a question word?"""
            return asked is None or _token_at(words, start) <= asked

        if not kb:
            for entry, (start, _end) in TIME.hits(words):
                if said(start):
                    hits.add("deadline", f"time:{entry}")
                    time_hit = True
        for entry, _span in DEADLINE.hits(words):
            hits.add("deadline", f"deadline:{entry}")
            time_hit = True
        for lexicon, flag in _RED_LINES:
            for entry, _span in lexicon.hits(words):
                hits.add(flag, f"{lexicon.name}:{entry}")
        for entry, (start, _end) in PROMISE_THIRD.hits(words):
            if said(start):
                hits.add("promise", f"promise:{entry}")
        if not clause.question:
            for entry, _span in PER_UNIT.hits(words):
                hits.add("price", f"price:{entry}")
        if role != "owner":
            for entry, _span in SELF_DISCLOSURE.hits(words):
                hits.add(None, f"self_disclosure:{entry}")
            _disguise_hits(words, hits, kb=kb)
        for i, token in enumerate(tokens):
            told = asked is None or i <= asked
            topical = any(lexicon.hit(token) for _flag, lexicon in TOPICS)
            if not kb and _LONE_ENDING.search(token):
                hits.add("promise", "promise:commitment")
            if _stalling(tokens, i, primed=primed):
                stall_verb = True
                primed = primed or not _token_in(token, WEAK_LEADS)
            elif not kb and _stall_verb(token):
                # «yukingizga qaraymiz», «yuk uchun javob beramiz»
                hits.add("promise", "promise:commitment")
            elif (
                not kb
                and not topical
                and _committing(tokens, i, script=script, primed=primed)
            ):
                hits.add("promise", "promise:commitment")
            elif not (kb or russian or topical) and told and _predicting(token):
                hits.add("promise", "promise:prediction")
            if not clause.question and _agreeing(tokens, i):
                hits.add("promise", f"promise:{token}")
            if not kb and not topical and told and token not in infinitives:
                status = _status(tokens, i, script=script)
                if status is not None:
                    hits.add("promise", f"promise:{status}")
            if not kb and _LIKE.search(token):
                hits.add("price", "price:like")
            if not kb and not clause.question and _PER_UNIT.match(token):
                hits.add("price", "price:per_unit")
            if not (kb or clause.question) and _located(tokens, i, script=script):
                hits.add("promise", "promise:located")
            bare = _bare(tokens, i)
            if bare == "guarantee":
                hits.add("guarantee", "guarantee:aniq")
            elif bare == "promise":
                hits.add("promise", "promise:qabul")
            if not kb and told and (_until(token) or _time_clause(tokens, i)):
                hits.add("deadline", "deadline:gacha")
                time_hit = True
            for flag, lexicon in TOPICS:
                if lexicon is INSURANCE and clause.question:
                    continue
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
    r"|ади|йди|майди|ишади|пти|бди|тти|кти|шти|[бвгзйклмнпрстфхчш]дим?"
    r"|вом(?:из|ан|миз|ман)|[бвгджзклмнпрстфхчш]ми|[бдмнптфхч]ли)$"
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
