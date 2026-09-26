"""The evening recap "🌆 Bugun nima bo'ldi", rendered (WP-53).

Pure: it takes what recaps.gather_activity found, the model's prose and the
queue, and returns Telegram messages. It imports only miya.bot.formatting —
every figure is formatting.money of a SQL value, every name, title and
quote is escaped, and the model's sentences are escaped and set in italics
under a label that says who wrote them.
"""

from __future__ import annotations

from miya.bot.formatting import (
    claim_ref,
    clock,
    escape,
    full_date,
    minutes_label,
    missed_line,
    money,
    new_record_lines,
    question_line,
    queue_line,
    quote,
    record_line,
    ref,
    split_message,
    tag,
)

EVENING_HEADER = "🌆 <b>Bugun nima bo'ldi</b> · {full_date}"
EVENING_CONTINUED = "🌆 <b>Bugun nima bo'ldi</b> (davomi {i}/{n})"
EVENING_OVERFLOW = "<i>… qolgani sig'madi — /hisobot</i>"
EVENING_EMPTY = (
    "✅ Bugun yozib qo'yadigan narsa bo'lmadi — pul harakati ham, yangi qarz yoki "
    "va'da ham, suhbat ham yo'q."
)

RECAP_MONEY = "💰 <b>Pul</b>"
MONEY_EMPTY = "Bugun pul harakati yozilmadi."
RECAP_CHECK = "📱 <b>SMS va ilovadan yozilganlar</b> — tekshirib qo'ying:"
RECAP_NEW = "🧾 <b>Yangi qarz va va'dalar</b>"
RECAP_DONE = "✅ <b>Bajarilgan va yopilganlar</b>"
RECAP_PEOPLE = "👥 <b>Kim bilan nima bo'ldi</b>"
RECAP_GROUPS = "💬 <b>Guruhlarda</b>"
RECAP_CALLS = "📞 <b>Qo'ng'iroqlar</b>"
RECAP_TO_ME = "📨 <b>Guruhlarda sizga yozilganlar</b>"
RECAP_OPEN = "⏳ <b>Javob kutayotganlar</b>"
RECAP_TOMORROW = "📅 <b>Ertaga</b>"
TOMORROW_TAIL = "<i>AI bilan reja: /reja</i>"
PROSE_LABEL = "<i>🤖 — AI yozgan qisqa xulosa. Raqamlar faqat bazadan.</i>"
PROSE_DOWN = (
    "<i>🤖 Suhbatlar xulosasi hozir yozilmadi (AI javob bermadi). "
    "Raqamlar va ro'yxatlar to'liq.</i>"
)
AUTO_LINE = "🤖 Bugun {n} ta savolni o'zim hal qildim — /savollar hal"
REVIEW_LINE = "⚠️ Tekshirish kerak: {n} ta yozuv — /tekshir"

# Where a booked payment came from, in the owner's words.
SOURCE_WORDS = (
    ("phone_sms", "SMS"),
    ("phone_notification", "Payme ilova"),
    ("telegram_userbot", "chat"),
    ("assistant_bot", "qo'lda"),
    ("manual", "qo'lda"),
    ("receipt_photo", "chek"),
)

CHECK_MAX = 10
TO_ME_MAX = 5
OPEN_MAX = 5


def _section(heading: str, lines: list[str]) -> str:
    return heading + "\n" + "\n".join(lines)


def _money_section(money_day) -> str:
    lines: list[str] = []
    for currency, total in money_day.income.items():
        lines.append(f"Kirim: {money(total, currency)}")
    for currency, total in money_day.expense.items():
        lines.append(f"Chiqim: {money(total, currency)}")
    for r in money_day.repayments:
        name = escape(r.person_name)
        amount = money(r.amount, r.currency)
        if r.direction.value == "they_owe_me":
            lines.append(
                f"↩️ <b>{name}</b> qarzini qaytardi: {amount}{tag('debt', r.debt_id)}"
            )
        else:
            lines.append(
                f"↪️ <b>{name}</b>ga qarz to'landi: {amount}{tag('debt', r.debt_id)}"
            )
    if money_day.biggest:
        lines.append("Eng katta xarajatlar:")
        for txn in money_day.biggest:
            what = escape(txn.description or txn.category or "?")
            lines.append(f"• {money(txn.amount, txn.currency)} — {what}")
    counts: dict[str, int] = {}
    for source, word in SOURCE_WORDS:
        n = money_day.by_source.get(source, 0)
        if n:
            counts[word] = counts.get(word, 0) + n
    if counts:
        lines.append("Manba: " + " · ".join(f"{w} {n}" for w, n in counts.items()))
    if money_day.from_phone:
        lines.append(f"- telefondan yozilgan: {money_day.from_phone} ta")
    if money_day.review_pending:
        lines.append(f"- tekshiruvda: {money_day.review_pending} ta (/tekshir)")
    if money_day.ignored:
        lines.append(
            f"- e'tiborsiz qoldirildi: {money_day.ignored} ta (kod, reklama) — "
            "/tekshir hammasi"
        )
    if money_day.voided:
        lines.append(f"- o'chirilgan: {money_day.voided} ta")
    if not (money_day.income or money_day.expense or money_day.repayments):
        lines.insert(0, MONEY_EMPTY)
    return _section(RECAP_MONEY, lines)


def _check_section(money_day, tz) -> str | None:
    lines = []
    for c in money_day.checkable[:CHECK_MAX]:
        sign = "+" if c.type == "income" else "−"
        when = c.occurred_at.astimezone(tz).strftime("%H:%M")
        lines.append(
            f"{when} · {sign}{money(c.amount, c.currency)} · "
            f"{escape(c.description or '')}{tag('transaction', c.id)}"
        )
    extra = money_day.checkable_total - len(lines)
    if extra > 0:
        lines.append(f"… va yana {extra} ta — /pul")
    if money_day.review_pending:
        lines.append(f"⚠️ {money_day.review_pending} ta to'lov SMS'i o'qilmadi — /tekshir")
    return _section(RECAP_CHECK, lines) if lines else None


def _person_header(day, codes: dict) -> str:
    person = day.person
    held = codes.get(person.id) or []
    name = f"<b>{escape(person.display_name)}</b>"
    if len(held) == 1:
        name += f" ({escape(held[0])})"
    bits = []
    messages = day.messages_in + day.messages_out
    if messages:
        bits.append(f"💬 {messages} xabar")
    if day.calls:
        minutes = minutes_label(day.call_seconds)
        bits.append(f"📞 {day.calls} qo'ng'iroq" + (f" ({minutes})" if minutes else ""))
    if day.missed:
        bits.append(f"📵 {day.missed} javobsiz")
    if day.group_mentions:
        bits.append(f"📨 guruhda {day.group_mentions} marta")
    return " · ".join([name, *bits])


def _person_block(day, prose: dict, codes: dict) -> tuple[str, bool]:
    lines = [_person_header(day, codes)]
    shown_prose = False
    text = prose.get(day.key)
    if text:
        lines.append(f"🤖 <i>{escape(text)}</i>")
        shown_prose = True
    elif day.messages_in + day.messages_out + day.calls or day.content_ids:
        held = codes.get(day.person.id) or []
        handle = held[0] if len(held) == 1 else day.person.display_name
        lines.append(f"<i>Qisqa xulosa yo'q — /tarix {escape(handle)}</i>")
    for q in day.questions[:2]:
        state = "javob berildi" if q.answered else "javobsiz"
        lines.append(f"❓ So'radi: {quote(q.text)} — {state}")
    if day.claim_ids:
        refs = ", ".join(claim_ref(i) for i in day.claim_ids)
        lines.append(
            f"🗣 Da'vo: {len(day.claim_ids)} ta tasdiq kutmoqda (<code>{refs}</code>) "
            "— /savollar"
        )
    handles = [ref("debt", i) for i in [*day.new_debt_ids, *day.repayment_debt_ids]]
    handles += [ref("promise", i) for i in day.new_promise_ids]
    handles += [ref("transaction", i) for i in day.transaction_ids]
    handles = list(dict.fromkeys(handles))
    if handles:
        lines.append(f"🧾 Bugun yozildi: <code>{', '.join(handles)}</code>")
    return "\n".join(lines), shown_prose


def _group_block(group, prose: dict) -> tuple[str, bool]:
    head = f"<b>{escape(group.title)}</b> · {group.messages} ta xabar"
    if group.to_me:
        head += f" · 📨 {group.to_me} tasi sizga"
    text = prose.get(group.key)
    if text:
        return f"{head}\n🤖 <i>{escape(text)}</i>", True
    return head, False


def _completed_lines(completed) -> list[str]:
    if completed is None:
        return []
    lines = [
        record_line("debt", d, getattr(d, "person", None))
        for d in completed.settled_debts
    ]
    lines += [record_line("promise", p, p.person) for p in completed.done_promises]
    lines += [record_line("task", t) for t in completed.done_tasks]
    return lines


def _tomorrow_lines(tomorrow) -> list[str]:
    if tomorrow is None:
        return []
    lines = [
        f"{clock(e.start_at)} — {escape(e.title)}"
        + (f" ({escape(e.location)})" if e.location else "")
        for e in tomorrow.events
    ]
    for b in tomorrow.debts:
        side = "sizdan qarzi" if b.direction.value == "they_owe_me" else "qarzingiz"
        lines.append(
            f"{escape(b.person.display_name)}: "
            f"{money(b.outstanding, b.currency)} ({side})"
            + "".join(tag("debt", i) for i in b.ids[:1])
        )
    lines += [
        f"{escape(person.display_name)}: {escape(p.description)}{tag('promise', p.id)}"
        for p, person in tomorrow.promises
    ]
    lines += [f"{escape(t.description)}{tag('task', t.id)}" for t in tomorrow.tasks]
    return lines


def evening_parts(
    activity,
    prose: dict[str, str],
    tomorrow,
    queue,
    *,
    day,
    tz,
    partial_until=None,
    max_parts: int = 4,
    codes: dict | None = None,
    prose_status: str = "none",
    max_people: int = 8,
    max_groups: int = 5,
    auto_resolved: int = 0,
) -> list[str]:
    """The recap as Telegram messages, sections in order, empty ones left out
    (except 💰 Pul)."""
    codes = codes or {}
    header = EVENING_HEADER.format(full_date=full_date(day))
    if partial_until is not None:
        header += f" · {clock(partial_until)} gacha"

    footer: list[str] = []
    if activity.needs_review:
        footer.append(REVIEW_LINE.format(n=activity.needs_review))
    line = queue_line(queue)
    if line:
        footer.append(line)
    if auto_resolved:
        footer.append(AUTO_LINE.format(n=auto_resolved))

    open_items = activity.questions_open or activity.missed_open
    tomorrow_lines = _tomorrow_lines(tomorrow)
    if activity.is_empty() and not open_items and not tomorrow_lines:
        body = "\n\n".join(
            [header, EVENING_EMPTY, *(["\n".join(footer)] if footer else [])]
        )
        return split_message(
            body,
            max_parts=max_parts,
            continued=EVENING_CONTINUED,
            overflow=EVENING_OVERFLOW,
        )

    sections = [header, _money_section(activity.money)]
    check = _check_section(activity.money, tz)
    if check:
        sections.append(check)
    new_lines = new_record_lines(activity.new_debts, activity.new_promises)
    if new_lines:
        sections.append(_section(RECAP_NEW, [f"• {x}" for x in new_lines]))
    done = _completed_lines(activity.completed)
    if done:
        sections.append(_section(RECAP_DONE, [f"• {x}" for x in done]))

    any_prose = False
    if activity.people:
        blocks = []
        for person_day in activity.people[:max_people]:
            block, shown = _person_block(person_day, prose, codes)
            any_prose = any_prose or shown
            blocks.append(block)
        extra = len(activity.people) - max_people
        if extra > 0:
            blocks.append(f"<i>… va yana {extra} kishi bilan aloqa bo'ldi — /bugun</i>")
        sections.append(RECAP_PEOPLE + "\n" + "\n\n".join(blocks))
    if activity.groups:
        blocks = []
        for group in activity.groups[:max_groups]:
            block, shown = _group_block(group, prose)
            any_prose = any_prose or shown
            blocks.append(block)
        sections.append(RECAP_GROUPS + "\n" + "\n".join(blocks))
    calls = activity.calls
    if calls.total:
        lines = [
            f"Jami {calls.total} ta: kiruvchi {calls.incoming}, chiquvchi "
            f"{calls.outgoing}, javobsiz {calls.missed + calls.rejected}"
            + (f" · {minutes_label(calls.seconds)} gaplashildi" if calls.seconds else "")
        ]
        if calls.unknown:
            lines.append(f"Noma'lum raqamlardan: {calls.unknown} ta")
        sections.append(_section(RECAP_CALLS, lines))
    if activity.to_me:
        lines = []
        for row in activity.to_me[:TO_ME_MAX]:
            when = row.occurred_at.astimezone(tz).strftime("%H:%M")
            where = escape(activity.chat_titles.get(row.tg_chat_id or 0, "guruh"))
            words = quote(row.raw_text or row.transcript or "")
            lines.append(f"• {when} · <b>{where}</b> — {words}")
        extra = len(activity.to_me) - TO_ME_MAX
        if extra > 0:
            lines.append(f"… va yana {extra} ta — /menga")
        sections.append(_section(RECAP_TO_ME, lines))
    if open_items:
        lines = [f"• {question_line(q)}" for q in activity.questions_open[:OPEN_MAX]]
        lines += [f"• {missed_line(mc)}" for mc in activity.missed_open[:OPEN_MAX]]
        extra = max(0, len(activity.questions_open) - OPEN_MAX) + max(
            0, len(activity.missed_open) - OPEN_MAX
        )
        if extra:
            lines.append(f"… va yana {extra} ta — /ertalab")
        sections.append(_section(RECAP_OPEN, lines))
    if tomorrow_lines:
        sections.append(
            _section(RECAP_TOMORROW, [f"• {x}" for x in tomorrow_lines] + [TOMORROW_TAIL])
        )

    if any_prose:
        footer.append(PROSE_LABEL)
    elif prose_status == "fallback":
        footer.append(PROSE_DOWN)
    if footer:
        sections.append("\n".join(footer))
    return split_message(
        "\n\n".join(sections),
        max_parts=max_parts,
        continued=EVENING_CONTINUED,
        overflow=EVENING_OVERFLOW,
    )


# --- the morning "🌙 Kecha" (WP-54) --------------------------------------------------

KECHA_HEADER = "🌙 <b>Kecha</b> · {full_date}"
KECHA_CONTINUED = "🌙 <b>Kecha</b> (davomi {i}/{n})"
KECHA_OVERFLOW = "<i>… qolgani sig'madi — /kecha</i>"
KECHA_TOP = "👥 <b>Asosiy suhbatlar</b>"
KECHA_LATE = "🌃 <b>Kechqurun va tunda</b> ({time} dan keyin)"
KECHA_FULL = "👥 <b>Kecha kim bilan nima bo'ldi</b>"
KECHA_FULL_NOTE = "<i>Kechki xulosa yetib bormagan edi — kun shu yerda to'liq.</i>"
KECHA_NONE = "🌙 Kecha yozib qo'yadigan narsa bo'lmadi."
KECHA_REFS_MAX = 6


def _kecha_money(money_day) -> list[str]:
    lines = [f"💰 Kirim: {money(t, c)}" for c, t in money_day.income.items()]
    lines += [f"💰 Chiqim: {money(t, c)}" for c, t in money_day.expense.items()]
    if money_day.repayments:
        refs = ", ".join(
            dict.fromkeys(ref("debt", r.debt_id) for r in money_day.repayments)
        )
        lines.append(
            f"↩️ Qaytgan qarzlar: {len(money_day.repayments)} ta (<code>{refs}</code>)"
        )
    if money_day.from_phone or money_day.review_pending:
        lines.append(
            f"📱 Telefondan yozilgan: {money_day.from_phone} ta · tekshiruvda: "
            f"{money_day.review_pending} ta (/tekshir)"
        )
    return lines


def _kecha_records(new_debts, new_promises, completed) -> str | None:
    done = 0
    if completed is not None:
        done = (
            len(completed.settled_debts)
            + len(completed.done_promises)
            + len(completed.done_tasks)
        )
    if not (new_debts or new_promises or done):
        return None
    line = (
        f"🧾 Yangi: {len(new_debts)} ta qarz, {len(new_promises)} ta va'da"
        f" · ✅ Yopildi: {done} ta"
    )
    refs = [ref("debt", d.id) for d in new_debts] + [
        ref("promise", p.id) for p in new_promises
    ]
    if refs and len(refs) <= KECHA_REFS_MAX:
        line += f" (<code>{', '.join(refs)}</code>)"
    return line


def kecha_parts(
    *,
    yesterday,
    money_day,
    new_debts,
    new_promises,
    completed,
    top,
    late,
    late_start,
    prose: dict[str, str],
    full: bool,
    tz,
    codes: dict | None = None,
    prose_status: str = "none",
    max_people: int = 8,
    max_groups: int = 5,
    max_parts: int = 2,
) -> list[str]:
    """The morning recap of yesterday and the night; [] when nothing happened.

    ``top`` is [(name, prose)] reused from the evening recap; ``late`` is the
    DayActivity since ``late_start`` (all of yesterday when ``full``)."""
    codes = codes or {}
    sections = [KECHA_HEADER.format(full_date=full_date(yesterday))]
    body: list[str] = []
    money_lines = _kecha_money(money_day)
    if money_lines:
        body.append("\n".join(money_lines))
    records = _kecha_records(new_debts, new_promises, completed)
    if records:
        body.append(records)
    any_prose = False
    if top:
        lines = [
            f"<b>{escape(name)}</b> — 🤖 <i>{escape(text)}</i>" for name, text in top
        ]
        body.append(KECHA_TOP + "\n" + "\n".join(lines))
        any_prose = True
    blocks = []
    if late is not None:
        for person_day in late.people[:max_people]:
            block, shown = _person_block(person_day, prose, codes)
            any_prose = any_prose or shown
            blocks.append(block)
        for group in late.groups[:max_groups]:
            block, shown = _group_block(group, prose)
            any_prose = any_prose or shown
            blocks.append(block)
    if blocks:
        if full:
            head = KECHA_FULL + "\n" + KECHA_FULL_NOTE
        else:
            head = KECHA_LATE.format(time=late_start.astimezone(tz).strftime("%H:%M"))
        body.append(head + "\n" + "\n\n".join(blocks))
    if not body:
        return []
    sections += body
    if any_prose:
        sections.append(PROSE_LABEL)
    elif prose_status == "fallback":
        sections.append(PROSE_DOWN)
    return split_message(
        "\n\n".join(sections),
        max_parts=max_parts,
        continued=KECHA_CONTINUED,
        overflow=KECHA_OVERFLOW,
    )
