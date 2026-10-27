#!/usr/bin/env python3
"""Check the upgrade guide against the feature registry and its production consumers.

`sync` refreshes inventories, never review attestations. After reviewing changed
behavior and its prose, `stamp Upgrade ...` records the production-source digest.
This is a review reminder, not a proof that English matches Rust semantics.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = "crates/common/genesis/src/feature.rs"
UPGRADES = "crates/common/genesis/src/chain/upgrade.rs"
GUIDE = "docs/guides/UPGRADES.md"
ADAPTERS = (
    REGISTRY,
    "crates/common/genesis/src/rollup.rs",
    "crates/common/chains/src/upgrades.rs",
    "crates/common/evm/src/spec.rs",
    "crates/common/evm2/src/spec.rs",
)
# Only schedule machinery and compatibility adapters may ask whether a named fork
# is active. Tests/benchmarks are excluded separately, not grandfathered here.
FORK_GATE_ADAPTERS = {
    *ADAPTERS,
    "crates/common/chains/src/upgrade.rs",  # Resolves the EVM spec identity.
    "crates/common/genesis/src/schedule.rs",  # Validates a block-index schedule.
}


def production(text: str) -> str:
    """Omit the colocated test module (required to be last by AGENTS.md)."""
    test_start = None
    offset = 0
    # Scan complete attribute lines rather than backtracking over an arbitrary
    # number of attributes. Attribute-like strings in source must not stall CI.
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (stripped.startswith("#[cfg(") and stripped.endswith(")]")
                and re.search(r"\btest\b", stripped)):
            if test_start is None:
                test_start = offset + line.index("#")
        elif test_start is not None:
            if re.match(r"(?:pub(?:\([^)]*\))?\s+)?mod\s+tests\b", stripped):
                return text[:test_start]
            if stripped and not (stripped.startswith("#[") and stripped.endswith("]")):
                test_start = None
        offset += len(line)
    return text


def sources(root: Path) -> dict[str, str]:
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.rs"],
        cwd=root,
    ).decode().split("\0")
    return {
        path: production((root / path).read_text())
        for path in sorted(set(paths))
        if path and (root / path).is_file()
        and not set(Path(path).parts).intersection({"tests", "benches", "test_utils", "target"})
        and not path.endswith(("/tests.rs", "/test_utils.rs"))
    }


def registry(text: str) -> list[tuple[str, str, str]]:
    entries = re.findall(
        r"((?:[ \t]*///[^\n]*\n)+)[ \t]*(\w+)\s*=>\s*(\w+),", text
    )
    result = []
    for docs, feature, upgrade in entries:
        description = " ".join(line.strip().removeprefix("///").strip() for line in docs.splitlines())
        result.append((feature, upgrade, description))
    if not result or len({entry[0] for entry in result}) != len(result):
        raise ValueError("feature registry is empty, malformed, or contains duplicate features")
    return result


def upgrade_names(text: str) -> list[str]:
    body = text.split("    BaseUpgrade {", 1)[1].split("\n    }", 1)[0]
    return re.findall(r"^        (\w+),$", body, re.MULTILINE)


def fork_gate_errors(code: dict[str, str]) -> list[str]:
    """Catch common ways to bypass the registry; this is not a Rust semantic lint."""
    names = upgrade_names(code[UPGRADES])
    snake_names = [re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() for name in names]
    pattern = re.compile(
        rf"\bis_(?:first_)?(?:base_)?(?:{'|'.join(snake_names)})_"
        r"(?:active(?:_at_(?:timestamp|block))?|block)\s*\("
        r"|\bis_enabled_in\s*\(\s*BaseUpgrade::"
        rf"|(?<![=\-])(?:>=|<=|>|<)\s*\(?BaseUpgrade::(?:{'|'.join(names)})\b"
    )
    errors = []
    for path, text in code.items():
        if path in FORK_GATE_ADAPTERS:
            continue
        text = re.sub(r"//[^\n]*", "", text)
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{path}:{line}: Base behavior must use a feature gate: {match[0]}")
    return errors


def inventory(root: Path) -> dict[str, tuple[str, str]]:
    code = sources(root)
    entries = registry(code[REGISTRY])
    upgrades = upgrade_names(code[UPGRADES])
    if not upgrades:
        raise ValueError("no BaseUpgrade variants found")
    unknown = {upgrade for _, upgrade, _ in entries} - set(upgrades)
    if unknown:
        raise ValueError(f"features refer to unknown upgrades: {sorted(unknown)}")
    known = {feature for feature, _, _ in entries}
    used = {
        name for path, text in code.items() if path != REGISTRY
        for name in re.findall(r"BaseFeature::(\w+)", text)
    } - {"ALL"}
    if used - known:
        raise ValueError(f"unregistered feature uses: {sorted(used - known)}")
    consumers = {
        feature: [
            path for path, text in code.items()
            if path != REGISTRY and re.search(rf"\bBaseFeature::{feature}\b", text)
        ]
        for feature in known
    }
    result = {}
    for upgrade in upgrades:
        rows = []
        evidence = {path: code[path] for path in ADAPTERS}
        for feature, activation, description in entries:
            if activation != upgrade:
                continue
            paths = consumers[feature]
            if not paths:
                raise ValueError(f"{feature} has no production consumers")
            links = ", ".join(f"[`{path}`](../../{path})" for path in paths)
            rows.append(f"| `{feature}` | {description} | {links} |")
            evidence.update((path, code[path]) for path in paths)
        table = "\n".join([
            "| Feature | Behavior | Production code |",
            "| --- | --- | --- |",
            *rows,
        ]) if rows else "No separately gated Base features; see the baseline description above."
        # Include the registry mapping and descriptions, plus production code, so changes
        # behind an unchanged feature name still require a human to review the guide.
        digest = hashlib.sha256(table.encode())
        for path, text in sorted(evidence.items()):
            digest.update(path.encode())
            digest.update(text.encode())
        result[upgrade] = (table, digest.hexdigest()[:16])
    return result


def update_guide(text: str, data: dict[str, tuple[str, str]], stamp: set[str]) -> str:
    unknown = stamp - data.keys()
    if unknown:
        raise ValueError(f"unknown upgrades: {sorted(unknown)}")
    documented = re.findall(r"<!-- FEATURES:(\w+):START -->", text)
    if len(documented) != len(set(documented)) or set(documented) != data.keys():
        raise ValueError("guide must have exactly one FEATURES block per BaseUpgrade variant")
    for upgrade, (table, digest) in data.items():
        pattern = rf"(<!-- FEATURES:{upgrade}:START -->\n).*?(<!-- FEATURES:{upgrade}:END -->)"
        text, count = re.subn(pattern, lambda m: m[1] + table + "\n" + m[2], text, flags=re.DOTALL)
        if count != 1:
            raise ValueError(f"missing or malformed feature block for {upgrade}")
        review = rf"<!-- REVIEW:{upgrade}:[a-z0-9]+ -->"
        if len(re.findall(review, text)) != 1:
            raise ValueError(f"missing or duplicate review attestation for {upgrade}")
        if upgrade in stamp:
            text = re.sub(review, f"<!-- REVIEW:{upgrade}:{digest} -->", text)
    return text


def check(root: Path) -> list[str]:
    data = inventory(root)
    text = (root / GUIDE).read_text()
    errors = fork_gate_errors(sources(root))
    if update_guide(text, data, set()) != text:
        errors.append("feature inventory is stale; run upgrade-docs.py sync")
    for upgrade, (_, digest) in data.items():
        if f"<!-- REVIEW:{upgrade}:{digest} -->" not in text:
            errors.append(f"{upgrade}: production code changed; review prose and run upgrade-docs.py stamp {upgrade}")
    for link in re.findall(r"\]\((\.\.?/[^)#]+)(?:#[^)]*)?\)", text):
        if not (root / GUIDE).parent.joinpath(link).is_file():
            errors.append(f"broken code/documentation link: {link}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sync", "stamp"))
    parser.add_argument("upgrades", nargs="*")
    args = parser.parse_args()
    try:
        if args.command == "stamp" and not args.upgrades:
            parser.error("stamp requires explicit upgrade names after reviewing their code and prose")
        if args.command != "stamp" and args.upgrades:
            parser.error("upgrade names are only accepted by stamp")
        if args.command == "check":
            errors = check(ROOT)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("Upgrade docs match the feature registry and reviewed production code.")
        else:
            path = ROOT / GUIDE
            path.write_text(update_guide(path.read_text(), inventory(ROOT), set(args.upgrades)))
            print("Updated upgrade inventory" + (" and selected review attestations." if args.upgrades else "; review attestations unchanged."))
    except (ValueError, KeyError, IndexError) as error:
        print(f"upgrade-docs: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
