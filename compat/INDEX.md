# Compatibility corpus index

Generated 2026-11-18T07:31:40Z by `compat/tools/render_index.py` from `manifest.json`. Do not edit by hand; regenerate after `corpusgen.py`.

Claims column: `B` bytes, `S` state, `H` original history consumers. `B-S-H` means bytes and state are expected to pass while change replay is not supported by that instance.

## Purpose

Separate three claims about a new reader: it can recognize the bytes, it can recover the relational state, and it can still serve the original history consumers. Backup restore and change replay must jointly explain commit identity and retention watermarks; equal directories or equal current queries do not imply continuous history.

## Healthy samples

| id | claims | history scope | seq | version floor | change floor | image sha256 | head rows digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `J10-full` | BSH | `commits_and_changes` | 10 | 5 | 0 | `-` | `712dd137f70b12e8` |
| `C8-prebackup` | BSH | `commits_and_changes` | 8 | 5 | 0 | `2ebc03264dd5` | `494d353559716057` |
| `C7-prefix` | BSH | `commits_and_changes` | 7 | 0 | 0 | `b55359da7dd3` | `494d353559716057` |
| `C9-suffix10` | BSH | `commits_and_changes` | 10 | 5 | 0 | `089427617a79` | `712dd137f70b12e8` |
| `V0-novacuum` | BSH | `commits_and_changes` | 10 | 0 | 0 | `0109f521254f` | `712dd137f70b12e8` |
| `R9-bare` | BS- | `snapshots_only` | 9 | 5 | 9 | `089427617a79` | `b10c54a22a827a33` |
| `R9-cont10` | BS- | `snapshots_plus_new_changes` | 10 | 5 | 9 | `089427617a79` | `712dd137f70b12e8` |
| `T7-twin` | BSH | `commits_and_changes` | 7 | 0 | 0 | `b55359da7dd3` | `494d353559716057` |
| `G1-single-tx` | BSH | `commits_and_changes` | 2 | 0 | 0 | `-` | `fa524907fc91a447` |
| `G4-four-tx` | BSH | `commits_and_changes` | 5 | 0 | 0 | `-` | `fa524907fc91a447` |
| `J10-tail` | BSH | `commits_and_changes` | 10 | 5 | 0 | `-` | `712dd137f70b12e8` |

### What each healthy sample is for

**`J10-full`** — Journal only, base 0, ten complete frames. A reader must replay every record to recover state.

- Current rows alone hide tombstones, DDL and the vacuum record. Paired with X03/X04/X05/X06/X07/X16-X19 for complete-frame corruption.
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- history scope: `commits_and_changes`
- oracle: corpus/expected/J10-full.json

**`C8-prebackup`** — Checkpoint written immediately after vacuum, journal through record 8.

- A genuine on-disk pair from the source run: image sequence 8 and horizon 5.
- artifact `checkpoint` `checkpoint.lmi` 1093 bytes sha256 `3f677bc819af20a4257900ca0e4045c9dd5437fcaee7189dadd1bd12c0049fe4`
- artifact `journal` `journal.lmw` 1842 bytes sha256 `20f1b50cbf728f0def5e0cea9391c5e834540ba262051816d3d8c5c44a3e8e39`
- history scope: `commits_and_changes`
- oracle: corpus/expected/C8-prebackup.json

**`C7-prefix`** — Older pre-vacuum pair: image sequence 7 and journal through record 7.

- Version floor 0 and three extra readable snapshots versus C8-prebackup. Used with T7-twin to show that an image digest is not commit identity.
- artifact `checkpoint` `checkpoint.lmi` 1281 bytes sha256 `288a7c2592e83ae73d620e822ccc7943e8f2dc8d8947f8b0aa44154b63b68ab7`
- artifact `journal` `journal.lmw` 1781 bytes sha256 `5a8a2db32991d94b08e3df6260408c56230ba9a093ec31c71f256d5b33379e11`
- history scope: `commits_and_changes`
- oracle: corpus/expected/C7-prefix.json

**`C9-suffix10`** — Canonical checkpoint plus journal suffix: image sequence 9 with one frame beyond it.

- A real source directory at sequence 10. Backup at sequence 9 equals checkpoint9 byte for byte.
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- history scope: `commits_and_changes`
- oracle: corpus/expected/C9-suffix10.json

**`V0-novacuum`** — Same recipe, sequence and head rows as C9-suffix10, but the vacuum targets horizon 0 so the version floor stays 0.

- The strongest state-control pair: identical visible rows, sequence and record count, different retained interval. A reader that only compares current query output cannot tell them apart, and a reader that infers the change floor from the version floor gets both wrong.
- artifact `checkpoint` `checkpoint.lmi` 1281 bytes sha256 `f1c32f93fbf1f0500fb4bd3f99288941f6fdd950078f9305b40aef23c9300ec8`
- artifact `journal` `journal.lmw` 2246 bytes sha256 `a0db97e643bced3dce86ebc19b25d9af5a1375d2aa7885636650e5a19e39a27e`
- history scope: `commits_and_changes`
- oracle: corpus/expected/V0-novacuum.json

**`R9-bare`** — Restored backup before any new commit: image 9 with the empty journal the restore installs.

- State and snapshots 5..9 are intact, but the instance has no change records. Every cursor below 9 returns 410 even though those rows are still visible.
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 16 bytes sha256 `c2917f985792b627feb05abc5d4b2a6439c702fc6206e3a15283cfda7abc5d11`
- history scope: `snapshots_only`
- oracle: corpus/expected/R9-bare.json

**`R9-cont10`** — Restored backup plus one new commit: equal sequence and head rows to C9-suffix10, different change floor and different commit identity at sequence 10.

- Equal rows digest and equal state sequence, but change_floor 9 versus 0 and a different transaction id at sequence 10. Backup/restore and change replay must be explained jointly, not by current queries.
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 226 bytes sha256 `a910d312d406110dd3e97c38d34fd5582bcf69df7a2554dc02dd731543ce0a89`
- history scope: `snapshots_plus_new_changes`
- oracle: corpus/expected/R9-cont10.json

**`T7-twin`** — Independent twin run of the C7 recipe: identical checkpoint bytes, different transaction ids.

- Checkpoint sha256 equals C7-prefix exactly; journal history digest differs. Format identity does not identify history.
- artifact `checkpoint` `checkpoint.lmi` 1281 bytes sha256 `288a7c2592e83ae73d620e822ccc7943e8f2dc8d8947f8b0aa44154b63b68ab7`
- artifact `journal` `journal.lmw` 1781 bytes sha256 `c0edf1afb26726c42ada52c3cb5fbc66b2de2eee8aba50018cdfe15ef936c4da`
- history scope: `commits_and_changes`
- oracle: corpus/expected/T7-twin.json

**`G1-single-tx`** — Four rows written by one transaction: one commit identity.

- Head rows match G4-four-tx while commit grouping does not.
- artifact `journal` `journal.lmw` 826 bytes sha256 `1bdd4f4a662bd8135013332c1b8d4d84c05e9d72390f98c502a342788eadb2fb`
- history scope: `commits_and_changes`
- oracle: corpus/expected/G1-single-tx.json

**`G4-four-tx`** — The same four rows written by four transactions: four commit identities.

- Same rows digest as G1-single-tx, different sequence span and history digest.
- artifact `journal` `journal.lmw` 1153 bytes sha256 `b9404f020ba08d62aba428446f6726d0d9f5265c9bafdfe2a594fd26b8985401`
- history scope: `commits_and_changes`
- oracle: corpus/expected/G4-four-tx.json

**`J10-tail`** — Complete history plus a three-byte incomplete final frame. A reader must recover, not reject.

- recovered_tail_bytes 3 and state equal to J10-full. Contrast X03-frame-crc: a complete frame with a bad checksum is fatal.
- artifact `journal` `journal.lmw` 2261 bytes sha256 `13ad30be1751accdf528ef7ec544aeb4ab0d6f9eef57c4d31c8d823ebc6cafff`
- history scope: `commits_and_changes`
- oracle: corpus/expected/J10-tail.json

## Controlled corruptions

Every entry below is isolated under `corpus/blobs` and referenced only by its corruption id. None is paired with a healthy sample id at read time; the base is named separately.

| id | base | transform | expected stage | expected class | oracle | live binary |
| --- | --- | --- | --- | --- | --- | --- |
| `X09-image-primary-mismatch` | `C9-suffix10` | stored row says sample 7 while the chain key stays int:8 | state | `invalid_stored_tuple` | rejected | rejected, restore rejected |
| `X10-image-version-order` | `C9-suffix10` | swap the two version sequence numbers of key int:8 | state | `invalid_version_order` | rejected | rejected, restore rejected |
| `X11-image-unique-violation` | `C9-suffix10` | set row 9 band to b3, duplicating row 3 under unique by_band | state | `unique_violation` | rejected | rejected, restore rejected |
| `X12-image-horizon-ahead` | `C9-suffix10` | set horizon above sequence | state | `invalid_header` | rejected | rejected, restore rejected |
| `X13-image-unknown-relation` | `C9-suffix10` | add a store table for a relation absent from the catalogue | state | `unknown_relation` | rejected | rejected, restore rejected |
| `X08-image-digest-mismatch` | `C9-suffix10` | flip one hexadecimal digit of the envelope digest | format | `digest_mismatch` | rejected | rejected, restore rejected |
| `X02-journal-header-crc` | `C9-suffix10` | flip the journal header CRC32 | format | `invalid_header` | rejected | rejected |
| `X01-journal-magic` | `C9-suffix10` | change the journal magic to LMW2 | format | `invalid_header` | rejected | rejected |
| `X03-frame-crc` | `C9-suffix10` | flip a payload byte in frame 5 without updating the frame CRC32 | format | `checksum_mismatch` | rejected | rejected |
| `X04-frame-length-zero` | `C9-suffix10` | set the length field of frame 5 to zero | format | `invalid_length` | rejected | rejected |
| `X05-frame-length-oversize` | `C9-suffix10` | set the length field of frame 5 above the 16MiB frame limit | format | `invalid_length` | rejected | rejected |
| `X06-frame-sequence-gap` | `C9-suffix10` | rewrite frame 5 sequence to 7 and recompute its CRC32 | format | `sequence_or_format_mismatch` | rejected | rejected |
| `X07-frame-format2` | `C9-suffix10` | rewrite frame 5 format to 2 and recompute its CRC32 | format | `sequence_or_format_mismatch` | rejected | rejected |
| `X16-journal-vacuum-regression` | `C8-prebackup` | replace the record 9 commit with a vacuum at horizon 3, CRC32 recomputed | state | `vacuum_regression` | rejected | rejected |
| `X17-journal-primary-mismatch` | `C8-prebackup` | change the stored primary in record 9 to 7 while its key stays int:8, CRC32 recomputed | state | `primary_mismatch` | rejected | rejected |
| `X18-journal-unknown-relation` | `C8-prebackup` | point record 9 at relation ghost, CRC32 recomputed | state | `unknown_relation` | rejected | rejected |
| `X19-journal-unique-violation` | `C8-prebackup` | set record 9 band to b3, duplicating row 3, CRC32 recomputed | state | `unique_violation` | rejected | rejected |
| `X14-checkpoint-ahead` | `C9-suffix10` | pair image sequence 9 with only records 1..8 | state | `checkpoint_ahead` | rejected | rejected |
| `X15-journal-requires-newer-checkpoint` | `C9-suffix10` | set the journal header base to 10 while the image is sequence 9 | state | `journal_newer` | rejected | rejected |

### Notes per corruption

**`X09-image-primary-mismatch`** — Digest recomputed and valid; the image parses and is still semantically invalid.

- oracle: state stage, `invalid_stored_tuple`: invalid stored tuple
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `800155a4ad554510febde0769f34b202f8ec8343b2db30f5978fc4bc481d625e`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: invalid stored tuple"
- live restore: exit `1`, target created `False`, "invalid stored tuple"

**`X10-image-version-order`** — Digest valid; the version chain is not monotonic.

- oracle: state stage, `invalid_version_order`: invalid version order
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `90c219c785fd9ec6ee8b4452721f005936d8619342c0af2c8d701c3ba8f0f02c`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: invalid version order"
- live restore: exit `1`, target created `False`, "invalid version order"

**`X11-image-unique-violation`** — Digest valid; the unique index is violated at the image sequence.

- oracle: state stage, `unique_violation`: image violates unique index by_band
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `e7b3b5fa859cd0e0d3fb4417bbc771e69ec58732abc7d5a59e868e01f2ede5b2`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: image violates unique index by_band"
- live restore: exit `1`, target created `False`, "image violates unique index by_band"

**`X12-image-horizon-ahead`** — Digest valid; the retained interval claims a future sequence.

- oracle: state stage, `invalid_header`: invalid image header
- artifact `checkpoint` `checkpoint.lmi` 1169 bytes sha256 `5e4883855d3094361f7880c99208033d01bf2c630d6a91d26e205ec80c26e910`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: invalid image header"
- live restore: exit `1`, target created `False`, "invalid image header"

**`X13-image-unknown-relation`** — Digest valid; store and catalogue disagree.

- oracle: state stage, `unknown_relation`: image references unknown relation
- artifact `checkpoint` `checkpoint.lmi` 1222 bytes sha256 `4777bcddcc8b5d201d4785ecd67097ac88a2eaea12e3d78d142e758cbb55c27c`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: image references unknown relation"
- live restore: exit `1`, target created `False`, "image references unknown relation"

**`X08-image-digest-mismatch`** — Rejected before semantic parsing. Contrast X09..X13, which carry a correct digest.

- oracle: format stage, `digest_mismatch`: image format or digest mismatch
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `92873c54a674a174979bb4bc272367ac0ab5b55b20f92eebe27e688f837f2bde`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1b3dacb2487a1638a2ba733297a2cfa17a9f793271bb5b2675b7aec7af5cb2b9`
- live binary: exit `1` "checkpoint: image format or digest mismatch"
- live restore: exit `1`, target created `False`, "image format or digest mismatch"

**`X02-journal-header-crc`** — Header failure is fatal; it is not a recoverable tail.

- oracle: format stage, `invalid_header`: invalid journal header
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `21b8815df059156cf2d81e42bd745b44db20742ed6ab06e03077e7901853cf31`
- live binary: exit `1` "invalid journal header"

**`X01-journal-magic`** — Wrong container family; must not be read as a version-2 format.

- oracle: format stage, `invalid_header`: invalid journal header
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `311d7220d3ad21347b35599885b1b173351feb7aeeba7bbe1292f2ce833a66e5`
- live binary: exit `1` "invalid journal header"

**`X03-frame-crc`** — A complete frame with a bad checksum must be rejected, unlike the incomplete tail in J10-tail.

- oracle: format stage, `checksum_mismatch`: journal checksum mismatch at 1193
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `030b56a724045471d20f5fc4fbe92e78d3f1b27a1a8e8030838a697e365c79a3`
- live binary: exit `1` "journal checksum mismatch at 1193"

**`X04-frame-length-zero`** — Zero length is explicitly invalid, not an empty record.

- oracle: format stage, `invalid_length`: invalid journal length at 1193
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `1a23ede1d4479b0f652aaf8c932c1963b5eb668abcb922c46669a516db87303f`
- live binary: exit `1` "invalid journal length at 1193"

**`X05-frame-length-oversize`** — Oversize length is rejected before allocation.

- oracle: format stage, `invalid_length`: invalid journal length at 1193
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `4f2a97653c672c53eadecc977cff48a926505149d613b501cbb3f5a2eae2cd0d`
- live binary: exit `1` "invalid journal length at 1193"

**`X06-frame-sequence-gap`** — Checksums are valid; ordering is not. Integrity does not imply sequence continuity. The product reports one combined sequence/format failure, so both X06 and X07 carry that class.

- oracle: format stage, `sequence_or_format_mismatch`: journal sequence or format mismatch at 1193
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `0cf683d55286736c17c00cf6fb8b1a78d95986633436de0619270654e79ef159`
- live binary: exit `1` "journal sequence or format mismatch at 1193"

**`X07-frame-format2`** — A future record format is rejected rather than guessed. The product reports one combined sequence/format failure, so both X06 and X07 carry that class.

- oracle: format stage, `sequence_or_format_mismatch`: journal sequence or format mismatch at 1193
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `8e4cf86a4671cc07be859f83e809caa333216c99571cc4c1b934b638d8a01fa0`
- live binary: exit `1` "journal sequence or format mismatch at 1193"

**`X16-journal-vacuum-regression`** — Frame and checksum are valid; the retention watermark moves backwards. Paired with image 8 so record 9 is genuinely replayed rather than skipped.

- oracle: state stage, `vacuum_regression`: invalid vacuum horizon
- artifact `checkpoint` `checkpoint.lmi` 1093 bytes sha256 `3f677bc819af20a4257900ca0e4045c9dd5437fcaee7189dadd1bd12c0049fe4`
- artifact `journal` `journal.lmw` 2113 bytes sha256 `f0c76afc14aedb2f5434ab2d11be1b717bea78d549f1f1251c87bb7b63e28360`
- live binary: exit `1` "replay sequence 9: invalid vacuum horizon"

**`X17-journal-primary-mismatch`** — Frame and checksum are valid; the record contradicts its own key. Paired with image 8 so record 9 is genuinely replayed.

- oracle: state stage, `primary_mismatch`: stored primary mismatch
- artifact `checkpoint` `checkpoint.lmi` 1093 bytes sha256 `3f677bc819af20a4257900ca0e4045c9dd5437fcaee7189dadd1bd12c0049fe4`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `afa62c40fc19de88050e9af90c7206b8f0618dd7aa610aac00c738e591de4664`
- live binary: exit `1` "replay sequence 9: stored primary mismatch"

**`X18-journal-unknown-relation`** — Frame and checksum are valid; the write references no catalogued relation. Paired with image 8.

- oracle: state stage, `unknown_relation`: unknown relation in commit
- artifact `checkpoint` `checkpoint.lmi` 1093 bytes sha256 `3f677bc819af20a4257900ca0e4045c9dd5437fcaee7189dadd1bd12c0049fe4`
- artifact `journal` `journal.lmw` 2256 bytes sha256 `390b62aa5c8a29e1576ae65182f870229b3c7b0bbfb59f61f94d78bdba4a4fac`
- live binary: exit `1` "replay sequence 9: unknown relation ghost"

**`X19-journal-unique-violation`** — Frame and checksum are valid; replay ends in a unique violation. Paired with image 8.

- oracle: state stage, `unique_violation`: unique constraint by_band violated
- artifact `checkpoint` `checkpoint.lmi` 1093 bytes sha256 `3f677bc819af20a4257900ca0e4045c9dd5437fcaee7189dadd1bd12c0049fe4`
- artifact `journal` `journal.lmw` 2258 bytes sha256 `e10c57e73c856ccd8e89a4bd95736502aef87836997cb5c53434d60cd474a35f`
- live binary: exit `1` "recovered constraints: unique constraint by_band violated"

**`X14-checkpoint-ahead`** — Each artifact is valid alone and restoring the image alone is legitimate; only the paired open is unsupportable, so a reader must validate the combination, not the files one at a time.

- oracle: state stage, `checkpoint_ahead`: checkpoint ahead of journal
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 1842 bytes sha256 `20f1b50cbf728f0def5e0cea9391c5e834540ba262051816d3d8c5c44a3e8e39`
- live binary: exit `1` "checkpoint ahead of journal"

**`X15-journal-requires-newer-checkpoint`** — The journal claims a newer base than the image; the pair must be refused, not guessed. Contrast R9-bare, where the same empty journal is legitimate because the restore installed it.

- oracle: state stage, `journal_newer`: journal requires newer checkpoint
- artifact `checkpoint` `checkpoint.lmi` 1168 bytes sha256 `d60764df4c448beb3529ee73c64e436e664dde1f1be2647a7b50880636cd4d6d`
- artifact `journal` `journal.lmw` 16 bytes sha256 `e06d1c1255ee861ceb69daedccd129c5b534b3271f079bb5d3c9ab1478b58cfc`
- live binary: exit `1` "journal requires newer checkpoint"

## Cross claims and how they are checked

`verify.py` re-derives each of these from sample bytes. The manifest copy is compared to the derivation, so editing a claim without changing bytes fails.

| check | holds | detail |
| --- | --- | --- |
| `checkpoint_digest_is_not_history_identity` | yes | C7-prefix and T7-twin share checkpoint sha256 b55359da7dd3619afbbe815ad477544e8bc88cebe1877b0f005a75b1515d6cf6 while their history digests differ. |
| `same_image_different_commit_identities` | yes | Two independent runs of the same recipe expose different transaction ids at every commit sequence. |
| `restore_preserves_rows_but_moves_change_floor` | yes | The source at sequence 9, the bare restore and the restored continuation agree on rows and version floor 5 while the change floor is 0 for the source and 9 for both restored instances. |
| `restore_preserves_old_snapshots_but_not_old_cursors` | yes | A restored instance still answers at=5 and at=8 while /changes?after=4 returns 410. |
| `restore_same_sequence_different_identity` | yes | Sequence 10 carries the source transaction id in C9-suffix10 and a different id in R9-cont10. |
| `vacuum_retention_is_a_semantic_difference` | yes | Identical sequence, record count and head rows; only the vacuum target moves the version floor from 0 to 5 and changes which old snapshots are answerable. |
| `commit_grouping_is_a_semantic_difference` | yes | One transaction or four produce identical rows while commit identity and history differ. |
| `tail_recovery_is_not_corruption` | yes | Three trailing bytes are truncated and state/history match J10-full. |
| `checkpoint_prefix_pair_is_consistent` | yes | Both prefix pairs replay to exactly their image sequence with no suffix. |
| `digest_valid_is_not_semantic_compatibility` | yes | X09..X13 all carry a recomputed valid digest and still fail state validation. |
| `engine_matches_oracle_on_corrupt` | yes | The real binary rejects every controlled corruption the oracle rejects. |

## Retention model

- version floor: oldest at accepted by a point-in-time query; carried by the image horizon and advanced only by a vacuum record
- change floor: earliest after cursor accepted by /changes; carried by the journal header base
- rule: The floors are independent. A restore moves the change floor to the image sequence while preserving the version floor, so old snapshots stay readable after old change cursors are already gone. A reader that derives one floor from the other is wrong in both directions.

## Cost accounting

- unique blobs: 32
- unique blob bytes: 50983
- bytes referenced without dedup: 87132
- saved by dedup: 36149
- considered `every_anchor_checkpoint`: Storing a checkpoint at each of sequences 1..10 plus every journal prefix would be about 10 images and 10 journals; the four images and eight journals kept here preserve every discriminating difference at roughly half the bytes.
- considered `latest_only`: One image plus one full journal is smaller but cannot represent the pre-vacuum horizon, so the most important retention control disappears.

Why not the latest file only: A single latest image erases the version floor transition, tombstone chains, the vacuum record and every commit identity. R9-cont10 would become indistinguishable from C9-suffix10 and X16-X19 could not replay.

## Rebuild and verify

```sh
go build -o bin/laminad ./cmd/laminad
python3 -I compat/tools/corpusgen.py --binary bin/laminad --out compat
python3 -I compat/tools/verify.py --corpus compat --binary bin/laminad
```

Transaction ids are random. Regeneration changes ids and therefore journal and history digests, so only within-run cross-checks and semantic digests are stable. The manifest records the generating binary digest and time.

## Known limits

- The corpus exercises one relation, one schema and sequences 0..12. It is not a fuzzing campaign or a claim about unrelated workloads.
- The oracle is a second implementation of the published format and semantics. Agreement with the binary on these samples is evidence, not proof of equivalence.
- The live layer runs against the binary recorded as `3ce6ef4efc66c61a`. A different binary must be verified separately.
- Transaction ids are random, so only within-run relations and semantic digests are stable across regeneration.
