#!/usr/bin/env python3
"""Download a PhotoShelter gallery or collection with its metadata embedded.

Accepts a gallery/collection ID or a PhotoShelter URL. Collections are walked
recursively, recreating their folder tree locally. Every item (photos, videos,
documents, ...) is downloaded as the original file where permitted. Otherwise
the best preview PhotoShelter offers is saved instead, marked ".thumb": a
1440px JPEG for images (with EXIF/IPTC glued on via ExifTool), a PDF for PDFs
and documents, or the streaming version for video/audio. Items with no
downloadable version at all get a ".MISSING.txt" placeholder.

See README.md for where each field ends up. The API key is read from config.json
(see config.example.json).
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pyrate_limiter import Duration, Limiter, Rate

DEFAULT_SITE = "www"
CONFIG_PATH = Path(__file__).with_name("config.json")
EXIFTOOL_CONFIG = Path(__file__).with_name("photoshelter.exiftool_config")
JPEG_PREVIEW_URL = "https://m.psecn.photoshelter.com/img-get2/{media_id}/fit=10000x10000/"
PDF_PREVIEW_URL = "https://m.psecn.photoshelter.com/img-get2/{media_id}/src=pdf"
STREAM_URL = "https://v.psecn.photoshelter.com/video-get/{media_id}/fmt={fmt}/"
# Every extension a ".thumb" preview can have (see Scraper.previews)
PREVIEW_EXTS = (".jpg", ".pdf", ".mp4", ".m4a")
# Formats (PhotoShelter's format field) that ExifTool cleanly adds XMP to.
# Other originals are left byte-for-byte as served: ExifTool can't tag zip,
# docx, SVG etc., and it still modifies some it can't really write (it adds
# bytes to Illustrator EPS files while warning it can't write their XMP).
TAGGABLE_FORMATS = {
    "image/jpeg", "image/png", "image/tiff", "image/webp", "application/pdf",
    "mp4", "video/mp4", "video/quicktime", "audio/mp4",
}

# ID shapes from the API spec. Collections start C0000, galleries G0000, and
# media AD000/VD000/DO000/I0000 (audio/video/document/image).
COLLECTION_RE = re.compile(r"C0000[A-Za-z0-9._]{11}")
GALLERY_RE = re.compile(r"G0000[A-Za-z0-9._]{11}")

# PhotoShelter's EXIF values are ExifTool's human-readable output, which
# ExifTool can parse back in, so most tags just need their label turned back
# into a tag name ("F Number" -> "FNumber"). These are the exceptions.
EXIF_NAME_OVERRIDES = {
    "Camera Model Name": "Model",
}

# Tags describing the original file's structure, which don't apply to the
# re-rendered thumbnail. (Writing Compression/Thumbnail* creates a bogus IFD1,
# and the rendered image is already rotated upright.)
EXIF_SKIP = {
    "Bits Per Sample",
    "Compression",
    "Exif Image Height",
    "Exif Image Width",
    "Image Height",
    "Image Width",
    "Orientation",
    "Photometric Interpretation",
    "Samples Per Pixel",
    "Thumbnail Length",
    "Thumbnail Offset",
}

# PhotoShelter IPTC field -> ExifTool tags. Fields with a legacy IPTC-IIM
# equivalent are written to both IIM and XMP, since different apps read
# different ones; the rest only exist in XMP.
IPTC_MAP = {
    "title": ["IPTC:ObjectName", "XMP-dc:Title"],
    "headline": ["IPTC:Headline", "XMP-photoshop:Headline"],
    "caption": ["IPTC:Caption-Abstract", "XMP-dc:Description"],
    "caption_writer": ["IPTC:Writer-Editor", "XMP-photoshop:CaptionWriter"],
    "keyword": ["IPTC:Keywords", "XMP-dc:Subject"],
    "category": ["IPTC:Category", "XMP-photoshop:Category"],
    "urgency": ["IPTC:Urgency", "XMP-photoshop:Urgency"],
    "edit_status": ["IPTC:EditStatus"],
    "instructions": ["IPTC:SpecialInstructions", "XMP-photoshop:Instructions"],
    "trans_ref": ["IPTC:OriginalTransmissionReference", "XMP-photoshop:TransmissionReference"],
    "author": ["IPTC:By-line", "XMP-dc:Creator"],
    "authors_position": ["IPTC:By-lineTitle", "XMP-photoshop:AuthorsPosition"],
    "credit": ["IPTC:Credit", "XMP-photoshop:Credit"],
    "source": ["IPTC:Source", "XMP-photoshop:Source"],
    "copyright": ["IPTC:CopyrightNotice", "XMP-dc:Rights"],
    "rights": ["XMP-xmpRights:UsageTerms"],
    "web_statement": ["XMP-xmpRights:WebStatement"],
    "location": ["IPTC:Sub-location", "XMP-iptcCore:Location"],
    "city": ["IPTC:City", "XMP-photoshop:City"],
    "state": ["IPTC:Province-State", "XMP-photoshop:State"],
    "country": ["IPTC:Country-PrimaryLocationName", "XMP-photoshop:Country"],
    "country_iso": ["IPTC:Country-PrimaryLocationCode", "XMP-iptcCore:CountryCode"],
    "genre": ["XMP-iptcCore:IntellectualGenre"],
    "scene": ["XMP-iptcCore:Scene"],
    "subject_code": ["XMP-iptcCore:SubjectCode"],
    "creator_address": ["XMP-iptcCore:CreatorAddress"],
    "ci_city": ["XMP-iptcCore:CreatorCity"],
    "ci_region": ["XMP-iptcCore:CreatorRegion"],
    "ci_postal_code": ["XMP-iptcCore:CreatorPostalCode"],
    "ci_country": ["XMP-iptcCore:CreatorCountry"],
    "ci_email": ["XMP-iptcCore:CreatorWorkEmail"],
    "ci_phone": ["XMP-iptcCore:CreatorWorkTelephone"],
    "ci_url": ["XMP-iptcCore:CreatorWorkURL"],
}
IPTC_LIST_FIELDS = {"keyword", "scene", "subject_code"}
# Handled specially in build_tags(), or PhotoShelter bookkeeping.
IPTC_HANDLED = {"creation_date", "created_at", "modified_at"}


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
class Unavailable(Exception):
    """PhotoShelter won't serve this version of a file; try the next one."""


class PhotoShelter:
    def __init__(self, site, api_key, requests_per_second):
        self.base = f"https://{site}.photoshelter.com/psapi/v4.0"
        self.limiter = Limiter(Rate(requests_per_second, Duration.SECOND))
        self.client = httpx.Client(
            timeout=120,
            follow_redirects=True,
            headers={"X-PS-Api-Key": api_key},
            transport=httpx.HTTPTransport(retries=3),
        )

    def _params(self, params):
        # httpx sends None as an empty value ("collection_id="), which
        # PhotoShelter treats as a real (and wrong) context, so drop them
        return {k: v for k, v in params.items() if v is not None}

    def _get(self, url, **params):
        self.limiter.try_acquire("photoshelter")
        return self.client.get(url, params=self._params(params))

    def api(self, path, **params):
        r = self._get(f"{self.base}/{path}", **params)
        r.raise_for_status()
        return r.json()

    def _paged(self, path, **params):
        page = 1
        while True:
            resp = self.api(path, per_page=500, page=page, **params)
            yield from resp["data"]
            if page >= resp["meta"]["paging"]["pages"]:
                return
            page += 1

    def get_collection(self, collection_id):
        return self.api(f"collections/{collection_id}")["data"]

    def get_gallery(self, gallery_id):
        """A gallery plus its parent collections (via include), so a bare
        gallery ID can be given the collection context its permissions need."""
        data = self.api(f"galleries/{gallery_id}", include="parent")["data"]
        parents = data.get("includes", {}).get("parent", {}).get("data") or []
        if isinstance(parents, dict):
            parents = [parents]
        return data, [p["id"] for p in parents if p.get("type") == "collection"]

    def collection_children(self, collection_id):
        return list(self._paged(f"collections/{collection_id}/children"))

    def gallery_media(self, gallery_id, collection_id=None):
        """Everything in a gallery, each with media/exif/iptc bundled via
        includes: one request per 500 items instead of four per item."""
        return list(self._paged(
            f"galleries/{gallery_id}/children",
            collection_id=collection_id,
            include="media,exif,iptc",
        ))

    def _fetch_to(self, url, dest, params=None, expect=None):
        """Stream url to dest, raising Unavailable if it isn't served.

        expect is the required content type; without it, anything but a JSON
        error body is accepted. 5xx and other unexpected statuses raise
        httpx.HTTPStatusError (worth retrying, unlike Unavailable).
        """
        self.limiter.try_acquire("photoshelter")
        with self.client.stream("GET", url, params=self._params(params or {})) as r:
            if r.status_code in (401, 403, 404):
                raise Unavailable(f"HTTP {r.status_code}")
            if r.status_code != 200:
                r.read()
                r.raise_for_status()
            content_type = r.headers.get("content-type", "").split(";")[0]
            if (expect and content_type != expect) or content_type == "application/json":
                raise Unavailable(f"served {content_type or 'no content type'}")
            # Streamed, since originals (especially video) can be large
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)

    def download_original(self, media_id, dest, gallery_id=None, collection_id=None):
        """The original file, requested in its gallery/collection context so
        PhotoShelter's permissions resolve properly."""
        self._fetch_to(
            f"{self.base}/media/{media_id}/download",
            dest,
            {"gallery_id": gallery_id, "collection_id": collection_id},
        )

    def download_jpeg_preview(self, media_id, dest):
        self._fetch_to(JPEG_PREVIEW_URL.format(media_id=media_id), dest, expect="image/jpeg")

    def download_pdf_preview(self, media_id, dest):
        self._fetch_to(PDF_PREVIEW_URL.format(media_id=media_id), dest, expect="application/pdf")

    def download_stream(self, media_id, dest):
        """Video/audio: reassemble PhotoShelter's HLS stream into one file.

        ffmpeg picks the best rendition in the playlist and copies its streams
        into an MP4 container without re-encoding.
        """
        if not shutil.which("ffmpeg"):
            raise Unavailable("ffmpeg is not installed")
        errors = []
        for fmt in ("original", "m3u8"):
            self.limiter.try_acquire("photoshelter")
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", STREAM_URL.format(media_id=media_id, fmt=fmt),
                    "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dest),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            last_line = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
            errors.append(f"fmt={fmt}: {last_line}")
        raise Unavailable("; ".join(errors))


# --------------------------------------------------------------------------- #
# Target parsing
# --------------------------------------------------------------------------- #
def parse_target(target):
    """Turn an ID or PhotoShelter URL into (site, kind, id, collection_ctx).

    site is None when the target is a bare ID (caller supplies the default).
    collection_ctx is the parent collection a gallery URL was browsed through,
    passed to permission-scoped endpoints; None otherwise.
    """
    site, path = None, target
    if "://" in target or target.startswith("www."):
        parsed = urlparse(target if "://" in target else f"https://{target}")
        host = parsed.hostname or ""
        if host.endswith(".photoshelter.com"):
            site = host[: -len(".photoshelter.com")]
        path = parsed.path

    collections = COLLECTION_RE.findall(path)
    galleries = GALLERY_RE.findall(path)
    if galleries:
        gallery_id = galleries[-1]
        # A collection appearing before the gallery in a URL is its browse
        # context; use it so gallery/collection permissions resolve.
        context = collections[-1] if collections else None
        return site, "gallery", gallery_id, context
    if collections:
        return site, "collection", collections[-1], None
    raise ValueError(f"no gallery (G0000...) or collection (C0000...) ID found in {target!r}")


# --------------------------------------------------------------------------- #
# Metadata helpers
# --------------------------------------------------------------------------- #
def parse_date(value):
    """Parse a PhotoShelter timestamp. These are ISO 8601, except a document's
    gallery creation_time, which looks like "2024-08-15 19:12:20 GMT"."""
    if value.endswith((" GMT", " UTC")):
        return datetime.fromisoformat(value[:-4]).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value)


def exiftool_date(value):
    """PhotoShelter timestamp -> ExifTool's "YYYY:MM:DD HH:MM:SS+HH:MM" format."""
    return parse_date(value).strftime("%Y:%m:%d %H:%M:%S%:z")


def real_iptc_date(iptc):
    # PhotoShelter fills creation_date with the upload time when the original
    # had no capture date; don't pass that off as when the photo was taken.
    return iptc.get("creation_date") and iptc["creation_date"] != iptc.get("created_at")


def capture_time(exif, iptc, media):
    """Best guess at when the photo was taken, as an aware datetime."""
    taken = exif.get("Date/Time Original")
    if taken:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", str(taken))
        offset = exif.get("Offset Time Original") or exif.get("Offset Time") or ""
        if m:
            return datetime.fromisoformat(m[1] + offset)
    if real_iptc_date(iptc):
        return parse_date(iptc["creation_date"])
    return parse_date(media["created_at"])


def provenance_tags(media_id, child_attrs, media):
    """PhotoShelter-specific tags that have no home in standard EXIF/IPTC.

    Additive and non-destructive, so they're safe to write onto an original
    file alongside its native metadata.
    """
    tags = {
        "XMP-xmpMM:PreservedFileName": media.get("file_name"),
        "XMP-photoshelter:MediaID": media_id,
        "XMP-photoshelter:GalleryID": child_attrs.get("gallery_id"),
        "XMP-photoshelter:OrganizationID": media.get("organization_id"),
        "XMP-photoshelter:OriginalWidth": media.get("width"),
        "XMP-photoshelter:OriginalHeight": media.get("height"),
        "XMP-photoshelter:OriginalFileSize": media.get("file_size"),
        # PhotoShelter's creation_time/modification_time are swapped relative to
        # created_at/modified_at; created_at (the earlier one) is the upload time.
        "XMP-photoshelter:UploadDate": exiftool_date(media["created_at"]),
        "XMP-photoshelter:ModifyDate": exiftool_date(media["modified_at"]),
    }
    if child_attrs.get("creation_time"):
        tags["XMP-photoshelter:GalleryAddDate"] = exiftool_date(child_attrs["creation_time"])
    return {k: v for k, v in tags.items() if v is not None}


def build_tags(media_id, child_attrs, media, exif, iptc):
    """Full tag set for the thumbnail path: EXIF + IPTC + provenance."""
    tags = {"IPTC:CodedCharacterSet": "UTF8"}

    for name, value in exif.items():
        if name in EXIF_SKIP or value is None:
            continue
        tag = EXIF_NAME_OVERRIDES.get(name, re.sub(r"[^A-Za-z0-9]", "", name))
        if isinstance(value, str) and "Date" in name:
            # "2024-10-17 19:35:01 EDT" -> "2024:10:17 19:35:01" (the offset
            # comes separately in the Offset Time tags)
            m = re.match(r"(\d{4})-(\d\d)-(\d\d) (\d\d:\d\d:\d\d)", value)
            if m:
                value = f"{m[1]}:{m[2]}:{m[3]} {m[4]}"
        tags[f"EXIF:{tag}"] = value

    for field, value in iptc.items():
        if value is None or value == "" or field in IPTC_HANDLED:
            continue
        if field not in IPTC_MAP:
            print(f"    note: no mapping for IPTC field {field!r} ({value!r}), not embedded")
            continue
        if field in IPTC_LIST_FIELDS and isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        for tag in IPTC_MAP[field]:
            tags[tag] = value
    if real_iptc_date(iptc):
        created = parse_date(iptc["creation_date"])
        tags["IPTC:DateCreated"] = created.strftime("%Y:%m:%d")
        tags["IPTC:TimeCreated"] = created.strftime("%H:%M:%S%:z")
        tags["XMP-photoshop:DateCreated"] = exiftool_date(iptc["creation_date"])

    tags.update(provenance_tags(media_id, child_attrs, media))
    return {k: v for k, v in tags.items() if v is not None}


def _run_exiftool(args, tags, expected_updates, quiet=False):
    result = subprocess.run(
        ["exiftool", "-config", str(EXIFTOOL_CONFIG), *args],
        input=json.dumps([{"SourceFile": "*", **tags}]),
        capture_output=True,
        text=True,
    )
    # ExifTool can exit 0 without having written anything, so count the
    # "updated" lines. (With -j=- it reports on stderr, not stdout.)
    output = (result.stdout + result.stderr).splitlines()
    updated = sum(line.strip() == "1 image files updated" for line in output)
    if result.returncode != 0 or updated != expected_updates:
        raise RuntimeError(f"exiftool failed (status {result.returncode}): {' / '.join(output)}")
    for line in [] if quiet else output:
        # Refers to PhotoShelter's stale digest, which gets replaced below.
        if "image files updated" not in line and "IPTCDigest is not current" not in line:
            print(f"    exiftool: {line}")


def write_full_metadata(path, tags):
    """Thumbnail path: replace PhotoShelter's stale XMP with our full tag set."""
    _run_exiftool(
        [
            # PhotoShelter's renderer embeds a stale subset of the XMP (with
            # the upload time as DateCreated); replace it wholesale.
            "-XMP:all=",
            "-j=-",
            str(path),
            # Copying tags can't share a command with -j= (ExifTool silently
            # drops the JSON), so do the rest in a second pass: dimensions of
            # the downloaded pixels (not the original's), and a fresh digest
            # so apps don't think the IPTC and XMP are out of sync.
            "-execute",
            "-ExifIFD:ExifImageWidth<File:ImageWidth",
            "-ExifIFD:ExifImageHeight<File:ImageHeight",
            "-Photoshop:IPTCDigest=new",
            str(path),
            "-common_args",
            "-m",
            "-overwrite_original",
        ],
        tags,
        expected_updates=2,
    )


def tag_provenance(path, tags):
    """Best-effort write_provenance(): a file ExifTool can't write (say, a
    malformed PDF) is left untouched, and the file matters more than tags."""
    try:
        write_provenance(path, tags)
    except RuntimeError:
        pass


def write_placeholder(path, child, media, attempts):
    """Note left in place of a file PhotoShelter wouldn't serve in any form."""
    attrs = child["attributes"]
    lines = [
        "This item is in the PhotoShelter gallery, but no version of it could be",
        "downloaded. Re-running the scraper will try again.",
        "",
        f"File name:  {media.get('file_name')}",
        f"Media ID:   {child['id']}",
        f"Type:       {child['type']} ({media.get('format')})",
        f"Size:       {media.get('file_size')} bytes",
        f"Uploaded:   {media.get('created_at')}",
        f"Gallery ID: {attrs.get('gallery_id')}",
        "",
        "Attempts:",
        *(f"- {a}" for a in attempts),
    ]
    path.write_text("\n".join(lines) + "\n")


def write_provenance(path, tags):
    """Original path: add provenance tags without touching native metadata.

    IPTCDigest=new refreshes the Photoshop digest (PhotoShelter's downloads
    ship a stale one); it's a no-op on files without an IPTC block.
    """
    _run_exiftool(
        ["-m", "-overwrite_original", "-j=-", str(path), "-Photoshop:IPTCDigest=new"],
        tags,
        expected_updates=1,
        quiet=True,
    )


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def sanitize(name):
    name = (name or "").replace("/", "_").replace("\0", "").strip()
    return name or "untitled"


def folder_name(name, obj_id):
    return f"{sanitize(name)} ({obj_id})"


def format_extension(fmt):
    """Extension for PhotoShelter's format field, which is usually a MIME type
    ("image/png") but sometimes a bare extension ("mp4")."""
    if not fmt:
        return ""
    if "/" not in fmt:
        return f".{fmt}"
    return mimetypes.guess_extension(fmt) or ""


def assign_stems(medias):
    """media_id -> (stem, ext), appending the media ID where names collide.

    Collisions are judged on the stem alone (case-insensitively): previews
    have their own extension, so two originals that differ only by extension
    (e.g. "foo.jpg" and "foo.png") would otherwise collide as "foo.thumb.jpg".
    """
    base = {}
    for media_id, media in medias.items():
        stem, ext = os.path.splitext(sanitize(media.get("file_name")))
        base[media_id] = (stem, ext or format_extension(media.get("format")))
    counts = Counter(stem.lower() for stem, _ in base.values())
    return {
        media_id: (f"{stem} ({media_id})" if counts[stem.lower()] > 1 else stem, ext)
        for media_id, (stem, ext) in base.items()
    }


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #
class Scraper:
    def __init__(self, ps, args):
        self.ps = ps
        self.args = args
        self.original = self.preview = self.missing = self.skipped = self.failed = 0

    def run(self, kind, obj_id, collection_ctx):
        if kind == "collection":
            self.walk_collection(self.ps.get_collection(obj_id), self.args.output)
        else:
            node, parents = self.ps.get_gallery(obj_id)
            # A gallery that inherits access from its collection can't be read
            # without that collection as context; use its parent if not given.
            if collection_ctx is None and parents:
                collection_ctx = parents[0]
            self.handle_gallery(node, self.args.output, collection_ctx)
        print(
            f"\nDone: {self.original} original, {self.preview} preview only, "
            f"{self.missing} missing, {self.skipped} skipped, {self.failed} failed"
        )
        return 1 if self.failed else 0

    def walk_collection(self, node, parent_dir):
        folder = parent_dir / folder_name(node["attributes"]["name"], node["id"])
        print(f"\nCollection: {folder.name}")
        folder.mkdir(parents=True, exist_ok=True)
        for child in self.ps.collection_children(node["id"]):
            try:
                if child["type"] == "gallery":
                    self.handle_gallery(child, folder, node["id"])
                elif child["type"] == "collection":
                    self.walk_collection(child, folder)
                else:
                    print(f"  skipping {child['id']}: unknown type {child['type']!r}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in (401, 403, 404):
                    raise
                # Can't list it at all: leave a note where its folder would be
                name = folder_name(child["attributes"].get("name"), child["id"])
                (folder / f"{name}.MISSING.txt").write_text(
                    f"The {child['type']} {name} couldn't be opened "
                    f"(HTTP {e.response.status_code}), so its contents weren't downloaded.\n"
                    "Re-running the scraper will try again.\n"
                )
                print(f"  {name}: no access (HTTP {e.response.status_code}), wrote placeholder")
                self.missing += 1

    def handle_gallery(self, node, parent_dir, collection_ctx):
        folder = parent_dir / folder_name(node["attributes"]["name"], node["id"])
        items = self.ps.gallery_media(node["id"], collection_ctx)
        print(f"\nGallery: {folder.name} ({len(items)} items)")
        folder.mkdir(parents=True, exist_ok=True)

        stems = assign_stems({c["id"]: include_attrs(c, "media") for c in items})
        for i, child in enumerate(items, 1):
            stem, ext = stems[child["id"]]
            prefix = f"  [{i}/{len(items)}]"
            self.process_media(child, stem, ext, folder, collection_ctx, prefix)

    def previews(self, child_type, media):
        """Versions to try when the original isn't available, best first:
        (extension, description, download function, embed full metadata?)"""
        ps = self.ps
        if child_type in ("video", "audio"):
            ext = ".m4a" if child_type == "audio" else ".mp4"
            return [(ext, "streaming version", ps.download_stream, False)]
        pdf = (".pdf", "PDF preview", ps.download_pdf_preview, False)
        if child_type == "document":
            return [pdf]
        jpeg = (".jpg", "1440px JPEG preview", ps.download_jpeg_preview, True)
        if media.get("format") == "application/pdf":
            return [pdf, jpeg]
        return [jpeg]

    def process_media(self, child, stem, ext, folder, collection_ctx, prefix):
        media_id = child["id"]
        original_path = folder / f"{stem}{ext}"
        preview_paths = [folder / f"{stem}.thumb{e}" for e in PREVIEW_EXTS]
        missing_path = folder / f"{stem}{ext}.MISSING.txt"

        # Placeholders aren't counted, so missing items are retried every run
        if not self.args.force and any(p.exists() for p in [original_path, *preview_paths]):
            print(f"{prefix} {stem}{ext} (exists, skipping)")
            self.skipped += 1
            return

        media = include_attrs(child, "media")
        exif_values = include_attrs(child, "exif").get("values", [])
        exif = {v["name"]: v["value"] for v in exif_values}
        iptc = include_attrs(child, "iptc")
        child_attrs = child["attributes"]

        tmp = folder / f".{media_id}.part{ext}"
        attempts = []
        try:
            try:
                self.ps.download_original(
                    media_id, tmp, child_attrs.get("gallery_id"), collection_ctx
                )
                dest, note, full_metadata = original_path, "", False
            except Unavailable as e:
                attempts.append(f"original file: {e}")
                dest = None
                for preview_ext, description, fetch, full_metadata in self.previews(child["type"], media):
                    tmp.unlink(missing_ok=True)
                    tmp = folder / f".{media_id}.part{preview_ext}"
                    try:
                        fetch(media_id, tmp)
                    except Unavailable as e:
                        attempts.append(f"{description}: {e}")
                        continue
                    dest = folder / f"{stem}.thumb{preview_ext}"
                    note = f"  (no original access; {description})"
                    break

            if dest is None:
                tmp.unlink(missing_ok=True)
                write_placeholder(missing_path, child, media, attempts)
                print(f"{prefix} {missing_path.name}  (nothing downloadable)")
                self.missing += 1
                return

            if full_metadata:
                write_full_metadata(tmp, build_tags(media_id, child_attrs, media, exif, iptc))
            elif tmp.stat().st_size == 0:
                # PhotoShelter has some empty uploads; keep them byte-for-byte
                note += "  (empty file on PhotoShelter)"
            elif dest != original_path or media.get("format") in TAGGABLE_FORMATS:
                tag_provenance(tmp, provenance_tags(media_id, child_attrs, media))

            taken = capture_time(exif, iptc, media).timestamp()
            os.utime(tmp, (taken, taken))
            tmp.replace(dest)
            missing_path.unlink(missing_ok=True)
            if dest == original_path:
                self.original += 1
                # Superseded by the original (e.g. access granted since a past run)
                for path in preview_paths:
                    path.unlink(missing_ok=True)
            else:
                self.preview += 1
            print(f"{prefix} {dest.name}{note}")

            if self.args.save_json:
                raw = {"gallery_child": child, "media": media, "exif": exif_values, "iptc": iptc}
                dest.with_suffix(dest.suffix + ".json").write_text(
                    json.dumps(raw, indent=2, ensure_ascii=False)
                )
        except Exception as e:
            self.failed += 1
            print(f"{prefix} FAILED {stem}{ext}: {e}")
            tmp.unlink(missing_ok=True)


def include_attrs(child, key):
    """Pull an included resource's attributes out of a child node."""
    included = child.get("includes", {}).get(key)
    return included["data"]["attributes"] if included else {}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def load_api_key(override):
    if override:
        return override
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        sys.exit(
            f"No config file at {CONFIG_PATH}.\n"
            f"Copy config.example.json to config.json and set your api_key."
        )
    except json.JSONDecodeError as e:
        sys.exit(f"{CONFIG_PATH} is not valid JSON: {e}")
    api_key = config.get("api_key")
    if not api_key:
        sys.exit(f'No "api_key" set in {CONFIG_PATH}.')
    return api_key


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="gallery/collection ID or a PhotoShelter URL")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("."),
        help="parent directory for the downloaded tree (default: current directory)",
    )
    parser.add_argument("--site", help=f"PhotoShelter subdomain (default: from URL, else {DEFAULT_SITE})")
    parser.add_argument("--api-key", help="override the api_key in config.json")
    parser.add_argument("--rate", type=int, default=5, help="max requests per second (default: 5)")
    parser.add_argument("--force", action="store_true", help="re-download files that already exist")
    parser.add_argument("--save-json", action="store_true", help="also save raw API responses next to each image")
    args = parser.parse_args()

    if not shutil.which("exiftool"):
        sys.exit("exiftool not found; install it first (see requirements.txt)")

    try:
        url_site, kind, obj_id, collection_ctx = parse_target(args.target)
    except ValueError as e:
        sys.exit(str(e))

    api_key = load_api_key(args.api_key)
    site = args.site or url_site or DEFAULT_SITE
    args.output.mkdir(parents=True, exist_ok=True)

    ps = PhotoShelter(site, api_key, args.rate)
    print(f"Site: {site}.photoshelter.com | {kind} {obj_id}")
    sys.exit(Scraper(ps, args).run(kind, obj_id, collection_ctx))


if __name__ == "__main__":
    main()
