package uz.miya.companion.discover

import android.os.Build

/**
 * Manufacturer-ordered candidate folders.
 *
 * This is an ORDERING HINT AND NOTHING MORE. The public path lists these are
 * drawn from contradict each other — the same repo family attributes
 * "Recordings/Call/" to Samsung, Pixel and Huawei, and "PhoneRecord/" to five
 * different vendors. Only two entries have first-party or broad corroboration:
 *
 *   MIUI/sound_recorder/call_rec/   (Xiaomi, documented by Xiaomi support)
 *   Sounds/CallRecord/              (Huawei EMUI, widely corroborated)
 *
 * Everything else must be CONFIRMED AT RUNTIME by actually finding an audio
 * file in it. A candidate that matches nothing is not a folder we use.
 *
 * Paths are MediaStore RELATIVE_PATH form: no volume, no leading slash,
 * trailing slash present.
 */
object OemCandidates {

    const val GOOGLE_DIALER_PACKAGE = "com.google.android.dialer"

    /** Unreachable by any app: app-private storage. Listed so we can name it. */
    const val GOOGLE_DIALER_PATH = "/data/user/0/com.google.android.dialer/files/callrecording"

    private val XIAOMI = listOf(
        "MIUI/sound_recorder/call_rec/",
        "MIUI/sound_recorder/",
        "Recordings/Calls/",
        "Recordings/",
        "Recorder/call/",
    )

    private val SAMSUNG = listOf(
        "Recordings/Call/",
        "Call/",
        "Sounds/",
    )

    private val HUAWEI = listOf(
        "Sounds/CallRecord/",
        "Sounds/callrecord/",
        "Recordings/Call/",
    )

    private val OPPO = listOf(
        "Music/Recordings/Call Recordings/",
        "Recordings/Call Recordings/",
        "PhoneRecord/",
        "Record/Call/",
        "CallRecord/",
    )

    private val VIVO = listOf(
        "Record/Call/",
        "Sounds/CallRecord/",
        "Recordings/",
    )

    private val REALME = listOf(
        "Recordings/",
        "Music/Recordings/Call Recordings/",
        "Calls/",
    )

    private val TRANSSION = listOf(
        "PhoneRecord/",
        "Music/PhoneRecord/",
    )

    /** Tried last, for everyone. */
    private val TAIL = listOf(
        "Recordings/Call/",
        "Recordings/Calls/",
        "Recordings/",
        "CallRecordings/",
        "Call Recordings/",
        "callrecordings/",
        "Recorder/call/",
        "PhoneRecord/",
        "Record/Call/",
        "Sounds/CallRecord/",
        "Truecaller/",
    )

    /**
     * Candidates in the order worth trying on THIS device. Brand is a hint:
     * Xiaomi ships Google Dialer on some Global SKUs and Realme ships both
     * recorders, so the brand list is always followed by the generic tail.
     */
    fun ordered(): List<String> {
        val brand = (Build.MANUFACTURER + " " + Build.BRAND).lowercase()
        val first = when {
            listOf("xiaomi", "redmi", "poco").any { brand.contains(it) } -> XIAOMI
            brand.contains("samsung") -> SAMSUNG
            listOf("huawei", "honor").any { brand.contains(it) } -> HUAWEI
            listOf("oppo", "oneplus").any { brand.contains(it) } -> OPPO
            listOf("vivo", "iqoo").any { brand.contains(it) } -> VIVO
            brand.contains("realme") -> REALME
            listOf("tecno", "infinix", "itel", "transsion").any { brand.contains(it) } -> TRANSSION
            brand.contains("meizu") -> listOf("Recorder/call/")
            brand.contains("asus") -> listOf("callrecordings/")
            else -> emptyList()
        }
        return (first + TAIL).distinct()
    }

    /** Extensions the OEM recorders are known to produce. */
    val AUDIO_EXTENSIONS = setOf("m4a", "mp3", "amr", "wav", "aac", "ogg", "opus", "3gp", "awb")

    /**
     * Written *during* the call, or by some other tool. Never ingest these:
     * hashing a partial file poisons dedupe with a hash the finished file will
     * never match, and then you pay to transcribe truncated audio.
     */
    val REJECTED_EXTENSIONS = setOf("3ga", "tmp", "part", "partial", "download", "crdownload")
}
