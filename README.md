# photoshelter-scraper

Downloads everything in a PhotoShelter gallery or collection (photos, videos,
documents, ...), with metadata embedded where possible. Collections are walked
recursively and their folder tree is recreated locally. Every item is
downloaded as its **original** file where PhotoShelter permits it; otherwise
the best available preview is saved instead, marked `.thumb`.

This tool uses the v4.0 API, documented here: <https://engineering.photoshelter.com/psapi-v4-doc/>. Older sites using the v3 API have not been investigated/tested as of yet.

This tool is mostly vibe-coded. I did a lot of my own investigation before writing a plan for Claude to implement, but I have not fully vetted every detail of the implementation. This tool comes with no warranty! It is free to use under the MIT License; see the LICENSE file.

## What you get for each item

| Item | Original allowed | Original not allowed |
|---|---|---|
| Photo / graphic | the original | `name.thumb.jpg`: maximum resolution JPEG (exact maximum depends on the server's settings), with EXIF/IPTC rebuilt from the API |
| PDF | the original | `name.thumb.pdf`: PhotoShelter's PDF preview (falls back to the JPEG) |
| Video | the original | `name.thumb.mp4`: the streaming version (best quality offered, typically up to 1080p), reassembled by ffmpeg |
| Audio | the original | `name.thumb.m4a`: same as video (**untested**; no audio to test with) |
| Other document (docx, zip, ...) | the original | `name.thumb.pdf` if PhotoShelter has a PDF preview |

If no version at all can be downloaded, a `name.ext.MISSING.txt` placeholder
takes the file's place, listing what was tried and why each attempt failed. A
gallery or collection that can't be opened at all gets a
`Name (ID).MISSING.txt` in place of its folder.

## Setup

```sh
pip install -r requirements.txt
cp config.example.json config.json
# then paste your API key into the config
```

Also needs [ExifTool](https://exiftool.org/) on your `PATH`
(`pacman -S perl-image-exiftool`), and [ffmpeg](https://ffmpeg.org/) for
video/audio you can't download the original of (`pacman -S ffmpeg`). Without
ffmpeg, those items get a placeholder instead.

The API key lives in `config.json` (`{"api_key": "..."}`). If it's missing the
tool errors out.

## Usage

Pass a gallery/collection ID or any PhotoShelter URL — the IDs are pulled out
of the URL path:

```sh
./scrape.py G0000wUXCzjQZiQg
./scrape.py https://example.photoshelter.com/galleries/C0000XXXXXXXXXXX/G0000XXXXXXXXXXX/example
./scrape.py https://example.photoshelter.com/galleries/C0000XXXXXXXXXXX/example
```

- `-o DIR` — parent directory for the download tree (default: current directory)
- `--site` — PhotoShelter subdomain; taken from the URL if given, else `www`
- `--api-key` — override the key in `config.json`
- `--rate N` — max requests/second (default 5)
- `--force` — re-download files that already exist
- `--save-json` — also save the raw API responses next to each image

Files that already exist (original or `.thumb`) are skipped, so an interrupted
run resumes by re-running it. Items that only have a `.MISSING.txt` placeholder
are retried every run, and the placeholder is removed once one succeeds. See
`--help` for everything.

## Output layout

Every collection and gallery becomes a folder named `Name (ID)`; files sit
inside their gallery's folder, named with PhotoShelter's `file_name`:

```
Some Collection (C0000XXXXXXXXXXX)/
  Some Gallery (G0000XXXXXXXXXXX)/
    Some File Name.ext
    ...
```

When two items in a gallery share a name (ignoring case and extension), each
gets its media ID appended: `Ambiguous Name (I0000XXXXXXXXXXX).thumb.jpg`. Names
are PhotoShelter's, so the extension doesn't always match the contents.

## Where the metadata goes

**Original files** are kept as-is (they already carry their full native
EXIF/IPTC/XMP, and PhotoShelter refreshes the embedded IPTC/XMP on download).
For JPEG, PNG, TIFF, WebP, PDF and MP4/MOV originals, the only additions are
the provenance tags below and a refreshed IPTC digest. Every other format
(EPS, SVG, zip, docx, ...) is left byte-for-byte as PhotoShelter serves it,
because ExifTool either can't write it or alters files it can't properly
tag.

**JPEG previews** (the photo fallback) get metadata rebuilt from the API:

- **EXIF** written as EXIF. PhotoShelter returns ExifTool's human-readable
  values ("1/200", "No Flash"), which ExifTool parses back. Tags describing the
  original file's structure (thumbnail, compression, orientation, dimensions)
  are skipped; `ExifImageWidth`/`Height` are set to the downloaded size.
- **IPTC** written to both legacy IPTC-IIM and XMP (IPTC Core) where both
  exist, since apps differ in which they read. Creator contact fields
  (`ci_*`, `creator_address`) only exist in XMP. `DateCreated` is only written
  when it's a real capture date — PhotoShelter fills it with the upload time
  when the original had none.

**Originals in the formats above, and all previews,** get these added:

- **Original filename** in the standard `XMP-xmpMM:PreservedFileName`.
- **PhotoShelter provenance** in a custom `XMP-photoshelter` namespace (defined
  in `photoshelter.exiftool_config`): `MediaID`, `GalleryID`, `OrganizationID`,
  `OriginalWidth`/`Height`/`FileSize`, `UploadDate`, `ModifyDate`,
  `GalleryAddDate`. To read them back with their labels:

  ```sh
  exiftool -config photoshelter.exiftool_config -XMP-photoshelter:all FILE.jpg
  ```

  The config is only needed for writing; plain `exiftool -XMP:all` reads them fine.
- **File modification time** (on every file) set to when the photo was
  taken (EXIF `DateTimeOriginal`, falling back to the PhotoShelter upload
  time), so file managers sort the folder chronologically.

## Request efficiency

Gallery contents are fetched with PhotoShelter's `include` feature
(`include=media,exif,iptc`, 500 per page), so a whole gallery's metadata comes
back in one request per page instead of four requests per item.

## Notes on the API

- In the media endpoint, `creation_time`/`modification_time` are swapped
  relative to `created_at`/`modified_at`; the code treats `created_at` (the
  earlier one) as the upload time.
- The original download is requested with the gallery/collection context so
  permissions resolve correctly; a permission denial is the signal to fall back
  to a preview. Galleries that inherit access from their collection can't
  be read without that context, so for a bare gallery ID the tool looks up its
  parent collection (bundled into the gallery request via `include=parent`).
- A file that was saved as a preview is skipped on later runs, like any
  existing file. If you're later given access to the originals, re-run with
  `--force` (which also removes the old `.thumb` file when an original replaces it).
- Documents' gallery `creation_time` comes back as `2024-08-15 19:12:20 GMT`
  rather than ISO 8601 (the `date_format` parameter doesn't change it).
- Some uploads are empty (0 bytes, format `inode/x-empty`). They're saved as
  the same empty files and flagged in the output.
