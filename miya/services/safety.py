"""The dangerous-file shield (JV-01, owner decision 4).

Executables, installers and archives are never downloaded, opened or offered
for approval, on any channel; the owner is warned once instead. Everything
here is pure: no database, no network, no settings. The classifiers run on
what a transport knows *before* a download (the claimed name and MIME type),
and ``sniff_magic`` checks the bytes of an allowed type after it.

The blocklists are code constants on purpose. No configuration key can
loosen them; a file type is made safe only by a reviewed change here.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

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
    or not, is never replaced.
    """
    out = dict(meta or {})
    if verdict.dangerous:
        out["safety"] = verdict.as_safety()
    notice = notice_for(verdict)
    if notify and notice is not None and SAFETY_NOTICE_KEY not in out:
        out[SAFETY_NOTICE_KEY] = notice
    return out
