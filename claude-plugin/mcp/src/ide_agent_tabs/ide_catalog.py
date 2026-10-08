from __future__ import annotations

import re
from typing import NamedTuple

from .jsjson import trim


class IdeEntry(NamedTuple):
    key: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    product: re.Pattern[str]
    cli: str | None = None
    product_codes: tuple[str, ...] = ()


def _vscode(key: str, name: str, cli: str, product: str, aliases: tuple[str, ...] = ()) -> IdeEntry:
    return IdeEntry(key, name, "vscode", aliases, re.compile(product, re.IGNORECASE | re.ASCII), cli=cli)


def _jetbrains(key: str, name: str, codes: tuple[str, ...], aliases: tuple[str, ...] = ()) -> IdeEntry:
    return IdeEntry(key, name, "jetbrains", aliases, re.compile(rf"^{re.escape(name)}\b", re.IGNORECASE | re.ASCII), product_codes=codes)


IDE_CATALOG: tuple[IdeEntry, ...] = (
    _vscode("vscode", "VS Code", "code", r"^visual studio code\Z", ("code", "visual-studio-code")),
    _vscode(
        "code-insiders",
        "VS Code Insiders",
        "code-insiders",
        r"^visual studio code - insiders\Z",
        ("vscode-insiders", "insiders", "visual-studio-code-insiders"),
    ),
    _vscode("cursor", "Cursor", "cursor", r"^cursor\Z"),
    _vscode("windsurf", "Windsurf", "windsurf", r"^windsurf\b"),
    _vscode("vscodium", "VSCodium", "codium", r"^vscodium\b", ("codium",)),
    _vscode("antigravity", "Antigravity", "antigravity-ide", r"^antigravity\b", ("antigravity-ide",)),
    _vscode("kiro", "Kiro", "kiro", r"^kiro\b"),
    _vscode("positron", "Positron", "positron", r"^positron\b"),
    _vscode("trae", "Trae", "trae", r"^trae\b"),
    _jetbrains("android-studio", "Android Studio", ("AI",), ("studio", "android")),
    _jetbrains(
        "idea",
        "IntelliJ IDEA",
        ("IU", "IC", "IE", "II"),
        ("intellij", "intellij-idea-ultimate", "intellij-idea-community", "idea-ultimate", "idea-community"),
    ),
    _jetbrains("pycharm", "PyCharm", ("PY", "PC", "PE")),
    _jetbrains("webstorm", "WebStorm", ("WS",)),
    _jetbrains("goland", "GoLand", ("GO",)),
    _jetbrains("rider", "Rider", ("RD",)),
    _jetbrains("clion", "CLion", ("CL",)),
    _jetbrains("rustrover", "RustRover", ("RR",)),
    _jetbrains("phpstorm", "PhpStorm", ("PS",)),
    _jetbrains("rubymine", "RubyMine", ("RM",)),
    _jetbrains("datagrip", "DataGrip", ("DB",)),
    _jetbrains("dataspell", "DataSpell", ("DS",)),
)

_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def normalize_ide_name(text: str) -> str:
    return _NOT_ALNUM.sub("", text.lower())


def matches_product(entry: IdeEntry, product: str) -> bool:
    return entry.product.search(trim(product)) is not None


def find_ide_entry(name: str) -> IdeEntry | None:
    wanted = normalize_ide_name(name)
    if wanted == "":
        return None
    for e in IDE_CATALOG:
        if any(normalize_ide_name(n) == wanted for n in (e.key, e.name, *e.aliases)):
            return e
    return next((e for e in IDE_CATALOG if matches_product(e, name)), None)


def entry_for_product(product: str) -> IdeEntry | None:
    return next((e for e in IDE_CATALOG if matches_product(e, product)), None)


def product_matches_name(product: str, name: str) -> bool:
    entry = find_ide_entry(name)
    if entry is not None:
        return matches_product(entry, product)
    return normalize_ide_name(product) != "" and normalize_ide_name(product) == normalize_ide_name(name)


def entry_for_product_info(name: str | None, product_code: str | None) -> IdeEntry | None:
    found = entry_for_product(name) if name is not None else None
    if found is None and product_code is not None:
        found = next((e for e in IDE_CATALOG if product_code.upper() in e.product_codes), None)
    return found
