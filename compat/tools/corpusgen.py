#!/usr/bin/env python3
"""Generate the LaminaDB compatibility corpus from a live binary.

The generator drives the real HTTP service, captures the artifacts it writes, records the
observations the service itself reports, and derives controlled corruptions from those
captured bytes. It depends only on the standard library. Run with `python3 -I`.

    python3 -I compat/tools/corpusgen.py --binary bin/laminad --out compat
"""

import argparse
import hashlib
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

SCHEMA = {"name": "spectra", "primary": "sample", "columns": [
    {"name": "sample", "type": "int"},
    {"name": "band", "type": "text"},
    {"name": "amplitude", "type": "int", "nullable": True},
    {"name": "valid", "type": "bool"},
]}
SNAPSHOT_ANCHORS = list(range(0, 13))
CHANGE_ANCHORS = list(range(0, 13))
RECIPE_VERSION = "1"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def row(n, amp=None, band=None):
    return {
        "sample": n,
        "band": band or ("b%d" % n),
        "amplitude": n * 10 if amp is None else amp,
        "valid": True,
    }


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Session:
    def __init__(self, binary, root, restore=None):
        self.binary = str(binary)
        self.root = pathlib.Path(root)
        self.restore = None if restore is None else str(restore)
        self.process = None
        self.url = None
        self.stderr = ""

    def start(self):
        port = free_port()
        self.url = "http://127.0.0.1:%d/v1/lamina" % port
        args = [self.binary, "-data", str(self.root), "-listen", "127.0.0.1:%d" % port]
        if self.restore:
            args += ["-restore", self.restore]
            self.restore = None
        self.process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                out, err = self.process.communicate()
                raise RuntimeError("service exited: %s%s" % (out, err))
            try:
                self.call("GET", "/health")
                return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        raise RuntimeError("service did not become ready")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process is not None:
            _out, err = self.process.communicate()
            self.stderr += err
            self.process = None

    def call(self, method, path, data=None, status=200, raw=False):
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=body, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                code, payload = response.status, response.read()
        except urllib.error.HTTPError as exc:
            code, payload = exc.code, exc.read()
        if code != status:
            raise RuntimeError("%s %s: want %d got %d: %s" % (
                method, path, status, code, payload.decode("utf-8", "replace")))
        return payload if raw else json.loads(payload)

    def begin(self, read_only=False):
        return self.call("POST", "/transactions", {"read_only": read_only}, 201)["id"]

    def put(self, tx, value):
        return self.call("POST", "/transactions/%s/put" % tx,
                         {"relation": "spectra", "row": value, "insert_only": False})

    def delete(self, tx, key):
        return self.call("POST", "/transactions/%s/delete" % tx,
                         {"relation": "spectra", "key": key})

    def commit(self, tx):
        return self.call("POST", "/transactions/%s/commit" % tx)

    def query(self, **kwargs):
        return self.call("POST", "/query", {"relation": "spectra", **kwargs})


def read_bytes(path):
    return pathlib.Path(path).read_bytes()


def error_code(exc):
    text = str(exc)
    marker = text.find("{")
    if marker >= 0:
        try:
            body = json.loads(text[marker:])
            return body.get("error", {}).get("code", "internal")
        except json.JSONDecodeError:
            pass
    return "internal"


def observe_http(session, sample_id):
    """Record the service's own answers for the anchors used by the oracle."""
    observations = {
        "sample": sample_id,
        "catalog": session.call("GET", "/catalog"),
        "stats": session.call("GET", "/stats"),
        "head": session.query(),
        "snapshots": {},
        "changes": {},
    }
    try:
        observations["changes_limit1_after_7"] = session.call("GET", "/changes?after=7&limit=1")
    except RuntimeError as exc:
        observations["changes_limit1_after_7"] = {"ok": False, "error": error_code(exc)}
    for at in SNAPSHOT_ANCHORS:
        try:
            result = session.query(at=at)
            observations["snapshots"][str(at)] = {"ok": True, "rows": result["rows"]}
        except RuntimeError as exc:
            observations["snapshots"][str(at)] = {"ok": False, "error": error_code(exc)}
    for after in CHANGE_ANCHORS:
        try:
            page = session.call("GET", "/changes?after=%d&limit=1000" % after)
            observations["changes"][str(after)] = {
                "ok": True,
                "cursor": page["cursor"],
                "head": page["head"],
                "history_base": page["history_base"],
                "more": page["more"],
                "records": page["records"],
            }
        except RuntimeError as exc:
            observations["changes"][str(after)] = {"ok": False, "error": error_code(exc)}
    stats = observations["stats"]
    observations["stats"] = {
        key: stats[key] for key in (
            "sequence", "horizon", "keys", "versions", "history_base",
            "journal_records", "recovered_tail_bytes",
        )
    }
    return observations


def build_journal(base, frame_payloads):
    header = bytearray(16)
    header[0:4] = b"LMW1"
    header[4:12] = base.to_bytes(8, "big")
    header[12:16] = oracle.crc32(bytes(header[:12])).to_bytes(4, "big")
    out = bytearray(header)
    for payload in frame_payloads:
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        out += len(payload).to_bytes(4, "big")
        out += oracle.crc32(payload).to_bytes(4, "big")
        out += payload
    return bytes(out)


def parse_frames(raw):
    parsed = oracle.parse_journal(raw)
    return parsed["base"], [frame["payload"] for frame in parsed["frames"]], parsed["frames"]


def rebuild_journal(raw, sequence, mutate, keep=1.0):
    """Re-encode a journal changing one record, optionally keeping only a prefix."""
    base, payloads, frames = parse_frames(raw)
    limit = int(len(payloads) * keep)
    out = []
    for index, payload in enumerate(payloads):
        if index >= limit:
            break
        record = json.loads(payload)
        if record["sequence"] == sequence:
            record = mutate(record)
            if record is None:
                continue
        out.append(json.dumps(record, separators=(",", ":")).encode("utf-8"))
    return build_journal(base, out)


def process_result(process, timeout, created=None):
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        return {
            "accepted": True,
            "exit": None,
            "stderr": (out + err).strip()[:300],
            "note": "still serving after %ss; artifact accepted" % timeout,
        }
    text = (out + err).strip()
    first_error = ""
    for line in text.splitlines():
        if "error=" in line:
            first_error = line.split("error=", 1)[1].strip()
            break
    result = {
        "accepted": process.returncode == 0,
        "exit": process.returncode,
        "stderr": text[:300],
        "error": first_error[:240],
    }
    if created is not None:
        result["created"] = pathlib.Path(created).exists()
    return result


def binary_open(binary, directory, timeout=8):
    process = subprocess.Popen(
        [str(binary), "-data", str(directory), "-listen", "127.0.0.1:0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return process_result(process, timeout)


def binary_restore(binary, image_file, target, timeout=8):
    process = subprocess.Popen(
        [str(binary), "-restore", str(image_file), "-data", str(target),
         "-listen", "127.0.0.1:0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return process_result(process, timeout, created=target)


class Corpus:
    def __init__(self, out):
        self.out = pathlib.Path(out)
        self.blob_dir = self.out / "corpus" / "blobs"
        self.expected_dir = self.out / "corpus" / "expected"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.expected_dir.mkdir(parents=True, exist_ok=True)
        self.samples = []
        self.corruptions = []
        self._blob_cache = {}

    def add_blob(self, data):
        digest = sha256(data)
        if digest not in self._blob_cache:
            path = self.blob_dir / digest
            if not path.exists():
                path.write_bytes(data)
            self._blob_cache[digest] = {"blob": digest, "sha256": digest, "size": len(data)}
        return dict(self._blob_cache[digest])

    def artifact_refs(self, artifacts):
        order = [role for role in ("checkpoint", "journal") if role in artifacts]
        refs = []
        for role in order:
            data = read_bytes(artifacts[role])
            ref = {"role": role,
                   "name": "checkpoint.lmi" if role == "checkpoint" else "journal.lmw"}
            ref.update(self.add_blob(data))
            refs.append(ref)
        return refs

    def write_expected(self, sample_id, payload):
        path = self.expected_dir / ("%s.json" % sample_id)
        path.write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return {"path": str(path.relative_to(self.out)), "sha256": sha256(read_bytes(path))}

    def add_sample(self, sample_id, purpose, claims, artifacts, observations, notes,
                   at_values=SNAPSHOT_ANCHORS, after_values=CHANGE_ANCHORS,
                   journal_base_override=None, history_scope="commits_and_changes"):
        refs = self.artifact_refs(artifacts)
        image_bytes = read_bytes(artifacts["checkpoint"]) if "checkpoint" in artifacts else None
        journal_bytes = read_bytes(artifacts["journal"]) if "journal" in artifacts else None
        description = oracle.describe(
            image_bytes, journal_bytes,
            at_values=at_values, after_values=after_values,
            journal_base_override=journal_base_override,
        )
        entry = {
            "id": sample_id,
            "kind": "healthy",
            "purpose": purpose,
            "claims": claims,
            "history_scope": history_scope,
            "notes": notes,
            "artifacts": refs,
            "retention": {
                "version_floor": description["stats"]["horizon"],
                "change_floor": description["stats"]["history_base"],
                "commit_identities": description["retention"]["journal_commits"],
            },
            "image_sequence": description["image_sequence"],
            "semantics": {
                "snapshot_ok": {anchor: result["ok"]
                                for anchor, result in description["snapshots"].items()},
                "head_rows_digest": description["rows_digest"],
                "history_digest": description["history_digest"],
                "image_sha256": description["image_sha256"],
                "journal_sha256": description["journal_sha256"],
                "stats": description["stats"],
            },
            "snapshot_anchors": at_values,
            "change_anchors": after_values,
        }
        expected = {"sample": sample_id, "oracle": description, "live": observations}
        entry["expectations"] = self.write_expected(sample_id, expected)
        self.samples.append(entry)
        return entry, description

    def add_corrupt(self, corruption_id, base_sample, transform, artifacts, expected, notes,
                    engine=None, engine_restore=None):
        refs = self.artifact_refs(artifacts)
        image_bytes = read_bytes(artifacts["checkpoint"]) if "checkpoint" in artifacts else None
        journal_bytes = read_bytes(artifacts["journal"]) if "journal" in artifacts else None
        try:
            oracle.describe(image_bytes, journal_bytes)
            oracle_result = {"accepted": True, "code": None, "stage": None}
        except oracle.FormatError as exc:
            oracle_result = {"accepted": False, "code": exc.code, "stage": "format",
                             "error": str(exc)}
        except oracle.StateError as exc:
            oracle_result = {"accepted": False, "code": exc.code, "stage": "state",
                             "error": str(exc)}
        entry = {
            "id": corruption_id,
            "kind": "corrupt",
            "base_sample": base_sample,
            "transform": transform,
            "notes": notes,
            "artifacts": refs,
            "expected": expected,
            "oracle": oracle_result,
            "engine": engine,
            "engine_restore": engine_restore,
        }
        self.corruptions.append(entry)
        return entry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default="bin/laminad")
    parser.add_argument("--out", default="compat")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    binary = pathlib.Path(args.binary).resolve()
    if not binary.exists():
        raise SystemExit("binary not found: %s" % binary)
    binary_digest = sha256(binary.read_bytes())
    try:
        go_version = subprocess.run(["go", "version"], capture_output=True, text=True,
                                    timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        go_version = "unreported"
    if not go_version:
        go_version = "unreported"

    corpus = Corpus(args.out)
    work = pathlib.Path(tempfile.mkdtemp(prefix="lamina-corpus-"))
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def stage_dir(name, image=None, journal=None):
        directory = work / name
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        if image is not None:
            shutil.copy2(image, directory / "checkpoint.lmi")
        if journal is not None:
            shutil.copy2(journal, directory / "journal.lmw")
        return directory

    def live_observe(name, sample_id, image, journal):
        session = Session(binary, stage_dir("live-" + name, image, journal)).start()
        try:
            return observe_http(session, sample_id)
        finally:
            session.stop()

    # ------------------------------------------------------------------ canonical source
    source = Session(binary, work / "source").start()
    recipe = []
    tx_ids = {}

    def step(description):
        recipe.append({"step": description, "sequence": source.call("GET", "/stats")["sequence"]})

    source.call("POST", "/relations", SCHEMA, 201)
    step("create relation spectra")
    tx = source.begin()
    for n in (1, 2, 3, 4):
        source.put(tx, row(n))
    commit = source.commit(tx)
    tx_ids["rows-1-4"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit rows 1..4 in one transaction")
    tx = source.begin()
    source.put(tx, row(1, amp=111))
    commit = source.commit(tx)
    tx_ids["overwrite-1"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit overwrite sample 1 amplitude=111")
    tx = source.begin()
    source.delete(tx, 2)
    commit = source.commit(tx)
    tx_ids["delete-2"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit delete sample 2")
    tx = source.begin()
    source.put(tx, row(8))
    source.put(tx, row(9))
    commit = source.commit(tx)
    tx_ids["insert-8-9"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit insert samples 8 and 9")
    source.call("POST", "/indexes", {"name": "by_amplitude", "relation": "spectra",
                                     "column": "amplitude", "unique": False}, 201)
    step("create non-unique index by_amplitude")
    source.call("POST", "/indexes", {"name": "by_band", "relation": "spectra",
                                     "column": "band", "unique": True}, 201)
    step("create unique index by_band")
    source.call("POST", "/checkpoint")
    image7 = work / "image7.lmi"
    shutil.copy2(source.root / "checkpoint.lmi", image7)
    journal7 = work / "journal7.lmw"
    shutil.copy2(source.root / "journal.lmw", journal7)
    step("checkpoint pre-vacuum anchor (sequence 7)")
    vacuum = source.call("POST", "/vacuum", {"horizon": 5})
    step("vacuum horizon=5")
    source.call("POST", "/checkpoint")
    image8 = work / "image8.lmi"
    shutil.copy2(source.root / "checkpoint.lmi", image8)
    step("checkpoint post-vacuum anchor (sequence 8)")
    tx = source.begin()
    source.put(tx, row(8, amp=888))
    commit = source.commit(tx)
    tx_ids["rewrite-8"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit rewrite sample 8 amplitude=888")
    source.call("POST", "/checkpoint")
    image9 = work / "image9.lmi"
    shutil.copy2(source.root / "checkpoint.lmi", image9)
    step("checkpoint backup anchor (sequence 9)")
    backup = source.call("GET", "/backup", raw=True)
    backup_path = work / "backup9.lmi"
    backup_path.write_bytes(backup)
    backup_matches_checkpoint = backup == read_bytes(image9)
    backup_sequence = json.loads(backup)["payload"]["sequence"]
    tx = source.begin()
    source.put(tx, row(10))
    commit = source.commit(tx)
    tx_ids["insert-10"] = {"transaction": tx, "sequence": commit["sequence"]}
    step("commit insert sample 10")
    source_observations = observe_http(source, "C9-suffix10")
    source.stop()
    journal10 = work / "journal10.lmw"
    shutil.copy2(source.root / "journal.lmw", journal10)

    _, _, frames10 = parse_frames(read_bytes(journal10))
    if len(frames10) != 10:
        raise SystemExit("expected 10 journal frames, found %d" % len(frames10))
    journal10_raw = read_bytes(journal10)
    journal8_raw = journal10_raw[: frames10[7]["end"]]  # records 1..8
    journal7_raw = journal10_raw[: frames10[6]["end"]]  # records 1..7
    journal8 = work / "journal8.lmw"
    journal8.write_bytes(journal8_raw)
    if read_bytes(journal7) != journal7_raw:
        raise SystemExit("captured pre-vacuum journal differs from the recorded prefix")
    journal8_obs = live_observe("c8", "C8-prebackup", image8, journal8)
    journal7_obs = live_observe("c7", "C7-prefix", image7, journal7)
    j10_obs = live_observe("j10", "J10-full", None, journal10)

    # ---------------------------------------------------------------- restored instance
    restored_before_session = Session(binary, work / "restored", restore=image9).start()
    restored_before = observe_http(restored_before_session, "R9-bare")
    restored_initial_journal = work / "restored-initial-journal.lmw"
    shutil.copy2(restored_before_session.root / "journal.lmw", restored_initial_journal)
    restored_initial_bytes = read_bytes(restored_initial_journal)
    restored_initial_base = oracle.parse_journal(restored_initial_bytes)["base"]
    if restored_initial_base != backup_sequence or len(restored_initial_bytes) != 16:
        raise SystemExit("restore did not install an empty journal at the image sequence")
    tx = restored_before_session.begin()
    restored_before_session.put(tx, row(10))
    restored_commit = restored_before_session.commit(tx)
    restored_tx_id = tx
    restored_after = observe_http(restored_before_session, "R9-cont10")
    restored_before_session.stop()
    restored_journal = work / "restored-journal.lmw"
    shutil.copy2(restored_before_session.root / "journal.lmw", restored_journal)

    # --------------------------------------------------------------------- twin source
    twin = Session(binary, work / "twin").start()
    twin.call("POST", "/relations", SCHEMA, 201)
    tx = twin.begin()
    for n in (1, 2, 3, 4):
        twin.put(tx, row(n))
    twin.commit(tx)
    tx = twin.begin()
    twin.put(tx, row(1, amp=111))
    twin.commit(tx)
    tx = twin.begin()
    twin.delete(tx, 2)
    twin.commit(tx)
    tx = twin.begin()
    twin.put(tx, row(8))
    twin.put(tx, row(9))
    twin.commit(tx)
    twin.call("POST", "/indexes", {"name": "by_amplitude", "relation": "spectra",
                                   "column": "amplitude", "unique": False}, 201)
    twin.call("POST", "/indexes", {"name": "by_band", "relation": "spectra",
                                   "column": "band", "unique": True}, 201)
    twin.call("POST", "/checkpoint")
    twin_observations = observe_http(twin, "T7-twin")
    twin.stop()
    twin_image = work / "twin-image7.lmi"
    shutil.copy2(twin.root / "checkpoint.lmi", twin_image)
    twin_journal = work / "twin-journal7.lmw"
    shutil.copy2(twin.root / "journal.lmw", twin_journal)

    # ------------------------------------------------------------- no-vacuum variant
    # Same recipe as the source but with an explicit horizon 0 vacuum in place of the
    # horizon 5 vacuum. The record count and sequence line up, so the only differences
    # are the vacuum target and the retention floor.
    novac = Session(binary, work / "novacuum").start()
    novac_recipe = []
    novac_tx = {}

    def novac_step(description):
        novac_recipe.append({"step": description,
                             "sequence": novac.call("GET", "/stats")["sequence"]})

    novac.call("POST", "/relations", SCHEMA, 201)
    novac_step("create relation spectra")
    tx = novac.begin()
    for n in (1, 2, 3, 4):
        novac.put(tx, row(n))
    commit = novac.commit(tx)
    novac_tx["rows-1-4"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit rows 1..4 in one transaction")
    tx = novac.begin()
    novac.put(tx, row(1, amp=111))
    commit = novac.commit(tx)
    novac_tx["overwrite-1"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit overwrite sample 1 amplitude=111")
    tx = novac.begin()
    novac.delete(tx, 2)
    commit = novac.commit(tx)
    novac_tx["delete-2"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit delete sample 2")
    tx = novac.begin()
    novac.put(tx, row(8))
    novac.put(tx, row(9))
    commit = novac.commit(tx)
    novac_tx["insert-8-9"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit insert samples 8 and 9")
    novac.call("POST", "/indexes", {"name": "by_amplitude", "relation": "spectra",
                                    "column": "amplitude", "unique": False}, 201)
    novac_step("create non-unique index by_amplitude")
    novac.call("POST", "/indexes", {"name": "by_band", "relation": "spectra",
                                    "column": "band", "unique": True}, 201)
    novac_step("create unique index by_band")
    novac.call("POST", "/checkpoint")
    novac_step("checkpoint pre-vacuum-control anchor (sequence 7)")
    novac_control_image = work / "novacuum-image7.lmi"
    shutil.copy2(novac.root / "checkpoint.lmi", novac_control_image)
    novac_control_journal = work / "novacuum-journal7.lmw"
    shutil.copy2(novac.root / "journal.lmw", novac_control_journal)
    control_vacuum = novac.call("POST", "/vacuum", {"horizon": 0})
    novac_step("vacuum horizon=0 (no version removed)")
    novac.call("POST", "/checkpoint")
    novac_step("checkpoint post-vacuum-control anchor (sequence 8)")
    novac_image = work / "novacuum-image8.lmi"
    shutil.copy2(novac.root / "checkpoint.lmi", novac_image)
    tx = novac.begin()
    novac.put(tx, row(8, amp=888))
    commit = novac.commit(tx)
    novac_tx["rewrite-8"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit rewrite sample 8 amplitude=888")
    novac.call("POST", "/checkpoint")
    novac_step("checkpoint control at sequence 9")
    tx = novac.begin()
    novac.put(tx, row(10))
    commit = novac.commit(tx)
    novac_tx["insert-10"] = {"transaction": tx, "sequence": commit["sequence"]}
    novac_step("commit insert sample 10")
    novac_observations = observe_http(novac, "V0-novacuum")
    novac.stop()
    novac_journal = work / "novacuum-journal.lmw"
    shutil.copy2(novac.root / "journal.lmw", novac_journal)
    if control_vacuum["removed"] != 0 or control_vacuum["horizon"] != 0:
        raise SystemExit("control vacuum was not a no-op: %r" % (control_vacuum,))

    # ------------------------------------------------------------ commit grouping pair
    def grouping_run(root, group_sizes, sample_id):
        session = Session(binary, root).start()
        session.call("POST", "/relations", SCHEMA, 201)
        written = 0
        for size in group_sizes:
            tx = session.begin()
            for _ in range(size):
                written += 1
                session.put(tx, row(written))
            session.commit(tx)
        observations = observe_http(session, sample_id)
        session.stop()
        journal = pathlib.Path(root) / "journal.lmw"
        return journal, observations

    single_journal, single_observations = grouping_run(
        work / "group-single", [4], "G1-single-tx")
    four_journal, four_observations = grouping_run(
        work / "group-four", [1, 1, 1, 1], "G4-four-tx")

    # ------------------------------------------------------------------- tail variant
    tail_journal = work / "tail-journal.lmw"
    tail_journal.write_bytes(journal10_raw + b"\x00\x00\x00")
    tail_observations = live_observe("tail", "J10-tail", None, tail_journal)

    # --------------------------------------------------------- healthy sample assembly
    descriptions = {}

    def register(sample_id, purpose, claims, artifacts, observations, notes, history_scope):
        entry, description = corpus.add_sample(
            sample_id, purpose, claims, artifacts, observations, notes,
            history_scope=history_scope)
        descriptions[sample_id] = description
        return entry

    register(
        "J10-full",
        "Journal only, base 0, ten complete frames. A reader must replay every record to recover state.",
        {"bytes": True, "state": True, "history": True},
        {"journal": journal10}, j10_obs,
        "Current rows alone hide tombstones, DDL and the vacuum record. Paired with X03/X04/X05/X06/X07/X16-X19 "
        "for complete-frame corruption.",
        history_scope="commits_and_changes",
    )
    register(
        "C8-prebackup",
        "Checkpoint written immediately after vacuum, journal through record 8.",
        {"bytes": True, "state": True, "history": True},
        {"checkpoint": image8, "journal": journal8}, journal8_obs,
        "A genuine on-disk pair from the source run: image sequence 8 and horizon 5.",
        history_scope="commits_and_changes",
    )
    register(
        "C7-prefix",
        "Older pre-vacuum pair: image sequence 7 and journal through record 7.",
        {"bytes": True, "state": True, "history": True},
        {"checkpoint": image7, "journal": journal7}, journal7_obs,
        "Version floor 0 and three extra readable snapshots versus C8-prebackup. Used with T7-twin to show that "
        "an image digest is not commit identity.",
        history_scope="commits_and_changes",
    )
    register(
        "C9-suffix10",
        "Canonical checkpoint plus journal suffix: image sequence 9 with one frame beyond it.",
        {"bytes": True, "state": True, "history": True},
        {"checkpoint": image9, "journal": journal10}, source_observations,
        "A real source directory at sequence 10. Backup at sequence 9 equals checkpoint9 byte for byte.",
        history_scope="commits_and_changes",
    )
    register(
        "V0-novacuum",
        "Same recipe, sequence and head rows as C9-suffix10, but the vacuum targets horizon 0 so the "
        "version floor stays 0.",
        {"bytes": True, "state": True, "history": True},
        {"checkpoint": novac_image, "journal": novac_journal}, novac_observations,
        "The strongest state-control pair: identical visible rows, sequence and record count, different "
        "retained interval. A reader that only compares current query output cannot tell them apart, and "
        "a reader that infers the change floor from the version floor gets both wrong.",
        history_scope="commits_and_changes",
    )
    register(
        "R9-bare",
        "Restored backup before any new commit: image 9 with the empty journal the restore installs.",
        {"bytes": True, "state": True, "history": False},
        {"checkpoint": image9, "journal": restored_initial_journal}, restored_before,
        "State and snapshots 5..9 are intact, but the instance has no change records. Every cursor below 9 "
        "returns 410 even though those rows are still visible.",
        history_scope="snapshots_only",
    )
    register(
        "R9-cont10",
        "Restored backup plus one new commit: equal sequence and head rows to C9-suffix10, "
        "different change floor and different commit identity at sequence 10.",
        {"bytes": True, "state": True, "history": False},
        {"checkpoint": image9, "journal": restored_journal}, restored_after,
        "Equal rows digest and equal state sequence, but change_floor 9 versus 0 and a different transaction id "
        "at sequence 10. Backup/restore and change replay must be explained jointly, not by current queries.",
        history_scope="snapshots_plus_new_changes",
    )
    register(
        "T7-twin",
        "Independent twin run of the C7 recipe: identical checkpoint bytes, different transaction ids.",
        {"bytes": True, "state": True, "history": True},
        {"checkpoint": twin_image, "journal": twin_journal}, twin_observations,
        "Checkpoint sha256 equals C7-prefix exactly; journal history digest differs. Format identity does not "
        "identify history.",
        history_scope="commits_and_changes",
    )
    register(
        "G1-single-tx",
        "Four rows written by one transaction: one commit identity.",
        {"bytes": True, "state": True, "history": True},
        {"journal": single_journal}, single_observations,
        "Head rows match G4-four-tx while commit grouping does not.",
        history_scope="commits_and_changes",
    )
    register(
        "G4-four-tx",
        "The same four rows written by four transactions: four commit identities.",
        {"bytes": True, "state": True, "history": True},
        {"journal": four_journal}, four_observations,
        "Same rows digest as G1-single-tx, different sequence span and history digest.",
        history_scope="commits_and_changes",
    )
    register(
        "J10-tail",
        "Complete history plus a three-byte incomplete final frame. A reader must recover, not reject.",
        {"bytes": True, "state": True, "history": True},
        {"journal": tail_journal}, tail_observations,
        "recovered_tail_bytes 3 and state equal to J10-full. Contrast X03-frame-crc: a complete frame with a "
        "bad checksum is fatal.",
        history_scope="commits_and_changes",
    )

    # ------------------------------------------------------------- corruption assembly
    image9_bytes = read_bytes(image9)
    image_payload = json.loads(oracle.extract_payload(image9_bytes))
    journal10_bytes = read_bytes(journal10)
    live_tail_bytes = tail_observations["stats"]["recovered_tail_bytes"]
    if live_tail_bytes != 3:
        raise SystemExit("tail fixture did not report 3 recovered bytes: %r" % live_tail_bytes)

    def write_work(name, data):
        path = work / name
        path.write_bytes(data)
        return path

    def image_corrupt(cid, transform, payload, code, notes):
        broken = write_work(cid + ".lmi", oracle.make_image(payload))
        engine = binary_open(binary, stage_dir("open-" + cid, broken, journal10))
        restore = binary_restore(binary, broken, work / ("restore-" + cid))
        corpus.add_corrupt(
            cid, "C9-suffix10", transform,
            {"checkpoint": broken, "journal": journal10},
            {"stage": "state", "code": code}, notes,
            engine=engine, engine_restore=restore,
        )

    payload = json.loads(json.dumps(image_payload))
    payload["store"]["tables"]["spectra"]["int:8"][1]["row"]["sample"] = 7
    image_corrupt(
        "X09-image-primary-mismatch", "stored row says sample 7 while the chain key stays int:8",
        payload, "invalid_stored_tuple",
        "Digest recomputed and valid; the image parses and is still semantically invalid.")
    payload = json.loads(json.dumps(image_payload))
    chain = payload["store"]["tables"]["spectra"]["int:8"]
    chain[0]["sequence"], chain[1]["sequence"] = chain[1]["sequence"], chain[0]["sequence"]
    image_corrupt(
        "X10-image-version-order", "swap the two version sequence numbers of key int:8",
        payload, "invalid_version_order",
        "Digest valid; the version chain is not monotonic.")
    payload = json.loads(json.dumps(image_payload))
    payload["store"]["tables"]["spectra"]["int:9"][0]["row"]["band"] = "b3"
    image_corrupt(
        "X11-image-unique-violation", "set row 9 band to b3, duplicating row 3 under unique by_band",
        payload, "unique_violation",
        "Digest valid; the unique index is violated at the image sequence.")
    payload = json.loads(json.dumps(image_payload))
    payload["horizon"] = payload["sequence"] + 1
    image_corrupt(
        "X12-image-horizon-ahead", "set horizon above sequence",
        payload, "invalid_header",
        "Digest valid; the retained interval claims a future sequence.")
    payload = json.loads(json.dumps(image_payload))
    payload["store"]["tables"]["ghost"] = {"int:1": [{"sequence": 2, "row": {"sample": 1}}]}
    image_corrupt(
        "X13-image-unknown-relation", "add a store table for a relation absent from the catalogue",
        payload, "unknown_relation",
        "Digest valid; store and catalogue disagree.")

    raw = bytearray(image9_bytes)
    marker = raw.find(b'"sha256":"')
    index = marker + len(b'"sha256":"')
    raw[index] = ord("0") if raw[index] != ord("0") else ord("1")
    digest_path = write_work("X08-image-digest-mismatch.lmi", bytes(raw))
    corpus.add_corrupt(
        "X08-image-digest-mismatch", "C9-suffix10", "flip one hexadecimal digit of the envelope digest",
        {"checkpoint": digest_path, "journal": journal10},
        {"stage": "format", "code": "digest_mismatch"},
        "Rejected before semantic parsing. Contrast X09..X13, which carry a correct digest.",
        engine=binary_open(binary, stage_dir("open-X08", digest_path, journal10)),
        engine_restore=binary_restore(binary, digest_path, work / "restore-X08"),
    )

    def journal_corrupt(cid, base_image, raw_bytes, expected, transform, notes,
                        base_sample="C9-suffix10"):
        path = write_work(cid + ".lmw", raw_bytes)
        corpus.add_corrupt(
            cid, base_sample, transform, {"checkpoint": base_image, "journal": path},
            expected, notes,
            engine=binary_open(binary, stage_dir("open-" + cid, base_image, path)),
        )
        return path

    header_crc = bytearray(journal10_bytes)
    header_crc[12] ^= 0xFF
    journal_corrupt(
        "X02-journal-header-crc", image9, bytes(header_crc),
        {"stage": "format", "code": "invalid_header"},
        "flip the journal header CRC32",
        "Header failure is fatal; it is not a recoverable tail.")
    header_magic = bytearray(journal10_bytes)
    header_magic[0:4] = b"LMW2"
    journal_corrupt(
        "X01-journal-magic", image9, bytes(header_magic),
        {"stage": "format", "code": "invalid_header"},
        "change the journal magic to LMW2",
        "Wrong container family; must not be read as a version-2 format.")

    frames = frames10
    frame5 = frames[4]
    frame_crc = bytearray(journal10_bytes)
    frame_crc[frame5["offset"] + 8 + 3] ^= 1
    journal_corrupt(
        "X03-frame-crc", image9, bytes(frame_crc),
        {"stage": "format", "code": "checksum_mismatch"},
        "flip a payload byte in frame 5 without updating the frame CRC32",
        "A complete frame with a bad checksum must be rejected, unlike the incomplete tail in J10-tail.")
    frame_zero = bytearray(journal10_bytes)
    frame_zero[frame5["offset"]:frame5["offset"] + 4] = b"\x00\x00\x00\x00"
    journal_corrupt(
        "X04-frame-length-zero", image9, bytes(frame_zero),
        {"stage": "format", "code": "invalid_length"},
        "set the length field of frame 5 to zero",
        "Zero length is explicitly invalid, not an empty record.")
    frame_big = bytearray(journal10_bytes)
    frame_big[frame5["offset"]:frame5["offset"] + 4] = (oracle.MAX_FRAME + 1).to_bytes(4, "big")
    journal_corrupt(
        "X05-frame-length-oversize", image9, bytes(frame_big),
        {"stage": "format", "code": "invalid_length"},
        "set the length field of frame 5 above the 16MiB frame limit",
        "Oversize length is rejected before allocation.")

    journal_corrupt(
        "X06-frame-sequence-gap", image9,
        rebuild_journal(journal10_raw, 5, lambda r: {**r, "sequence": 7}),
        {"stage": "format", "code": "sequence_or_format_mismatch"},
        "rewrite frame 5 sequence to 7 and recompute its CRC32",
        "Checksums are valid; ordering is not. Integrity does not imply sequence continuity. "
        "The product reports one combined sequence/format failure, so both X06 and X07 carry that class.")
    journal_corrupt(
        "X07-frame-format2", image9,
        rebuild_journal(journal10_raw, 5, lambda r: {**r, "format": 2}),
        {"stage": "format", "code": "sequence_or_format_mismatch"},
        "rewrite frame 5 format to 2 and recompute its CRC32",
        "A future record format is rejected rather than guessed. The product reports one combined "
        "sequence/format failure, so both X06 and X07 carry that class.")

    # Semantic frame corruption must replay, so pair with image 8 and mutate record 9.
    journal_corrupt(
        "X16-journal-vacuum-regression", image8,
        rebuild_journal(journal10_raw, 9, lambda r: {"format": 1, "sequence": 9,
                                                     "kind": "vacuum", "horizon": 3}),
        {"stage": "state", "code": "vacuum_regression"},
        "replace the record 9 commit with a vacuum at horizon 3, CRC32 recomputed",
        "Frame and checksum are valid; the retention watermark moves backwards. Paired with image 8 so "
        "record 9 is genuinely replayed rather than skipped.",
        base_sample="C8-prebackup")
    journal_corrupt(
        "X17-journal-primary-mismatch", image8,
        rebuild_journal(journal10_raw, 9, lambda r: {**r, "writes": [
            {**r["writes"][0], "row": {**r["writes"][0]["row"], "sample": 7}}]}),
        {"stage": "state", "code": "primary_mismatch"},
        "change the stored primary in record 9 to 7 while its key stays int:8, CRC32 recomputed",
        "Frame and checksum are valid; the record contradicts its own key. Paired with image 8 so record 9 "
        "is genuinely replayed.",
        base_sample="C8-prebackup")
    journal_corrupt(
        "X18-journal-unknown-relation", image8,
        rebuild_journal(journal10_raw, 9, lambda r: {**r, "writes": [
            {**r["writes"][0], "relation": "ghost"}]}),
        {"stage": "state", "code": "unknown_relation"},
        "point record 9 at relation ghost, CRC32 recomputed",
        "Frame and checksum are valid; the write references no catalogued relation. Paired with image 8.",
        base_sample="C8-prebackup")
    journal_corrupt(
        "X19-journal-unique-violation", image8,
        rebuild_journal(journal10_raw, 9, lambda r: {**r, "writes": [
            {**r["writes"][0], "row": {**r["writes"][0]["row"], "band": "b3"}}]}),
        {"stage": "state", "code": "unique_violation"},
        "set record 9 band to b3, duplicating row 3, CRC32 recomputed",
        "Frame and checksum are valid; replay ends in a unique violation. Paired with image 8.",
        base_sample="C8-prebackup")

    short_journal = write_work("X14-journal-short.lmw", journal8_raw)
    corpus.add_corrupt(
        "X14-checkpoint-ahead", "C9-suffix10",
        "pair image sequence 9 with only records 1..8",
        {"checkpoint": image9, "journal": short_journal},
        {"stage": "state", "code": "checkpoint_ahead"},
        "Each artifact is valid alone and restoring the image alone is legitimate; only the paired open is "
        "unsupportable, so a reader must validate the combination, not the files one at a time.",
        engine=binary_open(binary, stage_dir("open-X14", image9, short_journal)),
    )
    newer_journal = write_work("X15-journal-newer.lmw", build_journal(10, []))
    corpus.add_corrupt(
        "X15-journal-requires-newer-checkpoint", "C9-suffix10",
        "set the journal header base to 10 while the image is sequence 9",
        {"checkpoint": image9, "journal": newer_journal},
        {"stage": "state", "code": "journal_newer"},
        "The journal claims a newer base than the image; the pair must be refused, not guessed. "
        "Contrast R9-bare, where the same empty journal is legitimate because the restore installed it.",
        engine=binary_open(binary, stage_dir("open-X15", image9, newer_journal)),
    )

    # ---------------------------------------------------------------- cross assertions
    def desc(sample_id):
        return descriptions[sample_id]

    def identities(sample_id):
        return desc(sample_id)["retention"]["journal_commits"]

    cross = []

    def check(name, condition, detail):
        cross.append({"id": name, "holds": bool(condition), "detail": detail})

    check(
        "checkpoint_digest_is_not_history_identity",
        desc("C7-prefix")["image_sha256"] == desc("T7-twin")["image_sha256"]
        and desc("C7-prefix")["history_digest"] != desc("T7-twin")["history_digest"],
        "C7-prefix and T7-twin share checkpoint sha256 %s while their history digests differ."
        % (desc("C7-prefix")["image_sha256"],),
    )
    check(
        "same_image_different_commit_identities",
        identities("C7-prefix") != identities("T7-twin")
        and len(identities("C7-prefix")) == len(identities("T7-twin")),
        "Two independent runs of the same recipe expose different transaction ids at every commit sequence.",
    )
    check(
        "restore_preserves_rows_but_moves_change_floor",
        desc("C9-suffix10")["rows_digest"] == desc("R9-cont10")["rows_digest"]
        and desc("C9-suffix10")["snapshots"]["9"]["ok"] is True
        and desc("C9-suffix10")["snapshots"]["9"]["rows"] == desc("R9-bare")["snapshots"]["9"]["rows"]
        and desc("R9-bare")["stats"]["sequence"] == 9
        and desc("C9-suffix10")["retention"]["change_floor"] == 0
        and desc("R9-bare")["retention"]["change_floor"] == desc("R9-cont10")["retention"]["change_floor"] == 9
        and desc("C9-suffix10")["retention"]["version_floor"]
        == desc("R9-bare")["retention"]["version_floor"] == 5,
        "The source at sequence 9, the bare restore and the restored continuation agree on rows and "
        "version floor 5 while the change floor is 0 for the source and 9 for both restored instances.",
    )
    check(
        "restore_preserves_old_snapshots_but_not_old_cursors",
        desc("R9-bare")["snapshots"]["5"]["ok"] is True
        and desc("R9-bare")["snapshots"]["8"]["ok"] is True
        and desc("R9-bare")["changes"]["4"]["ok"] is False
        and desc("R9-bare")["changes"]["9"]["ok"] is True,
        "A restored instance still answers at=5 and at=8 while /changes?after=4 returns 410.",
    )
    check(
        "restore_same_sequence_different_identity",
        desc("C9-suffix10")["stats"]["sequence"] == desc("R9-cont10")["stats"]["sequence"] == 10
        and tx_ids["insert-10"]["transaction"] != restored_tx_id
        and any(item["sequence"] == 10 and item["transaction"] == restored_tx_id
                for item in identities("R9-cont10")),
        "Sequence 10 carries the source transaction id in C9-suffix10 and a different id in R9-cont10.",
    )
    check(
        "vacuum_retention_is_a_semantic_difference",
        desc("C9-suffix10")["rows_digest"] == desc("V0-novacuum")["rows_digest"]
        and desc("C9-suffix10")["stats"]["sequence"] == desc("V0-novacuum")["stats"]["sequence"]
        and desc("C9-suffix10")["stats"]["journal_records"]
        == desc("V0-novacuum")["stats"]["journal_records"]
        and desc("C9-suffix10")["retention"]["version_floor"] == 5
        and desc("V0-novacuum")["retention"]["version_floor"] == 0
        and desc("C9-suffix10")["snapshots"]["2"]["ok"] is False
        and desc("V0-novacuum")["snapshots"]["2"]["ok"] is True,
        "Identical sequence, record count and head rows; only the vacuum target moves the version floor "
        "from 0 to 5 and changes which old snapshots are answerable.",
    )
    check(
        "commit_grouping_is_a_semantic_difference",
        desc("G1-single-tx")["rows_digest"] == desc("G4-four-tx")["rows_digest"]
        and len(identities("G1-single-tx")) == 1
        and len(identities("G4-four-tx")) == 4
        and desc("G1-single-tx")["history_digest"] != desc("G4-four-tx")["history_digest"],
        "One transaction or four produce identical rows while commit identity and history differ.",
    )
    check(
        "tail_recovery_is_not_corruption",
        desc("J10-tail")["stats"]["recovered_tail_bytes"] == 3
        and desc("J10-tail")["stats"]["sequence"] == 10
        and desc("J10-tail")["history_digest"] == desc("J10-full")["history_digest"],
        "Three trailing bytes are truncated and state/history match J10-full.",
    )
    check(
        "checkpoint_prefix_pair_is_consistent",
        desc("C8-prebackup")["image_sequence"] == 8
        and desc("C8-prebackup")["stats"]["sequence"] == 8
        and desc("C7-prefix")["image_sequence"] == 7
        and desc("C7-prefix")["stats"]["sequence"] == 7,
        "Both prefix pairs replay to exactly their image sequence with no suffix.",
    )
    digest_valid_rejects = [entry for entry in corpus.corruptions if entry["id"][:3] in
                            ("X09", "X10", "X11", "X12", "X13")]
    check(
        "digest_valid_is_not_semantic_compatibility",
        len(digest_valid_rejects) == 5
        and all(entry["oracle"].get("stage") == "state" and not entry["oracle"]["accepted"]
                for entry in digest_valid_rejects),
        "X09..X13 all carry a recomputed valid digest and still fail state validation.",
    )
    check(
        "engine_matches_oracle_on_corrupt",
        all(entry["engine"] is None or entry["engine"]["accepted"] is False
            for entry in corpus.corruptions),
        "The real binary rejects every controlled corruption the oracle rejects.",
    )

    # --------------------------------------------------------------------- manifest
    blob_files = sorted(corpus.blob_dir.iterdir())
    blob_bytes = sum(path.stat().st_size for path in blob_files)
    referenced = sum(item["size"] for entry in corpus.samples + corpus.corruptions
                     for item in entry["artifacts"])
    manifest = {
        "schema": "lamina-compat-corpus/1",
        "generated_at": generated_at,
        "generator": {
            "tool": "compat/tools/corpusgen.py",
            "recipe_version": RECIPE_VERSION,
            "python": sys.version.split()[0],
            "go": go_version,
            "binary": str(binary),
            "binary_sha256": binary_digest,
        },
        "purpose": "Separate three claims about a new reader: it can recognize the bytes, it can recover the "
                   "relational state, and it can still serve the original history consumers. Backup restore "
                   "and change replay must jointly explain commit identity and retention watermarks; equal "
                   "directories or equal current queries do not imply continuous history.",
        "claim_model": {
            "bytes": "Parse the envelope and frame format, verify lengths, CRC32, SHA-256 and sequence "
                     "continuity; truncate only an incomplete tail and reject every complete corruption.",
            "state": "Reconstruct catalogue, index definitions, version chains, tombstones and logical "
                     "constraints at the head sequence.",
            "history": "Serve original history consumers: /changes pages carrying commit identities from the "
                       "change floor, and point-in-time reads at or above the version floor.",
            "important": "A sample can pass bytes and state while failing part of history: R9-bare keeps "
                         "snapshots but exposes no commit identities or change pages, and R9-cont10 adds "
                         "change records only from its restore base. A digest-valid image can fail state "
                         "(X09..X13). No single pass bit means semantic compatibility, and equal format "
                         "hashes do not.",
            "history_scope": {
                "commits_and_changes": "commit identities and change pages are available from the journal base",
                "snapshots_only": "point-in-time reads within the version floor work; no commit identity "
                                  "or change page for any earlier sequence is available",
                "snapshots_plus_new_changes": "point-in-time reads work and change records start at the "
                                              "restore base; earlier commit identities are unavailable",
            },
        },
        "retention_model": {
            "version_floor": "oldest at accepted by a point-in-time query; carried by the image horizon and "
                             "advanced only by a vacuum record",
            "change_floor": "earliest after cursor accepted by /changes; carried by the journal header base",
            "rule": "The floors are independent. A restore moves the change floor to the image sequence while "
                    "preserving the version floor, so old snapshots stay readable after old change cursors "
                    "are already gone. A reader that derives one floor from the other is wrong in both "
                    "directions.",
        },
        "source_recipe": {
            "steps": recipe,
            "transaction_ids": tx_ids,
            "restored_sequence_10_transaction": restored_tx_id,
            "restored_commit": restored_commit,
            "backup_equals_checkpoint": backup_matches_checkpoint,
            "backup_sequence": backup_sequence,
            "vacuum": vacuum,
        },
        "control_recipes": {
            "V0-novacuum": {
                "steps": novac_recipe,
                "transaction_ids": novac_tx,
                "vacuum": control_vacuum,
                "relation_to_source": "Same schema, transaction boundaries and row values as the source; "
                                      "the single differing operation is the vacuum target (0 instead of 5).",
            },
            "T7-twin": "The source recipe replayed independently in a fresh directory. Transaction ids are "
                       "random, so the checkpoint bytes match while journal identities do not.",
            "G1-single-tx/G4-four-tx": "Four distinct rows inserted either in one commit or four commits.",
        },
        "samples": corpus.samples,
        "corruptions": corpus.corruptions,
        "cross_checks": cross,
        "coverage": {
            "images_kept": [
                "sequence 7 pre-vacuum (horizon 0)",
                "sequence 8 post-vacuum (horizon 5)",
                "sequence 9 post-rewrite backup anchor (horizon 5)",
                "sequence 8 no-vacuum control (horizon 0)",
            ],
            "journals_kept": [
                "source records 1..10",
                "source prefix 1..8",
                "source prefix 1..7",
                "restored base 9 empty",
                "restored continuation record 10",
                "twin run records 1..7",
                "one commit versus four commits (four rows)",
                "tail-extended source records 1..10 + 3 bytes",
            ],
            "reconstructed_not_stored": "Prefixes are sliced from the source journal; empty restored journals "
                                        "are rebuilt as a 16-byte header. No prefix is stored twice.",
            "why_not_latest_only": "A single latest image erases the version floor transition, tombstone chains, "
                                   "the vacuum record and every commit identity. R9-cont10 would become "
                                   "indistinguishable from C9-suffix10 and X16-X19 could not replay.",
        },
        "cost": {
            "unique_blobs": len(blob_files),
            "unique_blob_bytes": blob_bytes,
            "artifact_bytes_referenced": referenced,
            "dedup_saved_bytes": referenced - blob_bytes,
            "alternatives_considered": {
                "every_anchor_checkpoint": "Storing a checkpoint at each of sequences 1..10 plus every journal "
                                           "prefix would be about 10 images and 10 journals; the four images "
                                           "and eight journals kept here preserve every discriminating "
                                           "difference at roughly half the bytes.",
                "latest_only": "One image plus one full journal is smaller but cannot represent the pre-vacuum "
                               "horizon, so the most important retention control disappears.",
            },
        },
        "rebuild": {
            "steps": [
                "go build -o bin/laminad ./cmd/laminad",
                "python3 -I compat/tools/corpusgen.py --binary bin/laminad --out compat",
                "python3 -I compat/tools/verify.py --corpus compat --binary bin/laminad",
            ],
            "determinism": "Transaction ids are random. Regeneration changes ids and therefore journal and "
                           "history digests, so only within-run cross-checks and semantic digests are stable. "
                           "The manifest records the generating binary digest and time.",
        },
    }
    manifest_path = corpus.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps({
        "manifest": str(manifest_path),
        "samples": len(corpus.samples),
        "corruptions": len(corpus.corruptions),
        "cross_checks": len(cross),
        "cross_checks_hold": all(item["holds"] for item in cross),
        "unique_blobs": len(blob_files),
        "unique_blob_bytes": blob_bytes,
        "work": str(work) if args.keep_work else None,
    }, indent=2))
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
