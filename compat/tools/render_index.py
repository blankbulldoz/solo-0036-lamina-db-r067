#!/usr/bin/env python3
"""Render human-readable indexes from the corpus manifest.

    python3 compat/tools/render_index.py --corpus compat

Writes:
  compat/INDEX.md            one row per sample and corruption with the discrimination point
  compat/reports/summary.json compact machine-readable digest of the same facts
"""

import argparse
import json
import pathlib
import sys


def load(corpus):
    return json.loads((pathlib.Path(corpus) / "manifest.json").read_text())


def claims_text(claims):
    return "".join([
        "B" if claims.get("bytes") else "-",
        "S" if claims.get("state") else "-",
        "H" if claims.get("history") else "-",
    ])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="compat")
    args = parser.parse_args()
    corpus = pathlib.Path(args.corpus)
    manifest = load(corpus)

    lines = []
    lines.append("# Compatibility corpus index")
    lines.append("")
    lines.append("Generated %s by `compat/tools/render_index.py` from `manifest.json`. "
                 "Do not edit by hand; regenerate after `corpusgen.py`." % manifest["generated_at"])
    lines.append("")
    lines.append("Claims column: `B` bytes, `S` state, `H` original history consumers. "
                 "`B-S-H` means bytes and state are expected to pass while change replay is not "
                 "supported by that instance.")
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.append(manifest["purpose"])
    lines.append("")
    lines.append("## Healthy samples")
    lines.append("")
    lines.append("| id | claims | history scope | seq | version floor | change floor | image sha256 | head rows digest |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for entry in manifest["samples"]:
        sem = entry["semantics"]
        image = (sem["image_sha256"] or "-")[:12]
        lines.append("| `%s` | %s | `%s` | %d | %d | %d | `%s` | `%s` |" % (
            entry["id"], claims_text(entry["claims"]), entry.get("history_scope", "commits_and_changes"),
            sem["stats"]["sequence"],
            entry["retention"]["version_floor"], entry["retention"]["change_floor"],
            image, sem["head_rows_digest"][:16]))
    lines.append("")
    lines.append("### What each healthy sample is for")
    lines.append("")
    for entry in manifest["samples"]:
        lines.append("**`%s`** — %s" % (entry["id"], entry["purpose"]))
        lines.append("")
        lines.append("- %s" % entry["notes"])
        for artifact in entry["artifacts"]:
            lines.append("- artifact `%s` `%s` %d bytes sha256 `%s`" % (
                artifact["role"], artifact["name"], artifact["size"], artifact["blob"]))
        lines.append("- history scope: `%s`" % entry.get("history_scope", "commits_and_changes"))
        lines.append("- oracle: %s" % entry["expectations"]["path"])
        lines.append("")

    lines.append("## Controlled corruptions")
    lines.append("")
    lines.append("Every entry below is isolated under `corpus/blobs` and referenced only by "
                 "its corruption id. None is paired with a healthy sample id at read time; "
                 "the base is named separately.")
    lines.append("")
    lines.append("| id | base | transform | expected stage | expected class | oracle | live binary |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entry in manifest["corruptions"]:
        engine = entry.get("engine")
        if engine is None:
            engine_text = "not run"
        elif engine["accepted"]:
            engine_text = "ACCEPTED"
        else:
            engine_text = "rejected"
        restore = entry.get("engine_restore")
        if restore is not None:
            engine_text += ", restore " + ("accepted" if restore["accepted"] else "rejected")
        lines.append("| `%s` | `%s` | %s | %s | `%s` | %s | %s |" % (
            entry["id"], entry["base_sample"], entry["transform"],
            entry["expected"].get("stage", "-"), entry["expected"]["code"],
            "rejected" if not entry["oracle"]["accepted"] else "accepted", engine_text))
    lines.append("")
    lines.append("### Notes per corruption")
    lines.append("")
    for entry in manifest["corruptions"]:
        lines.append("**`%s`** — %s" % (entry["id"], entry["notes"]))
        lines.append("")
        if entry["oracle"]["accepted"]:
            lines.append("- oracle: accepted")
        else:
            lines.append("- oracle: %s stage, `%s`: %s" % (
                entry["oracle"].get("stage", "?"), entry["expected"]["code"],
                entry["oracle"].get("error", "")))
        for artifact in entry["artifacts"]:
            lines.append("- artifact `%s` `%s` %d bytes sha256 `%s`" % (
                artifact["role"], artifact["name"], artifact["size"], artifact["blob"]))
        engine = entry.get("engine")
        if engine is not None:
            lines.append("- live binary: exit `%s` %s" % (engine.get("exit"),
                                                          engine.get("error", "")))
        restore = entry.get("engine_restore")
        if restore is not None:
            lines.append("- live restore: exit `%s`, target created `%s`, %s" % (
                restore.get("exit"), restore.get("created"), restore.get("error", "")))
        lines.append("")

    lines.append("## Cross claims and how they are checked")
    lines.append("")
    lines.append("`verify.py` re-derives each of these from sample bytes. The manifest copy is "
                 "compared to the derivation, so editing a claim without changing bytes fails.")
    lines.append("")
    lines.append("| check | holds | detail |")
    lines.append("| --- | --- | --- |")
    for item in manifest["cross_checks"]:
        lines.append("| `%s` | %s | %s |" % (item["id"], "yes" if item["holds"] else "NO",
                                              item["detail"]))
    lines.append("")
    lines.append("## Retention model")
    lines.append("")
    lines.append("- version floor: %s" % manifest["retention_model"]["version_floor"])
    lines.append("- change floor: %s" % manifest["retention_model"]["change_floor"])
    lines.append("- rule: %s" % manifest["retention_model"]["rule"])
    lines.append("")
    lines.append("## Cost accounting")
    lines.append("")
    cost = manifest["cost"]
    lines.append("- unique blobs: %d" % cost["unique_blobs"])
    lines.append("- unique blob bytes: %d" % cost["unique_blob_bytes"])
    lines.append("- bytes referenced without dedup: %d" % cost["artifact_bytes_referenced"])
    lines.append("- saved by dedup: %d" % cost["dedup_saved_bytes"])
    for name, reason in cost["alternatives_considered"].items():
        lines.append("- considered `%s`: %s" % (name, reason))
    lines.append("")
    lines.append("Why not the latest file only: %s" % manifest["coverage"]["why_not_latest_only"])
    lines.append("")
    lines.append("## Rebuild and verify")
    lines.append("")
    lines.append("```sh")
    for step in manifest["rebuild"]["steps"]:
        lines.append(step)
    lines.append("```")
    lines.append("")
    lines.append(manifest["rebuild"]["determinism"])
    lines.append("")
    lines.append("## Known limits")
    lines.append("")
    lines.append("- The corpus exercises one relation, one schema and sequences 0..12. It is not a "
                 "fuzzing campaign or a claim about unrelated workloads.")
    lines.append("- The oracle is a second implementation of the published format and semantics. "
                 "Agreement with the binary on these samples is evidence, not proof of equivalence.")
    lines.append("- The live layer runs against the binary recorded as `%s`. A different binary "
                 "must be verified separately." % manifest["generator"]["binary_sha256"][:16])
    lines.append("- Transaction ids are random, so only within-run relations and semantic digests "
                 "are stable across regeneration.")

    index_path = corpus / "INDEX.md"
    index_path.write_text("\n".join(lines) + "\n")

    summary = {
        "generated_at": manifest["generated_at"],
        "binary_sha256": manifest["generator"]["binary_sha256"],
        "healthy": [
            {
                "id": entry["id"],
                "claims": entry["claims"],
                "history_scope": entry.get("history_scope"),
                "sequence": entry["semantics"]["stats"]["sequence"],
                "version_floor": entry["retention"]["version_floor"],
                "change_floor": entry["retention"]["change_floor"],
                "image_sha256": entry["semantics"]["image_sha256"],
                "head_rows_digest": entry["semantics"]["head_rows_digest"],
                "artifacts": entry["artifacts"],
            }
            for entry in manifest["samples"]
        ],
        "corrupt": [
            {
                "id": entry["id"],
                "base": entry["base_sample"],
                "expected": entry["expected"],
                "oracle": entry["oracle"],
                "live_binary_rejected": (entry.get("engine") or {}).get("accepted") is False,
            }
            for entry in manifest["corruptions"]
        ],
        "cross_checks": manifest["cross_checks"],
        "cost": manifest["cost"],
    }
    report_dir = corpus / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({"index": str(index_path),
                      "summary": str(report_dir / "summary.json"),
                      "healthy": len(summary["healthy"]),
                      "corrupt": len(summary["corrupt"])}, indent=2))


if __name__ == "__main__":
    main()
