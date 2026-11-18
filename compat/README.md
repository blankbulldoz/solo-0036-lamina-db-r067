# LaminaDB compatibility corpus

This directory is a curated, traceable set of on-disk samples for judging a new reader of
LaminaDB data. It separates three claims that are routinely conflated:

| claim | question | passes when |
| --- | --- | --- |
| **bytes** (`B`) | Can the reader parse the container? | framing, lengths, CRC32, SHA-256, magic and sequence shape are understood; complete corruption is refused and only an incomplete tail is truncated |
| **state** (`S`) | Can it recover the relation state? | catalogue, index definitions, version chains, tombstones, unique constraints and the head snapshot are reproduced |
| **history** (`H`) | Can it still serve the original history consumers? | `/changes` pages carry the same records and commit identities from the change floor, and point-in-time reads answer the same snapshots as the version floor allows |

History is not one bit. Each sample records a `history_scope`:

* `commits_and_changes` — commit identities and change pages are available from the
  journal base.
* `snapshots_only` — point-in-time reads inside the version floor still work, but no
  earlier commit identity or change page exists anywhere in the instance.
* `snapshots_plus_new_changes` — point-in-time reads work and change records start at the
  restore base; everything before it is unavailable.

A reader can honestly hold **B+S** while failing **H**. That is exactly what a restored
backup is: current rows and old snapshots are intact, but source change history is gone.
Conversely a digest-valid image can fail **S**; checksum agreement is not semantic
compatibility.

The corpus deliberately stores samples that make each distinction observable, plus
controlled corruptions kept in a separate namespace.

## Layout

```
compat/
  INDEX.md                 generated human index of every sample and corruption
  manifest.json            traceability: blobs, sizes, digests, floors, expectations, checks
  corpus/blobs/<sha256>    content-addressed artifacts (checkpoint.lmi / journal.lmw bytes)
  corpus/expected/<id>.json  per-sample oracle description plus the service's live answers
  reports/summary.json     compact machine-readable digest of the same facts
  tools/lamina_oracle.py   independent format/state/history model (stdlib only)
  tools/corpusgen.py       regenerates everything from a live binary
  tools/verify.py          independent verification, optionally against a live binary
  tools/render_index.py    renders INDEX.md and reports/summary.json
```

Blobs are addressed by the SHA-256 of their **file bytes**. The image envelope also carries
its own payload digest; both are checked. Nothing here is stored by sample name, so two
samples that share a checkpoint share one blob.

## The discriminating samples

Sequence numbers below are commit/record sequence numbers, not wall-clock time. The
scenario records four rows, overwrites one, deletes one, inserts two, builds a
non-unique and a unique index, vacuums, rewrites one row, and finally inserts one row.

| sample | what it proves |
| --- | --- |
| `J10-full` | Journal only, base 0. State cannot be recovered without replaying every frame; rows alone hide deletes and DDL. |
| `C8-prebackup` | A genuine checkpoint (sequence 8, version floor 5) plus its journal prefix. |
| `C7-prefix` | Pre-vacuum checkpoint (sequence 7, version floor 0) plus prefix. Shows a different retained interval at a different sequence. |
| `C9-suffix10` | Source directory at sequence 10: image 9 and one frame beyond it. The change floor is 0. |
| `V0-novacuum` | Same recipe, sequence, record count and **head rows** as `C9-suffix10`, but the vacuum targets horizon 0. Version floor 0 versus 5. |
| `R9-bare` | `GET /backup` at sequence 9 restored into a fresh directory. Same image bytes as `C9-suffix10`; journal is the empty base-9 header. |
| `R9-cont10` | `R9-bare` after one new commit. Sequence and head rows equal `C9-suffix10`; change floor 9 versus 0. |
| `T7-twin` | An independent run of the `C7-prefix` recipe. Checkpoint bytes are **identical**, transaction ids are not. |
| `G1-single-tx` / `G4-four-tx` | The same four rows written in one commit versus four. Identical rows, different commit identities. |
| `J10-tail` | Complete history plus three stray bytes. Must recover with `recovered_tail_bytes = 3`, not reject. |

The most important pair is `C9-suffix10` versus `R9-cont10`. They have the same head row
digest and the same sequence, yet:

* the change floor differs (0 vs 9), so old cursors are answerable in one and `410` in the
  other;
* the commit at sequence 10 has a different transaction id;
* both can still answer point-in-time queries down to version floor 5.

This is why backup/restore and change replay must be explained **jointly**. A reader that
only compares the current directory or the current query output will call these
equivalent and be wrong.

`C7-prefix` versus `T7-twin` makes the other point: the checkpoint file and its SHA-256
are identical across independent runs. Format identity cannot stand in for history
identity; only the journal carries transaction identity.

## Controlled corruptions

Kept under the same blob store but a distinct namespace (`X..`). They are never mixed into
the healthy list. Every one is derived from captured bytes and every expected failure is
recorded. Classes covered:

* **Image envelope**: digest mismatch (`X08`); valid digest but invalid state —
  primary/key mismatch (`X09`), version order (`X10`), unique violation (`X11`), horizon
  ahead of sequence (`X12`), unknown relation in store (`X13`).
* **Journal container**: magic (`X01`), header CRC (`X02`), frame CRC (`X03`), zero length
  (`X04`), oversize length (`X05`), sequence gap (`X06`), record format 2 (`X07`).
* **Journal semantics**: vacuum horizon regression (`X16`), primary mismatch (`X17`),
  unknown relation (`X18`), unique violation (`X19`). These four pair with the sequence-8
  checkpoint so record 9 is genuinely replayed; the replay is what exposes the fault.
* **Combination**: image sequence 9 with only records 1..8 (`X14`), journal base newer than
  the image (`X15`). Each artifact is individually valid; only the pair is unsupportable.

`X08` versus `X09`..`X13` is the checksum-versus-semantics split: `X08` is caught by the
digest, the rest carry a correctly recomputed digest and must still be refused.
`J10-tail` versus `X03` is the truncation-versus-corruption split: an incomplete tail is
recovered, a complete frame with a bad checksum is fatal.

## Retention model recorded by the corpus

* **Version floor** — the oldest `at` a point-in-time query accepts. It is carried in the
  image `horizon` and advanced only by a vacuum record. Reclaiming a version below it is
  what makes an old snapshot unanswerable (`410`).
* **Change floor** — the earliest `after` cursor `/changes` accepts. It is the journal
  header base. A restore installs an empty journal at the image sequence, so the change
  floor jumps forward even though the version floor does not.

The floors are independent. `R9-bare` answers `at=5` and `at=8` while refusing every
`after` below 9. A reader that derives one floor from the other is wrong in both
directions: assuming the change floor equals the version floor would wrongly reject
snapshots 5..8, and assuming the version floor survives a restore's change floor would
wrongly claim old change cursors still work.

## Traceability

`manifest.json` records, for each sample:

* the exact blobs (SHA-256 of file bytes, size) and their roles;
* image sequence, version floor, change floor and journal base;
* head-row digest and history digest computed over decoded semantics, not file bytes;
* commit identities visible from the journal;
* per-anchor expectations (snapshots and change pages) with a SHA-256 of the expectation
  file.

`reports/summary.json` is the same facts in a compact form for tooling.

Note that `history_digest` and `rows_digest` are **semantic** digests over canonical JSON.
File-level SHA-256 appears separately as `image_sha256` and `journal_sha256`. The corpus
never uses a matching format hash as evidence of compatibility.

## Rebuild

```sh
go build -o bin/laminad ./cmd/laminad
python3 -I compat/tools/corpusgen.py --binary bin/laminad --out compat
python3 -I compat/tools/verify.py --corpus compat --binary bin/laminad
python3 -I compat/tools/render_index.py --corpus compat
```

`corpusgen.py` drives the real HTTP service, captures what it writes, records the service's
own answers, and derives corruptions from those bytes. It refuses to finish if any cross
claim does not hold. `verify.py` re-derives everything with the independent oracle, checks
blob integrity, then starts a real server on a scratch copy of each healthy sample and
compares answers; corrupt samples must be refused in the same class. Both scripts use only
the Python standard library and are intended to be run with `-I` so a stray module in the
input directory cannot shadow the standard library.

Regeneration is not byte-reproducible: transaction ids are random, so journal bytes and
history digests change. Within-run cross claims and semantic digests are stable. The
manifest records the generating binary digest and timestamp.

## Cost and why not a single latest file

Four images (sequences 7, 8, 9 and a no-vacuum control), eight journals (including
reconstructed prefixes and twin/grouping/control runs) and one 3-byte tail extension cover
every discriminating difference. Prefixes are sliced from the source journal and empty
restored journals are rebuilt from a 16-byte header, so no prefix is stored twice.

A single latest image would be smaller but would erase the retention transition: it cannot
represent the pre-vacuum version floor, so `C7-prefix`, `V0-novacuum` and the restore pair
could not be told apart. Storing a checkpoint at every sequence would be roughly twice the
bytes for no extra discrimination. The current choice is the recovery-coverage/size
trade-off, and the manifest records both alternatives and their consequences.

## Known limits

* One relation, one schema, sequences 0..12. This is a curated matrix, not a fuzzer.
* The oracle is a second implementation of the published contract. Agreement on these
  samples is evidence, not proof of behavioural equivalence.
* Live verification is bound to the binary digest recorded in the manifest; a different
  binary needs its own run.
* The corpus does not exercise multi-relation DDL ordering, text values near the 64 KiB
  limit, or request-budget boundaries. Those are adjacent risks, not covered claims.
