#!/usr/bin/env node
// Independent in-memory consumer of the portable rule / observe-state profile.
import { readFileSync } from 'node:fs';
import { isDeepStrictEqual as equal } from 'node:util';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' && value.trim().length > 0;
const own = (value, key) => object(value) && Object.hasOwn(value, key);
const types = new Set(['text', 'bool', 'resultId', 'factId']);

function requireThat(condition, message) {
  if (!condition) throw new Error(message);
}

function inspectFields(schema) {
  requireThat(object(schema), 'field schema must be an object');
  const names = new Set();
  for (const [declaration, type] of Object.entries(schema)) {
    const name = declaration.replace(/\?$/, '');
    requireThat(text(name) && !names.has(name) && types.has(type), 'invalid field ' + declaration);
    names.add(name);
  }
}

function inspectWhen(test) {
  requireThat(Array.isArray(test) && test.length === 3 && text(test[0]) && test[1] === 'eq',
    'expected [field, eq, value]');
}

function inspectDefinition(d) {
  requireThat(d.protocolVersion === '1.0' && text(d.taskId) && text(d.goal), 'invalid definition header');
  for (const list of [d.roles, d.sources ?? []]) {
    requireThat(Array.isArray(list) && list.every(text) && list.length === new Set(list).size,
      'invalid role or source list');
  }
  if (own(d, 'distinctRoles')) {
    requireThat(Array.isArray(d.distinctRoles), 'INVALID_DEFINITION: distinctRoles must be a list');
    const seenPairs = new Set();
    for (const pair of d.distinctRoles) {
      requireThat(Array.isArray(pair) && pair.length === 2 && pair[0] !== pair[1] &&
        pair.every(role => d.roles.includes(role)), 'INVALID_DEFINITION: expected two distinct declared roles');
      const key = JSON.stringify([...pair].sort());
      requireThat(!seenPairs.has(key), 'INVALID_DEFINITION: duplicate distinctRoles pair');
      seenPairs.add(key);
    }
  }
  requireThat(object(d.activities) && own(d.activities, d.start?.activity), 'invalid entry activity');
  requireThat(!Object.keys(d.after ?? {}).length, 'after hooks require another profile');
  inspectFields(d.start.input);
  const waits = d.waits ?? {};
  function inspectExit(exit, resuming = false) {
    const options = ['next', 'return', 'wait', 'end'].filter(key => own(exit, key));
    requireThat(options.length === 1, 'an exit must specify one operation');
    const [kind] = options;
    if (resuming) requireThat(kind === 'next' || kind === 'end', 'invalid wait continuation');
    if (kind === 'next' || kind === 'return') {
      requireThat(own(d.activities, exit[kind]), 'unknown target ' + exit[kind]);
    }
    if (kind === 'return') {
      requireThat(text(exit.base) && exit.base.startsWith('in.') && text(exit.reason) &&
        Array.isArray(exit.edit) && exit.edit.every(text), 'invalid rework declaration');
      const output = d.activities[exit.return].output;
      const shapes = own(output, 'variants') ? Object.values(output.variants) : [output];
      for (const field of exit.edit) {
        requireThat(shapes.some(shape => own(shape, field) || own(shape, field + '?')) ||
          (own(output, 'variants') && field === 'kind'), 'unknown edit field ' + field);
      }
    }
    if (kind === 'wait') requireThat(own(waits, exit.wait), 'unknown wait ' + exit.wait);
    if (kind === 'end') requireThat(text(exit.end), 'expected final result binding');
  }
  for (const [name, a] of Object.entries(d.activities)) {
    requireThat(text(a.goal) && d.roles.includes(a.assignee) && a.execution?.kind === 'rule' &&
      !own(a.execution, 'executeWith') && (!own(a.execution, 'instructions') || text(a.execution.instructions)),
      'unsupported or invalid activity ' + name);
    requireThat((a.checks ?? []).length === 0 && (a.resourceUses ?? []).length === 0,
      'method checks and resource registration require another profile');
    inspectFields(a.input);
    if (own(a.output, 'variants')) {
      requireThat(object(a.output.variants) && Object.keys(a.output.variants).length, 'empty output variants');
      Object.values(a.output.variants).forEach(inspectFields);
    } else {
      inspectFields(a.output);
    }
    requireThat(Array.isArray(a.routes) && a.routes.length, 'missing routes for ' + name);
    a.routes.forEach((route, i) => {
      if (i === a.routes.length - 1) {
        requireThat(own(route, 'default') && !own(route, 'when'), 'missing final default for ' + name);
        inspectExit(route.default);
      } else {
        requireThat(!own(route, 'default'), 'a default route must be last');
        inspectWhen(route.when);
        inspectExit(route.exit);
      }
    });
  }
  for (const [name, w] of Object.entries(waits)) {
    requireThat(w.mode === 'observe' && w.semantics === 'state' && d.sources?.includes(w.source) &&
      text(w.subject) && !own(w, 'method'), 'unsupported or invalid wait ' + name);
    inspectFields(w.fact);
    inspectWhen(w.when);
    inspectExit(w.resume, true);
  }
}

function valueOf(expression, context) {
  requireThat(text(expression), 'invalid binding expression');
  let value = context;
  for (const step of expression.split('.')) {
    if (!own(value, step)) return undefined;
    value = value[step];
  }
  return value;
}

function mappedInputs(withMap, context) {
  return Object.fromEntries(Object.entries(withMap ?? {}).map(([key, expr]) =>
    [key, valueOf(expr, context)]).filter(([, value]) => value !== undefined));
}

function matches(test, context) {
  const value = valueOf(test[0], context);
  return value !== undefined && equal(value, test[2]);
}

function validFields(schema, values, instance) {
  if (!object(values)) return false;
  const declared = new Set();
  for (const [declaration, type] of Object.entries(schema)) {
    const optional = declaration.endsWith('?');
    const key = optional ? declaration.slice(0, -1) : declaration;
    declared.add(key);
    if (!own(values, key)) {
      if (!optional) return false;
      continue;
    }
    const value = values[key];
    if (type === 'text' && !text(value)) return false;
    if (type === 'bool' && typeof value !== 'boolean') return false;
    if (type === 'resultId' && !instance.results.has(value)) return false;
    if (type === 'factId' && !instance.facts.has(value)) return false;
  }
  return Object.keys(values).every(key => declared.has(key));
}

function validOutput(schema, values, instance) {
  if (!own(schema, 'variants')) return validFields(schema, values, instance);
  if (!object(values) || !own(values, 'kind') || !own(schema.variants, values.kind)) return false;
  const { kind, ...fields } = values;
  return typeof kind === 'string' && validFields(schema.variants[kind], fields, instance);
}

class Instance {
  constructor(definition, start) {
    this.definition = definition;
    this.start = start;
    this.results = new Map();
    this.facts = new Map();
    this.rounds = new Map();
    this.waits = [];
    this.submitReceipts = new Map();
    this.final = null;
    for (const [declared, actual] of [
      [definition.roles, start.bindings], [definition.sources ?? [], start.sources ?? {}],
    ]) {
      requireThat(object(actual) && equal(Object.keys(actual).sort(), [...declared].sort()) &&
        Object.values(actual).every(text), 'invalid role or source bindings');
    }
    for (const [left, right] of definition.distinctRoles ?? []) {
      requireThat(start.bindings[left] !== start.bindings[right],
        'INVALID_BINDINGS: ' + left + ' and ' + right + ' require different actors');
    }
    requireThat(validFields(definition.start.input, start.input, this), 'invalid start input');
    this.open(definition.start.activity, mappedInputs(definition.start.with, {start: start.input}));
  }

  open(activity, input, rework) {
    requireThat(validFields(this.definition.activities[activity].input, input, this),
      'invalid mapped input for ' + activity);
    const round = (this.rounds.get(activity) ?? 0) + 1;
    this.rounds.set(activity, round);
    this.position = {kind: 'open', activity, round, input, rework};
  }

  install(exit, context) {
    if (own(exit, 'next')) {
      this.open(exit.next, mappedInputs(exit.with, context));
    } else if (own(exit, 'return')) {
      const base = this.results.get(valueOf(exit.base, context));
      const reason = valueOf(exit.reason, context);
      requireThat(base && base.activity === exit.return && text(reason), 'invalid result or reason for rework');
      this.open(exit.return, base.input, {base, edit: new Set(exit.edit), reason});
    } else if (own(exit, 'wait')) {
      const name = exit.wait;
      const spec = this.definition.waits[name];
      const subject = valueOf(spec.subject, context);
      requireThat(subject !== undefined, 'missing wait subject');
      const wait = {kind: 'wait', name, round: this.waits.filter(w => w.name === name).length + 1,
        source: this.start.sources[spec.source], subject, context, spec, receipts: new Map(), closed: false};
      this.waits.push(wait);
      this.position = wait;
    } else {
      const finalId = valueOf(exit.end, context);
      const final = this.results.get(finalId);
      requireThat(final, 'end must select an accepted result in this instance');
      this.position = {kind: 'done', finalId, decisionId: context.result};
      this.final = final.output;
    }
  }

  state() {
    const p = this.position;
    if (p.kind === 'open') return 'OPEN:' + p.activity + ':' + p.round;
    if (p.kind === 'wait') return 'WAIT:' + p.name;
    return 'DONE';
  }

  submit(event) {
    const p = this.position;
    if (!Object.values(this.start.bindings).includes(event.actor)) return 'NOT_VISIBLE';
    const key = JSON.stringify([event.actor, event.key]);
    const receipt = this.submitReceipts.get(key);
    // Short event notation keeps the original work target when the request is replayed.
    const activity = event.activity ?? receipt?.request.activity ?? p.activity;
    const round = event.round ?? receipt?.request.round ?? p.round;
    const request = {activity, round, output: event.output};
    if (receipt) return equal(receipt.request, request) ? receipt.outcome : 'KEY_CONFLICT';
    const spec = own(this.definition.activities, activity) && this.definition.activities[activity];
    if (spec && this.start.bindings[spec.assignee] !== event.actor) return 'FORBIDDEN';
    if (p.kind !== 'open' || activity !== p.activity || round !== p.round) return 'STALE_WORK';

    let output = event.output;
    if (p.rework && object(output)) {
      const previous = p.rework.base.output;
      const permitted = Object.entries(output).every(([field, v]) =>
        p.rework.edit.has(field) || (own(previous, field) && equal(previous[field], v)));
      output = permitted ? {...previous, ...output} : undefined;
    }
    let outcome = 'rejected';
    if (validOutput(spec.output, output, this)) {
      const result = 'result/' + (this.results.size + 1);
      this.results.set(result, {activity, round, input: p.input, output});
      const context = {start: this.start.input, in: p.input, out: output, result};
      const route = spec.routes.find(route => own(route, 'default') || matches(route.when, context));
      this.install(route.exit ?? route.default, context);
      outcome = 'accepted';
    }
    this.submitReceipts.set(key, {request, outcome});
    return outcome;
  }

  signal(event) {
    const w = this.waits.findLast(w => (!own(event, 'wait') || w.name === event.wait) &&
      (!own(event, 'round') || w.round === event.round));
    if (!w || w.source !== event.source) return 'NOT_VISIBLE';
    const request = {subject: event.subject, version: event.version, fact: event.fact};
    const seen = w.receipts.get(event.key);
    if (seen) return equal(request, seen.request) ? seen.outcome : 'KEY_CONFLICT';
    if (!equal(event.subject, w.subject)) return 'NOT_VISIBLE';
    if (!Number.isSafeInteger(event.version) || !validFields(w.spec.fact, event.fact, this)) return 'INVALID_FACT';

    let outcome;
    if (w.closed) {
      outcome = 'already_resumed';
    } else if (w.latest && event.version < w.latest.version) {
      outcome = 'not_matched(stale)';
    } else if (w.latest && event.version === w.latest.version && !equal(event.fact, w.latest.fact)) {
      outcome = 'SOURCE_CONFLICT';
    } else {
      requireThat(this.position === w, 'only the current wait may advance');
      w.latest = {version: event.version, fact: event.fact};
      const factId = 'fact/' + (this.facts.size + 1);
      this.facts.set(factId, event.fact);
      const context = {...w.context, fact: event.fact, factId};
      if (matches(w.spec.when, context)) {
        this.install(w.spec.resume, context);
        w.closed = true;
        outcome = 'resumed';
      } else {
        outcome = 'not_matched(condition)';
      }
    }
    w.receipts.set(event.key, {request, outcome});
    return outcome;
  }
}

function replay(definition, {name, start, events}) {
  const instance = new Instance(definition, start);
  const states = [instance.state()];
  const outcomes = [];
  for (const event of events) {
    requireThat(text(event.key), 'missing request key');
    if (event.op === 'submit') outcomes.push(instance.submit(event));
    else if (event.op === 'signal') outcomes.push(instance.signal(event));
    else throw new Error('unknown event ' + event.op);
    states.push(instance.state());
  }
  return {name, states, outcomes, final: instance.final};
}

try {
  const [definitionPath, vectorsPath, ...extra] = process.argv.slice(2);
  requireThat(definitionPath && vectorsPath && !extra.length, 'usage: replay.mjs DEFINITION VECTORS');
  const definition = JSON.parse(readFileSync(definitionPath, 'utf8'));
  const vectors = JSON.parse(readFileSync(vectorsPath, 'utf8'));
  inspectDefinition(definition);
  requireThat(Array.isArray(vectors.cases), 'vectors must provide cases');
  const actual = vectors.cases.map(c => replay(definition, c));
  process.stdout.write(JSON.stringify(actual, null, 2) + '\n');
  for (const [i, c] of vectors.cases.entries()) {
    for (const [expectedField, actualField] of [['expect', 'states'], ['outcomes', 'outcomes'], ['final', 'final']]) {
      if (own(c, expectedField) && !equal(c[expectedField], actual[i][actualField])) {
        console.error(c.name + ': ' + actualField + ' differ from expectations');
        process.exitCode = 1;
      }
    }
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
