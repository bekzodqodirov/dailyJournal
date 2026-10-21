"""The dangerous-file shield (JV-01, owner decision 4) and the spam and scam
message shield (JV-09).

Executables, installers and archives are never downloaded, opened or offered
for approval, on any channel; the owner is warned once instead. Everything
here is pure: no database, no network, no settings. The classifiers run on
what a transport knows *before* a download (the claimed name and MIME type),
and ``sniff_magic`` checks the bytes of an allowed type after it.

The message shield reads what a message carries (its text, its links, its
buttons, the sender's account flags) and what the caller found in the
sender's history, and returns ``meta.safety``: ``scam`` (any one hard rule;
quarantined), ``hijack`` (a known contact suddenly writing unlike
themselves), ``spam`` (soft signals that add up) or nothing. A single soft
signal never blocks a reply: a new client writing first is answered.

The blocklists are code constants on purpose. No configuration key can
loosen them; a file type is made safe only by a reviewed change here. The
one message-shield key, SAFETY_EXTRA_SAFE_DOMAINS, only adds owner-verified
domains to the look-alike allowlist and is passed in by the caller.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from rapidfuzz.distance import Levenshtein

from miya.services import codes, sms_money
from miya.services.text import fold_apostrophes

RiskKind = Literal["exec", "archive", "macro", "html", "mismatch", "ok"]

EXEC: RiskKind = "exec"
ARCHIVE: RiskKind = "archive"
MACRO: RiskKind = "macro"
HTML: RiskKind = "html"
# Only from sniff_magic: the bytes are not what the suffix claims, and are not
# a known dangerous type either. Never parsed; nothing to warn about.
MISMATCH: RiskKind = "mismatch"
OK: RiskKind = "ok"

DANGEROUS_KINDS = frozenset({EXEC, ARCHIVE})

# meta.safety for a message that carried a dangerous file: a hard hit, the
# same scale JV-09's message verdicts use.
VERDICT_DANGEROUS_FILE = "dangerous_file"
HARD_SCORE = 100

# Seeded from the Gmail blocked-file-types list, plus mobile installers and
# the Unix and macOS packages a phone or laptop would run.
EXEC_SUFFIXES = frozenset(
    {
        # Android and iOS
        "apk",
        "xapk",
        "apks",
        "apkm",
        "aab",
        "dex",
        "ipa",
        "mobileconfig",
        "mobileprovision",
        # Windows
        "ade",
        "adp",
        "appx",
        "appxbundle",
        "application",
        "bat",
        "cab",
        "chm",
        "cmd",
        "com",
        "cpl",
        "diagcab",
        "diagcfg",
        "diagpack",
        "dll",
        "ex",
        "ex_",
        "exe",
        "gadget",
        "hta",
        "inf",
        "ins",
        "isp",
        "jse",
        "js",
        "lib",
        "lnk",
        "mde",
        "mjs",
        "msc",
        "msh",
        "msi",
        "msix",
        "msixbundle",
        "msp",
        "mst",
        "nsh",
        "pif",
        "ps1",
        "ps1xml",
        "psc1",
        "psm1",
        "reg",
        "scf",
        "scr",
        "sct",
        "shb",
        "sys",
        "url",
        "vb",
        "vbe",
        "vbs",
        "vxd",
        "website",
        "ws",
        "wsc",
        "wsf",
        "wsh",
        "xll",
        # Java
        "jar",
        "jnlp",
        "class",
        # Disk images
        "dmg",
        "img",
        "iso",
        "vhd",
        "vhdx",
        # macOS and Unix
        "app",
        "command",
        "pkg",
        "mpkg",
        "deb",
        "rpm",
        "run",
        "sh",
        "appimage",
        "snap",
        "flatpak",
        # Browser extensions
        "crx",
        "xpi",
    }
)

ARCHIVE_SUFFIXES = frozenset(
    {
        "zip",
        "zipx",
        "rar",
        "7z",
        "s7z",
        "tar",
        "gz",
        "gzip",
        "tgz",
        "xz",
        "txz",
        "zst",
        "tzst",
        "bz2",
        "tbz",
        "tbz2",
        "lz",
        "lzma",
        "lz4",
        "z",
        "arj",
        "ace",
        "lzh",
        "lha",
        "cpio",
        "wim",
        "xar",
        "sit",
        "sitx",
        "egg",
        "alz",
    }
)
# Split-archive parts: file.001, file.r00, file.z01. Only as the last suffix,
# so a name like «hisob.001.pdf» is not mistaken for one.
_ARCHIVE_PART = re.compile(r"^(?:\d{3}|r\d{2}|z\d{2})$")

MACRO_SUFFIXES = frozenset(
    {"docm", "dotm", "xlsm", "xltm", "xlam", "pptm", "potm", "ppsm", "ppam", "sldm"}
)

HTML_SUFFIXES = frozenset({"html", "htm", "xhtml", "xht", "shtml", "mht", "mhtml", "svg"})

ANDROID_SUFFIXES = frozenset({"apk", "xapk", "apks", "apkm", "aab"})

# A double extension counts anywhere in the name, but an inner part is only
# read as a suffix when it is unambiguous: «first.class.pdf» and «gsr.com.pdf»
# are words and domains, «hisob.exe.pdf» is not.
_INNER_SUFFIXES: dict[str, RiskKind] = {
    **{
        s: "exec"
        for s in (
            "apk",
            "xapk",
            "apks",
            "apkm",
            "aab",
            "dex",
            "ipa",
            "mobileconfig",
            "exe",
            "ex_",
            "msi",
            "msix",
            "scr",
            "bat",
            "cmd",
            "vbs",
            "vbe",
            "js",
            "jse",
            "jar",
            "lnk",
            "ps1",
            "hta",
            "pif",
            "cpl",
            "dll",
            "dmg",
            "iso",
            "wsf",
        )
    },
    **{s: "archive" for s in ("zip", "rar", "7z", "tar", "gz", "tgz", "xz", "zst")},
}

EXEC_MIMES = frozenset(
    {
        "application/vnd.android.package-archive",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-ms-installer",
        "application/x-msi",
        "application/x-dosexec",
        "application/x-executable",
        "application/x-elf",
        "application/x-mach-binary",
        "application/x-apple-diskimage",
        "application/x-iso9660-image",
        "application/java-archive",
        "application/x-java-archive",
        "application/x-java-jnlp-file",
        "application/x-sh",
        "application/x-shellscript",
        "application/x-bat",
        "application/javascript",
        "application/x-javascript",
        "text/javascript",
        "application/vnd.microsoft.portable-executable",
        "application/x-ms-shortcut",
        "application/x-apple-aspen-config",
        "application/x-itunes-ipa",
        "application/vnd.debian.binary-package",
        "application/x-debian-package",
        "application/x-rpm",
        "application/x-redhat-package-manager",
        "application/x-xpinstall",
        "application/x-chrome-extension",
        "application/hta",
        "application/x-ms-application",
        "application/vnd.ms-cab-compressed",
    }
)

ARCHIVE_MIMES = frozenset(
    {
        "application/zip",
        "application/x-zip",
        "application/x-zip-compressed",
        "application/vnd.rar",
        "application/x-rar",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/x-tar",
        "application/gzip",
        "application/x-gzip",
        "application/x-xz",
        "application/zstd",
        "application/x-zstd",
        "application/x-bzip",
        "application/x-bzip2",
        "application/x-lzip",
        "application/x-lzma",
        "application/x-lzh-compressed",
        "application/x-compress",
        "application/x-compressed",
        "application/x-cpio",
        "application/x-archive",
        "application/x-ace-compressed",
        "application/x-arj",
        "application/x-stuffit",
    }
)

MACRO_MIMES = frozenset(
    {
        "application/vnd.ms-word.document.macroenabled.12",
        "application/vnd.ms-word.template.macroenabled.12",
        "application/vnd.ms-excel.sheet.macroenabled.12",
        "application/vnd.ms-excel.template.macroenabled.12",
        "application/vnd.ms-excel.addin.macroenabled.12",
        "application/vnd.ms-powerpoint.presentation.macroenabled.12",
        "application/vnd.ms-powerpoint.slideshow.macroenabled.12",
        "application/vnd.ms-powerpoint.template.macroenabled.12",
        "application/vnd.ms-powerpoint.addin.macroenabled.12",
    }
)

HTML_MIMES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml"})

# Office Open XML and OpenDocument are ZIP containers: some clients label them
# application/zip. For these suffixes the bytes decide (sniff_magic), not the
# label.
_OOXML_ZIP = frozenset(
    {"docx", "dotx", "xlsx", "xltx", "pptx", "potx", "ppsx"} | MACRO_SUFFIXES
)
_ODF_ZIP = frozenset({"odt", "ods", "odp"})
OFFICE_ZIP_SUFFIXES = _OOXML_ZIP | _ODF_ZIP
# The only ZIP containers MIYA parses (documents.SUPPORTED_SUFFIXES).
OOXML_SUFFIXES = frozenset({".docx", ".xlsx", ".xlsm"})
_TEXT_SUFFIXES = frozenset({".txt", ".csv", ".md"})
# Images, as a photo or as a file sent uncompressed (JV-04): the bytes must be
# an image, whichever of these formats the name claims.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

# ZIP central-directory limits: a decompression bomb is refused unopened.
ZIP_MAX_UNCOMPRESSED = 100 * 1024 * 1024
ZIP_MAX_RATIO = 100

_DOT_LOOKALIKES = str.maketrans(
    {"\uff0e": ".", "\u2024": ".", "\ufe52": ".", "\u3002": ".", "\u2e33": "."}
)
_SPACES_AROUND_DOTS = re.compile(r"\s*\.\s*")
_SPACES = re.compile(r"\s+")

_HEAD_BYTES = 4096

_EXEC_MAGIC = (
    b"\x7fELF",  # Linux
    b"\xfe\xed\xfa\xce",  # Mach-O 32
    b"\xfe\xed\xfa\xcf",  # Mach-O 64
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",  # Mach-O universal, Java class
    b"L\x00\x00\x00\x01\x14\x02\x00",  # Windows shortcut
)
_ARCHIVE_MAGIC = (
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",  # gzip
    b"\xfd7zXZ\x00",
    b"\x28\xb5\x2f\xfd",  # zstd
    b"MSCF\x00\x00\x00\x00",  # cabinet
    b"SZDD\x88\xf0\x27\x33",  # MS-DOS COMPRESS, the setup.ex_ of installers
    b"KWAJ\x88\xf0\x27\xd1",
)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_BZIP2 = re.compile(rb"^BZh[1-9]1AY&SY")

# Entries that make a ZIP an Android package or a Java archive, not a document.
_APP_ENTRIES = frozenset({"androidmanifest.xml", "classes.dex", "meta-inf/manifest.mf"})


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a file is, as far as the shield is concerned."""

    kind: RiskKind
    reason: str | None = None  # which rule fired, for logs and meta.safety

    @property
    def dangerous(self) -> bool:
        """Never fetched, never offered, the owner warned."""
        return self.kind in DANGEROUS_KINDS

    @property
    def readable(self) -> bool:
        """May be parsed for text (a macro document as text only)."""
        return self.kind in (OK, MACRO)

    def as_risk(self, name: str | None) -> dict[str, str | None]:
        """``interactions.media.risk``."""
        return {"kind": self.kind, "name": name}

    def as_safety(self) -> dict[str, object]:
        """``interactions.meta.safety`` for a dangerous file."""
        reasons = [f"{self.kind}_file"]
        if self.reason:
            reasons.append(self.reason)
        return {
            "verdict": VERDICT_DANGEROUS_FILE,
            "score": HARD_SCORE,
            "reasons": reasons,
        }


SAFE = Verdict(OK)


def normalise_filename(name: str | None) -> str:
    """The name as the operating system would act on it.

    NFKC, dot look-alikes folded to «.», bidi and zero-width characters (every
    Unicode format character) removed — an RLO can make «gpj.apk» display as
    «kpa.jpg» — casefolded, spaces around dots collapsed, and trailing dots
    and spaces dropped the way Windows drops them. Only the last path
    component counts.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name).translate(_DOT_LOOKALIKES)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    # «ilova.apk/» names the same file as «ilova.apk»: an empty last
    # component must not hide the real one.
    parts = [p for p in re.split(r"[\\/]", text) if p.strip().strip(".").strip()]
    text = parts[-1] if parts else ""
    text = _SPACES.sub(" ", text.casefold())
    text = _SPACES_AROUND_DOTS.sub(".", text)
    return text.strip().rstrip(". ")


def display_name(text: str) -> str:
    """A sender's file or chat name made safe to show the owner.

    NFKC with every Unicode format character (bidi overrides, zero-width
    marks) removed, so an RLO cannot make «gpj.apk» read as «kpa.jpg» or
    reverse the rest of the warning. Case and punctuation are kept.
    """
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _suffixes(normalised: str) -> list[str]:
    """Every suffix after the first dot, last one last."""
    parts = normalised.split(".")
    return [p for p in parts[1:] if p]


def _suffix_kind(suffix: str) -> RiskKind:
    if suffix in EXEC_SUFFIXES:
        return EXEC
    if suffix in ARCHIVE_SUFFIXES or _ARCHIVE_PART.match(suffix):
        return ARCHIVE
    if suffix in MACRO_SUFFIXES:
        return MACRO
    if suffix in HTML_SUFFIXES:
        return HTML
    return OK


def _mime_kind(mime: str | None, last_suffix: str | None) -> RiskKind:
    label = (mime or "").split(";")[0].strip().casefold()
    if not label:
        return OK
    if label in EXEC_MIMES:
        return EXEC
    if label in ARCHIVE_MIMES:
        # An Office document labelled application/zip: its bytes decide.
        return OK if last_suffix in OFFICE_ZIP_SUFFIXES else ARCHIVE
    if label in MACRO_MIMES:
        return MACRO
    if label in HTML_MIMES:
        return HTML
    return OK


_ORDER: tuple[RiskKind, ...] = (EXEC, ARCHIVE, MACRO, HTML)


def classify_attachment(name: str | None, mime: str | None = None) -> Verdict:
    """Classify an attachment before anything is downloaded.

    The worst of three readings wins: the last suffix, any inner suffix (a
    double extension anywhere, «Rasmlar.jpg.apk» or «hisob.exe.pdf»), and
    the MIME type the sender's client declared.
    """
    normalised = normalise_filename(name)
    suffixes = _suffixes(normalised)
    found: dict[RiskKind, str] = {}
    if suffixes:
        last = suffixes[-1]
        kind = _suffix_kind(last)
        if kind != OK:
            found.setdefault(kind, "suffix")
        for inner in suffixes[:-1]:
            if inner in _INNER_SUFFIXES:
                found.setdefault(_INNER_SUFFIXES[inner], "double_extension")
    kind = _mime_kind(mime, suffixes[-1] if suffixes else None)
    if kind != OK:
        found.setdefault(kind, "mime")
    for kind in _ORDER:
        if kind in found:
            return Verdict(kind, found[kind])
    return SAFE


def is_android_package(name: str | None) -> bool:
    """An Android installer by name, for the «MIYA app only from the bot» line."""
    return any(s in ANDROID_SUFFIXES for s in _suffixes(normalise_filename(name)))


def _zip_verdict(path: Path, claimed: str) -> Verdict:
    """A ZIP container: an Office document, or something to refuse."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return Verdict(MISMATCH, "bad_zip")
    names = {info.filename.casefold() for info in infos}
    if names & _APP_ENTRIES:
        return Verdict(EXEC, "app_package")
    suffix = claimed.lstrip(".")
    total = sum(info.file_size for info in infos)
    packed = sum(info.compress_size for info in infos)
    if total > ZIP_MAX_UNCOMPRESSED or total > ZIP_MAX_RATIO * max(packed, 1):
        if suffix in OFFICE_ZIP_SUFFIXES:
            # A big spreadsheet is not an installer: kept for the owner's eye,
            # never unpacked or parsed.
            return Verdict(MISMATCH, "zip_too_large")
        return Verdict(ARCHIVE, "zip_bomb")
    if suffix not in OFFICE_ZIP_SUFFIXES:
        # A ZIP posing as a PDF or a CSV is an archive under another name.
        return Verdict(ARCHIVE, "zip_container")
    marker = "mimetype" if suffix in _ODF_ZIP else "[content_types].xml"
    if marker not in names:
        return Verdict(ARCHIVE, "not_office")
    if any(n.endswith("vbaproject.bin") for n in names):
        return Verdict(MACRO, "vba_project")
    if suffix in MACRO_SUFFIXES:
        return Verdict(MACRO, "suffix")
    return SAFE


def sniff_magic(path: Path | str, claimed_suffix: str | None = None) -> Verdict:
    """Check the bytes of a downloaded file of an allowed type.

    The signature must match the claimed suffix. A ``PK`` container passes
    only as an Office document (of those, MIYA parses .docx/.xlsx/.xlsm) with
    ``[Content_Types].xml`` and none of ``AndroidManifest.xml``,
    ``classes.dex`` or ``META-INF/MANIFEST.MF``, and within the
    central-directory size and ratio limits (an Office file over them is a
    mismatch, kept but never parsed); a VBA project makes it a macro
    document. Executables (MZ, ELF, dex, Mach-O) are refused whatever the
    name says; an OLE file posing as OOXML is a mismatch and is never parsed.
    A name claiming an image (.jpg, .png, .webp, .gif) must hold JPEG, PNG,
    GIF or WebP bytes, or it is a mismatch the vision model never sees.
    """
    path = Path(path)
    claimed = (claimed_suffix or path.suffix or "").casefold()
    if claimed and not claimed.startswith("."):
        claimed = f".{claimed}"
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return Verdict(MISMATCH, "unreadable")

    if head.startswith(_EXEC_MAGIC) or _windows_program(head) or _dex(head):
        return Verdict(EXEC, "magic")
    if head.startswith(_ZIP_MAGIC):
        return _zip_verdict(path, claimed)
    if head.startswith(_ARCHIVE_MAGIC) or _BZIP2.match(head):
        return Verdict(ARCHIVE, "magic")
    if len(head) > 262 and head[257:262] == b"ustar":
        return Verdict(ARCHIVE, "magic")
    if head.startswith(_OLE_MAGIC):
        # Legacy Office, or a password-protected OOXML: not what was claimed.
        return Verdict(MISMATCH, "ole_container")

    if claimed in IMAGE_SUFFIXES and image_media_type(head) is None:
        return Verdict(MISMATCH, "not_image")
    if claimed in OOXML_SUFFIXES:
        return Verdict(MISMATCH, "not_zip")
    if claimed == ".pdf" and b"%PDF-" not in head[:1024]:
        return Verdict(MISMATCH, "not_pdf")
    if claimed in _TEXT_SUFFIXES and b"\x00" in head and not _utf16(head):
        return Verdict(MISMATCH, "binary_text")
    return SAFE


def image_media_type(head: bytes) -> str | None:
    """The media type of an image's first bytes: JPEG, PNG, GIF or WebP, else
    None. The vision call is sent what the bytes are, not what the name says."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _windows_program(head: bytes) -> bool:
    """MZ, then a PE header where the DOS header points, or binary bytes.

    A bare «MZ» prefix is also how a CSV of «MZ Logistics» rows begins.
    """
    if not head.startswith(b"MZ") or len(head) < 64:
        return False
    offset = int.from_bytes(head[60:64], "little")
    if head[offset : offset + 4] == b"PE\x00\x00":
        return True
    return b"\x00" in head[2:64]


def _dex(head: bytes) -> bool:
    """Android bytecode: «dex\\n», a three-digit version, NUL."""
    return head.startswith(b"dex\n") and head[7:8] == b"\x00"


def _utf16(head: bytes) -> bool:
    """A UTF-16 text file has NUL bytes too; its byte-order mark tells it apart."""
    return head.startswith((b"\xff\xfe", b"\xfe\xff"))


# --- the owner's warning queue (interactions.meta) ------------------------------

SAFETY_NOTICE_KEY = "safety_notice"
SAFETY_NOTIFIED_KEY = "safety_notified"
# Set with safety_notified on a warning that was not sent because its chat
# was already warned today (JV-09).
SAFETY_FOLDED_KEY = "safety_folded"
NOTICE_DANGEROUS_FILE = "dangerous_file"
NOTICE_MACRO = "macro"
# A warning about a message older than this is history, not news: an archive
# import or a long outage queues none.
NOTICE_MAX_AGE = timedelta(days=7)


def notice_for(verdict: Verdict) -> str | None:
    """Which warning a file earns: a dangerous one, or a macro read as text."""
    if verdict.dangerous:
        return NOTICE_DANGEROUS_FILE
    if verdict.kind == MACRO:
        return NOTICE_MACRO
    return None


def with_file_verdict(
    meta: dict[str, Any] | None, verdict: Verdict, *, notify: bool
) -> dict[str, Any]:
    """``meta.safety`` for a dangerous file, and the owner's warning queued.

    A warning is queued at most once per message: a key already there, sent
    or not, is never replaced — except a message-shield warning (a scam or a
    possible hijack, JV-09) by a dangerous file's own. That one says not to
    open the file, so it goes out even when the shield's warning was already
    sent or folded into the chat's daily one.
    """
    out = dict(meta or {})
    if verdict.dangerous:
        out["safety"] = verdict.as_safety()
    notice = notice_for(verdict)
    queued = out.get(SAFETY_NOTICE_KEY)
    if not notify or notice is None:
        return out
    if queued is None:
        out[SAFETY_NOTICE_KEY] = notice
    elif verdict.dangerous and queued in CHAT_DAILY_NOTICES:
        out[SAFETY_NOTICE_KEY] = notice
        out.pop(SAFETY_NOTIFIED_KEY, None)
        out.pop(SAFETY_FOLDED_KEY, None)
    return out


# --- the message shield (JV-09) ---------------------------------------------------
#
# Verdicts in ``interactions.meta.safety``. A clean message gets no key at all.

MessageVerdictKind = Literal["ok", "spam", "scam", "hijack"]

VERDICT_OK: MessageVerdictKind = "ok"
VERDICT_SPAM: MessageVerdictKind = "spam"
VERDICT_SCAM: MessageVerdictKind = "scam"
VERDICT_HIJACK: MessageVerdictKind = "hijack"

# The message shield's verdicts that hide a message (spam and scam).
UNSAFE_MESSAGE_VERDICTS: tuple[str, ...] = (VERDICT_SPAM, VERDICT_SCAM)
# Never an open question, never nudged, never answered: spam, scam, and a
# message that carried an installer or an archive (JV-01).
HIDDEN_VERDICTS: tuple[str, ...] = (*UNSAFE_MESSAGE_VERDICTS, VERDICT_DANGEROUS_FILE)
# Never answered by the AI; a possible hijack is shown to the owner but the AI
# stays silent there too.
REPLY_BLOCKING_VERDICTS: tuple[str, ...] = (*HIDDEN_VERDICTS, VERDICT_HIJACK)

# Soft signals add up to this before a message counts as spam and auto-reply
# is blocked. Every single soft signal weighs less (pinned by a test), so one
# alone — a first-ever contact above all (owner decision 2) — never blocks.
SPAM_SCORE = 50
# A soft score never reaches the hard one.
SOFT_SCORE_CAP = HARD_SCORE - 1

# Hard rules: any one of them quarantines the message (no reply, no download,
# no model call, the owner warned).
R_TEXT_LINK_MISMATCH = "text_link_mismatch"
R_LOOKALIKE_DOMAIN = "lookalike_domain"
R_BRAND_DOMAIN = "brand_domain"
R_MIXED_SCRIPT_DOMAIN = "mixed_script_domain"
R_USERINFO_URL = "userinfo_url"
R_EXEC_URL = "exec_url"
R_ARCHIVE_URL = "archive_url"
R_EXEC_FILE = "exec_file"
R_ARCHIVE_FILE = "archive_file"
R_CODE_REQUEST = "code_request"
R_CARD_DATA_REQUEST = "card_data_request"
R_TELEGRAM_SCAM = "telegram_scam_flag"
R_TELEGRAM_FAKE = "telegram_fake_flag"
R_PHOTO_LURE = "photo_lure"
R_BRAND_PATH = "brand_path"
R_LOGIN_PAGE = "login_page"
R_HTML_FILE = "html_file"
R_MONEY_PRETEXT = "money_pretext"
R_QR_RECEIVE = "qr_receive"

HARD_REASONS = frozenset(
    {
        R_TEXT_LINK_MISMATCH,
        R_LOOKALIKE_DOMAIN,
        R_BRAND_DOMAIN,
        R_MIXED_SCRIPT_DOMAIN,
        R_USERINFO_URL,
        R_EXEC_URL,
        R_ARCHIVE_URL,
        R_EXEC_FILE,
        R_ARCHIVE_FILE,
        R_CODE_REQUEST,
        R_CARD_DATA_REQUEST,
        R_TELEGRAM_SCAM,
        R_TELEGRAM_FAKE,
        R_PHOTO_LURE,
        R_BRAND_PATH,
        R_LOGIN_PAGE,
        R_HTML_FILE,
        R_MONEY_PRETEXT,
        R_QR_RECEIVE,
    }
)

# Soft signals and their weights.
R_SHORTENER = "shortener"
R_FREE_HOST = "free_host"
R_CHEAP_TLD = "cheap_tld"
R_PUNYCODE = "punycode"
R_BARE_IP = "bare_ip"
R_LURE = "lure_words"
R_URGENCY = "urgency"
R_MASS_FORWARD = "mass_forward"
R_PROMO = "promo"
R_FORWARDED = "forwarded"
R_VIA_BOT = "via_bot"
R_FIRST_CONTACT = "first_contact"
R_SCRIPT_SWITCH = "script_switch"
R_PRETEXT = "pretext"
R_MONEY_ASK = "money_ask"
R_JOB_AD = "job_ad"

SOFT_WEIGHTS: dict[str, int] = {
    R_SHORTENER: 25,
    R_FREE_HOST: 25,
    R_CHEAP_TLD: 20,
    R_PUNYCODE: 30,
    R_BARE_IP: 30,
    R_LURE: 20,
    R_URGENCY: 10,
    R_MASS_FORWARD: 20,
    R_PROMO: 25,
    R_FORWARDED: 10,
    R_VIA_BOT: 20,
    R_FIRST_CONTACT: 15,
    R_SCRIPT_SWITCH: 15,
    R_PRETEXT: 20,
    R_MONEY_ASK: 20,
    R_JOB_AD: 25,
}

# Possible hijack: a contact with history suddenly sends what they never did.
R_HIJACK_FILE = "hijack_file"
R_HIJACK_SHORT_LINK = "hijack_short_link"
R_HIJACK_LOAN = "hijack_loan"
# This many earlier messages from them before "unlike themselves" means
# anything; with fewer there is nothing to compare with.
HIJACK_MIN_HISTORY = 3
# The script switch needs this much to compare: earlier messages with
# letters, and letters in this one.
SCRIPT_MIN_HISTORY = 5
SCRIPT_MIN_LETTERS = 15
SCRIPT_DOMINANCE = 0.8

NOTICE_SCAM = "scam"
NOTICE_HIJACK = "hijack"
# Warned about at most once per chat per (owner's calendar) day.
CHAT_DAILY_NOTICES = frozenset({NOTICE_SCAM, NOTICE_HIJACK})
# media.skipped for a quarantined message's photo, voice or file.
SKIP_QUARANTINED = "quarantined"
# media.skipped for a spam message's big file: never worth the owner's tap.
SKIP_SPAM = "spam"

# The domains a look-alike imitates. The owner adds their own verified ones
# with SAFETY_EXTRA_SAFE_DOMAINS; a host is safe when it is one of these or a
# subdomain of one. Being safe never makes an installer link safe.
#
# The payment, bank and government ones are also what a misspelt name is
# compared with (a «paymee.uz»); generic names like «google» or «telegram»
# are not, since a real «doodle.com» or «telegraf.com» is one letter away —
# the brand and mixed-script rules catch those.
LOOKALIKE_TARGET_DOMAINS = frozenset(
    {
        # payments and cards
        "payme.uz",
        "paycom.uz",
        "click.uz",
        "uzum.uz",
        "uzumbank.uz",
        "apelsin.uz",
        "paynet.uz",
        "uzcard.uz",
        "humocard.uz",
        "humo.uz",
        "visa.com",
        "mastercard.com",
        # banks
        "cbu.uz",
        "kapitalbank.uz",
        "nbu.uz",
        "agrobank.uz",
        "sqb.uz",
        "ipakyulibank.uz",
        "hamkorbank.uz",
        "asakabank.uz",
        "aloqabank.uz",
        "xb.uz",
        "ofb.uz",
        "infinbank.com",
        "tbcbank.uz",
        "anorbank.uz",
        "davrbank.uz",
        "trustbank.uz",
        "octobank.uz",
        "hayotbank.uz",
        "turonbank.uz",
        "universalbank.uz",
        "mkbank.uz",
        # government
        "gov.uz",
        "soliq.uz",
        "lex.uz",
        "mvd.uz",
        "customs.uz",
        "uzpost.uz",
    }
)
SENSITIVE_SAFE_DOMAINS = LOOKALIKE_TARGET_DOMAINS | frozenset(
    {
        # accounts people get phished for
        "telegram.org",
        "telegram.me",
        "t.me",
        "whatsapp.com",
        "wa.me",
        "google.com",
        "apple.com",
        "icloud.com",
    }
)
# Google's own share links: a goo.gl host, but they open only Google Maps or
# Google Photos, so they hide no target and are no short link. The older
# «goo.gl/maps/…» links open only Google Maps too.
GOOGLE_APP_LINKS = frozenset({"maps.app.goo.gl", "photos.app.goo.gl"})
_GOOGLE_MAPS_PATH = re.compile(r"(?i)^/maps(?:/|$)")
SAFE_DOMAINS = SENSITIVE_SAFE_DOMAINS | frozenset(
    {
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "facebook.com",
        "googleapis.com",
        "googleusercontent.com",
        "gstatic.com",
        "microsoft.com",
        "live.com",
        "yandex.uz",
        "yandex.ru",
        "yandex.com",
        "mail.ru",
        "gmail.com",
        "beeline.uz",
        "ucell.uz",
        "uztelecom.uz",
        "mobi.uz",
        "uzmobile.uz",
        "olx.uz",
        "korzinka.uz",
        "express24.uz",
        "kun.uz",
        "gazeta.uz",
        "daryo.uz",
        "alibaba.com",
        "1688.com",
        "taobao.com",
        "aliexpress.com",
        "wildberries.ru",
        "ozon.ru",
        # the marketplaces clients buy from in China, Korea and Turkey
        "tmall.com",
        "jd.com",
        "yangkeduo.com",
        "pinduoduo.com",
        "temu.com",
        "trendyol.com",
        "hepsiburada.com",
        "coupang.com",
        "gmarket.co.kr",
        "amazon.com",
        "uzum.market",
        # the parcel trackers a seller's track numbers open in
        "17track.net",
        "cainiao.com",
        "parcelsapp.com",
        "track24.ru",
        # maps a client shares a pickup address with
        "2gis.uz",
        "2gis.ru",
        "2gis.com",
        *GOOGLE_APP_LINKS,
    }
)

# Names a look-alike borrows: a host that carries one and is not on the
# allowlist is a hard hit. Matched as a whole part of a label, or glued to
# one of the lure affixes below («payme-uz», «paymebonus», «tgpremium»).
BRAND_TOKENS = frozenset(
    {
        "payme",
        "paycom",
        "click",
        "uzum",
        "uzumbank",
        "apelsin",
        "paynet",
        "uzcard",
        "humo",
        "humocard",
        "kapitalbank",
        "agrobank",
        "hamkorbank",
        "ipakyuli",
        "ipakyulibank",
        "asakabank",
        "aloqabank",
        "xalqbank",
        "anorbank",
        "tbcbank",
        "davrbank",
        "octobank",
        "infinbank",
        "soliq",
        "mygov",
        "gov",
        # the post and the customs a cargo client hears from
        "uzpost",
        "customs",
        "bojxona",
        "tamozhnya",
        "tamojnya",
        "tamozhnia",
        "telegram",
        "whatsapp",
    }
)
_BRAND_AFFIXES = frozenset(
    {
        "uz",
        "pay",
        "bonus",
        "bank",
        "app",
        "online",
        "gift",
        "premium",
        "card",
        "login",
        "verify",
        "secure",
        "support",
        "official",
        "help",
        "free",
        "web",
        "auth",
        # the lure in Uzbek and Russian: a refund, a cashback, a promotion
        "qaytarish",
        "qaytarildi",
        "qaytim",
        "vozvrat",
        "refund",
        "cashback",
        "keshbek",
        "kashback",
        "aksiya",
        "akciya",
        "aktsiya",
        "sovga",
        "yutuq",
        "prize",
        "tolov",
        "tulov",
        "oplata",
        "payment",
        "kompensatsiya",
        "kompensaciya",
        "pul",
        "karta",
        "wallet",
        "tasdiq",
        "tiklash",
        "restore",
        "unblock",
    }
)
# Brands that are also everyday words and company names: «humo» is the
# bird («Humo Express», «Humo Trans»), «uzum» the grape, «apelsin» the orange;
# «customs» and «bojxona» name every broker («customs-broker.uz»). Read as the
# brand only alone in a label or beside a lure affix.
_WORD_BRANDS = frozenset(
    {
        "humo",
        "click",
        "uzum",
        "apelsin",
        "customs",
        "bojxona",
        "tamozhnya",
        "tamojnya",
        "tamozhnia",
    }
)
# A login or verify page: on a free host, a cheap TLD or a bare IP address it
# is a credential phish («yiwu-sklad-login.web.app»). Read in the host's own
# labels; «kirish» (an entrance, a door shop's word) is no such word.
_CREDENTIAL_WORDS = frozenset(
    {
        "login",
        "signin",
        "logon",
        "verify",
        "verification",
        "auth",
        "secure",
        "tasdiqlash",
        "vhod",
        "unlock",
        "unblock",
    }
)
# In the path of a free host or a bare address, a restore page too
# («45.141.84.212/click/restore»); in a host name «restore» is a furniture
# workshop's.
_CREDENTIAL_PATH_WORDS = _CREDENTIAL_WORDS | frozenset(
    {"restore", "recovery", "tiklash", "wallet", "kirish"}
)
_FUZZY_MIN_NAME = 5

SHORTENER_DOMAINS = frozenset(
    {
        "bit.ly",
        "bitly.com",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "cutt.ly",
        "is.gd",
        "v.gd",
        "rb.gy",
        "ow.ly",
        "shorturl.at",
        "clck.ru",
        "tiny.cc",
        "s.id",
        "rebrand.ly",
        "u.to",
        "qps.ru",
        "vk.cc",
        "buff.ly",
        "lnkd.in",
        "t.ly",
        "shorturl.asia",
    }
)
FREE_HOST_DOMAINS = frozenset(
    {
        "telegra.ph",
        "blogspot.com",
        "wixsite.com",
        "weebly.com",
        "github.io",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "workers.dev",
        "web.app",
        "firebaseapp.com",
        "glitch.me",
        "000webhostapp.com",
        "tilda.ws",
        "ngrok.io",
        "ngrok-free.app",
        "herokuapp.com",
        "replit.app",
        "surge.sh",
        "webflow.io",
        "wordpress.com",
        "ucoz.ru",
        "narod.ru",
        "site123.me",
    }
)
# Services known by name: their names are never read as a look-alike of a
# brand («telegra.ph» is not «telegram»), though they stay soft signals.
_KNOWN_HOSTS = SHORTENER_DOMAINS | FREE_HOST_DOMAINS
CHEAP_TLDS = frozenset(
    {
        "xyz",
        "top",
        "click",
        "link",
        "site",
        "online",
        "shop",
        "store",
        "live",
        "icu",
        "buzz",
        "monster",
        "rest",
        "cfd",
        "sbs",
        "bond",
        "lat",
        "cyou",
        "win",
        "bid",
        "loan",
        "gq",
        "tk",
        "ml",
        "cf",
        "ga",
        "work",
        "fun",
        "space",
        "website",
        "pw",
        "su",
        "zip",
        "mov",
    }
)
# Second-level labels under which the registrable name is one level deeper
# («foo.com.uz», «bar.co.uk»).
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "msk", "spb"})
# Top-level domains a bare «name.tld» in text is read as a link for; a file
# name («hisob.pdf») or a code («GS367.A») is not one.
_BARE_TLDS = (
    "uz|com|ru|net|org|io|me|info|biz|app|dev|ly|gl|gd|gy|cc|kz|kg|tj|tm|ph|to|in|co"
    "|us|tv|ws|pro|page|ai|xyz|top|click|link|site|online|shop|store|live|icu|buzz"
    "|monster|rest|cfd|sbs|bond|lat|cyou|win|bid|loan|gq|tk|ml|cf|ga|work|fun|space"
    "|website|pw|su|zip|mov|uk|de|cn|tr|ae"
)

_SCHEME_URL = re.compile(r"(?i)(?<![\w@])(?:(?:https?|ftp)://|www\.)[^\s<>\"'«»]+")
_BARE_URL = re.compile(
    rf"(?i)(?<![\w@./:-])(?:[\w-]+\.)+(?:{_BARE_TLDS})(?![\w-])(?:/[^\s<>\"'«»]*)?"
)
_URL_TRAILING = ".,;:!?)]}»\"'"

# Cyrillic letters that pass for Latin ones, from the client-code table
# (codes.LOOKALIKE) plus the few it does not need.
_CYRILLIC_LOOKALIKES: dict[str, str] = {
    **{
        ch.casefold(): latin.casefold()
        for latin, chars in codes.LOOKALIKE.items()
        for ch in chars
        if ch != latin
    },
    "і": "i",
    "ј": "j",
    "ѕ": "s",
    "ԁ": "d",
    "ԛ": "q",
    "ԝ": "w",
    "ү": "y",
}

Source = Literal["text", "text_link", "button"]


@dataclass(frozen=True, slots=True)
class FoundUrl:
    """One link a message carries, wherever it sits."""

    url: str
    host: str
    path: str
    source: Source = "text"
    # The text a Telegram text link shows, when it is one.
    shown: str | None = None
    userinfo: bool = False


@dataclass(frozen=True, slots=True)
class LinkVerdict:
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()


def _trim_url(raw: str) -> str:
    return raw.rstrip(_URL_TRAILING)


def _parse(raw: str, source: Source, shown: str | None = None) -> FoundUrl | None:
    url = _trim_url(raw.strip())
    if not url:
        return None
    target = url if re.match(r"(?i)^[a-z][a-z0-9+.-]*://", url) else f"http://{url}"
    try:
        parts = urlsplit(target)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    return FoundUrl(
        url=url,
        host=unicodedata.normalize("NFKC", host).casefold(),
        path=parts.path or "",
        source=source,
        shown=shown,
        userinfo="@" in (parts.netloc or ""),
    )


def _urls_in_text(text: str) -> list[str]:
    found = [m.group(0) for m in _SCHEME_URL.finditer(text)]
    rest = _SCHEME_URL.sub(" ", text)
    found += [m.group(0) for m in _BARE_URL.finditer(rest)]
    return found


def extract_urls(
    text: str | None,
    *,
    text_links: Iterable[tuple[str, str]] = (),
    buttons: Iterable[str] = (),
) -> list[FoundUrl]:
    """Every link a message carries: written in its text, hidden behind a
    Telegram text link (``(shown text, url)`` pairs) and on its URL buttons.

    The text is read with format characters removed, so a zero-width space
    cannot split a domain past the reader.
    """
    clean = display_name(text or "")
    out: list[FoundUrl] = []
    for raw in _urls_in_text(clean):
        found = _parse(raw, "text")
        if found is not None:
            out.append(found)
    for shown, url in text_links:
        found = _parse(url, "text_link", display_name(shown or ""))
        if found is not None:
            out.append(found)
    for url in buttons:
        found = _parse(url, "button")
        if found is not None:
            out.append(found)
    return out


def normalise_domain(domain: str) -> str:
    """«https://www.Payme.uz/» → «payme.uz»: what an allowlist entry means."""
    text = unicodedata.normalize("NFKC", domain or "").strip().casefold()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("@")[-1].split(":")[0].strip(".")
    return text.removeprefix("www.")


def _safe_domains(extra_safe: Iterable[str]) -> frozenset[str]:
    """The allowlist: the code constant plus the owner's verified domains."""
    return SAFE_DOMAINS | {normalise_domain(d) for d in extra_safe if d.strip()}


def _within(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in domains)


def _labels(host: str) -> list[str]:
    return [label for label in host.split(".") if label]


def registrable(host: str) -> str:
    """The name a person registered: «pay.payme.uz» → «payme.uz»,
    «shop.foo.com.uz» → «foo.com.uz»."""
    labels = _labels(normalise_domain(host))
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _name_label(host: str) -> str:
    """The label before the public suffix: «payme» of «pay.payme.uz»."""
    labels = _labels(registrable(host))
    return labels[0] if labels else ""


def _idna_decoded(host: str) -> str:
    if "xn--" not in host:
        return host
    try:
        return host.encode("ascii").decode("idna").casefold()
    except UnicodeError:
        return host


def _latinised(label: str) -> str:
    return "".join(_CYRILLIC_LOOKALIKES.get(ch, ch) for ch in label)


def _is_cyrillic(ch: str) -> bool:
    return "CYRILLIC" in unicodedata.name(ch, "")


def _mixed_script(label: str) -> bool:
    """Latin and Cyrillic letters in one label: «pаyme» with a Cyrillic «а»."""
    letters = [ch for ch in label if ch.isalpha()]
    has_latin = any(ch.isascii() for ch in letters)
    has_cyrillic = any(_is_cyrillic(ch) for ch in letters)
    return has_latin and has_cyrillic


def _brand_hit(labels: list[str]) -> bool:
    """A brand name in any label but the top-level one. «gov» right under a
    country code is that country's own government registry («gov.kz»)."""
    last = len(labels) - 2
    names = [lb for i, lb in enumerate(labels[:-1]) if not (i == last and lb == "gov")]
    return any(_brand_in_label(label) or _confused_brand(label) for label in names)


# Digits that pass for letters in a name («te1egram», «0ctobank»).
_DIGIT_LOOKALIKES = str.maketrans({"0": "o", "1": "l", "3": "e", "4": "a", "5": "s"})


def _skeleton(label: str) -> str:
    """What a label reads as at a glance: «telegrarn» → «telegram»."""
    return _unconfused(label.translate(_DIGIT_LOOKALIKES))


def _confused_brand(label: str) -> bool:
    """A brand spelled with letters that pass for its own: «telegrarn»,
    «whatsaрp» aside (that is the mixed-script rule), «te1egram». Only the
    names that are no everyday word, from five letters: «humo» and «click»
    are words, and a short name is too easily another word."""
    own = set(re.split(r"[-_]+", label))
    for part in re.split(r"[-_]+", _skeleton(label)):
        if part in own or len(part) < _FUZZY_MIN_NAME or part in _WORD_BRANDS:
            continue
        if part in BRAND_TOKENS:
            return True
    return False


def _brand_in_label(label: str) -> bool:
    parts = [part for part in re.split(r"[-_0-9]+", label) if part]
    for part in parts:
        if part in BRAND_TOKENS:
            if part not in _WORD_BRANDS:
                return True
            # «humo-express», «click-cargo»: a company named with the word.
            # Alone («humo.top») or next to a lure word («humo-bonus») it is
            # the brand.
            if len(parts) == 1 or any(p in _BRAND_AFFIXES for p in parts):
                return True
            continue
        for brand in BRAND_TOKENS:
            if len(brand) < 4:
                continue
            if part.startswith(brand) and part[len(brand) :] in _BRAND_AFFIXES:
                return True
            if part.endswith(brand) and part[: -len(brand)] in _BRAND_AFFIXES:
                return True
    return False


# Letters and pairs that pass for each other at a glance. On a short name a
# single swapped letter counts as a look-alike only when it is one of these
# («uzkard» for «uzcard»), never any letter («uzcars» is another word).
_CONFUSABLE_LETTERS = frozenset(
    frozenset(pair)
    for pair in (
        ("c", "k"),
        ("q", "k"),
        ("x", "h"),
        ("o", "0"),
        ("l", "1"),
        ("i", "1"),
        ("i", "l"),
        ("m", "n"),
        ("u", "v"),
        ("s", "5"),
        ("e", "3"),
        ("a", "4"),
        ("g", "q"),
    )
)
_CONFUSABLE_PAIRS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
# Below this length a look-alike is one edit away; from it, two.
_FUZZY_LONG_NAME = 8


def _unconfused(name: str) -> str:
    """«payrne» → «payme»: letter pairs read as one letter."""
    for pair, letter in _CONFUSABLE_PAIRS:
        name = name.replace(pair, letter)
    return name


def _near(name: str, other: str) -> bool:
    limit = 1 if len(other) < _FUZZY_LONG_NAME else 2
    if abs(len(name) - len(other)) > limit:
        return False
    if Levenshtein.distance(name, other, score_cutoff=limit) > limit:
        return False
    if len(name) == len(other) and len(other) < _FUZZY_LONG_NAME:
        swapped = [frozenset((a, b)) for a, b in zip(name, other, strict=True) if a != b]
        return all(pair in _CONFUSABLE_LETTERS for pair in swapped)
    return True


def _fuzzy_lookalike(name: str, targets: Iterable[str]) -> bool:
    """A name a payment, bank or government name is misspelt into: one edit
    away (two from eight letters), or the same once confusable letters are
    read as what they imitate, and not that name itself."""
    if len(name) < _FUZZY_MIN_NAME:
        return False
    plain = _unconfused(name)
    for domain in targets:
        other = _name_label(domain)
        if len(other) < _FUZZY_MIN_NAME or other == name:
            continue
        if plain == other or _near(name, other):
            return True
    return False


def _bare_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _public_ip(host: str) -> bool:
    """A bare address on the internet: a LAN camera or router («192.168.1.10»)
    is no phishing page."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return address.is_global


def _path_words(path: str) -> list[str]:
    """The words of a link's path: «/click/restore-card» → click, restore, card."""
    return [w for w in re.split(r"[^a-z]+", path.casefold()) if w]


def _brand_path(path: str) -> bool:
    """A payment, bank or government domain written into another site's path:
    «secure-checkout.top/payme.uz/confirm» shows «payme.uz» to the eye."""
    segments = [seg for seg in path.casefold().split("/") if "." in seg]
    return any(_within(seg, LOOKALIKE_TARGET_DOMAINS) for seg in segments)


def _credential_host(labels: list[str]) -> bool:
    """A login or verify word in a host's own labels («yiwu-sklad-login»)."""
    return any(
        part in _CREDENTIAL_WORDS
        for label in labels
        for part in re.split(r"[-_0-9]+", label)
    )


def _google_app_link(found: FoundUrl) -> bool:
    """A Google Maps or Photos share link, the new kind or the old goo.gl/maps."""
    if _within(found.host, GOOGLE_APP_LINKS):
        return True
    return found.host == "goo.gl" and bool(_GOOGLE_MAPS_PATH.match(found.path))


def _file_reason(path: str) -> str | None:
    """An installer or an archive at the end of a link's path."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    verdict = classify_attachment(name)
    if verdict.kind == EXEC:
        return R_EXEC_URL
    if verdict.kind == ARCHIVE:
        return R_ARCHIVE_URL
    return None


def link_verdict(found: FoundUrl, *, extra_safe: Iterable[str] = ()) -> LinkVerdict:
    """What one link is: hard hits (a look-alike, a disguise) and soft signals.

    The allowlist is the code constant plus ``extra_safe`` (the owner's
    SAFETY_EXTRA_SAFE_DOMAINS). An allowlisted host is never a look-alike,
    but an installer or an archive behind it is still a hard hit: the domain
    list never loosens the file rules.
    """
    hard: list[str] = []
    soft: list[str] = []
    host = found.host
    safe = _safe_domains(extra_safe)
    file_reason = _file_reason(found.path)
    if file_reason:
        hard.append(file_reason)
    # A text link that opens a real allowlisted site disguises nothing
    # («youtube.com» opening youtu.be, «gov.uz» opening my.gov.uz).
    if found.source == "text_link" and found.shown and not _within(host, safe):
        named = [u for u in (_parse(r, "text") for r in _urls_in_text(found.shown)) if u]
        if any(registrable(u.host) != registrable(host) for u in named):
            hard.append(R_TEXT_LINK_MISMATCH)
    if found.userinfo:
        hard.append(R_USERINFO_URL)

    if _bare_ip(host):
        soft.append(R_BARE_IP)
        if _public_ip(host):
            # «45.141.84.212/click/restore»: a brand or a login page behind a
            # bare address is no one's real site.
            words = _path_words(found.path)
            if any(w in BRAND_TOKENS for w in words) or _brand_path(found.path):
                hard.append(R_BRAND_PATH)
            if any(w in _CREDENTIAL_PATH_WORDS for w in words):
                hard.append(R_LOGIN_PAGE)
        return LinkVerdict(tuple(hard), tuple(soft))
    decoded = _idna_decoded(host)
    if decoded != host:
        soft.append(R_PUNYCODE)
    if _within(host, safe) or _within(decoded, safe) or _google_app_link(found):
        return LinkVerdict(tuple(hard), tuple(soft))

    labels = _labels(decoded)
    if any(_mixed_script(label) for label in labels):
        hard.append(R_MIXED_SCRIPT_DOMAIN)
    latin_labels = [_latinised(label) for label in labels]
    latin_host = ".".join(latin_labels)
    sensitive = SENSITIVE_SAFE_DOMAINS
    if _within(latin_host, sensitive) and latin_host != decoded:
        # Every letter a Cyrillic stand-in: «рауме.uz» spelled to read «payme».
        hard.append(R_MIXED_SCRIPT_DOMAIN)
    elif _brand_hit(latin_labels):
        hard.append(R_BRAND_DOMAIN)
    elif not _within(host, _KNOWN_HOSTS) and _fuzzy_lookalike(
        _name_label(latin_host), LOOKALIKE_TARGET_DOMAINS
    ):
        hard.append(R_LOOKALIKE_DOMAIN)

    if _brand_path(found.path):
        hard.append(R_BRAND_PATH)
    free_host = _within(host, FREE_HOST_DOMAINS)
    cheap = bool(labels) and labels[-1] in CHEAP_TLDS
    if free_host or cheap:
        # A free host's own labels are its service's name: only the part the
        # sender chose counts («yiwu-sklad-login» of «….web.app»).
        own = labels[:-1]
        if free_host:
            suffix = next(d for d in FREE_HOST_DOMAINS if _within(host, (d,)))
            own = labels[: len(labels) - len(_labels(suffix))]
        path_login = free_host and any(
            w in _CREDENTIAL_PATH_WORDS for w in _path_words(found.path)
        )
        if _credential_host(own) or path_login:
            hard.append(R_LOGIN_PAGE)
    if _within(host, SHORTENER_DOMAINS):
        soft.append(R_SHORTENER)
    if free_host:
        soft.append(R_FREE_HOST)
    if cheap:
        soft.append(R_CHEAP_TLD)
    return LinkVerdict(tuple(dict.fromkeys(hard)), tuple(dict.fromkeys(soft)))


def is_short_link(found: FoundUrl) -> bool:
    """A link whose real target is hidden: a shortener or a free host. A
    Google Maps or Photos share link opens only Google's own pages."""
    return _within(found.host, _KNOWN_HOSTS) and not _google_app_link(found)


# --- the message lexicons ---------------------------------------------------------
#
# Matched on folded text (format characters removed, apostrophes folded,
# casefolded, ё→е). A marker ending in «\b» must end a word; the others are
# stems. Extend them here, with a corpus case in tests/fixtures/scam_corpus.py.


def _markers(markers: Iterable[str]) -> list[re.Pattern[str]]:
    compiled = []
    for marker in markers:
        word_end = marker.endswith(r"\b")
        stem = marker[:-2] if word_end else marker
        tail = r"(?![\w'])" if word_end else ""
        compiled.append(re.compile(r"(?<![\w'])" + re.escape(stem) + tail))
    return compiled


def _fold(text: str) -> str:
    text = fold_apostrophes(display_name(text or "")).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def _any(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


# The one-time-code words of the payment reader (sms_money.OTP_MARKERS) say a
# code is meant; a request verb and an SMS context make it a request for one.
# Its bank-warning words («hech kimga», «никому») say not to share a code and
# name none, so «kelgan tovarni hech kimga bermang» asks for nothing.
_BANK_WARNINGS = frozenset(
    {"hech kimga", "ҳеч кимга", "nikomu", "никому", "ne soobshch", "не сообщ"}
)
_OTP = _markers(
    (
        *(m for m in sms_money.OTP_MARKERS if m not in _BANK_WARNINGS),
        r"kodini\b",
        r"kodim\b",
        r"кодни\b",
        r"кодини\b",
        r"кодим\b",
        r"кодингиз\b",
    )
)
# Where the code came: an SMS, the phone. A bare «kelgan» is every delivery
# («kelgan tovarlar»); it counts only next to the code or the SMS.
_SMS_CONTEXT = _markers(
    (
        "sms",
        "смс",
        "telefoningizga",
        "nomeringizga",
        "raqamingizga",
        "на телефон",
        "на номер",
    )
)
# «Цифры из СМС», «SMS dagi raqamlar»: the one-time code by another name, and
# an SMS context in itself.
_SMS_DIGITS = re.compile(
    r"(?<![\w'])(?:цифр\w* (?:из|в) (?:смс|sms|сообщени\w*)"
    r"|(?:sms|смс) ?(?:dagi|даги|da kelgan|да келган|dan|дан)? "
    r"(?:\d+ (?:xonali |хонали |ta |та )?)?"
    r"(?:raqamlar\w*|raqamni|рақамлар\w*|рақамни|цифр\w*))"
)
# «SMS qilib yuboring» is «send it by SMS» (to a relative without Telegram),
# not «tell me what an SMS said».
_SMS_OUT = re.compile(r"(?<![\w'])(?:sms|смс) (?:qilib|қилиб|килиб)(?![\w'])")
_ARRIVED_CODE = re.compile(
    r"(?<![\w'])(?:kelgan kod|kod\w* kelgan|келган код|код\w* келган|пришедш\w* код"
    r"|код\w*,? (?:котор\w+ )?(?:вам |тебе )?приш(?:ел|ла|ло|ли)(?![\w']))"
)
# Codes that are no one-time code: a client's GS code, a tariff (HS, ТН ВЭД)
# code, a track or a bar code («GS412 kodi», «код клиента», «ТИФ ТН кодини»).
# Removed before the code words are read.
_NAMED_CODE = re.compile(
    r"(?<![\w'])(?:"
    r"(?:gs ?\d*|mijoz|klient|client|клиент|мижоз|tif tn|тиф тн|tn ?ved|тн ?вэд|tn|тн"
    r"|hs|trek|track|трек|shtrix|штрих|pochta|почта|indeks|индекс)"
    r"\w*[ -](?:kod|код|code)\w*"
    r"|(?:код|kod)\w* (?:клиент|klient|тн ?вэд|tn ?ved|тиф|tif|hs|товар|tovar)\w*"
    r"|(?:клиентск|почтов)\w* код\w*|(?:client|tracking|hs) code\w*"
    r")"
)
# The writer's own code («kodim esimda yo'q», «забыла свой код»): its owner
# asking for it means their client code, not a code sent to the reader.
_OWN_CODE = re.compile(
    r"(?<![\w'])(?:kodim\w*|кодим\w*|(?:mening|менинг) (?:\S+ )?(?:kod|код)\w*"
    r"|(?:мой|моего|свой|своего|my) (?:\S+ )?(?:код|kod|code)\w*)"
)
# What makes it the reader's one-time code whoever claims it: it came to the
# reader's phone, or by mistake.
_CODE_TO_READER = re.compile(
    r"(?<![\w'])(?:"
    r"(?:sizga|senga|сизга|сенга) (?:\S+ )?(?:kel|кел)\w*"
    r"|telefoningizga|nomeringizga|raqamingizga|телефонингизга|номерингизга|рақамингизга"
    r"|(?:вам|тебе) (?:\S+ )?(?:приш|отправ|выслан)\w*|приш\w* (?:вам|тебе)"
    r"|(?:отправлен|выслан)\w* (?:вам|тебе)|на (?:ваш|твой)\w* (?:номер|телефон)"
    r"|adashib|адашиб|по ошибке|ошибочно"
    r")(?![\w'])"
)
# ... or it is to confirm something.
_CODE_CONFIRMS = re.compile(
    r"(?<![\w'])(?:tasdiqla|тасдиқла|подтвержд|podtverzhd|верифик)\w*"
)
# The delivery code said to the warehouse keeper at pickup is the procedure,
# not a fraud, unless it is to be written here.
# So is the code a courier asks for once the cargo is in the reader's hands
# («faqat yukni qo'lingizga olgandan keyin ayting»).
_IN_PERSON = re.compile(
    r"(?<![\w'])(?:(?:кладовщ|kladovsh|omborchi|омборчи|skladchi|складчи)\w*"
    r"|при (?:выдаче|получении|вручении)|(?:omborga|омборга) (?:kelganda|келганда)"
    r"|(?:qo'lingizga ol(?:gan|ganingiz)|қўлингизга ол(?:ган|ганингиз))\w*"
    r"|(?:yuk|tovar|posilka)\w* ol(?:gan|ganingiz)dan (?:keyin|so'ng)"
    r"|(?:юк|товар|посилка)\w* ол(?:ган|ганингиз)дан (?:кейин|сўнг)"
    r"|после получения (?:груза|посылки|товара)|получив (?:груз|посылку|товар))"
)
_HERE = re.compile(
    r"(?<![\w'])(?:сюда|мне|в (?:этот |наш )?чат|menga|менга|shu yerga|bu yerga"
    r"|шу ерга)(?![\w'])"
)
# «Скажите, а код придёт в смс?», «Ayting-chi, qachon?»: asking how things
# work, not for a code.
_ASK_QUESTION = re.compile(
    r"(?<![\w'])(?:скажите|подскажите|скажи|подскажи|ayting|aytingchi|айтинг|айтингчи)"
    r",? (?:пожалуйста,? |iltimos,? )?(?:а|ли|как\w*|когда|где|куда|сколько|почему"
    r"|зачем|кто|qachon|qayer\w*|qanday|qancha|nima|nega|kim)(?![\w'])"
)
# A Wi-Fi password is no account's.
_WIFI_PASSWORD = re.compile(
    r"(?:wi-?fi|вай-?фа)\w*(?: \S+){0,2}? (?:parol|парол)\w*"
    r"|(?:parol|парол)\w*(?: \S+){0,2}? (?:wi-?fi|вай-?фа)\w*"
)
# Secrets nobody but a fraudster asks for, with or without an SMS.
_SECRET: list[re.Pattern[str]] = [
    *_markers(
        (
            "tasdiqlash kodi",
            "bir martalik kod",
            "parol",
            "пароль",
            r"otp\b",
            "код подтвержден",
            "kod podtverzhden",
            r"pin kod",
            r"пин-код",
            r"пин код",
            "telegram kod",
            "телеграм код",
        )
    ),
    # «одноразовый код», never «одноразовые поддоны».
    re.compile(r"(?<![\w'])(?:одноразов|odnorazov)\w* (?:код|парол|kod|parol)"),
]
# Card secrets: the security code on its own; the expiry date, the back of the
# card and its two sides only beside a bank card, since a contract has a «срок
# действия», a waybill an «оборот» and a payment goes «karta orqali».
_CARD_STRONG = _markers((r"cvv\b", r"cvv2\b", r"cvc\b", r"cvc2\b"))
# A bank card: not an ID card, a company's «карточка предприятия», a
# tachograph card, a picture («картинка»), a carton, nor the writer's own.
_CARD = (
    r"(?<![\w'])(?<!id )(?<!id-)(?<!корхона )(?<!мою )(?<!моей )(?<!моя )(?<!свою )"
    r"(?<!своей )(?:karta(?!m)\w*|карт(?:а(?!м)\w*|ы|е|у|ой|очк\w*)|card|plastik\w*"
    r"|пластик\w*)(?![\w'])"
    r"(?! (?:предприят|организац|компан|фирм|контрагент|тахограф|водител))"
)
_CARD_DETAIL = (
    r"(?:amal qilish muddat|amal muddat|orqa(?:s|d| tomon)|срок\w* действи"
    r"|srok\w* deystvi|на обороте|na oborote|обратн\w* сторон|obratn\w* storon"
    r"|обеих сторон|ikki tomon|ikkala tomon)\w*"
)
# A document has a term and a back too: «karta orqali ... shartnoma amal
# qilish muddati», «на карту, срок действия счета».
_DOCUMENT = (
    r"(?:shartnoma|hisob|ishonchnoma|pasport|hujjat|договор|сч[её]т|доверенност"
    r"|паспорт|контракт|лиценз|пропуск|накладн|документ|предложени)\w*"
)
_CARD_DATA = re.compile(
    rf"{_CARD}[,:]?(?: (?!{_DOCUMENT})[^\s.!?]+){{0,3}}? "
    rf"{_CARD_DETAIL}(?![\w'])(?! {_DOCUMENT})"
    rf"|(?<![\w']){_CARD_DETAIL} (?:(?:вашей|твоей|своей|банковской) )*{_CARD}"
)
# «orqasidagi 3 raqam», «три цифры на обороте»: the security code by another
# name, with or without the card.
_CARD_BACK_DIGITS = re.compile(
    r"(?<![\w'])(?:orqa(?:s|d| tomon)\w*|на обороте|обратн\w* сторон\w*)"
    r"(?: [^\s.!?]+){0,2}? (?:3|uch|три)(?: ta)? (?:raqam|цифр|xonali)\w*"
    r"|(?<![\w'])(?:3|uch|три)(?: ta)? (?:raqam|цифр)\w*(?: [^\s.!?]+){0,2}?"
    r" (?:на обороте|с обратной|orqa(?:s|d| tomon))\w*"
)
# Asking for something to be told, sent or typed: an imperative or a request
# ending only, so neither the negative imperative («aytmang») nor what the
# writer did themselves («yubordim», «yozib qo'ydim», «пришло») is one. Matched
# on folded text, where «ё» is «е». The endings after a stem that ends in a
# consonant: «ayt» → «ayting», «aytgin», «aytsangiz», «aytasizmi», «aytib
# bering», «aytvoring».
_LATIN_ASK = (
    r"(?:ing|ingiz|gin|sangiz|asizmi|ib (?:bering|yuboring|jo'nating|qo'ying)|voring)"
)
_CYRILLIC_ASK = r"(?:инг|ингиз|гин|сангиз|асизми|иб (?:беринг|юборинг|қўйинг)|воринг)"
_REQUEST_VERB = re.compile(
    r"(?<![\w'])(?:"
    # «yubor», «ayt» alone are imperatives too; a bare «yoz» is the summer.
    rf"(?:yubor|jo'nat|ayt|kirit){_LATIN_ASK}?"
    rf"|yoz{_LATIN_ASK}"
    r"|tashla(?:ng|ngiz|gin|sangiz|ysizmi|b (?:bering|yuboring|qo'ying)|voring)?"
    rf"|(?:юбор|жўнат|айт|кирит){_CYRILLIC_ASK}?"
    rf"|ез{_CYRILLIC_ASK}"
    r"|ташла(?:нг|нгиз|гин|сангиз|йсизми|б (?:беринг|юборинг)|воринг)?"
    r"|отправьте|отправь|отправите|пришлите|скажите|скажи|напишите|напиши"
    r"|сообщите|сообщи|продиктуйте|продиктуй|скиньте|скинь|введите|назовите|назови"
    r"|укажите|укажи|подскажите|подскажи"
    r"|otprav'?te|prishlite|skazhite|napishite|skin'?te|vvedite|nazovite|ukazhite"
    r")(?![\w'])"
)
# The advert words of the payment reader say an SMS is an advert, but some of
# them are how any client writes («narx taklif qiling», «подробнее»): those
# never count as a lure here.
_BUSINESS_WORDS = frozenset(
    {
        "taklif",
        "таклиф",
        "predlozheni",
        "предложени",
        "offer",
        "batafsil",
        "батафсил",
        "podrobn",
        "подробн",
        "ulaning",
        "obuna",
    }
)
_LURE = _markers(
    (
        *(m for m in sms_money.ADVERT_MARKERS if m not in _BUSINESS_WORDS),
        "yutib oldingiz",
        "yutdingiz",
        "g'olib",
        "sovrin",
        "bepul",
        "tekin",
        "premium",
        "pul ishlash",
        "kunlik daromad",
        "kunlik foyda",
        "investitsiya",
        "kripto",
        "reklama joyla",
        "reklama xizmat",
        "выиграли",
        "победител",
        "бесплатн",
        "заработ",
        "инвестиц",
        "премиум",
        # «приз», never «признателен» or «призыв».
        r"приз\b",
        r"призы\b",
        r"приза\b",
        "призов",
        "крипт",
        "размещение рекламы",
        "рекламные услуги",
        "vyigrali",
        "besplatn",
        "zarabot",
        # trading signals and mass ad posting
        "bitcoin",
        "биткоин",
        "forex",
        "форекс",
        "guruhlarga reklama",
        "reklama tarqat",
        "рассылка по группам",
    )
)
# Earnings promised by the day or the month, or as a percentage: «kunlik 40%
# foyda», «kuniga 2-3 mln daromad», «kuniga 1 000 000 so'm ishlang».
_EARNINGS = re.compile(
    r"(?<![\w'])(?:kuniga|kunlik|oyiga|haftasiga|в день|в неделю|в месяц|ежедневно"
    r"|per day|a day|per week)"
    r"(?: \S+){0,4}? (?:daromad|foyda|ishlang|ishlab oling|заработ|доход|прибыл|earn)\w*"
    r"|(?<![\w'])(?:daromad|заработ|доход|earning)\w* (?:от|dan|from)(?: \S+){1,3}? "
    r"(?:kuniga|oyiga|haftasiga|в день|в неделю|в месяц|per day|a day|per week)"
    r"|\d ?% (?:foyda|daromad|прибыл\w*|доход\w*|profit)"
)
# Trade words that hold an advert stem: «акциз» (excise) is no «акция», and
# free storage days are a freight term. Removed before the lure is read.
_NOT_LURE = re.compile(
    r"(?<![\w'])(?:(?:акциз|aktsiz|akciz)\w*|бесплатн\w* хранени\w*|bepul saqla\w*)"
)
_URGENCY = _markers(
    (
        "shoshiling",
        "zudlik bilan",
        "faqat bugun",
        "faqat shu hafta",
        "oxirgi imkoniyat",
        "bloklanadi",
        "bloklandi",
        "bloklangan",
        "muzlatiladi",
        "24 soat ichida",
        "фақат бугун",
        "фақат шу ҳафта",
        "срочно",
        "немедленно",
        "только сегодня",
        "только на этой неделе",
        "последний шанс",
        "заблокир",
        "srochno",
        "zablokir",
        "faqat shu oy",
        "фақат шу ой",
        "только в этом месяце",
        "joylar cheklangan",
        "жойлар чекланган",
        "места ограничены",
    )
)
_MASS_FORWARD = _markers(
    (
        "do'stlaringizga",
        "дўстларингизга",
        "barchaga yuboring",
        "guruhlarga yuboring",
        "tarqating",
        "ulashing",
        "перешлите",
        "поделитесь",
        "разошлите",
        "друзьям",
        "share with",
    )
)
# «Поделитесь контактом брокера», «manzilni ulashing»: asking for a contact
# or an address to be shared is no chain letter. Removed before the forward
# words are read.
_NOT_MASS = re.compile(
    r"(?<![\w'])(?:поделитесь|поделись|ulashing|ulashib)\w*,? (?:пожалуйста,? "
    r"|iltimos,? )?(?:контакт|номер|телефон|адрес|локаци|информаци|мнени|kontakt"
    r"|raqam|nomer|manzil|lokatsiya)\w*"
    r"|(?<![\w'])(?:kontakt|raqam|nomer|manzil|lokatsiya)\w* ulashing"
)
# An advert's shape: a channel to join, followers or reach for sale, a site or
# a bot built, a portfolio. A client never writes these.
_PROMO = _markers(
    (
        "kanalimiz",
        "каналимиз",
        "guruhimiz",
        "гуруҳимиз",
        "нашем канале",
        "наш канал",
        "подпишитесь",
        "подписывайтесь",
        "присоединяйтесь",
        "obuna bo'ling",
        "обуна бўлинг",
        "obunachi",
        "обуначи",
        "подписчик",
        "охват",
        "yasab beramiz",
        "ясаб берамиз",
        "создадим сайт",
        "разработаем сайт",
        "портфолио",
        "portfolio",
        "join our",
        "kanalga qo'shiling",
        "каналга қўшилинг",
        "vip kanal",
        "vip guruh",
        "vip-kanal",
        "tarqatamiz",
        "тарқатамиз",
        "разошлем",
        "разместим рекламу",
    )
)
# A job advert: earnings from home, no experience needed, money passed through
# the reader's own card (a money mule). A client asking for work writes none of
# these together, and one alone stays under the spam line.
_JOB_AD = _markers(
    (
        "ish taklifi",
        "иш таклифи",
        "uydan turib ish",
        "uydan turib pul",
        "uydan turib daromad",
        "уйдан туриб иш",
        "уйдан туриб пул",
        "tajriba shart emas",
        "тажриба шарт эмас",
        "tajriba talab qilinmaydi",
        "har o'tkazmadan",
        "kartangiz orqali pul",
        "картангиз орқали пул",
        "работа на дому",
        "без опыта",
        "подработк",
        "через вашу карту",
        "work from home",
    )
)
# «Is this you?» asked on its own: the phrase ends its clause, so «bu sizmi
# buyurtma bergansiz?» and «это вы отправили счет?» are ordinary questions.
_PHOTO_LURE_QUESTION = re.compile(
    r"(?<![\w'])(?:bu sizmi(?:siz)?|bu senmi|это ты|это вы|eto ty|eto vy|is this you)"
    r"(?![\w'])\s*(?:[?!.…]|$)"
)
# «You in the photo?» as the whole question: «Это ты на фото?», never «это ты
# на фото с нашим грузом?» or «как вы на фото видите»; «твои фото тут:», never
# «ваши фото получили».
_PHOTO_LURE: list[re.Pattern[str]] = [
    *_markers(
        (
            "bu sizmi rasmda",
            "sizmi rasmda",
            "rasmda sizmi",
            "rasmdagi sizmi",
            "rasmda sizsiz",
            "videoda sizmi",
            "videodagi sizmi",
            "rasmda senmi",
            "sizmi videoda",
            "senmi videoda",
            "senmi rasmda",
            # «your photo is being spread»: the shame lure
            "rasmingizni tarqat",
            "rasmlaringizni tarqat",
            "videongizni tarqat",
            "расмингизни тарқат",
            "расмларингизни тарқат",
            "видеонгизни тарқат",
            "ваше фото слили",
            "ваши фото слили",
            "ваше видео слили",
            "твои фото слили",
            "твое фото слили",
        )
    ),
    re.compile(
        r"(?<![\w'])(?:это |eto )?(?:ты|вы|ty|vy) (?:на|na) (?:этом |том |etom )?"
        r"(?:фото|видео|снимке|фотке|foto|video)\w*\s*(?:[?!.…]|$)"
    ),
    re.compile(
        r"(?<![\w'])(?:твои|ваши|tvoi|vashi) (?:фото|видео|foto)\w*"
        r"(?: (?:тут|здесь|по ссылке|tut))?\s*(?:[?!:…]|$)"
    ),
    re.compile(r"(?<![\w'])sizning rasmingiz\s*(?:[?!:…]|$)"),
]
# The lure is the whole message, give or take a greeting: a client who asks
# «bu sizmi?» and explains («Kecha ombordan yuk olib ketgan odam») is talking.
_LURE_MAX_EXTRA_WORDS = 3
_LETTER_WORD = re.compile(r"[^\W\d_]{2,}")
# Borrowing money, the hijacked account's usual ask. An ask is one on its own;
# the weaker words (a need, money to my card, a promise to repay) count only
# two together and with no trade words around, since shipping on credit
# («yukni qarzga», «отгрузите в долг»), a refund to one's own card, a balance
# by card «как в прошлый раз» and repaying a debt are business.
_LOAN_ASK = _markers(
    (
        "qarz berib tur",
        "qarz bera olasiz",
        "qarz berasiz",
        r"qarz bering\b",
        "qarz kerak",
        "pul qarz",
        "qarzga pul",
        "қарз бериб тур",
        "одолжи",
        "займи",
        "дай в долг",
        "дайте в долг",
        "дашь в долг",
        "денег в долг",
        "деньги в долг",
        "в долг денег",
        "в долг деньги",
        "odolzhi",
        "zaymi",
        "v dolg deneg",
    )
)
_LOAN_NEED = _markers(
    (
        "pul kerak edi",
        "pul kerak bo'lib",
        "srochno pul",
        "пул керак",
        "нужны деньги",
        "нужно денег",
        "срочно деньги",
        "выручи",
    )
)
_LOAN_SEND: list[re.Pattern[str]] = [
    *_markers(("kartamga", "картамга", "в долг", "qarzga", "v dolg")),
    re.compile(r"(?<![\w'])(?:скин|перевед|перекин)\w*(?: \S+){0,3}? на (?:мою )?карт"),
]
_LOAN_PROMISE = _markers(
    (
        "qaytaraman",
        "qaytarib beraman",
        "қайтараман",
        "верну",
        "отдам завтра",
        "ertaga beraman",
    )
)
_LOAN_TRADE = _markers(
    (
        "yuk",
        "tovar",
        "юк",
        "товар",
        "груз",
        "отгруз",
        "ortiqcha",
        "qoldiq",
        "остаток",
        "сдач",
        "переплат",
        "предоплат",
        "оплат",
        "to'lov",
        "har doimgidek",
        "как в прошлый раз",
        "как обычно",
        "oldin ham",
        "qarzim",
        "поддон",
    )
)
# Social engineering with no link (owner decision: hard only in a strong
# combination). A pretext: a new number «it's me», an officer of the customs
# or the police, or a payee whose bank account «changed».
_NEW_NUMBER = re.compile(
    r"(?<![\w'])(?:yangi (?:raqam|nomer)(?:im|imdan|imga)"
    r"|янги (?:рақам|номер)(?:им|имдан|имга)|(?:raqamim|nomerim) o'zgardi"
    r"|(?:мой|это мой|у меня) нов\w* номер|с нов\w* номера|(?:my|this is my) new number"
    r"|it'?s me,? (?:\S+ )?new number)"
)
_AUTHORITY = re.compile(
    r"(?<![\w'])(?:"
    r"(?:bojxona|божхона|customs|таможн\w*|гтк) (?:inspektor|xodim|officer|инспектор"
    r"|ходим|сотрудник|офицер)\w*"
    r"|(?:инспектор|сотрудник) (?:таможни|гтк|полиции|мвд|банка|службы безопасности)"
    r"|(?:militsiya|politsiya|prokuratura|ichki ishlar"
    r"|милиция|полиция|прокуратура|ички ишлар)"
    r"\w* (?:xodim|ходим|сотрудник|inspektor|инспектор)\w*"
    r"|(?:bank|банк)\w* (?:xavfsizlik xizmati|хавфсизлик хизмати|служб\w* безопасности)"
    r"|служба безопасности банка|security department"
    r")"
)
_CHANGED_ACCOUNT = re.compile(
    r"(?<![\w'])(?:"
    r"(?:bank )?account (?:has )?changed|account details (?:have )?changed"
    r"|new (?:bank )?account(?! manager| owner| holder| executive)"
    # «новый счёт» alone is also a new invoice; the account is «расчётный».
    r"|реквизит\w* (?:изменил|поменял|сменил)\w*|нов\w* реквизит\w*"
    r"|нов\w* расч[её]тн\w*|на нов\w* (?:расч[её]тн\w* )?сч[её]т\w*"
    r"|(?:hisob raqam|rekvizit|karta)\w* (?:o'zgardi|o'zgartirildi|almashdi)"
    r"|(?:ҳисоб рақам|реквизит|карта)\w* (?:ўзгарди|алмашди)"
    r"|yangi (?:hisob raqam|rekvizit|karta)\w*|янги (?:ҳисоб рақам|реквизит|карта)\w*"
    r")"
)
# Money asked for, to a card or an account the sender names.
_MONEY_SEND = re.compile(
    r"(?<![\w'])(?:"
    r"(?:tashla|o'tkaz|to'la|jo'nat)(?:ing|ingiz|ib yuboring|b yuboring|ib bering"
    r"|b bering|ng|ngiz)"
    r"|(?:ташла|ўтказ|тўла)(?:инг|иб юборинг|б юборинг|нг)"
    r"|переведите|переведи|скиньте|скинь|перекиньте|оплатите|перечислите|внесите"
    r"|pay|transfer|send (?:the )?(?:money|payment)"
    r")(?![\w'])"
)
_MONEY_TARGET = re.compile(
    r"(?<![\w'])(?:(?:shu|bu|quyidagi|mana shu) (?:karta|hisob)\w*|kartaga|kartamga"
    r"|(?:шу|бу) (?:карта|ҳисоб)\w*|картага"
    r"|на (?:эту |этот |мою |новую |новый )?(?:карт|сч[её]т)\w*"
    r"|to (?:this|the new|our new|the following|my) (?:card|account)"
    r")|\b\d{4} ?\d{4} ?\d{4} ?\d{4}\b"
)
# The pressure a fraudster adds: hurry, a threat, no time to talk.
_THREAT = _markers(
    (
        "jinoiy ish",
        "жиноий иш",
        "aks holda",
        "акс ҳолда",
        "musodara",
        "мусодара",
        "уголовн",
        "иначе",
        "конфиск",
        "арест",
        "qamoq",
        "criminal",
        "otherwise",
    )
)
_PRESSURE = _markers(
    (
        "tezda",
        "тезда",
        "tezroq",
        "hozir gaplasha olmayman",
        "гаплаша олмайман",
        "majlisdaman",
        "мажлисдаман",
        "не могу говорить",
        "на совещании",
        "can't talk",
        "urgent",
        "asap",
        "immediately",
        r"today\b",
    )
)
# «Pul o'tkazdim, qabul qilish uchun QR kodni skanerlang»: nobody scans a QR
# code to receive money; the code is a payment request.
_QR = re.compile(r"(?<![\w'])(?:qr|кьюар|кюар)(?![a-z])")
_SCAN_ASK = re.compile(
    r"(?<![\w'])(?:skanerlang|skaner qiling|скан(?:ерланг|ер қилинг)|сканируйте"
    r"|отсканируйте|scan)(?![\w'])"
)
_RECEIVE_MONEY = re.compile(
    r"(?<![\w'])(?:"
    r"(?:pul\w*|to'lov\w*|o'tkazma\w*|пул\w*|тўлов\w*) (?:\S+ )?"
    r"(?:(?:qabul qil|ol)(?:ish|ing|asiz|ib|ib oling)|(?:қабул қил|ол)(?:иш|инг|асиз|иб))"
    r"(?![\w'])"
    r"|(?:получить|получения|принять) (?:\S+ )?(?:деньг|перевод|оплат|средств|платеж)\w*"
    r"|(?:receive|claim) (?:the |your )?(?:money|payment|funds|transfer)"
    r")"
)
_SENT_MONEY = re.compile(
    r"(?<![\w'])(?:pul (?:o'tkazdim|tashladim|yubordim)|пул (?:ўтказдим|ташладим)"
    r"|(?:перевел|перевела|отправил|отправила|скинул|скинула)"
    r" (?:вам |тебе )?(?:деньги|оплату)?"
    r"|i (?:have )?sent (?:you )?(?:the )?money)"
)
_RECEIVE = re.compile(
    r"(?<![\w'])(?:qabul qilish uchun|qabul qilib olish|olish uchun|қабул қилиш учун"
    r"|олиш учун|чтобы получить|для получения|to receive)"
)
_MENTION_OR_TAG = re.compile(r"(?<![\w])[#@]\w+")
_MASS_LINKS = 3
_MASS_TAGS = 5


def is_loan_request(text: str | None) -> bool:
    folded = _fold(text or "")
    if _any(_LOAN_ASK, folded):
        return True
    weak = sum(_any(group, folded) for group in (_LOAN_NEED, _LOAN_SEND, _LOAN_PROMISE))
    return weak >= 2 and not _any(_LOAN_TRADE, folded)


def has_link(text: str | None) -> bool:
    return bool(_urls_in_text(display_name(text or "")))


Script = Literal["latin", "cyrillic"]


def script_of(text: str | None, *, min_letters: int = 1) -> Script | None:
    """The script most of a text's letters are written in, when it has enough."""
    letters = [ch for ch in display_name(text or "") if ch.isalpha()]
    if len(letters) < min_letters:
        return None
    cyrillic = sum(1 for ch in letters if _is_cyrillic(ch))
    return "cyrillic" if cyrillic * 2 > len(letters) else "latin"


def dominant_script(texts: Iterable[str | None]) -> Script | None:
    """The script someone almost always writes in, or None when they mix or
    there is too little to tell."""
    scripts = [s for s in (script_of(t, min_letters=3) for t in texts) if s]
    if len(scripts) < SCRIPT_MIN_HISTORY:
        return None
    for script in ("latin", "cyrillic"):
        if scripts.count(script) >= SCRIPT_DOMINANCE * len(scripts):
            return script
    return None


@dataclass(frozen=True, slots=True)
class MessageFeatures:
    """What the shield reads: the message, and what the caller found about
    its sender. Built before any download or model call."""

    text: str = ""
    urls: tuple[FoundUrl, ...] = ()
    # classify_attachment of what the message carries, before any download.
    attachment: Verdict | None = None
    has_file: bool = False
    sender_scam: bool = False  # Telegram's User.scam
    sender_fake: bool = False  # Telegram's User.fake
    forwarded: bool = False  # fwd_from
    via_bot: bool = False  # via_bot_id
    saved_contact: bool = False
    # Their earlier messages MIYA stored (the caller caps the count).
    prior_messages: int = 0
    owner_wrote_before: bool = False
    history_script: Script | None = None
    history_risky_files: bool = False
    history_short_links: bool = False
    history_loans: bool = False

    @property
    def first_contact(self) -> bool:
        """Never seen, never written to, not in the owner's contacts."""
        return not (self.contact or self.prior_messages)

    @property
    def contact(self) -> bool:
        """In the owner's contacts, or someone the owner wrote to."""
        return self.saved_contact or self.owner_wrote_before

    @property
    def known(self) -> bool:
        """A contact with enough history to say how they usually write. A
        stranger who wrote a few times is not one: their spam is spam, never
        a hijacked account."""
        return self.contact and self.prior_messages >= HIJACK_MIN_HISTORY


@dataclass(frozen=True, slots=True)
class MessageVerdict:
    verdict: MessageVerdictKind
    score: int
    reasons: tuple[str, ...] = ()

    @property
    def flagged(self) -> bool:
        return self.verdict != VERDICT_OK

    @property
    def quarantined(self) -> bool:
        """No reply, no download, no model call; the owner is warned."""
        return self.verdict == VERDICT_SCAM

    @property
    def notice(self) -> str | None:
        if self.verdict == VERDICT_SCAM:
            return NOTICE_SCAM
        if self.verdict == VERDICT_HIJACK:
            return NOTICE_HIJACK
        return None

    @property
    def hard_reasons(self) -> tuple[str, ...]:
        return tuple(r for r in self.reasons if r in HARD_REASONS)

    def as_safety(self) -> dict[str, object]:
        """``interactions.meta.safety``."""
        return {
            "verdict": self.verdict,
            "score": self.score,
            "reasons": list(self.reasons),
        }


CLEAN = MessageVerdict(VERDICT_OK, 0)


def _code_request(folded: str) -> bool:
    """A one-time code or a password asked for.

    A request verb and a secret (a password, «tasdiqlash kodi»), or a code
    word («the digits from the SMS» too) in an SMS context. Codes with a name
    (a client's GS code, a tariff code, a Wi-Fi password) are no secret. A
    code to be said to the warehouse keeper at pickup, or to the courier once
    the cargo is in hand, is the procedure. The writer's own code, a code to be
    sent on by SMS, and a question about how codes arrive are a client's
    talk, unless the code came to the reader or by mistake.
    """
    question = bool(_ASK_QUESTION.search(folded))
    if not _REQUEST_VERB.search(_ASK_QUESTION.sub(" ", folded)) and not question:
        return False
    text = _NAMED_CODE.sub(" ", _WIFI_PASSWORD.sub(" ", folded))
    to_reader = bool(_CODE_TO_READER.search(text))
    arrived = bool(_ARRIVED_CODE.search(text))
    secret = _any(_SECRET, text)
    digits = bool(_SMS_DIGITS.search(text))
    sms = digits or _any(_SMS_CONTEXT, _SMS_OUT.sub(" ", text))
    otp = digits or _any(_OTP, text)
    if not secret and not (otp and (sms or arrived or to_reader)):
        return False
    if _IN_PERSON.search(text) and not _HERE.search(text):
        return False
    if secret or to_reader or arrived or _CODE_CONFIRMS.search(text):
        return True
    own = bool(_OWN_CODE.search(text) or _NAMED_CODE.search(folded))
    return not (own or question or _SMS_OUT.search(text))


def _card_request(folded: str) -> bool:
    """The card's security code, or its expiry, back or sides beside a bank
    card, asked for. A card number alone is how a client pays."""
    if not _REQUEST_VERB.search(folded):
        return False
    return (
        _any(_CARD_STRONG, folded)
        or bool(_CARD_DATA.search(folded))
        or bool(_CARD_BACK_DIGITS.search(folded))
    )


def _bare_lure(folded: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    """The lure question is about all the message says."""
    rest = _BARE_URL.sub(" ", _SCHEME_URL.sub(" ", folded))
    for pattern in patterns:
        rest = pattern.sub(" ", rest)
    return len(_LETTER_WORD.findall(rest)) <= _LURE_MAX_EXTRA_WORDS


def _photo_lure(folded: str, features: MessageFeatures, risky_links: bool) -> bool:
    """«Is this you in the photo?» with a link or a file.

    With a link off the allowlist: «you in the photo?» in any message, a bare
    «is this you?» only as about the whole message. With a file: as about the
    whole message, from a stranger, or with a file that is no plain document
    (HTML, a macro). A known contact's «bu sizmi?» under a warehouse photo is
    a question about the cargo; an image itself runs nothing.
    """
    specific = _any(_PHOTO_LURE, folded)
    question = bool(_PHOTO_LURE_QUESTION.search(folded))
    if not (specific or question):
        return False
    bare = _bare_lure(folded, [_PHOTO_LURE_QUESTION, *_PHOTO_LURE])
    if risky_links and (specific or bare):
        return True
    if not (features.has_file and bare):
        return False
    attachment = features.attachment
    risky_file = attachment is not None and attachment.kind in (HTML, MACRO)
    return risky_file or not features.known


def _money_pretext(folded: str, features: MessageFeatures) -> tuple[list[str], list[str]]:
    """Money to a card under a pretext, with no link at all.

    A pretext (a new number «it's me», a customs or police officer, a payee
    whose bank account changed) and money asked for to a card or an account
    are each a soft signal; together from a stranger they add up to spam.
    With pressure on top (hurry, a threat, «I can't talk now») from someone
    who is no contact, it is the fraud itself: a hard hit.
    """
    pretext = bool(
        _NEW_NUMBER.search(folded)
        or _AUTHORITY.search(folded)
        or _CHANGED_ACCOUNT.search(folded)
    )
    money = bool(_MONEY_SEND.search(folded)) and bool(_MONEY_TARGET.search(folded))
    soft = [r for r, hit in ((R_PRETEXT, pretext), (R_MONEY_ASK, money)) if hit]
    pressure = _any(_URGENCY, folded) or _any(_THREAT, folded) or _any(_PRESSURE, folded)
    if pretext and money and pressure and not features.contact:
        return [R_MONEY_PRETEXT], soft
    return [], soft


def _qr_receive(folded: str) -> bool:
    """«Scan this QR to receive the money»: a QR code to scan, and money said
    to be received by it."""
    if not (_QR.search(folded) and _SCAN_ASK.search(folded)):
        return False
    if _RECEIVE_MONEY.search(folded):
        return True
    return bool(_SENT_MONEY.search(folded) and _RECEIVE.search(folded))


def _text_hits(
    text: str, features: MessageFeatures, safe: frozenset[str]
) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    soft: list[str] = []
    folded = _fold(text)
    if _code_request(folded):
        hard.append(R_CODE_REQUEST)
    if _card_request(folded):
        hard.append(R_CARD_DATA_REQUEST)
    # Links to the owner's everyday sites carry no lure and are no mass mail.
    risky_urls = [u for u in features.urls if not _within(u.host, safe)]
    if _photo_lure(folded, features, bool(risky_urls)):
        hard.append(R_PHOTO_LURE)
    if _qr_receive(folded):
        hard.append(R_QR_RECEIVE)
    pretext_hard, pretext_soft = _money_pretext(folded, features)
    hard += pretext_hard
    soft += pretext_soft
    if _any(_LURE, _NOT_LURE.sub(" ", folded)) or _EARNINGS.search(folded):
        soft.append(R_LURE)
    if _any(_PROMO, folded):
        soft.append(R_PROMO)
    if _any(_JOB_AD, folded):
        soft.append(R_JOB_AD)
    if _any(_URGENCY, folded):
        soft.append(R_URGENCY)
    tags = len(_MENTION_OR_TAG.findall(folded))
    mass = len(risky_urls) >= _MASS_LINKS or tags >= _MASS_TAGS
    if mass or _any(_MASS_FORWARD, _NOT_MASS.sub(" ", folded)):
        soft.append(R_MASS_FORWARD)
    return hard, soft


def _hijack(features: MessageFeatures, text: str) -> list[str]:
    """A contact with history doing what they never did before."""
    if not features.known:
        return []
    found = []
    kind = features.attachment.kind if features.attachment else OK
    if kind in (HTML, MACRO) and not features.history_risky_files:
        found.append(R_HIJACK_FILE)
    if any(is_short_link(u) for u in features.urls) and not features.history_short_links:
        found.append(R_HIJACK_SHORT_LINK)
    if is_loan_request(text) and not features.history_loans:
        found.append(R_HIJACK_LOAN)
    return found


def scam_verdict(
    features: MessageFeatures, *, extra_safe: Iterable[str] = ()
) -> MessageVerdict:
    """The verdict on one incoming message.

    Any hard rule makes it ``scam``. Otherwise a contact with history doing
    something they never did (a risky file, a short link, a loan request) is
    a possible ``hijack``; otherwise soft signals are summed and at
    SPAM_SCORE it is ``spam``. A known contact is not a safety signal: it
    lowers nothing, it only lets the hijack check compare.
    """
    hard: list[str] = []
    soft: list[str] = []
    attachment = features.attachment
    if attachment is not None and attachment.kind == EXEC:
        hard.append(R_EXEC_FILE)
    if attachment is not None and attachment.kind == ARCHIVE:
        hard.append(R_ARCHIVE_FILE)
    if attachment is not None and attachment.kind == HTML and not features.contact:
        # A web page as a file, from someone the owner never dealt with: a
        # login form that runs offline («Akt_sverki.html»). From a contact it
        # is the hijack check's to judge.
        hard.append(R_HTML_FILE)
    if features.sender_scam:
        hard.append(R_TELEGRAM_SCAM)
    if features.sender_fake:
        hard.append(R_TELEGRAM_FAKE)
    extra = tuple(extra_safe)
    for found in features.urls:
        link = link_verdict(found, extra_safe=extra)
        hard += link.hard
        soft += link.soft
    text_hard, text_soft = _text_hits(features.text, features, _safe_domains(extra))
    hard += text_hard
    soft += text_soft
    if features.forwarded:
        soft.append(R_FORWARDED)
    if features.via_bot:
        soft.append(R_VIA_BOT)
    if features.first_contact:
        soft.append(R_FIRST_CONTACT)
    own = script_of(features.text, min_letters=SCRIPT_MIN_LETTERS)
    if features.history_script and own and own != features.history_script:
        soft.append(R_SCRIPT_SWITCH)

    soft_reasons = tuple(dict.fromkeys(soft))
    score = min(SOFT_SCORE_CAP, sum(SOFT_WEIGHTS[r] for r in soft_reasons))
    if hard:
        reasons = tuple(dict.fromkeys(hard)) + soft_reasons
        return MessageVerdict(VERDICT_SCAM, HARD_SCORE, reasons)
    hijack = _hijack(features, features.text)
    if hijack:
        return MessageVerdict(VERDICT_HIJACK, score, tuple(hijack) + soft_reasons)
    if score >= SPAM_SCORE:
        return MessageVerdict(VERDICT_SPAM, score, soft_reasons)
    return MessageVerdict(VERDICT_OK, score, soft_reasons)


def with_message_verdict(
    meta: dict[str, Any] | None, verdict: MessageVerdict, *, notify: bool
) -> dict[str, Any]:
    """``meta.safety`` for a flagged message, and the owner's warning queued.

    A clean message gets no key. A file verdict already there (JV-01's
    ``dangerous_file``) is the harder one and stays; its own warning, queued
    first, is the one the owner gets — never two for one message.
    """
    out = dict(meta or {})
    if not verdict.flagged:
        return out
    out.setdefault("safety", verdict.as_safety())
    notice = verdict.notice
    if notify and notice is not None and SAFETY_NOTICE_KEY not in out:
        out[SAFETY_NOTICE_KEY] = notice
    return out


def _verdict_of(meta: dict[str, Any] | None) -> str | None:
    safety = (meta or {}).get("safety")
    if not isinstance(safety, dict):
        return None
    value = safety.get("verdict")
    return value if isinstance(value, str) else None


def is_hidden(meta: dict[str, Any] | None) -> bool:
    """Spam, scam or a dangerous file: never an open question or a nudge."""
    return _verdict_of(meta) in HIDDEN_VERDICTS


def blocks_reply(meta: dict[str, Any] | None) -> bool:
    """The AI never answers this message (the responder, JV-16, and the
    outbox gate, JV-11, read it)."""
    return _verdict_of(meta) in REPLY_BLOCKING_VERDICTS
