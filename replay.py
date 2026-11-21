"""Replay registered task documents against offline protocol conformance vectors.

The definition has already passed the registry's structural and semantic validation.
The fixture supplies actor identities, submissions and source observations. Each case
starts with an empty in-memory instance. No external service is called.
Run: python3 replay.py TASK.json VECTORS.json [--first-work]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


MISSING = object()
PORT_TYPES = {"text", "bool", "resultId", "factId"}


def only(obj, keys):
    if not isinstance(obj, dict) or set(obj) - set(keys):
        raise ValueError(f"unsupported fields, expected a subset of {sorted(keys)}")


def load_json(path):
    def unique_members(pairs):
        obj = dict(pairs)
        if len(pairs) != len(obj):
            raise ValueError(f"duplicate JSON member in {path.name}")
        return obj

    def invalid_number(value):
        raise ValueError(f"invalid JSON number in {path.name}: {value}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_members,
                      parse_constant=invalid_number)


def check_ports(declaration):
    if not isinstance(declaration, dict) or any(t not in PORT_TYPES for t in declaration.values()):
        raise ValueError(f"replay supports these port types: {sorted(PORT_TYPES)}")


def check_mapping(value):
    if isinstance(value, dict) and set(value) == {"const"}:
        if value["const"] is None or type(value["const"]) in (bool, str, int, float):
            return
    if type(value) in (bool, int, float) or value is None:
        return
    if isinstance(value, str):
        fields = value.split(".")
        if value in ("result", "factId") or (len(fields) == 2 and fields[0] in ("start", "in", "out", "fact") and fields[1]):
            return
    raise ValueError(f"unsupported path or literal: {value!r}")


def check_condition(condition):
    if len(condition) != 3 or condition[1] != "eq":
        raise ValueError("replay supports typed eq conditions")
    check_mapping(condition[0])
    constant = condition[2]
    if isinstance(constant, (dict, list)):
        check_mapping(constant)


def check_exit(edge):
    kinds = set(edge) & {"next", "return", "wait", "end"}
    if len(kinds) != 1:
        raise ValueError("each exit requires one target")
    kind = kinds.pop()
    only(edge, {"next": {"next", "with"}, "return": {"return", "base", "edit", "reason"},
                "wait": {"wait"}, "end": {"end"}}[kind])
    if kind == "next":
        for binding in edge.get("with", {}).values():
            check_mapping(binding)
    elif kind == "return":
        check_mapping(edge["base"])
        check_mapping(edge["reason"])
        if not all(isinstance(field, str) and "." not in field for field in edge["edit"]):
            raise ValueError("replay supports only flat edit fields")
    elif kind == "end":
        check_mapping(edge["end"])


def check_profile(doc):
    """Reject features which this offline consumer cannot faithfully simulate."""
    only(doc, {"protocolVersion", "taskId", "goal", "roles", "distinctRoles", "sources", "start", "activities", "waits", "after"})
    if doc["protocolVersion"] != "1.0" or doc.get("after"):
        raise ValueError("replay requires protocol 1.0 and an empty after-hook set")
    only(doc["start"], {"input", "activity", "with"})
    check_ports(doc["start"]["input"])
    for binding in doc["start"].get("with", {}).values():
        check_mapping(binding)
    for node in doc["activities"].values():
        only(node, {"assignee", "goal", "execution", "input", "output", "routes", "checks", "resourceUses"})
        only(node["execution"], {"kind", "instructions"})
        if node["execution"]["kind"] != "rule" or node.get("checks") or node.get("resourceUses"):
            raise ValueError("execution requires a method, check or resource provider outside the replay profile")
        check_ports(node["input"])
        output = node["output"]
        if "variants" in output:
            only(output, {"variants"})
            for ports in output["variants"].values():
                check_ports(ports)
        else:
            check_ports(output)
        routes = node["routes"]
        if not routes or set(routes[-1]) != {"default"}:
            raise ValueError("routes require a final default exit")
        for route in routes[:-1]:
            only(route, {"when", "exit"})
            check_condition(route["when"])
            check_exit(route["exit"])
        check_exit(routes[-1]["default"])
    for wait in doc.get("waits", {}).values():
        only(wait, {"mode", "source", "subject", "semantics", "fact", "when", "resume", "resourceUses"})
        if (wait["mode"], wait["semantics"]) != ("observe", "state") or wait.get("resourceUses"):
            raise ValueError("replay supports observed state facts without resource access")
        check_mapping(wait["subject"])
        check_ports(wait["fact"])
        check_condition(wait["when"])
        if not ({"next", "end"} & set(wait["resume"])):
            raise ValueError("wait must resume to an activity or end")
        check_exit(wait["resume"])


def read(expr, context):
    if isinstance(expr, dict):
        return expr["const"]
    if not isinstance(expr, str):
        return expr
    value = context
    for part in expr.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def equal(left, right):
    return type(left) is type(right) and left == right


def digest_value(value):
    # A transient test key for comparing data, including the difference between true and 1.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def matches(condition, context):
    left, _, constant = condition
    if isinstance(constant, dict):
        constant = constant["const"]
    return equal(read(left, context), constant)


def mapped(spec, context):
    values = {key: read(expr, context) for key, expr in spec.items()}
    return {key: value for key, value in values.items() if value is not MISSING}


class Instance:
    def __init__(self, doc, start):
        self.doc = doc
        self.bindings = start["bindings"]
        self.sources = start.get("sources", {})
        only(start, {"bindings", "sources", "input"})
        if set(self.bindings) != set(doc["roles"]) or set(self.sources) != set(doc.get("sources", [])):
            raise ValueError("fixture bindings must resolve every declared role and source")
        if not all(isinstance(v, str) and v.strip() for v in (*self.bindings.values(), *self.sources.values())):
            raise ValueError("fixture bindings require concrete actor/source identifiers")
        seen_pairs = set()
        for pair in doc.get("distinctRoles", []):
            if not isinstance(pair, list) or len(pair) != 2 or any(role not in self.bindings for role in pair):
                raise ValueError("distinctRoles requires pairs of declared roles")
            left, right = pair
            unordered = frozenset(pair)
            if left == right or unordered in seen_pairs:
                raise ValueError("distinctRoles requires unique pairs of different roles")
            if self.bindings[left] == self.bindings[right]:
                raise ValueError(f"INVALID_BINDINGS: {left} and {right} require different actors")
            seen_pairs.add(unordered)
        self.results, self.facts = {}, {}
        self.submissions, self.signals, self.waits = {}, {}, {}
        self.last_wait = None
        self.visits = Counter()
        self.final = None
        entry = doc["start"]
        if not self.valid(entry["input"], start["input"]):
            raise ValueError("invalid fixture start input")
        self.start_input = start["input"].copy()
        self.open_work(entry["activity"], mapped(entry.get("with", {}), {"start": self.start_input}))

    def valid(self, ports, value):
        if not isinstance(value, dict):
            return False
        if isinstance(ports.get("variants"), dict):
            kind = value.get("kind")
            if not isinstance(kind, str) or kind not in ports["variants"]:
                return False
            return self.valid(ports["variants"][kind], {k: v for k, v in value.items() if k != "kind"})
        if set(value) - {k.removesuffix("?") for k in ports}:
            return False
        for key, kind in ports.items():
            item = value.get(key.removesuffix("?"), MISSING)
            if item is MISSING and key.endswith("?"):
                continue
            if kind == "bool" and type(item) is bool:
                continue
            if isinstance(item, str) and item.strip():
                if kind == "text" or (kind == "resultId" and item in self.results) or (kind == "factId" and item in self.facts):
                    continue
            return False
        return True

    def open_work(self, name, inputs, rework=None):
        if not self.valid(self.doc["activities"][name]["input"], inputs):
            raise ValueError(f"mapped input for {name} does not match its declaration")
        self.visits["OPEN", name] += 1
        self.phase, self.name, self.round = "OPEN", name, self.visits["OPEN", name]
        self.inputs, self.rework = inputs.copy(), rework

    def state(self):
        if self.phase == "DONE":
            return "DONE"
        if self.phase == "WAIT":
            return "WAIT:" + self.name
        return f"OPEN:{self.name}:{self.round}"

    def transition(self, edge, context):
        if "next" in edge:
            self.open_work(edge["next"], mapped(edge.get("with", {}), context))
        elif "return" in edge:
            origin, original_input, baseline = self.results[read(edge["base"], context)]
            reason = read(edge["reason"], context)
            if origin != edge["return"] or not isinstance(reason, str) or not reason.strip():
                raise ValueError("rework requires a result from the target activity and a reason")
            self.open_work(origin, original_input, (baseline, set(edge["edit"])))
        elif "wait" in edge:
            name = edge["wait"]
            spec = self.doc["waits"][name]
            subject = read(spec["subject"], context)
            if not isinstance(subject, str) or not subject.strip():
                raise ValueError("this replay profile requires a text observation subject")
            self.visits["WAIT", name] += 1
            self.phase, self.name, self.round = "WAIT", name, self.visits["WAIT", name]
            self.last_wait = (name, self.round)
            self.waits[self.last_wait] = {"subject": subject, "context": context.copy(), "version": -1,
                                          "fact": None, "consumed": False}
        else:
            self.final = self.results[read(edge["end"], context)][2]
            self.phase = "DONE"

    def submit(self, event):
        only(event, {"op", "actor", "key", "output", "activity", "round"})
        key = (event["actor"], event["key"])
        if event["actor"] not in self.bindings.values():
            return "NOT_VISIBLE"
        prior = self.submissions.get(key)
        implicit_target = prior[0] if prior else (self.name, self.round)
        target = (event.get("activity", implicit_target[0]), event.get("round", implicit_target[1]))
        payload = digest_value([target, event["output"]])
        if prior:
            return prior[2] if payload == prior[1] else "KEY_CONFLICT"
        if self.phase != "OPEN" or target != (self.name, self.round):
            return "STALE_WORK"
        node = self.doc["activities"][self.name]
        if event["actor"] != self.bindings[node["assignee"]]:
            return "FORBIDDEN"
        self.submissions[key] = (target, payload, "rejected")
        output = event["output"]
        if self.rework and isinstance(output, dict):
            baseline, editable = self.rework
            if any(k not in editable and not equal(baseline.get(k, MISSING), v) for k, v in output.items()):
                return "rejected"
            output = {**baseline, **output}
        if not self.valid(node["output"], output):
            return "rejected"
        result_id = f"R{len(self.results) + 1}"
        self.results[result_id] = (self.name, self.inputs, output)
        context = {"start": self.start_input, "in": self.inputs, "out": output, "result": result_id}
        edge = node["routes"][-1]["default"]
        for route in node["routes"][:-1]:
            if matches(route["when"], context):
                edge = route["exit"]
                break
        self.transition(edge, context)
        self.submissions[key] = (target, payload, "accepted")
        return "accepted"

    def signal(self, event):
        only(event, {"op", "source", "key", "subject", "version", "fact", "wait", "round"})
        implicit_target = self.last_wait or (None, None)
        target = (event.get("wait", implicit_target[0]), event.get("round", implicit_target[1]))
        row = self.waits.get(target)
        if row is None:
            return "NOT_VISIBLE"
        wait = self.doc["waits"][target[0]]
        if event["source"] != self.sources[wait["source"]]:
            return "NOT_VISIBLE"
        key = (target, event["source"], event["key"])
        payload = digest_value([event["subject"], event["version"], event["fact"]])
        if key in self.signals:
            saved, outcome = self.signals[key]
            return outcome if saved == payload else "KEY_CONFLICT"
        if not equal(event["subject"], row["subject"]):
            return "NOT_VISIBLE"
        version, fact = event["version"], event["fact"]
        if type(version) is not int or version < 0 or not self.valid(wait["fact"], fact):
            return "INVALID_FACT"
        if row["consumed"]:
            outcome = "already_resumed"
        elif version < row["version"]:
            outcome = "not_matched(stale)"
        elif version == row["version"] and digest_value(fact) != digest_value(row["fact"]):
            outcome = "SOURCE_CONFLICT"
        else:
            row["version"], row["fact"] = version, fact
            fact_id = f"F{len(self.facts) + 1}"
            self.facts[fact_id] = fact
            context = {**row["context"], "fact": fact, "factId": fact_id}
            outcome = "not_matched(condition)"
            if matches(wait["when"], context):
                self.transition(wait["resume"], context)
                row["consumed"] = True
                outcome = "resumed"
        self.signals[key] = (payload, outcome)
        return outcome


def replay(doc, case):
    run = Instance(doc, case["start"])
    states = [run.state()]
    outcomes = []
    for event in case["events"]:
        if not isinstance(event.get("key"), str) or not event["key"].strip():
            raise ValueError("fixture event requires a nonempty key")
        if "round" in event and (type(event["round"]) is not int or event["round"] < 1):
            raise ValueError("fixture target round must be a positive integer")
        if event["op"] == "submit":
            outcomes.append(run.submit(event))
        elif event["op"] == "signal":
            outcomes.append(run.signal(event))
        else:
            raise ValueError(f"unsupported fixture operation: {event['op']}")
        states.append(run.state())
    result = {"name": case["name"], "states": states, "outcomes": outcomes, "final": run.final}
    if states != case["expect"] or outcomes != case["outcomes"] or digest_value(run.final) != digest_value(case["final"]):
        raise ValueError(f"trace mismatch: {json.dumps(result, ensure_ascii=False)}")
    return result


def first_work(doc, case):
    """Read the first work as the assignee identified by this trusted fixture."""
    run = Instance(doc, case["start"])
    node = doc["activities"][run.name]
    role = node["assignee"]
    return {"name": case["name"], "role": role, "actor": run.bindings[role],
            "goal": node["goal"], "input": run.inputs, "output": node["output"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("definition", type=Path)
    parser.add_argument("vectors", type=Path)
    parser.add_argument("--first-work", action="store_true",
                        help="show the first assigned work as its bound fixture actor, without applying events")
    args = parser.parse_args()
    try:
        doc = load_json(args.definition)
        check_profile(doc)
        vector_set = load_json(args.vectors)
        observe = first_work if args.first_work else replay
        results = [observe(doc, case) for case in vector_set["cases"]]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    except (KeyError, TypeError, ValueError, OSError, IndexError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
