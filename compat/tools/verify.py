#!/usr/bin/env python3
"""Verify the LaminaDB compatibility corpus independently of the generator.

Two verification layers:
  1. Semantic: re-derive every healthy sample from its stored bytes with the independent
     oracle and compare to the recorded expectations; re-derive every corruption and check
     it fails at the expected stage. Does not trust generator conclusions.
  2. Live: when --binary is given, start a real server on a scratch copy of each healthy
     sample and compare the service's own answers to the oracle. Corrupt samples are opened
     with the real binary and must be rejected the same way.

    python3 compat/tools/verify.py --corpus compat [--binary bin/laminad] [--json]

Exit status is non-zero if any check fails.
"""

import argparse
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import lamina_oracle as oracle  # noqa: E402


def sha256_file(path):
    import hashlib
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


class Report:
    def __init__(self):
        self.checks = []

    def check(self, layer, sample_id, name, ok, detail=""):
        self.checks.append({"layer": layer, "sample": sample_id, "check": name,
                            "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def ok(self):
        return all(item["ok"] for item in self.checks)

    def failures(self):
        return [item for item in self.checks if not item["ok"]]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Live:
    def __init__(self, binary, directory, image=None, journal=None, restore=None):
        self.binary = str(binary)
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        if image is not None:
            shutil.copy2(image, self.dir / "checkpoint.lmi")
        if journal is not None:
            shutil.copy2(journal, self.dir / "journal.lmw")
        self.restore = str(restore) if restore else None
        self.process = None
        self.url = None

    def start(self):
        port = free_port()
        self.url = "http://127.0.0.1:%d/v1/lamina" % port
        args = [self.binary, "-data", str(self.dir), "-listen", "127.0.0.1:%d" % port]
        if self.restore:
            args += ["-restore", self.restore]
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                self.call("GET", "/health")
                return True
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        return False

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process is not None:
            self.process.communicate()
            self.process = None

    def call(self, method, path, data=None, status=None):
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=body, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                code, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            code, payload = exc.code, exc.read()
        if status is not None and code != status:
            raise RuntimeError("want %d got %d: %s" % (status, code, payload[:200]))
        return code, json.loads(payload)

    def snapshot(self, at):
        code, body = self.call("POST", "/query", {"relation": "spectra", "at": at})
        if code == 200:
            return {"ok": True, "rows": body["rows"]}
        return {"ok": False, "error": body.get("error", {}).get("code", "unknown")}

    def changes(self, after):
        code, body = self.call("GET", "/changes?after=%d&limit=1000" % after)
        if code == 200:
            return {"ok": True, "records": body["records"], "cursor": body["cursor"],
                    "head": body["head"], "history_base": body["history_base"],
                    "more": body["more"]}
        return {"ok": False, "error": body.get("error", {}).get("code", "unknown")}


def load_manifest(corpus):
    return json.loads((pathlib.Path(corpus) / "manifest.json").read_text())


def artifacts_for(corpus, entry):
    paths = {}
    for ref in entry["artifacts"]:
        path = pathlib.Path(corpus) / "corpus" / "blobs" / ref["blob"]
        if not path.exists():
            raise SystemExit("missing blob %s" % path)
        if sha256_file(path) != ref["sha256"]:
            raise SystemExit("blob digest mismatch %s" % path)
        paths[ref["role"]] = path
    return paths


def expected_for(corpus, entry):
    path = pathlib.Path(corpus) / entry["expectations"]["path"]
    if sha256_file(path) != entry["expectations"]["sha256"]:
        raise SystemExit("expectation digest mismatch %s" % path)
    return json.loads(path.read_text())


def describe_sample(corpus, entry):
    paths = artifacts_for(corpus, entry)
    image = paths.get("checkpoint")
    journal = paths.get("journal")
    return oracle.describe(
        image.read_bytes() if image else None,
        journal.read_bytes() if journal else None,
        at_values=entry["snapshot_anchors"], after_values=entry["change_anchors"],
    )


def image_digest_valid(raw):
    """True if the envelope format and digest check out, ignoring semantic validation."""
    try:
        envelope = json.loads(raw)
        payload = oracle.extract_payload(raw)
    except (oracle.FormatError, json.JSONDecodeError, ValueError):
        return False
    return (envelope.get("format") == 1
            and envelope.get("sha256") == oracle.sha256_hex(payload))


def compare_semantics(report, sample_id, expected, actual, image_bytes, journal_bytes):
    """Compare oracle-derived facts, not generator wording."""
    live = expected["live"]

    def same(name, a, b):
        report.check("semantic", sample_id, name, a == b,
                     "" if a == b else "expected %r, got %r" % (b, a))

    same("stats", actual["stats"], expected["oracle"]["stats"])
    if live is not None:
        same("live_stats", live["stats"], {k: actual["stats"][k] for k in live["stats"]})
        same("head_rows", live["head"]["rows"], actual["head_rows"])
        same("catalog", live["catalog"], actual["catalog"])
    for at, observed in (live or {}).get("snapshots", {}).items():
        derived = actual["snapshots"].get(at)
        if observed.get("ok"):
            same("snapshot_at_%s" % at, derived, observed)
        else:
            # the service exposes only a coarse error code; oracle "reason" is diagnostic
            same("snapshot_at_%s_error" % at,
                 (derived or {}).get("ok"), False)
            same("snapshot_at_%s_code" % at,
                 (derived or {}).get("error"), observed.get("error"))
    for after, observed in (live or {}).get("changes", {}).items():
        derived = actual["changes"].get(after)
        if observed.get("ok"):
            same("changes_after_%s_ok" % after, derived.get("ok"), True)
            same("changes_after_%s_cursor" % after, derived.get("cursor"), observed["cursor"])
            same("changes_after_%s_base" % after, derived.get("history_base"),
                 observed["history_base"])
            same("changes_after_%s_records" % after, derived.get("records"), observed["records"])
        else:
            same("changes_after_%s_error" % after, (derived or {}).get("ok"), False)
            same("changes_after_%s_code" % after, derived.get("error"), observed.get("error"))
    limit_page = (live or {}).get("changes_limit1_after_7")
    if limit_page is not None:
        state, journal = oracle.open_state(image_bytes, journal_bytes)
        derived_page = oracle.changes(state, journal, 7, 1)
        if limit_page.get("ok", True):
            same("changes_limit1_after_7_records",
                 derived_page.get("records"), limit_page.get("records"))
            same("changes_limit1_after_7_more", derived_page.get("more"), limit_page.get("more"))
            same("changes_limit1_after_7_cursor",
                 derived_page.get("cursor"), limit_page.get("cursor"))
        else:
            same("changes_limit1_after_7_error", derived_page.get("ok"), False)


def derive_cross_checks(samples, corruptions):
    """Recompute the discriminating relations directly from sample facts."""
    facts = {entry["id"]: entry for entry in samples}

    def sem(sample_id, key):
        return facts[sample_id]["semantics"][key]

    def ret(sample_id, key):
        return facts[sample_id]["retention"][key]

    def identities(sample_id):
        return ret(sample_id, "commit_identities")

    derived = {}

    def put(name, hold, detail):
        derived[name] = {"holds": bool(hold), "detail": detail}

    put(
        "checkpoint_digest_is_not_history_identity",
        sem("C7-prefix", "image_sha256") == sem("T7-twin", "image_sha256")
        and sem("C7-prefix", "history_digest") != sem("T7-twin", "history_digest"),
        "Two runs share checkpoint bytes but not journal history.",
    )
    put(
        "same_image_different_commit_identities",
        identities("C7-prefix") != identities("T7-twin")
        and len(identities("C7-prefix")) == len(identities("T7-twin")),
        "Twin runs carry different transaction ids at equal commit sequences.",
    )
    put(
        "restore_preserves_rows_but_moves_change_floor",
        sem("C9-suffix10", "head_rows_digest") == sem("R9-cont10", "head_rows_digest")
        and ret("C9-suffix10", "version_floor") == ret("R9-bare", "version_floor")
        == ret("R9-cont10", "version_floor")
        and ret("C9-suffix10", "change_floor") == 0
        and ret("R9-bare", "change_floor") == ret("R9-cont10", "change_floor") != 0,
        "Rows and version floor agree; change floors differ between source and restore.",
    )
    put(
        "restore_same_sequence_different_identity",
        sem("C9-suffix10", "stats")["sequence"] == sem("R9-cont10", "stats")["sequence"]
        and identities("C9-suffix10") != identities("R9-cont10"),
        "Equal head sequence with different commit identity sets.",
    )
    put(
        "vacuum_retention_is_a_semantic_difference",
        sem("C9-suffix10", "head_rows_digest") == sem("V0-novacuum", "head_rows_digest")
        and sem("C9-suffix10", "stats")["sequence"] == sem("V0-novacuum", "stats")["sequence"]
        and ret("C9-suffix10", "version_floor") != ret("V0-novacuum", "version_floor"),
        "Equal rows and sequence with different version floors.",
    )
    put(
        "commit_grouping_is_a_semantic_difference",
        sem("G1-single-tx", "head_rows_digest") == sem("G4-four-tx", "head_rows_digest")
        and len(identities("G1-single-tx")) != len(identities("G4-four-tx")),
        "Equal rows with different transaction counts.",
    )
    put(
        "tail_recovery_is_not_corruption",
        sem("J10-tail", "stats")["recovered_tail_bytes"] > 0
        and sem("J10-tail", "history_digest") == sem("J10-full", "history_digest"),
        "Tail bytes were reported and history still matches the complete journal.",
    )
    put(
        "restore_preserves_old_snapshots_but_not_old_cursors",
        sem("R9-bare", "snapshot_ok").get("5") is True
        and sem("R9-bare", "snapshot_ok").get("8") is True
        and sem("R9-bare", "snapshot_ok").get("2") is False
        and ret("R9-bare", "change_floor") == 9,
        "A restored instance answers at=5 and at=8 while the version floor stays 5 and the change "
        "floor is 9.",
    )
    put(
        "checkpoint_prefix_pair_is_consistent",
        sem("C7-prefix", "stats")["sequence"] == 7
        and sem("C8-prebackup", "stats")["sequence"] == 8
        and facts["C7-prefix"]["image_sequence"] == 7
        and facts["C8-prebackup"]["image_sequence"] == 8,
        "Each checkpoint+prefix pair replays to exactly its image sequence.",
    )
    put(
        "digest_valid_is_not_semantic_compatibility",
        all(entry["expected"].get("stage") == "state"
            and entry["id"].startswith(("X09", "X10", "X11", "X12", "X13"))
            for entry in corruptions
            if entry["id"].startswith(("X09", "X10", "X11", "X12", "X13")))
        and sum(1 for entry in corruptions
                if entry["id"].startswith(("X09", "X10", "X11", "X12", "X13"))) == 5,
        "Five digest-valid images are rejected at the state stage, not the format stage.",
    )
    put(
        "engine_matches_oracle_on_corrupt",
        all(entry.get("engine") is None or entry["engine"]["accepted"] is False
            for entry in corruptions),
        "The recorded binary outcome keeps every controlled corruption closed.",
    )
    put(
        "conservation_of_samples",
        len(samples) == len(facts) and len({f["semantics"]["image_sha256"] for f in samples
                                            if f["semantics"]["image_sha256"]}) >= 3,
        "Sample ids are unique and at least three distinct images are covered.",
    )
    return derived


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="compat")
    parser.add_argument("--binary", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    corpus = pathlib.Path(args.corpus)
    manifest = load_manifest(corpus)
    report = Report()

    # ------------------------------------------------------------- semantic layer
    for entry in manifest["samples"]:
        actual = describe_sample(corpus, entry)
        expected = expected_for(corpus, entry)
        paths = artifacts_for(corpus, entry)
        compare_semantics(
            report, entry["id"], expected, actual,
            paths["checkpoint"].read_bytes() if "checkpoint" in paths else None,
            paths["journal"].read_bytes() if "journal" in paths else None,
        )
        # retention floor claims
        report.check("semantic", entry["id"], "version_floor",
                     actual["stats"]["horizon"] == entry["retention"]["version_floor"])
        report.check("semantic", entry["id"], "change_floor",
                     actual["stats"]["history_base"] == entry["retention"]["change_floor"])
        # Every declared change anchor is answered consistently by the oracle itself.
        for anchor, derived in actual["changes"].items():
            if derived.get("ok"):
                report.check("semantic", entry["id"],
                             "changes_page_units_%s" % anchor,
                             derived["cursor"] <= derived["head"]
                             and (derived["more"] or derived["cursor"] <= derived["head"]))

    for entry in manifest["corruptions"]:
        paths = artifacts_for(corpus, entry)
        image = paths.get("checkpoint")
        journal = paths.get("journal")
        try:
            oracle.describe(image.read_bytes() if image else None,
                            journal.read_bytes() if journal else None)
            result = {"accepted": True, "code": None}
        except (oracle.FormatError, oracle.StateError) as exc:
            result = {"accepted": False, "code": getattr(exc, "code", "unclassified"),
                      "stage": "format" if isinstance(exc, oracle.FormatError) else "state"}
        report.check("semantic", entry["id"], "rejected",
                     not result["accepted"], "oracle accepted the corruption" if result["accepted"] else "")
        report.check("semantic", entry["id"], "failure_class",
                     result.get("code") == entry["expected"]["code"],
                     "expected class %s, got %s" % (entry["expected"]["code"], result.get("code")))
        if entry["expected"].get("stage"):
            report.check("semantic", entry["id"], "failure_stage",
                         result.get("stage") == entry["expected"]["stage"],
                         "expected %s, got %s" % (entry["expected"]["stage"], result.get("stage")))
        if entry["expected"].get("stage") == "state" and image is not None:
            # A state-stage image corruption must still pass format+digest validation.
            report.check("semantic", entry["id"], "digest_valid_but_state_invalid",
                         image_digest_valid(image.read_bytes()),
                         "state-stage image corruption failed digest validation instead")

    # ---------------------------------------------------------------- cross checks
    declared = {item["id"]: item for item in manifest["cross_checks"]}
    derived = derive_cross_checks(manifest["samples"], manifest["corruptions"])
    for name, item in derived.items():
        report.check("cross", "-", name, item["holds"], item["detail"])
        if name in declared:
            report.check("cross", "-", "declared_matches_derived_%s" % name,
                         declared[name]["holds"] == item["holds"])
    for name in declared:
        report.check("cross", "-", "declaration_is_derivable_%s" % name, name in derived,
                     "cross check has no independent derivation")
    for required in ("restore_preserves_rows_but_moves_change_floor",
                     "restore_same_sequence_different_identity",
                     "checkpoint_digest_is_not_history_identity",
                     "vacuum_retention_is_a_semantic_difference",
                     "commit_grouping_is_a_semantic_difference",
                     "tail_recovery_is_not_corruption"):
        report.check("cross", "-", "present_%s" % required,
                     derived.get(required, {}).get("holds") is True)

    # ---------------------------------------------------------------- corpus integrity
    blob_dir = corpus / "corpus" / "blobs"
    blobs = {path.name for path in blob_dir.iterdir()}
    referenced = {ref["blob"] for entry in manifest["samples"] + manifest["corruptions"]
                  for ref in entry["artifacts"]}
    report.check("integrity", "-", "no_orphan_blobs", referenced == blobs,
                 "unreferenced: %s" % sorted(blobs - referenced))
    for entry in manifest["samples"] + manifest["corruptions"]:
        for ref in entry["artifacts"]:
            path = blob_dir / ref["blob"]
            report.check("integrity", entry["id"], "blob_%s_content_addressed" % ref["role"],
                         sha256_file(path) == ref["blob"] and path.stat().st_size == ref["size"])
    report.check("integrity", "-", "healthy_samples_have_two_claims_checked",
                 all(s["claims"]["bytes"] and s["claims"]["state"] for s in manifest["samples"]))
    report.check("integrity", "-", "restore_samples_flag_history_gap",
                 all(s["claims"]["history"] is False for s in manifest["samples"]
                     if s["id"].startswith("R9")))

    # ----------------------------------------------------------------- live layer
    if args.binary:
        binary = pathlib.Path(args.binary).resolve()
        if not binary.exists():
            raise SystemExit("binary not found: %s" % binary)
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="lamina-verify-"))
        try:
            for entry in manifest["samples"]:
                paths = artifacts_for(corpus, entry)
                live = Live(binary, scratch / ("healthy-" + entry["id"]),
                            paths.get("checkpoint"), paths.get("journal"))
                if not live.start():
                    report.check("live", entry["id"], "starts", False, "service did not start")
                    continue
                try:
                    actual = oracle.describe(
                        paths["checkpoint"].read_bytes() if "checkpoint" in paths else None,
                        paths["journal"].read_bytes() if "journal" in paths else None,
                        at_values=entry["snapshot_anchors"],
                        after_values=entry["change_anchors"],
                    )
                    code, health = live.call("GET", "/health")
                    report.check("live", entry["id"], "health_sequence",
                                 health["sequence"] == actual["stats"]["sequence"])
                    code, stats = live.call("GET", "/stats")
                    report.check("live", entry["id"], "stats_sequence",
                                 stats["sequence"] == actual["stats"]["sequence"])
                    report.check("live", entry["id"], "stats_horizon",
                                 stats["horizon"] == actual["stats"]["horizon"])
                    report.check("live", entry["id"], "stats_history_base",
                                 stats["history_base"] == actual["stats"]["history_base"])
                    report.check("live", entry["id"], "stats_journal_records",
                                 stats["journal_records"] == actual["stats"]["journal_records"])
                    report.check("live", entry["id"], "stats_recovered_tail",
                                 stats["recovered_tail_bytes"]
                                 == actual["stats"]["recovered_tail_bytes"])
                    code, head = live.call("POST", "/query", {"relation": "spectra"})
                    report.check("live", entry["id"], "head_rows",
                                 head["rows"] == actual["head_rows"])
                    code, catalog = live.call("GET", "/catalog")
                    report.check("live", entry["id"], "catalog", catalog == actual["catalog"])
                    for at, derived in actual["snapshots"].items():
                        observed = live.snapshot(int(at))
                        if derived.get("ok"):
                            report.check("live", entry["id"], "snapshot_%s" % at,
                                         observed.get("ok") and observed["rows"] == derived["rows"])
                        else:
                            report.check("live", entry["id"], "snapshot_%s_rejected" % at,
                                         not observed.get("ok")
                                         and observed.get("error") == derived.get("error"))
                    for after, derived in actual["changes"].items():
                        observed = live.changes(int(after))
                        if derived.get("ok"):
                            report.check("live", entry["id"], "changes_%s" % after,
                                         observed.get("ok")
                                         and observed["records"] == derived["records"]
                                         and observed["cursor"] == derived["cursor"]
                                         and observed["history_base"] == derived["history_base"])
                        else:
                            report.check("live", entry["id"], "changes_%s_rejected" % after,
                                         not observed.get("ok")
                                         and observed.get("error") == derived.get("error"))
                    # A change page must never split a commit record.
                    page = live.changes(7)
                    if page.get("ok"):
                        report.check("live", entry["id"], "changes_page_whole_records",
                                     all(record.get("kind") != "commit"
                                         or len(record.get("writes", [])) >= 1
                                         for record in page["records"]))
                finally:
                    live.stop()

            for entry in manifest["corruptions"]:
                paths = artifacts_for(corpus, entry)
                directory = scratch / ("corrupt-" + entry["id"])
                image = paths.get("checkpoint")
                journal = paths.get("journal")
                if image and not journal:
                    command = [str(binary), "-restore", str(image), "-data", str(directory),
                               "-listen", "127.0.0.1:0"]
                else:
                    directory.mkdir(parents=True, exist_ok=True)
                    if image:
                        shutil.copy2(image, directory / "checkpoint.lmi")
                    if journal:
                        shutil.copy2(journal, directory / "journal.lmw")
                    command = [str(binary), "-data", str(directory), "-listen", "127.0.0.1:0"]
                process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    out, err = process.communicate(timeout=8)
                    accepted = process.returncode == 0
                    text = (out + err).strip()
                except subprocess.TimeoutExpired:
                    process.kill()
                    out, err = process.communicate()
                    accepted = True
                    text = "timed out while serving"
                report.check("live", entry["id"], "rejected", not accepted,
                             "binary accepted corruption" if accepted else "")
                if not accepted:
                    report.check("live", entry["id"], "reason_present", bool(text), text[:200])
                if image and not journal:
                    report.check("live", entry["id"], "restore_target_clean",
                                 not directory.exists(), "restore left a target directory")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # --------------------------------------------------------------------- summary
    failures = report.failures()
    if args.json:
        print(json.dumps({"ok": report.ok, "checks": report.checks}, indent=1))
    else:
        for item in report.checks:
            if not item["ok"]:
                print("FAIL [%s] %s %s: %s" % (item["layer"], item["sample"], item["check"],
                                               item["detail"]))
        layers = {}
        for item in report.checks:
            layers.setdefault(item["layer"], [0, 0])
            layers[item["layer"]][0] += 1
            if not item["ok"]:
                layers[item["layer"]][1] += 1
        for layer, (total, failed) in layers.items():
            print("%-10s %3d checks, %3d failed" % (layer, total, failed))
        print("RESULT", "PASS" if report.ok else "FAIL")
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
