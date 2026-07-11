#!/usr/bin/env python3
"""Build the /investigate page data from the scraped ufos.wiki page dump.

Parses ufo.wiki/page-*.md (the "Investigate a Sighting" card grid) into
entries, matches each entry to a full-size image in ufo.wiki/images/ by
filename slug, copies matched images to static/investigate/<slug>.<ext>,
and regenerates app/investigate_data.py as a plain literal list.

Re-runnable: python scripts/build_investigate.py
"""
import html
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = ROOT / "ufo.wiki" / "images"
OUT_STATIC = ROOT / "static" / "investigate"
OUT_MODULE = ROOT / "app" / "investigate_data.py"

# The original grid's parent category — not a real filter, drop it.
SKIP_CATEGORIES = {"Investigations"}
# ufos.wiki's category is misspelled "Artifiacts" on some entries.
CATEGORY_FIXES = {"Artifiacts": "Artifacts"}

# Entries whose image filename doesn't slug-match the title/slug.
MANUAL_IMAGES = {
    "airplanes-night": "Plane_Night_UFOSwiki4.jpg",
    "backscatter": "House_Dust_Orbs.jpg",           # backscatter "orbs" photo
    "ball-lightning": "ballightning.png",
    "dust-and-scratches": "Dust.jpg",
    "interior-reflections": "Relfections.jpg",      # [sic] upstream typo
    "lunar-halo-moon-dog": "Moon-dogs.jpg",
    "meteoroids": "Meteor.jpg",
    "mirages": "Fata-Morgana.jpg",                  # a Fata Morgana is a mirage
    "radio-controlled-aircraft": "Remote-controlled-airplanes.jpg",
    "satellite-flares": "Iridium-Flash.jpg",        # iridium flare = satellite flare
    "smoke-rings": "Screenshot-2023-06-18-at-2-05-50-AM.png",  # black smoke ring
}

INV_LINK = re.compile(r"\[([^\]]*)\]\(https://ufos\.wiki/investigation/([a-z0-9-]+)/\)")
CAT_LINK = re.compile(r"\[([^\]]+)\]\(https://ufos\.wiki/category/investigations/[a-z0-9-]*/")


def clean_teaser(text: str) -> str:
    t = html.unescape(text).strip()
    t = re.sub(r"(\.\.\.|…)$", "", t).rstrip()
    # the scrape truncates mid-HTML-entity sometimes ("lumpy&# ...")
    t = re.sub(r"&#?[0-9a-zA-Z]*$", "", t).rstrip()
    return t


def parse_entries(md_text: str) -> list[dict]:
    entries = []
    for block in md_text.split("\n-   ![")[1:]:
        links = [(t.strip(), s) for t, s in INV_LINK.findall(block)
                 if t.strip() and not t.lstrip().startswith("\\>")]
        if len(links) < 2:
            continue
        (title, slug), (teaser, _) = links[0], links[1]
        cats = []
        for c in CAT_LINK.findall(block):
            c = CATEGORY_FIXES.get(c, c)
            if c not in SKIP_CATEGORIES and c not in cats:
                cats.append(c)
        entries.append({
            "title": html.unescape(title),
            "slug": slug,
            "categories": cats,
            "teaser": clean_teaser(teaser),
            "source_url": f"https://ufos.wiki/investigation/{slug}/",
        })
    return entries


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def variants(key: str) -> set[str]:
    v = {key, key.replace("-", "")}
    if key.endswith("es"):
        v.add(key[:-2])
    if key.endswith("s"):
        v.add(key[:-1])
    return v


def match_images(entries: list[dict]) -> None:
    stems = {}  # normalized stem -> Path (full-size variants only)
    for f in sorted(IMAGES_DIR.iterdir()):
        if f.is_file() and "-25x25" not in f.stem:
            stems[norm(f.stem)] = f
    used = set()

    def claim(entry, path):
        entry["_image_src"] = path
        used.add(path)

    for e in entries:  # pass 1: manual overrides
        m = MANUAL_IMAGES.get(e["slug"])
        if m and (IMAGES_DIR / m).is_file():
            claim(e, IMAGES_DIR / m)
    for e in entries:  # pass 2: exact slug/title match
        if "_image_src" in e:
            continue
        for key in (norm(e["slug"]), norm(e["title"])):
            p = stems.get(key)
            if p and p not in used:
                claim(e, p)
                break
    for e in entries:  # pass 3: singular/plural, dashless, -N suffix variants
        if "_image_src" in e:
            continue
        keys = variants(norm(e["slug"])) | variants(norm(e["title"]))
        for stem, p in stems.items():
            if p in used:
                continue
            image_keys = variants(stem) | variants(re.sub(r"-\d+$", "", stem))
            if keys & image_keys:
                claim(e, p)
                break


def copy_images(entries: list[dict]) -> None:
    if OUT_STATIC.exists():
        shutil.rmtree(OUT_STATIC)
    OUT_STATIC.mkdir(parents=True)
    for e in entries:
        src = e.pop("_image_src", None)
        if src is None:
            e["image"] = None
            continue
        e["image"] = e["slug"] + src.suffix.lower()
        shutil.copyfile(src, OUT_STATIC / e["image"])


def emit_module(entries: list[dict]) -> None:
    lines = [
        '"""Commonly misidentified objects for the /investigate page.',
        "",
        "AUTO-GENERATED by scripts/build_investigate.py from the ufo.wiki/ scrape",
        "of https://ufos.wiki/investigate/ — regenerate rather than editing by hand.",
        '"""',
        "",
        "ENTRIES = [",
    ]
    for e in entries:
        lines += [
            "    {",
            f"        \"title\": {e['title']!r},",
            f"        \"slug\": {e['slug']!r},",
            f"        \"categories\": {e['categories']!r},",
            f"        \"teaser\": {e['teaser']!r},",
            f"        \"image\": {e['image']!r},",
            f"        \"source_url\": {e['source_url']!r},",
            "    },",
        ]
    lines.append("]")
    OUT_MODULE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    md_file = sorted((ROOT / "ufo.wiki").glob("page-*.md"))[-1]
    entries = parse_entries(md_file.read_text(encoding="utf-8"))
    match_images(entries)
    copy_images(entries)
    emit_module(entries)
    missing = [e["slug"] for e in entries if not e["image"]]
    cats = sorted({c for e in entries for c in e["categories"]})
    print(f"{len(entries)} entries parsed from {md_file.name}")
    print(f"{len(entries) - len(missing)} images matched -> {OUT_STATIC}")
    print(f"categories: {', '.join(cats)}")
    print(f"missing images ({len(missing)}): {', '.join(missing) or 'none'}")
    print(f"wrote {OUT_MODULE}")


if __name__ == "__main__":
    main()
