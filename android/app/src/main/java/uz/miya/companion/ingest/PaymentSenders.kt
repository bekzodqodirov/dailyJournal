package uz.miya.companion.ingest

import java.util.Locale

/**
 * The client half of the "payments" SMS mode: is this sender a bank or a
 * payment service?
 *
 * This list MIRRORS the server's `miya/services/sms_money.py::DEFAULT_SENDERS`
 * and its normalisation (casefold, every space and punctuation mark dropped),
 * so "Kapital Bank", "KAPITALBANK" and "kapitalbank" are one sender. The
 * server re-checks the sender on every message it stores — the client filter
 * only limits what leaves the phone, it is not the security boundary, so a
 * list that drifts slightly behind the server's costs nothing but a missed
 * upload the owner can fix by switching to "all".
 */
object PaymentSenders {

    private val KNOWN: Set<String> = setOf(
        "payme",
        "click",
        "uzum",
        "uzumbank",
        "uzcard",
        "humo",
        "paynet",
        "apelsin",
        "kapitalbank",
        "ipoteka",
        "ipotekabank",
        "asaka",
        "asakabank",
        "hamkorbank",
        "agrobank",
        "infinbank",
        "tbc",
        "tbcbank",
        "anorbank",
        "aloqabank",
        "trastbank",
        "ipakyuli",
        "ipakyulibank",
        "sqb",
        "nbu",
        "xalqbank",
        "davrbank",
        "turonbank",
        // The card processors' own service numbers: 8600 = Uzcard, 9860 = Humo.
        "8600",
        "9860",
        "3700",
    )

    /** The server's `normalise_sender`, in Kotlin. */
    fun normalise(sender: String?): String =
        sender.orEmpty().lowercase(Locale.ROOT).filter { it.isLetterOrDigit() }

    fun isPayment(sender: String?): Boolean = normalise(sender) in KNOWN
}
