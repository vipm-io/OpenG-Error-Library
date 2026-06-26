# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Update a VIPM ``Settings.ini`` file in place with our preferred CI defaults.

VIPM (VI Package Manager) stores its configuration as INI, but with quirks that
ordinary ``configparser`` use gets wrong:

  * every value is wrapped in double quotes, e.g.
    ``Mass Compile After Package Install?="FALSE"`` and ``Features 0.enabled="TRUE"``
  * keys are written without spaces around ``=`` and are case-sensitive, and
    values legitimately contain ``%`` and ``"``
  * the file uses a single-byte Windows/Latin codec, not UTF-8, and escapes
    ``\\`` as ``\\\\`` plus control characters as ``\\<HH>`` -- a newline is ``\\0A``

So the parser reads and writes latin-1 with interpolation disabled and key case
preserved, and the file is written back with ``space_around_delimiters=False`` --
making the round-trip byte-compatible with what VIPM reads and writes. Values
this tool does not touch pass through verbatim; those it reads or writes go
through :func:`decode_value` / :func:`encode_value` for the quoting and escaping.

Preview features are stored positionally in the ``[Preview Features]`` section:
``Features N.feature name`` / ``Features N.enabled`` ... with the count in
``Features.<size(s)>``. They are toggled here by name rather than by index.

Usage::

    uv run python tools/update_vipm_settings.py path/to/Settings.ini
"""

from __future__ import annotations

import argparse
import configparser
import re
import sys
from pathlib import Path


# [General] settings to force on (True) or off (False) by name, applied on top
# of whatever the file already contains; the section is created if absent.
GENERAL_SECTION_OVERRIDES: dict[str, bool] = {
    "Mass Compile After Package Install?": False,
    "High-performance HTTP": True,
}

# Preview features to force on (True) or off (False) by name, applied on top of
# whatever the file already contains. Names must match a "Features N.feature
# name" value exactly; unknown names are reported and skipped.
PREVIEW_FEATURES_SECTION_OVERRIDES: dict[str, bool] = {
    "VIPB LabVIEW Save Version": True,
}


# VIPM writes its Settings.ini in a single-byte Windows/Latin codec, not UTF-8:
# feature descriptions contain cp1252 bytes (smart quotes, en/em dashes). latin-1
# is used for both read and write because it maps every byte value 0x00-0xFF
# bijectively -- it never raises on input and reproduces untouched bytes exactly,
# so the file round-trips losslessly. Every value this tool writes is ASCII, which
# is identical across latin-1, cp1252, and UTF-8.
ENCODING = "latin-1"

GENERAL_SECTION = "General"
PREVIEW_FEATURES_SECTION = "Preview Features"
FEATURE_COUNT_KEY = "Features.<size(s)>"

# VIPM escapes the backslash as ``\\`` and C0 control characters (0x00-0x1F,
# notably CR and LF) as a backslash plus two uppercase hex digits, so a newline
# is stored as ``\0A``. Printable characters -- including raw cp1252 high bytes
# -- are stored verbatim, and the surrounding double quotes are literal.
_UNESCAPE_RE = re.compile(r"\\(\\|[0-9A-Fa-f]{2})")


def vipm_escape(text: str) -> str:
    """Encode real text into VIPM's backslash/hex escape form."""
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20:
            out.append(f"\\{ord(ch):02X}")
        else:
            out.append(ch)
    return "".join(out)


def vipm_unescape(text: str) -> str:
    """Decode VIPM's backslash/hex escapes back into real text."""
    return _UNESCAPE_RE.sub(
        lambda m: "\\" if m.group(1) == "\\" else chr(int(m.group(1), 16)),
        text,
    )


def encode_value(text: str) -> str:
    """Render ``text`` as an on-disk VIPM value: escaped and double-quoted."""
    return f'"{vipm_escape(text)}"'


def decode_value(raw: str) -> str:
    """Parse an on-disk VIPM value: drop the surrounding quotes and unescape."""
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return vipm_unescape(raw)


def _vipm_bool(value: bool) -> str:
    """Render a bool the way VIPM stores it: a quoted ``TRUE``/``FALSE``."""
    return encode_value("TRUE" if value else "FALSE")


def new_parser() -> configparser.ConfigParser:
    """Create a parser configured for VIPM's INI dialect.

    Interpolation is disabled because values contain literal ``%``; key case is
    preserved because VIPM keys are case-sensitive.
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    return parser


def load_settings(path: Path) -> configparser.ConfigParser:
    """Read a VIPM ``Settings.ini`` from ``path`` into a new parser."""
    parser = new_parser()
    parser.read_string(path.read_text(encoding=ENCODING))
    return parser


def save_settings(config: configparser.ConfigParser, path: Path) -> None:
    """Write ``config`` to ``path`` in VIPM's quoted, no-space-delimiter format."""
    with path.open("w", encoding=ENCODING) as f:
        config.write(f, space_around_delimiters=False)


def set_general_defaults(
    config: configparser.ConfigParser,
    overrides: dict[str, bool],
) -> None:
    """Apply the ``[General]`` boolean settings in ``overrides``, adding the section if absent."""
    if not config.has_section(GENERAL_SECTION):
        config.add_section(GENERAL_SECTION)
    for key, value in overrides.items():
        config.set(GENERAL_SECTION, key, _vipm_bool(value))


def build_feature_index(config: configparser.ConfigParser) -> dict[str, int]:
    """Map each preview feature's name to its positional index.

    Returns ``{feature name: N}`` for the ``Features N.*`` entries, or an empty
    mapping if there is no ``[Preview Features]`` section.
    """
    if not config.has_section(PREVIEW_FEATURES_SECTION):
        return {}
    count = int(decode_value(config.get(PREVIEW_FEATURES_SECTION, FEATURE_COUNT_KEY, fallback='"0"')))
    index: dict[str, int] = {}
    for i in range(count):
        name = config.get(PREVIEW_FEATURES_SECTION, f"Features {i}.feature name", fallback=None)
        if name is not None:
            index[decode_value(name)] = i
    return index


def set_feature_enabled(
    config: configparser.ConfigParser,
    feature_index: dict[str, int],
    feature_name: str,
    enabled: bool,
) -> bool:
    """Enable or disable a preview feature by name.

    Returns ``True`` if the feature existed and was updated, ``False`` if the
    name was not found (a warning is printed to stderr in that case).
    """
    i = feature_index.get(feature_name)
    if i is None:
        print(f"Preview feature not found: {feature_name!r}", file=sys.stderr)
        return False
    config.set(PREVIEW_FEATURES_SECTION, f"Features {i}.enabled", _vipm_bool(enabled))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "settings_path",
        type=Path,
        help="path to the VIPM Settings.ini to update in place",
    )
    args = parser.parse_args(argv)

    config = load_settings(args.settings_path)

    set_general_defaults(config, GENERAL_SECTION_OVERRIDES)

    feature_index = build_feature_index(config)
    for feature_name, enabled in PREVIEW_FEATURES_SECTION_OVERRIDES.items():
        set_feature_enabled(config, feature_index, feature_name, enabled)

    save_settings(config, args.settings_path)
    print(f"Updated {args.settings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
