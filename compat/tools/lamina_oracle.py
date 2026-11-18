"""Independent reader/model for LaminaDB on-disk artifacts.

This module is written against the published format contract (README/PROJECT_SPEC):
  * journal.lmw: 16-byte "LMW1" header (base sequence + CRC32) followed by frames of
    u32 big-endian length, u32 big-endian CRC32(payload), and a JSON record {format:1,...}.
  * checkpoint.lmi / backup: JSON envelope {format:1, sha256, payload} where payload is
    the JSON image {format, sequence, horizon, catalog, store}; the digest covers the raw
    payload bytes exactly as embedded.
  * version chains: per relation key, append-only records with commit sequence; a row is
    visible at sequence `at` iff the last version with sequence <= at is not a tombstone.
  * vacuum(horizon): keep the last version at or before the horizon plus everything newer;
    fully remove a chain whose newest version is a tombstone at or before the horizon.
  * changes(after, limit): only journal frames are reported (image contents are not),
    history starts at the journal header base, and a cursor below that base is unavailable.

It deliberately does not import or execute LaminaDB code. It is a second, independent
implementation used as the query basis for the compatibility corpus. Where the published
contract is silent (for example JSON key ordering inside a record) the oracle compares
decoded structures rather than raw record bytes.
"""

import hashlib
import json
import zlib

MAX_FRAME = 16 << 20
MAX_IMAGE = 128 << 20
IDENTIFIER_MIN = 1


class FormatError(Exception):
    """The bytes do not follow the documented frame/envelope format."""

    def __init__(self, message):
        super().__init__(message)
        self.code = _code_for(message)


class StateError(Exception):
    """The bytes parse, but the represented state is not valid."""

    def __init__(self, message):
        super().__init__(message)
        self.code = _code_for(message)


# Failure classes exercised by the corpus. Codes are corpus vocabulary, not a product
# contract; the engine reports free-form messages.
_CODE_TABLE = (
    ("journal header", "invalid_header"),
    ("invalid journal header", "invalid_header"),
    ("invalid journal length", "invalid_length"),
    ("journal checksum mismatch", "checksum_mismatch"),
    ("journal sequence or format mismatch", "sequence_or_format_mismatch"),
    ("journal json", "invalid_json"),
    ("image format or digest mismatch", "digest_mismatch"),
    ("invalid image header", "invalid_header"),
    ("invalid stored tuple", "invalid_stored_tuple"),
    ("invalid version order", "invalid_version_order"),
    ("image violates unique index", "unique_violation"),
    ("image references unknown relation", "unknown_relation"),
    ("invalid vacuum horizon", "vacuum_regression"),
    ("stored primary mismatch", "primary_mismatch"),
    ("unknown relation", "unknown_relation"),
    ("unique constraint", "unique_violation"),
    ("checkpoint ahead of journal", "checkpoint_ahead"),
    ("journal requires newer checkpoint", "journal_newer"),
    ("checkpoint without journal", "checkpoint_without_journal"),
    ("no artifacts", "no_artifacts"),
)


def _code_for(message):
    for prefix, code in _CODE_TABLE:
        if message.startswith(prefix):
            return code
    return "unclassified"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(value):
    return sha256_hex(canonical_json(value).encode("utf-8"))


# --------------------------------------------------------------------------------------
# scalar values


def value_kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "text"
    raise StateError("unsupported scalar value %r" % (value,))


def value_key(value):
    kind = value_kind(value)
    return kind + ":" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def order_value(value):
    kind = value_kind(value)
    if kind == "int":
        return (0, value)
    if kind == "text":
        return (1, value)
    if kind == "bool":
        return (2, 1 if value else 0)
    return (-1, 0)


def valid_name(name):
    if not isinstance(name, str) or not name:
        return False
    if not ("a" <= name[0] <= "z"):
        return False
    if len(name) > 63:
        return False
    return all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "_" for c in name)


# --------------------------------------------------------------------------------------
# image envelope


def extract_payload(raw):
    """Return the exact raw bytes of the top-level "payload" value."""
    if len(raw) > MAX_IMAGE:
        raise FormatError("image exceeds limit")
    key = b'"payload"'
    start = raw.find(key)
    if start < 0:
        raise FormatError("image envelope has no payload")
    colon = raw.find(b":", start + len(key))
    if colon < 0:
        raise FormatError("image envelope payload is unterminated")
    value_start = colon + 1
    while value_start < len(raw) and raw[value_start : value_start + 1] in b" \t\r\n":
        value_start += 1
    if raw[value_start : value_start + 1] not in (b"{", b"["):
        raise FormatError("image payload is not a JSON object")
    index = value_start
    depth = 0
    in_string = False
    escaped = False
    while index < len(raw):
        c = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif c == 0x5C:
                escaped = True
            elif c == 0x22:
                in_string = False
        else:
            if c == 0x22:
                in_string = True
            elif c in (0x7B, 0x5B):
                depth += 1
            elif c in (0x7D, 0x5D):
                depth -= 1
                if depth == 0:
                    return raw[value_start:index + 1]
        index += 1
    raise FormatError("image payload is unterminated")


def decode_image(raw):
    """Decode and validate an image envelope. Returns (payload, info)."""
    if len(raw) > MAX_IMAGE:
        raise FormatError("image exceeds limit")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FormatError("image envelope is not JSON: %s" % exc)
    if not isinstance(envelope, dict):
        raise FormatError("image envelope is not an object")
    payload_raw = extract_payload(raw)
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise FormatError("image payload is not JSON: %s" % exc)
    digest = hashlib.sha256(payload_raw).hexdigest()
    if envelope.get("format") != 1 or envelope.get("sha256") != digest:
        raise FormatError("image format or digest mismatch")
    validate_image(payload)
    return payload, {
        "sha256": hashcode(envelope.get("sha256")),
        "payload_sha256": digest,
        "payload_bytes": len(payload_raw),
    }


def hashcode(value):
    return value


def make_image(payload):
    """Build an envelope whose digest matches a freshly serialized payload."""
    body = canonical_json(payload).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return b'{"format":1,"payload":' + body + b',"sha256":"' + digest.encode("ascii") + b'"}'


def validate_image(image):
    if not isinstance(image, dict):
        raise StateError("invalid image header")
    if (
        image.get("format") != 1
        or not isinstance(image.get("sequence"), int)
        or not isinstance(image.get("horizon"), int)
        or image["horizon"] > image["sequence"]
        or not isinstance(image.get("catalog"), dict)
        or not isinstance(image.get("store"), dict)
        or not isinstance(image["catalog"].get("relations"), dict)
        or not isinstance(image["catalog"].get("indexes"), dict)
        or not isinstance(image["store"].get("tables"), dict)
    ):
        raise StateError("invalid image header")
    sequence = image["sequence"]
    relations = image["catalog"]["relations"]
    indexes = image["catalog"]["indexes"]
    tables = image["store"]["tables"]
    for name, relation in relations.items():
        if name != relation.get("name") or not isinstance(relation.get("created"), int):
            raise StateError("invalid catalog sequence")
        if relation["created"] == 0 or relation["created"] > sequence:
            raise StateError("invalid catalog sequence")
        validate_relation(relation)
    for name, definition in indexes.items():
        relation = relations.get(definition.get("relation"))
        if relation is None:
            raise StateError("invalid index metadata")
        if name != definition.get("name") or not valid_name(name):
            raise StateError("invalid index metadata")
        if (
            not isinstance(definition.get("created"), int)
            or definition["created"] < relation["created"]
            or definition["created"] > sequence
        ):
            raise StateError("invalid index metadata")
        if definition.get("column") not in {c["name"] for c in relation["columns"]}:
            raise StateError("invalid index column")
    for name, rows in tables.items():
        relation = relations.get(name)
        if relation is None:
            raise StateError("image references unknown relation")
        for key, chain in rows.items():
            if not isinstance(chain, list) or not chain:
                raise StateError("empty version chain")
            last = 0
            for version in chain:
                seq = version.get("sequence")
                if not isinstance(seq, int) or seq <= last or seq > sequence or seq <= relation["created"]:
                    raise StateError("invalid version order")
                last = seq
                if not version.get("deleted", False):
                    row = normalize_row(relation, version.get("row"))
                    if key_of(relation, row) != key:
                        raise StateError("invalid stored tuple")
    for definition in indexes.values():
        if not definition.get("unique"):
            continue
        seen = set()
        for chain in tables.get(definition["relation"], {}).values():
            version = visible_version(chain, sequence)
            if version is None:
                continue
            value = version.get("row", {}).get(definition["column"])
            if value is None:
                continue
            token = value_key(value)
            if token in seen:
                raise StateError("image violates unique index %s" % definition["name"])
            seen.add(token)


def validate_relation(relation):
    if not valid_name(relation.get("name")) or not valid_name(relation.get("primary")):
        raise StateError("invalid relation or primary name")
    columns = relation.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 64:
        raise StateError("relation needs 1..64 columns")
    seen = set()
    found = False
    for column in columns:
        name = column.get("name")
        if not valid_name(name) or name in seen:
            raise StateError("invalid or duplicate column %r" % (name,))
        seen.add(name)
        if column.get("type") not in ("int", "text", "bool"):
            raise StateError("unsupported type %r" % (column.get("type"),))
        if name == relation.get("primary"):
            found = True
            if column.get("nullable") or column.get("type") == "bool":
                raise StateError("primary must be non-null int or text")
    if not found:
        raise StateError("primary column does not exist")


def normalize_row(relation, row):
    if not isinstance(row, dict):
        raise StateError("invalid stored tuple")
    columns = relation["columns"]
    names = {c["name"] for c in columns}
    for name in row:
        if name not in names:
            raise StateError("invalid stored tuple")
    out = {}
    for column in columns:
        name = column["name"]
        value = row.get(name)
        if value is None:
            if not column.get("nullable"):
                raise StateError("invalid stored tuple")
            out[name] = None
            continue
        if value_kind(value) != column["type"]:
            raise StateError("invalid stored tuple")
        if column["type"] == "text" and len(value.encode("utf-8")) > 65536:
            raise StateError("invalid stored tuple")
        out[name] = value
    return out


def key_of(relation, row):
    return value_key(row[relation["primary"]])


# --------------------------------------------------------------------------------------
# journal


def parse_journal(raw):
    """Parse a journal file. Raises FormatError on complete-frame corruption.

    Returns base, frames, records, recovered_tail, effective_length. An incomplete final
    frame is reported in recovered_tail and is excluded from `records` (the server
    truncates it on open).
    """
    if len(raw) < 16:
        raise FormatError("journal header: unexpected EOF")
    if raw[:4] != b"LMW1":
        raise FormatError("invalid journal header")
    if crc32(raw[:12]) != int.from_bytes(raw[12:16], "big"):
        raise FormatError("invalid journal header")
    base = int.from_bytes(raw[4:12], "big")
    offset = 16
    last = base
    frames = []
    recovered_tail = 0
    while True:
        head = raw[offset : offset + 8]
        if len(head) == 0:
            break
        if len(head) < 8:
            recovered_tail = len(head)
            break
        length = int.from_bytes(head[:4], "big")
        stored_crc = int.from_bytes(head[4:8], "big")
        if length == 0 or length > MAX_FRAME:
            raise FormatError("invalid journal length at %d" % offset)
        payload = raw[offset + 8 : offset + 8 + length]
        if len(payload) < length:
            recovered_tail = 8 + len(payload)
            break
        if crc32(payload) != stored_crc:
            raise FormatError("journal checksum mismatch at %d" % offset)
        try:
            record = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FormatError("journal json at %d: %s" % (offset, exc))
        if record.get("format") != 1 or record.get("sequence") != last + 1:
            raise FormatError("journal sequence or format mismatch at %d" % offset)
        frames.append(
            {
                "offset": offset,
                "end": offset + 8 + length,
                "length": length,
                "crc32": stored_crc,
                "payload": payload,
                "record": record,
            }
        )
        last = record["sequence"]
        offset += 8 + length
    records = [frame["record"] for frame in frames]
    return {
        "base": base,
        "frames": frames,
        "records": records,
        "recovered_tail": recovered_tail,
        "effective_length": offset,
    }


# --------------------------------------------------------------------------------------
# state replay


def empty_state():
    return {
        "sequence": 0,
        "horizon": 0,
        "catalog": {"relations": {}, "indexes": {}},
        "tables": {},
    }


def state_from_image(image):
    # deep copy through JSON so later replay cannot mutate the decoded image
    copied = json.loads(canonical_json(image))
    return {
        "sequence": copied["sequence"],
        "horizon": copied["horizon"],
        "catalog": copied["catalog"],
        "tables": copied["store"]["tables"],
    }


def visible_version(chain, at):
    lo, hi = 0, len(chain)
    while lo < hi:
        mid = (lo + hi) // 2
        if chain[mid]["sequence"] <= at:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    return chain[lo - 1]


def visible_rows(state, relation_name, at):
    rows = {}
    for key, chain in state["tables"].get(relation_name, {}).items():
        version = visible_version(chain, at)
        if version is not None and not version.get("deleted", False):
            rows[key] = version["row"]
    return rows


def apply_record(state, record):
    if record.get("sequence") != state["sequence"] + 1 or record.get("format") != 1:
        raise StateError("invalid record sequence")
    kind = record.get("kind")
    seq = record["sequence"]
    if kind == "relation":
        relation = record.get("relation")
        if not isinstance(relation, dict) or relation.get("created") != seq:
            raise StateError("invalid relation record")
        validate_relation(relation)
        name = relation["name"]
        if name in state["catalog"]["relations"]:
            raise StateError("duplicate relation")
        state["catalog"]["relations"][name] = relation
        state["tables"].setdefault(name, {})
    elif kind == "index":
        definition = record.get("index")
        if not isinstance(definition, dict) or definition.get("created") != seq:
            raise StateError("invalid index record")
        check_index(state["catalog"], definition)
        state["catalog"]["indexes"][definition["name"]] = definition
    elif kind == "commit":
        seen = set()
        for mutation in record.get("writes", []):
            relation = state["catalog"]["relations"].get(mutation.get("relation"))
            if relation is None:
                raise StateError("unknown relation in commit")
            token = (mutation.get("relation"), mutation.get("key"))
            if token in seen:
                raise StateError("duplicate mutation")
            seen.add(token)
            if not mutation.get("deleted", False):
                row = normalize_row(relation, mutation.get("row"))
                if key_of(relation, row) != mutation.get("key"):
                    raise StateError("stored primary mismatch")
        for mutation in record.get("writes", []):
            table = state["tables"].setdefault(mutation["relation"], {})
            table.setdefault(mutation["key"], []).append(
                {
                    "sequence": seq,
                    "row": mutation.get("row"),
                    "deleted": bool(mutation.get("deleted", False)),
                }
            )
    elif kind == "vacuum":
        horizon = record.get("horizon")
        if not isinstance(horizon, int) or horizon < state["horizon"] or horizon > seq:
            raise StateError("invalid vacuum horizon")
        vacuum_store(state["tables"], horizon)
        state["horizon"] = horizon
    else:
        raise StateError("unknown record kind %s" % kind)
    state["sequence"] = seq


def check_index(catalog, definition):
    name = definition.get("name")
    if not valid_name(name):
        raise StateError("invalid index name")
    if name in catalog["indexes"]:
        raise StateError("index already exists")
    relation = catalog["relations"].get(definition.get("relation"))
    if relation is None:
        raise StateError("unknown relation")
    if definition.get("column") not in {c["name"] for c in relation["columns"]}:
        raise StateError("unknown index column")


def vacuum_store(tables, horizon):
    removed = 0
    retained = 0
    for rows in tables.values():
        for key in list(rows.keys()):
            chain = rows[key]
            keep = 0
            while keep + 1 < len(chain) and chain[keep + 1]["sequence"] <= horizon:
                keep += 1
            if chain and chain[-1]["sequence"] <= horizon and chain[-1].get("deleted", False):
                removed += len(chain)
                del rows[key]
                continue
            removed += keep
            remaining = chain[keep:]
            rows[key] = remaining
            retained += len(remaining)
    return {"removed": removed, "retained": retained}


def check_unique_all(state):
    for definition in state["catalog"]["indexes"].values():
        if not definition.get("unique"):
            continue
        seen = {}
        for key, chain in state["tables"].get(definition["relation"], {}).items():
            version = visible_version(chain, state["sequence"])
            if version is None or version.get("deleted", False):
                continue
            value = version.get("row", {}).get(definition["column"])
            if value is None:
                continue
            token = value_key(value)
            if token in seen and seen[token] != key:
                raise StateError("unique constraint %s violated" % definition["name"])
            seen[token] = key


def open_state(image_bytes, journal_bytes, journal_base_override=None):
    """Model engine.Open: load an optional image, then replay journal frames beyond it.

    `journal_base_override` models a restored instance whose journal starts at the image
    sequence with no source history; a plain checkpoint+journal directory keeps base 0.
    """
    image = None
    if image_bytes is not None:
        image, _info = decode_image(image_bytes)
    if journal_bytes is None:
        if journal_base_override is None:
            if image is not None:
                raise StateError("checkpoint without journal; use validated restore")
            raise StateError("no artifacts")
        journal = {
            "base": journal_base_override,
            "records": [],
            "frames": [],
            "recovered_tail": 0,
            "effective_length": 0,
        }
    else:
        journal = parse_journal(journal_bytes)
    if image is not None and journal["base"] > image["sequence"]:
        raise StateError("journal requires newer checkpoint")
    if image is None and journal_bytes is None:
        raise StateError("no artifacts")
    state = state_from_image(image) if image else empty_state()
    last = journal["base"] + len(journal["records"])
    if state["sequence"] > last:
        raise StateError("checkpoint ahead of journal")
    for record in journal["records"]:
        if record["sequence"] <= state["sequence"]:
            continue
        apply_record(state, record)
    check_unique_all(state)
    state["journal"] = journal
    return state, journal


# --------------------------------------------------------------------------------------
# observable queries


def stats(state, journal):
    keys = 0
    versions = 0
    for rows in state["tables"].values():
        keys += len(rows)
        for chain in rows.values():
            versions += len(chain)
    return {
        "sequence": state["sequence"],
        "horizon": state["horizon"],
        "keys": keys,
        "versions": versions,
        "history_base": journal["base"],
        "journal_records": len(journal["records"]),
        "recovered_tail_bytes": journal["recovered_tail"],
    }


def query(state, at, relation_name="spectra", where=None):
    """Return {"ok": rows} or {"ok": False, "error": <code>, "reason": <detail>}."""
    relation = state["catalog"]["relations"].get(relation_name)
    if relation is None:
        return {"ok": False, "error": "not_found", "reason": "unknown_relation"}
    if at < state["horizon"]:
        return {"ok": False, "error": "snapshot", "reason": "below_version_floor"}
    if at > state["sequence"]:
        return {"ok": False, "error": "snapshot", "reason": "future_snapshot"}
    if at < relation["created"]:
        return {"ok": False, "error": "snapshot", "reason": "relation_absent"}
    rows = visible_rows(state, relation_name, at)
    selected = []
    for key in sorted(rows):
        row = rows[key]
        if matches(row, where or []):
            selected.append(row)
    selected.sort(key=lambda row: order_value(row[relation["primary"]]))
    return {"ok": True, "rows": selected}


def matches(row, where):
    for predicate in where:
        value = row.get(predicate["column"])
        op = predicate["op"]
        if op == "is_null":
            if value is not None:
                return False
            continue
        if op == "not_null":
            if value is None:
                return False
            continue
        if value is None or predicate.get("value") is None:
            return False
        left = order_value(value)
        right = order_value(predicate["value"])
        if left[0] != right[0]:
            return False
        if op == "=":
            pass_ = left == right
        elif op == "!=":
            pass_ = left != right
        elif op == "<":
            pass_ = left < right
        elif op == ">":
            pass_ = left > right
        elif op == "<=":
            pass_ = left <= right
        elif op == ">=":
            pass_ = left >= right
        else:
            return False
        if not pass_:
            return False
    return True


def changes(state, journal=None, after=0, limit=1000):
    journal = journal or state["journal"]
    out = {
        "records": [],
        "cursor": after,
        "head": state["sequence"],
        "history_base": journal["base"],
        "more": False,
    }
    if after < journal["base"]:
        return {
            "ok": False,
            "error": "snapshot",
            "reason": "below_change_floor",
            "history_base": journal["base"],
        }
    if after > state["sequence"] or limit < 1 or limit > 1000:
        return {"ok": False, "error": "invalid", "reason": "invalid_cursor"}
    for record in journal["records"]:
        if record["sequence"] <= after:
            continue
        if len(out["records"]) >= limit:
            out["more"] = True
            break
        out["records"].append(record)
        out["cursor"] = record["sequence"]
    return {"ok": True, **out}


def rows_digest(rows):
    return canonical_digest(rows)


def history_digest(journal):
    material = []
    for record in journal["records"]:
        material.append(
            {
                "sequence": record["sequence"],
                "kind": record["kind"],
                "transaction": record.get("transaction"),
                "writes": record.get("writes"),
                "horizon": record.get("horizon"),
                "relation": record.get("relation"),
                "index": record.get("index"),
            }
        )
    return canonical_digest(material)


def commit_identities(journal):
    out = []
    for record in journal["records"]:
        if record["kind"] == "commit":
            out.append({"sequence": record["sequence"], "transaction": record.get("transaction")})
    return out


def retention_contract(state):
    """Two independent floors that a consumer must not conflate.

    version_floor  -- oldest `at` a point-in-time query accepts (image horizon survives).
    change_floor   -- earliest `after` cursor /changes accepts (journal header base).
    A commit at or below change_floor has no journal identity available from this instance,
    even though its resulting rows may still be visible and its history may still be
    readable while sequences remain above version_floor.
    """
    return {
        "version_floor": state["horizon"],
        "change_floor": state["journal"]["base"],
        "journal_commits": commit_identities(state["journal"]),
        "identity_unavailable_through": state["journal"]["base"],
    }


def catalog_view(state):
    """Projection matching GET /catalog: relations and indexes sorted by name."""
    relations = []
    for name in sorted(state["catalog"]["relations"]):
        relation = state["catalog"]["relations"][name]
        relations.append(
            {
                "name": relation["name"],
                "primary": relation["primary"],
                "columns": relation["columns"],
                "created": relation["created"],
            }
        )
    indexes = []
    for name in sorted(state["catalog"]["indexes"]):
        definition = state["catalog"]["indexes"][name]
        indexes.append(
            {
                "name": definition["name"],
                "relation": definition["relation"],
                "column": definition["column"],
                "unique": definition["unique"],
                "created": definition["created"],
            }
        )
    return {"relations": relations, "indexes": indexes}


def describe(
    image_bytes,
    journal_bytes,
    relation_name="spectra",
    at_values=(),
    after_values=(),
    journal_base_override=None,
):
    """Full independent description used by both corpus generation and verification."""
    state, journal = open_state(image_bytes, journal_bytes, journal_base_override)
    image_sequence = 0
    image_digest = None
    if image_bytes is not None:
        image, info = decode_image(image_bytes)
        image_sequence = image["sequence"]
        image_digest = info["sha256"]
    head_rows = query(state, state["sequence"], relation_name)["rows"]
    description = {
        "catalog": catalog_view(state),
        "stats": stats(state, journal),
        "head_rows": head_rows,
        "rows_digest": rows_digest(head_rows),
        "history_digest": history_digest(journal),
        "image_sha256": image_digest,
        "image_sequence": image_sequence,
        "journal_sha256": sha256_hex(journal_bytes) if journal_bytes is not None else None,
        "recovered_tail_bytes": journal["recovered_tail"],
        "retention": retention_contract(state),
        "snapshots": {},
        "changes": {},
    }
    for at in sorted(set(at_values)):
        description["snapshots"][str(at)] = query(state, at, relation_name)
    for after in sorted(set(after_values)):
        description["changes"][str(after)] = changes(state, journal, after, 1000)
    return description
