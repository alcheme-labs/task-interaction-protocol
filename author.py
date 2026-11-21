#!/usr/bin/env python3
"""Local design exercise for validating and registering immutable task definitions."""

import argparse
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


SCHEMA = Path(__file__).with_name("definition.schema.json")
VALUE_TYPES = ("text", "bool")
ID_TYPES = ("resultId", "factId")
SOURCES = {
    "confirmation": {"mode": "observe", "semantics": "state", "subjectType": "text", "fact": {"ready": "bool"}},
}


class Problem(Exception):
    def __init__(self, code, message, path=None, **details):
        super().__init__(message)
        self.result = {"ok": False, "code": code, "message": message, **details}
        if path is not None:
            self.result["path"] = "".join(
                "/" + str(part).replace("~", "~0").replace("/", "~1") for part in path
            )


def invalid(path, message, fix="Apply the stated constraint at path and run validate again."):
    raise Problem("INVALID_DEFINITION", message, path, fix=fix)


def unsupported(path, message):
    raise Problem(
        "UNSUPPORTED_CAPABILITY", message, path,
        fix="Use describe to inspect local capabilities. If the requirement needs this capability, publish through a host that can verify it.",
    )


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode(raw):
    result = json.loads(raw, object_pairs_hook=unique_keys)
    # Reject non-finite numbers and text that cannot be stored or read back as UTF-8.
    canonical(result).encode("utf-8")
    return result


def load_document(path):
    try:
        raw = path.read_bytes().decode("utf-8")
        return raw, decode(raw)
    except OSError as error:
        raise Problem("INPUT_UNAVAILABLE", str(error)) from error
    except (ValueError, RecursionError) as error:
        raise Problem("INVALID_JSON", str(error), (), fix="Supply a UTF-8 JSON document with unique object keys.") from error


def schema_validator():
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
        from referencing import Registry
    except ModuleNotFoundError as error:
        raise Problem("SCHEMA_UNAVAILABLE", "The local Python environment lacks jsonschema.",
                      fix="Run python -m pip install jsonschema in the local virtual environment.") from error

    try:
        schema = decode(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError) as error:
        raise Problem("SCHEMA_UNAVAILABLE", f"Cannot load {SCHEMA.name}. {error}") from error
    return Draft202012Validator(schema, registry=Registry())


@dataclass(frozen=True)
class Port:
    kind: str
    optional: bool = False


def ports(items, path, allow_ids=False):
    result = {}
    available = VALUE_TYPES + ID_TYPES if allow_ids else VALUE_TYPES
    for name, kind in items.items():
        key = name.removesuffix("?")
        if key in result:
            invalid(path + (name,), f"Port {key!r} is declared twice.")
        if kind not in available:
            unsupported(path + (name,), f"This local position supports port types {', '.join(available)}.")
        result[key] = Port(kind, name.endswith("?"))
    return result


def fields(namespace, items):
    return {f"{namespace}.{name}": port for name, port in items.items()}


def literal_type(value, path):
    if type(value) is bool:
        return "bool"
    if isinstance(value, str) and value.strip():
        return "text"
    invalid(path, "A local literal must be a nonblank text or a boolean value.")


def require_value(scope, expression, expected, path):
    if isinstance(expression, str):
        actual = scope.get(expression)
    else:
        value = expression["const"] if isinstance(expression, dict) else expression
        actual = Port(literal_type(value, path))
    if actual is None:
        invalid(path, f"{expression!r} is unavailable in this phase or output variant.",
                "Map a field available on this route. Use describe to check the permitted mapping scopes.")
    if actual.kind != expected.kind:
        invalid(path, f"{expression!r} has type {actual.kind}. This position requires {expected.kind}.",
                f"Choose a source of type {expected.kind} for this position.")
    if actual.optional and not expected.optional:
        invalid(path, f"Optional field {expression!r} cannot provide a required value.",
                "Choose a required source or, if absence is allowed, make the receiving input optional.")


def guard_value(guard, scope, namespace, path):
    expression, _, literal = guard
    if not expression.startswith(namespace + "."):
        invalid(path, f"This condition must compare a {namespace} field.")
    if isinstance(literal, dict):
        literal = literal["const"]
    require_value(scope, expression, Port(literal_type(literal, path)), path)
    return expression, literal


class Definition:
    """Semantic checks after JSON Schema has accepted the definition's shape."""

    def __init__(self, document):
        self.document = document
        self.roles = set(document["roles"])
        self.activities = document["activities"]
        self.waits = document.get("waits", {})
        self.start_scope = fields("start", ports(document["start"]["input"], ("start", "input")))
        self.inputs = {
            name: ports(activity["input"], ("activities", name, "input"), allow_ids=True)
            for name, activity in self.activities.items()
        }
        self.graph = defaultdict(set)
        self.links = []
        self.used_waits = set()
        self.ends = set()
        self.formal_uses = []

    def next(self, source, target, mapping, scope, path):
        if target not in self.activities:
            invalid(path, f"Unknown activity {target!r}.", "Reference a declared activity or define the intended target.")
        inputs = self.inputs[target]
        for name, expected in inputs.items():
            if name not in mapping and not expected.optional:
                invalid(path + ("with", name), f"Required input {name!r} for {target!r} is missing.")
        for name, expr in mapping.items():
            if name not in inputs:
                invalid(path + ("with", name), f"Unknown input {name!r} for {target!r}.")
            require_value(scope, expr, inputs[name], path + ("with", name))
        self.links.append((source, target, mapping))
        if source is not None:
            self.graph[source].add(target)

    def wait(self, name, scope, path):
        if name not in self.waits:
            invalid(path + ("wait",), f"Unknown wait {name!r}.")
        self.used_waits.add(name)
        wait = self.waits[name]
        wait_path = ("waits", name)
        source = wait["source"]
        if source not in self.document.get("sources", []):
            invalid(wait_path + ("source",), f"Undeclared source slot {source!r}.")
        capability = SOURCES[source]
        if any(wait[key] != capability[key] for key in ("mode", "semantics")):
            unsupported(wait_path, f"Source {source!r} only supports local observe/state.")
        if wait["fact"] != capability["fact"]:
            unsupported(wait_path + ("fact",), f"Fact fields do not match the fixture contract for {source!r}.")
        require_value(scope, wait["subject"], Port(capability["subjectType"]), wait_path + ("subject",))
        facts = fields("fact", ports(wait["fact"], wait_path + ("fact",)))
        guard_value(wait["when"], facts, "fact", wait_path + ("when",))
        return wait["resume"], {**scope, **facts, "factId": Port("factId")}, wait_path + ("resume",)

    def exit(self, source, route, scope, path):
        if "wait" in route:
            route, scope, path = self.wait(route["wait"], scope, path)
        if "next" in route:
            self.next(source, route["next"], route.get("with", {}), scope, path)
            return
        if "end" in route:
            require_value(scope, route["end"], Port("resultId"), path + ("end",))
            self.formal_uses.append((source, route["end"], None, path + ("end",)))
            self.ends.add(source)
            return
        target = route["return"]
        if target not in self.activities:
            invalid(path + ("return",), f"Unknown return activity {target!r}.")
        base = route["base"]
        if not base.startswith("in."):
            invalid(path + ("base",), "Return base must refer to a formal result in the current input.")
        require_value(scope, base, Port("resultId"), path + ("base",))
        require_value(scope, route["reason"], Port("text"), path + ("reason",))
        output = self.activities[target]["output"]
        variants = output["variants"].values() if "variants" in output else [output]
        for name in route["edit"]:
            kinds = {variant.get(name) for variant in variants}
            if name == "kind" or None in kinds or len(kinds) != 1:
                invalid(path + ("edit",), f"Editable field {name!r} must have one type in all target outputs.")
        self.formal_uses.append((source, base, target, path + ("base",)))

    def activity(self, name, activity):
        path = ("activities", name)
        if activity["assignee"] not in self.roles:
            invalid(path + ("assignee",), f"Undeclared role {activity['assignee']!r}.")
        if activity["execution"]["kind"] != "rule":
            unsupported(path + ("execution",), "Local registration supports rule execution only.")
        for key in ("checks", "resourceUses"):
            if activity.get(key):
                unsupported(path + (key,), f"No verified local provider is available for {key}.")
        routes = activity["routes"]
        if [i for i, route in enumerate(routes) if "default" in route] != [len(routes) - 1]:
            invalid(path + ("routes",), "Exactly one default route is required and must be last.",
                    "Place conditional routes before the unconditional fallback.")
        output = activity["output"]
        variants = output.get("variants")
        used_routes = set()
        for kind, values in (variants or {None: output}).items():
            output_path = path + ("output",) if kind is None else path + ("output", "variants", kind)
            scope = {**self.start_scope, **fields("in", self.inputs[name]), "result": Port("resultId"),
                     **fields("out", ports(values, output_path))}
            if kind is not None:
                if "out.kind" in scope:
                    invalid(output_path + ("kind",), "A variant's kind is set by its declaration.")
                scope["out.kind"] = Port("text")
            excluded = defaultdict(set)
            for i, route in enumerate(routes):
                route_path = path + ("routes", i)
                if "default" in route:
                    self.exit(name, route["default"], scope, route_path + ("default",))
                    used_routes.add(i)
                    break
                expression, literal = guard_value(route["when"], scope, "out", route_path + ("when",))
                if kind is not None and expression == "out.kind":
                    if literal not in variants:
                        invalid(route_path + ("when",), f"Unknown output variant {literal!r}.")
                    if kind != literal:
                        continue
                if literal in excluded[expression]:
                    continue
                self.exit(name, route["exit"], scope, route_path + ("exit",))
                used_routes.add(i)
                excluded[expression].add(literal)
                if (kind is not None and expression == "out.kind") or excluded[expression] == {True, False}:
                    break
        for i in range(len(routes)):
            if i not in used_routes:
                invalid(path + ("routes", i), "No possible local output can reach this route.")

    def verify_formal_results(self):
        origins = defaultdict(set)

        def origin(actor, expr):
            if expr == "result":
                return {actor}
            if expr.startswith("in."):
                return origins[actor, expr[3:]]
            return {None}

        # A return reopens the original input, whose possible origins are already
        # represented by the start and next edges that initially created it.
        changed = True
        while changed:
            changed = False
            for source, target, mapping in self.links:
                for name, expr in mapping.items():
                    if self.inputs[target][name].kind == "resultId":
                        known = origins[target, name]
                        new = origin(source, expr) - known
                        known.update(new)
                        changed = changed or bool(new)
        for source, expr, target, path in self.formal_uses:
            possible = origin(source, expr)
            if not possible or None in possible:
                invalid(path, "Every incoming path must provide a known formal result of this instance.",
                        "Pass result from the producing activity and preserve that resultId along the relevant edges.")
            if target is not None and possible != {target}:
                invalid(path, f"The return base must come from {target!r} on every incoming path.",
                        "Map the target activity's result to a dedicated required input for the return base.")

    def verify_graph(self, entry):
        reachable = {entry}
        pending = [entry]
        while pending:
            following = self.graph[pending.pop()] - reachable
            reachable.update(following)
            pending.extend(following)
        can_finish = set(self.ends)
        while True:
            previous = len(can_finish)
            can_finish.update(name for name in self.activities if self.graph[name] & can_finish)
            if len(can_finish) == previous:
                break
        for name in self.activities:
            if name not in reachable:
                invalid(("activities", name), "Activity is unreachable from the entry.",
                        "Provide the intended next or wait-resume path from a reachable activity.")
            if name not in can_finish:
                invalid(("activities", name), "Activity has no path to an end without using return.",
                        "Provide a route that can finish directly or via next or a wait resume.")
        for name in self.waits:
            if name not in self.used_waits:
                invalid(("waits", name), "Wait is unreachable from an activity route.")

    def validate(self):
        document = self.document
        if "\x00" in document["taskId"]:
            invalid(("taskId",), "A local task ID cannot contain a NUL character.")
        distinct = set()
        for i, pair in enumerate(document.get("distinctRoles", [])):
            path = ("distinctRoles", i)
            for j, role in enumerate(pair):
                if role not in self.roles:
                    invalid(path + (j,), f"Undeclared role {role!r}.", "Reference a declared role in the pair.")
            roles = frozenset(pair)
            if len(roles) != 2:
                invalid(path, "A distinct-role pair must name two different roles.")
            if roles in distinct:
                invalid(path, "This distinct-role pair is already declared.", "Keep one declaration of the pair in either order.")
            distinct.add(roles)
        if document.get("after"):
            unsupported(("after",), "No post-commit handler is registered for the local exercise.")
        for i, source in enumerate(document.get("sources", [])):
            if source not in SOURCES:
                unsupported(("sources", i), f"Source {source!r} has no verified local fixture contract.")
        start = document["start"]
        self.next(None, start["activity"], start.get("with", {}), self.start_scope, ("start",))
        for name, activity in self.activities.items():
            self.activity(name, activity)
        self.verify_graph(start["activity"])
        self.verify_formal_results()


def selected_branches(error, definitions):
    branches = [definitions[branch["$ref"].removeprefix("#/$defs/")]
                if branch.get("$ref", "").startswith("#/$defs/") else branch
                for branch in error.validator_value]
    tagged = [i for i, branch in enumerate(branches)
              if any("const" in rule and key in error.instance and error.instance[key] == rule["const"]
                     for key, rule in branch.get("properties", {}).items())]
    if tagged:
        return tagged
    owners = defaultdict(list)
    for i, branch in enumerate(branches):
        for key in branch.get("required", []):
            owners[key].append(i)
    return {indices[0] for key, indices in owners.items() if key in error.instance and len(indices) == 1}


def schema_problem(error, validator):
    from jsonschema.exceptions import best_match

    definitions = validator.schema["$defs"]
    while error.validator == "oneOf" and error.context and isinstance(error.instance, dict):
        selected = selected_branches(error, definitions)
        if not selected:
            break
        if len(selected) != 1:
            invalid(tuple(error.absolute_path), "This object selects more than one form.",
                    "Keep the fields for the single intended form in the definition schema.")
        branch, = selected
        error = next(child for child in error.context if child.schema_path[0] == branch)
    error = best_match([error])
    path = tuple(error.absolute_path)
    message = error.message
    fix = f"Check the required shape at path in {SCHEMA.name}, then run validate again."

    # Strings in a map may fail both the reference and literal forms. Inspect
    # the reference constraint to explain why that phase cannot read the fact.
    same_field = error
    while same_field.parent is not None and tuple(same_field.parent.absolute_path) == path:
        same_field = same_field.parent
    before_fact = ("startPath", "inputPath", "checkPath", "requestPath", "exitPath")
    reference = next((child for child in (error, *same_field.context) if child.validator == "pattern"
                      and any(child.schema == definitions[name] for name in before_fact)), None)
    if reference is not None and validator.evolve(schema=definitions["factPath"]).is_valid(reference.instance):
        message = f"{reference.instance!r} is only available after the wait receives a matching external fact."
        fix = "Use the fact in the wait or pass it to a later activity from its resume path."
        if len(path) >= 2 and path[-2] == "with":
            fix += " Omit this mapping on the direct path if the destination input is optional."
    elif error.validator == "required":
        missing = next(key for key in error.validator_value if key not in error.instance)
        if path:
            path += (missing,)
        message = f"Required field {missing!r} is missing."
        fix = f"Add {missing!r} with the shape described in {SCHEMA.name}."
    elif error.validator == "additionalProperties" and "patternProperties" not in error.schema:
        unknown = sorted(error.instance.keys() - error.schema.get("properties", {}).keys())
        if unknown:
            path += (unknown[0],)
            message = f"Field {unknown[0]!r} is unavailable in this form."
            fix = f"Use the corresponding declared field from {SCHEMA.name} if this setting is required."
    invalid(path, message, fix)


def validate(document):
    validator = schema_validator()
    from referencing.exceptions import Unresolvable

    try:
        error = next(validator.iter_errors(document), None)
    except Unresolvable as unavailable:
        raise Problem("SCHEMA_UNAVAILABLE", "The authoring schema must resolve from the local file.") from unavailable
    if error is not None:
        schema_problem(error, validator)
    Definition(document).validate()


def publish(db_path, raw, document, request_id, base):
    if not request_id.strip():
        raise Problem("USAGE", "--request-id must be nonblank.")
    digest = hashlib.sha256(canonical({"base": base, "document": document}).encode("utf-8")).hexdigest()
    # Validate before locking the database but honor a stored key before applying
    # current validation rules, so a retry reads the original committed receipt.
    problem = None
    try:
        validate(document)
    except Problem as error:
        problem = error
    if problem and not db_path.exists():
        raise problem
    with closing(sqlite3.connect(db_path, timeout=10)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS task_definitions (
            task_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
            document TEXT NOT NULL, PRIMARY KEY (task_id, revision))""")
        db.execute("""CREATE TABLE IF NOT EXISTS task_publish_receipts (
            request_id TEXT PRIMARY KEY NOT NULL, digest TEXT NOT NULL,
            task_id TEXT NOT NULL, revision INTEGER NOT NULL,
            FOREIGN KEY (task_id, revision) REFERENCES task_definitions (task_id, revision))""")
        previous = db.execute("SELECT digest, task_id, revision FROM task_publish_receipts WHERE request_id = ?",
                              (request_id,)).fetchone()
        if previous:
            if previous[0] != digest:
                raise Problem("KEY_CONFLICT", "This request key belongs to a different document or base revision.",
                              fix="Replay the original document and base to recover its receipt. Use a fresh request key for a different publication.")
            return {"ok": True, "definition": f"{previous[1]}@{previous[2]}"}
        if problem:
            raise problem
        task_id = document["taskId"]
        head = db.execute("SELECT max(revision) FROM task_definitions WHERE task_id = ?", (task_id,)).fetchone()[0]
        current = f"{task_id}@{head}" if head is not None else None
        if base != current:
            raise Problem("STALE_BASE", "Base must match the current definition revision.", current=current,
                          fix="Read current before revising and use it as --base. Omit --base if current is null.")
        revision = (head or 0) + 1
        db.execute("INSERT INTO task_definitions VALUES (?, ?, ?)", (task_id, revision, raw))
        db.execute("INSERT INTO task_publish_receipts VALUES (?, ?, ?, ?)", (request_id, digest, task_id, revision))
        return {"ok": True, "definition": f"{task_id}@{revision}"}


def read(db_path, reference):
    task_id, _, number = reference.rpartition("@")
    if (not task_id or not number.isascii() or not number.isdecimal() or number[0] == "0"
            or len(number) > 19 or int(number) >= 2**63):
        raise Problem("INVALID_REF", "Expected a definition reference of the form taskId@revision.")
    if not db_path.is_file():
        raise Problem("NOT_FOUND", "Definition store does not exist.")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        found = db.execute("SELECT document FROM task_definitions WHERE task_id = ? AND revision = ?",
                           (task_id, int(number))).fetchone()
    if not found:
        raise Problem("NOT_FOUND", "Definition revision does not exist.")
    try:
        document = decode(found[0])
    except (TypeError, ValueError, RecursionError) as error:
        raise Problem("STORE_UNAVAILABLE", "Stored definition is not valid JSON.") from error
    return {"ok": True, "definition": reference, "document": document}


def describe():
    return {
        "ok": True,
        "protocolVersion": "1.0",
        "scope": "Single-owner local SQLite definition registration. No task starts or external calls.",
        "schemaFile": SCHEMA.name,
        "commands": {
            "describe": "python author.py describe",
            "validate": "python author.py validate FILE",
            "publish": "python author.py publish FILE --db DB --request-id K [--base REF]",
            "read": "python author.py read REF --db DB",
        },
        "profile": {
            "execution": ["rule"],
            "distinctRoles": {
                "example": [["writer", "reviewer"]],
                "rule": "Each unordered pair names two different declared roles.",
                "atStart": "The starter must bind the pair to different canonical principal identities after resolving aliases and checking authorization.",
            },
            "sources": SOURCES,
            "sourceAuthority": "The offline fixture supplies versioned state. A host must bind and verify its own source and observing capability.",
            "portTypes": {"values": VALUE_TYPES, "activityInputOnly": ID_TYPES},
            "mapping": "Use flat start.* references at entry. Exits may also read in.*, out.* and result. Resume adds fact.* and factId. Use {\"const\":\"text\"} or a boolean for literals.",
            "conditions": "Equality against text or bool in the current output or, for a wait, the declared fact.",
            "routing": "First match, one final default, each route possible. Every activity must be reachable and have a path to an end without relying on a return.",
            "return": "A required input must reference a formal result of the target activity on every path. Edit only fields with the same type in every target variant.",
            "unavailable": ["method execution", "checks", "post-commit handlers", "declared resource use", "request or event waits", "other source providers"],
        },
        "registration": "Omit --base on first registration. Reuse the request ID with the same document and base after an uncertain response. To revise, use a new request ID and the current definition as --base.",
        "readResult": "Use document from the read response to recover the exact definition object.",
    }


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Problem("USAGE", message)


def main():
    parser = Parser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("describe")
    actions.add_parser("validate").add_argument("file", type=Path)
    writer = actions.add_parser("publish")
    writer.add_argument("file", type=Path)
    writer.add_argument("--db", type=Path, required=True)
    writer.add_argument("--request-id", required=True)
    writer.add_argument("--base")
    reader = actions.add_parser("read")
    reader.add_argument("reference")
    reader.add_argument("--db", type=Path, required=True)
    try:
        args = parser.parse_args()
        if args.command == "describe":
            result = describe()
        elif args.command == "read":
            result = read(args.db, args.reference)
        else:
            raw, document = load_document(args.file)
            if args.command == "publish":
                result = publish(args.db, raw, document, args.request_id, args.base)
            else:
                validate(document)
                result = {"ok": True, "taskId": document["taskId"]}
    except Problem as error:
        result = error.result
    except sqlite3.Error as error:
        code = "STORE_BUSY" if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "STORE_UNAVAILABLE"
        result = {"ok": False, "code": code, "message": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
