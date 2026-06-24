# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare the structure and text metadata of two VI Package (.vip) files.

A VI Package (``.vip``) is a ZIP archive whose payload is the built LabVIEW
library plus a handful of metadata members::

    icon.bmp                 package icon (binary)
    File Group N/<path...>   installed files: VIs, controls, .lvlib, menus (binary)
    license                  license text shown at install time (text)
    spec                     INI/config manifest describing the package (text)

This tool answers one question: *does a freshly built package have the same
structure and metadata as a known-good released package?* It deliberately does
**not** compare the bytes of LabVIEW binaries -- VIs embed compile timestamps
and recompile nondeterministically, so identical source produces different
bytes every build. What it compares instead is everything that should stay
stable from one release to the next:

  * the manifest -- which entries exist, and in which ``File Group`` they install
  * the ``spec`` -- section / key / value metadata, parsed as INI (not text-diffed)
  * the ``license`` -- exact text

A package's own version string is expected to differ between releases, so every
occurrence of it is normalized to ``<VERSION>`` before the spec is compared; the
two versions are reported in the header instead. The ``ID`` and ``Release Notes``
also change release-to-release and are reported as informational rather than
counted as structural differences. Everything else that differs is a finding.

Exit codes: ``0`` = structural parity, ``1`` = structural differences found,
``2`` = usage / load error.

Usage::

    uv run python tools/compare_package_structure.py BASELINE.vip CANDIDATE.vip
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import difflib
import json
import re
import sys
import zipfile
from pathlib import Path

# "File 0", "File 1", ... entries inside a "[File Group N]" section are a
# positional index into the group's file list, not stable keys.
FILE_ENTRY_RE = re.compile(r"^File \d+$")

# Members carrying human-readable text we compare directly; everything else in
# the archive is a LabVIEW binary whose bytes are not meaningfully comparable.
TEXT_ENTRIES = ("spec", "license")
SPEC_ENTRY = "spec"
LICENSE_ENTRY = "license"

# Compiled LabVIEW binaries embed compile timestamps and recompile
# nondeterministically, so their size varies from build to build with no change
# in source. Presence is verified by the manifest; a size delta here is expected
# and carries no signal, so it is reported as allowed rather than as a finding.
LABVIEW_BINARY_SUFFIXES = frozenset(
    {".vi", ".ctl", ".vit", ".ctt", ".llb", ".lvlib", ".lvclass", ".xctl", ".mnu"}
)

# (section, key) values that legitimately change every release and therefore do
# not count as structural differences. Version is handled separately by
# normalization. ID is left informational because we could not confirm it is
# reproduced deterministically by a rebuild -- if you verify that it must match
# (it is how VIPM tracks package identity across upgrades), drop it from here so
# a mismatch becomes a hard finding.
DEFAULT_VOLATILE_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Package", "ID"),
        ("Description", "Release Notes"),
    }
)


@dataclasses.dataclass
class Package:
    """A loaded ``.vip``: its archive manifest plus parsed text metadata."""

    path: Path
    entries: dict[str, int]  # archive member name -> uncompressed size (files only)
    spec: configparser.ConfigParser
    license_text: str | None

    @property
    def name(self) -> str:
        return self._pkg("Name")

    @property
    def version(self) -> str:
        return self._pkg("Version")

    @property
    def id(self) -> str:
        return self._pkg("ID")

    def _pkg(self, key: str) -> str:
        # Spec string values are wrapped in double quotes; strip them for display.
        return self.spec.get("Package", key, fallback="").strip().strip('"')


@dataclasses.dataclass
class Difference:
    """A single spec difference, tagged as informational (volatile) or not."""

    location: str
    baseline: str | None
    candidate: str | None
    volatile: bool


def load_package(path: Path) -> Package:
    """Read a ``.vip`` archive into a :class:`Package`."""
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = {
                info.filename: info.file_size
                for info in archive.infolist()
                if not info.is_dir()
            }
            text = {
                name: archive.read(name) for name in TEXT_ENTRIES if name in entries
            }
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid .vip (ZIP) archive: {path} ({exc})") from exc

    if SPEC_ENTRY not in text:
        raise ValueError(f"missing '{SPEC_ENTRY}' member; not a VI Package?: {path}")

    license_bytes = text.get(LICENSE_ENTRY)
    return Package(
        path=path,
        entries=entries,
        spec=parse_spec(text[SPEC_ENTRY]),
        license_text=None if license_bytes is None else decode_text(license_bytes),
    )


def decode_text(raw: bytes) -> str:
    """Decode a text member and normalize line endings.

    latin-1 round-trips any byte value, so legacy non-UTF-8 text (e.g. an
    accented contributor name in the license) decodes losslessly. Newlines are
    normalized so a CRLF-vs-LF difference -- which says nothing about content --
    never reads as a structural difference.
    """
    return raw.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")


def parse_spec(raw: bytes) -> configparser.ConfigParser:
    """Parse the internal ``spec`` member as INI, preserving key case."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # keys like "Display Name" are case- and space-sensitive
    parser.read_string(raw.decode("latin-1"))
    return parser


def normalize(value: str, version: str) -> str:
    """Replace a package's own version string so versions don't read as diffs."""
    return value.replace(version, "<VERSION>") if version else value


def compare_manifest(
    baseline: Package, candidate: Package
) -> tuple[list[str], list[str], list[str]]:
    """Return (only-in-baseline, only-in-candidate, common) member names."""
    base, cand = set(baseline.entries), set(candidate.entries)
    return sorted(base - cand), sorted(cand - base), sorted(base & cand)


def compare_spec(
    baseline: Package,
    candidate: Package,
    volatile: frozenset[tuple[str, str]],
) -> list[Difference]:
    """Compare the two parsed specs section-by-section, key-by-key."""
    diffs: list[Difference] = []
    base_sections = set(baseline.spec.sections())
    cand_sections = set(candidate.spec.sections())

    for section in sorted(base_sections - cand_sections):
        diffs.append(Difference(f"[{section}]", "present", "absent", False))
    for section in sorted(cand_sections - base_sections):
        diffs.append(Difference(f"[{section}]", "absent", "present", False))

    for section in sorted(base_sections & cand_sections):
        diffs.extend(
            compare_section(
                section,
                dict(baseline.spec.items(section)),
                dict(candidate.spec.items(section)),
                baseline.version,
                candidate.version,
                volatile,
            )
        )
    return diffs


def compare_section(
    section: str,
    base_items: dict[str, str],
    cand_items: dict[str, str],
    base_version: str,
    cand_version: str,
    volatile: frozenset[tuple[str, str]],
) -> list[Difference]:
    """Compare one spec section's keys and values."""
    diffs: list[Difference] = []

    if section.startswith("File Group "):
        # Compare the group's file list as an unordered set: which files install
        # is structural, but the order VIPM happens to enumerate them in is not.
        # The remaining keys (Target Dir, Replace Mode, Num Files) still compare
        # normally below.
        base_files = {v for k, v in base_items.items() if FILE_ENTRY_RE.match(k)}
        cand_files = {v for k, v in cand_items.items() if FILE_ENTRY_RE.match(k)}
        for value in sorted(base_files - cand_files):
            diffs.append(Difference(f"[{section}] file entry", value, None, False))
        for value in sorted(cand_files - base_files):
            diffs.append(Difference(f"[{section}] file entry", None, value, False))
        base_items = {k: v for k, v in base_items.items() if not FILE_ENTRY_RE.match(k)}
        cand_items = {k: v for k, v in cand_items.items() if not FILE_ENTRY_RE.match(k)}

    for key in sorted(set(base_items) - set(cand_items)):
        diffs.append(Difference(f"[{section}] {key}", base_items[key], None, (section, key) in volatile))
    for key in sorted(set(cand_items) - set(base_items)):
        diffs.append(Difference(f"[{section}] {key}", None, cand_items[key], (section, key) in volatile))
    for key in sorted(set(base_items) & set(cand_items)):
        if normalize(base_items[key], base_version) != normalize(cand_items[key], cand_version):
            diffs.append(
                Difference(
                    f"[{section}] {key}", base_items[key], cand_items[key], (section, key) in volatile
                )
            )
    return diffs


def compare_license(baseline: Package, candidate: Package) -> list[str] | None:
    """Return a unified diff of the license text, or None if identical."""
    if baseline.license_text == candidate.license_text:
        return None
    return list(
        difflib.unified_diff(
            (baseline.license_text or "").splitlines(),
            (candidate.license_text or "").splitlines(),
            fromfile="baseline/license",
            tofile="candidate/license",
            lineterm="",
        )
    )


def binary_size_deltas(
    baseline: Package, candidate: Package, common: list[str]
) -> list[tuple[str, int, int]]:
    """Side-by-side sizes of common binary members (informational only)."""
    return [
        (name, baseline.entries[name], candidate.entries[name])
        for name in common
        if name not in TEXT_ENTRIES
    ]


def is_labview_binary(name: str) -> bool:
    """True for compiled LabVIEW files, whose size varies build-to-build."""
    return Path(name).suffix.lower() in LABVIEW_BINARY_SUFFIXES


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def short(value: str | None, limit: int = 60) -> str:
    if value is None:
        return "(absent)"
    value = value.strip()
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def render_text_report(
    baseline: Package,
    candidate: Package,
    only_base: list[str],
    only_cand: list[str],
    common: list[str],
    spec_diffs: list[Difference],
    license_diff: list[str] | None,
    size_deltas: list[tuple[str, int, int]],
    structural: bool,
    verbose: bool,
) -> str:
    out: list[str] = []
    add = out.append

    add("== VI Package structure comparison ==")
    add(f"  baseline : {baseline.path}")
    add(f"             name={baseline.name!r}  version={baseline.version}  id={baseline.id}")
    add(f"  candidate: {candidate.path}")
    add(f"             name={candidate.name!r}  version={candidate.version}  id={candidate.id}")
    add("")

    # -- Manifest --
    add(f"-- Manifest ({len(baseline.entries)} -> {len(candidate.entries)} entries) --")
    if not only_base and not only_cand:
        add(f"  OK    all {len(common)} entries present in both")
    else:
        for name in only_base:
            add(f"  DIFF  only in baseline : {name}")
        for name in only_cand:
            add(f"  DIFF  only in candidate: {name}")
    add("")

    # -- Spec metadata --
    structural_spec = [d for d in spec_diffs if not d.volatile]
    info_spec = [d for d in spec_diffs if d.volatile]
    add("-- Spec metadata --")
    if not structural_spec:
        add("  OK    structural fields match")
    for diff in structural_spec:
        add(f"  DIFF  {diff.location}")
        add(f"          baseline : {short(diff.baseline)}")
        add(f"          candidate: {short(diff.candidate)}")
    for diff in info_spec:
        if verbose:
            add(f"  info  {diff.location}")
            add(f"          baseline : {short(diff.baseline, 200)}")
            add(f"          candidate: {short(diff.candidate, 200)}")
        else:
            add(f"  info  {diff.location} differs (expected per release)")
    add("")

    # -- License --
    add("-- License --")
    if license_diff is None:
        add("  OK    identical")
    else:
        add("  DIFF  license text differs")
        if verbose:
            out.extend(f"        {line}" for line in license_diff)
    add("")

    # -- Binary payload --
    # LabVIEW binaries are allowed to vary in size (nondeterministic recompile);
    # a size change in a non-LabVIEW asset (e.g. icon.bmp) is worth surfacing.
    add(f"-- Binary payload ({len(size_deltas)} files, bytes not compared) --")
    lv = [(n, b, c) for n, b, c in size_deltas if is_labview_binary(n)]
    other = [(n, b, c) for n, b, c in size_deltas if not is_labview_binary(n)]
    other_changed = [(n, b, c) for n, b, c in other if b != c]
    if lv:
        add(f"  info  LabVIEW binaries: {len(lv)} present; size varies per build (allowed)")
    if other:
        add(f"  info  other binaries: {len(other)} present, {len(other_changed)} differ in size")
    for name, base_size, cand_size in other_changed:
        add(f"      ~ {base_size:>7} -> {cand_size:>7}  {name}")
    if verbose:
        add("      -- all binary sizes --")
        out.extend(
            f"      {'~' if b != c else '='} {b:>7} -> {c:>7}  {n}"
            for n, b, c in size_deltas
        )
    add("")

    # Only non-LabVIEW size changes count as informational differences; LabVIEW
    # size variance is expected and not tallied.
    info_count = len(info_spec) + len(other_changed)
    if structural:
        add(f"== RESULT: FAIL -- structural differences found ({info_count} informational) ==")
    else:
        add(f"== RESULT: PASS -- structural parity ({info_count} informational differences) ==")
    return "\n".join(out)


def build_json_report(
    baseline: Package,
    candidate: Package,
    only_base: list[str],
    only_cand: list[str],
    common: list[str],
    spec_diffs: list[Difference],
    license_diff: list[str] | None,
    size_deltas: list[tuple[str, int, int]],
    structural: bool,
) -> dict:
    structural_spec = [d for d in spec_diffs if not d.volatile]
    return {
        "baseline": {
            "path": str(baseline.path),
            "name": baseline.name,
            "version": baseline.version,
            "id": baseline.id,
        },
        "candidate": {
            "path": str(candidate.path),
            "name": candidate.name,
            "version": candidate.version,
            "id": candidate.id,
        },
        "manifest": {
            "only_in_baseline": only_base,
            "only_in_candidate": only_cand,
            "common_count": len(common),
        },
        "spec_differences": [dataclasses.asdict(d) for d in spec_diffs],
        "license_identical": license_diff is None,
        # LabVIEW binaries are allowed to vary in size; only non-LabVIEW assets
        # are reported here, where a size change may be meaningful.
        "binary_size_deltas": [
            {"name": n, "baseline": b, "candidate": c}
            for n, b, c in size_deltas
            if b != c and not is_labview_binary(n)
        ],
        "result": {
            "structural_parity": not structural,
            "structural_difference_count": (
                len(only_base) + len(only_cand) + len(structural_spec)
                + (0 if license_diff is None else 1)
            ),
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare the structure and text metadata of two .vip packages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("baseline", type=Path, help="known-good released .vip")
    parser.add_argument("candidate", type=Path, help="freshly built .vip to check")
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="SECTION:KEY",
        help="treat this spec field as informational (repeatable), "
        'e.g. --ignore "Package:ID"',
    )
    parser.add_argument(
        "--fail-on-version",
        action="store_true",
        help="also fail when the two package versions differ",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show full values and diffs"
    )
    return parser.parse_args(argv)


def extra_volatile(ignores: list[str]) -> frozenset[tuple[str, str]]:
    fields = set()
    for item in ignores:
        section, sep, key = item.partition(":")
        if not sep:
            raise SystemExit(f"--ignore expects SECTION:KEY, got: {item!r}")
        fields.add((section.strip(), key.strip()))
    return frozenset(fields)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        baseline = load_package(args.baseline)
        candidate = load_package(args.candidate)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    volatile = DEFAULT_VOLATILE_FIELDS | extra_volatile(args.ignore)

    only_base, only_cand, common = compare_manifest(baseline, candidate)
    spec_diffs = compare_spec(baseline, candidate, volatile)
    license_diff = compare_license(baseline, candidate)
    size_deltas = binary_size_deltas(baseline, candidate, common)

    structural = bool(
        only_base
        or only_cand
        or any(not d.volatile for d in spec_diffs)
        or license_diff is not None
        or (args.fail_on_version and baseline.version != candidate.version)
    )

    if args.json:
        print(json.dumps(
            build_json_report(
                baseline, candidate, only_base, only_cand, common,
                spec_diffs, license_diff, size_deltas, structural,
            ),
            indent=2,
        ))
    else:
        print(render_text_report(
            baseline, candidate, only_base, only_cand, common,
            spec_diffs, license_diff, size_deltas, structural, args.verbose,
        ))

    return 1 if structural else 0


if __name__ == "__main__":
    raise SystemExit(main())
