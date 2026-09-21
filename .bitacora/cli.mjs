#!/usr/bin/env node
/**
 * bitacora — the logbook your coding agent keeps.
 *
 * Zero dependencies. Lives inside your repo on purpose: no supply chain,
 * no version drift, and your agent can read the source of its own tooling.
 *
 * Usage:
 *   node .bitacora/cli.mjs doctor [--strict]
 *   node .bitacora/cli.mjs new mistake "Scraper overwrote manual prices" --tags pricing,data-loss --severity high
 *   node .bitacora/cli.mjs recall pricing [--brief]
 *   node .bitacora/cli.mjs rotate [--dry-run]
 *   node .bitacora/cli.mjs stats
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';

const ROOT = process.cwd();
const MARKER = '<!-- bitacora:entry';
const ARCHIVE_HEADING = '## Archived';
const HAS_FILL_ME = /<!--\s*bitacora:fill-me/;
const STRIP_FILL_ME = /<!--\s*bitacora:fill-me[\s\S]*?-->/g;
const SEVERITIES = ['low', 'medium', 'high'];
const RECALL_FULL = 5;          // entries printed in full before falling back to an index
const MIN_SECTION_CHARS = 40;   // below this, a section is a gesture rather than a thought
const RECENT_DAYS = 90;
const MIN_KEEP = 3;             // a log with fewer live entries than this is not a log

const DEFAULTS = {
  version: 1,
  logs: {
    'MISTAKES.md': {
      prefix: 'M',
      maxLines: 400,
      keepEntries: 20,
      severity: true,
      sections: ['What happened', 'Root cause', 'Guardrail'],
    },
    'LEARNINGS.md': {
      prefix: 'L',
      maxLines: 400,
      keepEntries: 20,
      sections: ['What worked', 'Why it worked', 'Reuse it when'],
    },
    'DECISIONS.md': {
      prefix: 'D',
      maxLines: 600,
      keepEntries: 40,
      sections: ['Context', 'Decision', 'Consequences'],
    },
  },
  state: { file: 'STATE.md', maxLines: 200, maxAgeDays: 14 },
  required: ['CLAUDE.md', 'ARCHITECTURE.md', 'STATE.md', 'MISTAKES.md', 'LEARNINGS.md', 'DECISIONS.md'],
  archiveDir: 'docs/bitacora-archive',
};

const KIND_TO_FILE = { mistake: 'MISTAKES.md', learning: 'LEARNINGS.md', decision: 'DECISIONS.md' };

// ---------------------------------------------------------------- utilities

// Colour is off when asked, and off when nobody is watching — hook output and
// test assertions must never have to match escape codes.
const PLAIN = Boolean(process.env.NO_COLOR) || !process.stdout.isTTY;
const paint = (code) => (s) => (PLAIN ? s : `\x1b[${code}m${s}\x1b[0m`);
const C = { red: paint(31), green: paint(32), yellow: paint(33), dim: paint(2), bold: paint(1) };

function fail(msg) {
  console.error(C.red(msg));
  process.exit(1);
}

function loadConfig() {
  const p = join(ROOT, 'bitacora.config.json');
  if (!existsSync(p)) return DEFAULTS;
  let user;
  try {
    user = JSON.parse(readFileSync(p, 'utf8'));
  } catch (e) {
    fail(`bitacora.config.json is not valid JSON: ${e.message}`);
  }
  // Per-log specs merge field by field: overriding maxLines must not silently
  // drop the section requirements that give doctor its teeth.
  const logs = { ...DEFAULTS.logs };
  for (const [file, spec] of Object.entries(user.logs || {})) {
    logs[file] = { ...(DEFAULTS.logs[file] || {}), ...spec };
  }
  return { ...DEFAULTS, ...user, logs, state: { ...DEFAULTS.state, ...(user.state || {}) } };
}

// Markdown with its code stripped. A logbook documents its own format, so
// prose quotes bitacora's markers constantly; scanning raw text for them makes
// every such sentence a false positive. Quote a marker in backticks and it is
// prose; write it bare and it is a marker. (See M-0001 and M-0006.)
// An inline span may wrap across lines — hand-written markdown at 80 columns
// does it constantly — but never across a blank line, which would mean the
// backticks are unbalanced rather than spanning.
const withoutCode = (text) =>
  text.replace(/```[\s\S]*?```/g, '').replace(/`(?:[^`\n]|\n(?!\s*\n))*`/g, '');

const read = (file) => (existsSync(join(ROOT, file)) ? readFileSync(join(ROOT, file), 'utf8') : null);
const today = () => new Date().toISOString().slice(0, 10);
function plural(n, word) {
  if (n === 1) return `1 ${word}`;
  if (/[^aeiou]y$/.test(word)) return `${n} ${word.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/.test(word)) return `${n} ${word}es`;
  return `${n} ${word}s`;
}
const kindOf = (file) => file.replace('.md', '').toLowerCase().replace(/s$/, '');

function parseList(raw) {
  if (!raw || raw === '[]') return [];
  return raw
    .replace(/^\[|\]$/g, '')
    .split(',')
    .map((s) => s.trim().replace(/^["']|["']$/g, ''))
    .filter(Boolean);
}

function daysSince(iso) {
  const then = Date.parse(iso);
  return Number.isNaN(then) ? null : Math.floor((Date.now() - then) / 86400000);
}

/**
 * Split a log into { head, entries[], tail }.
 *
 * Each entry owns its raw text, from its marker to the next marker, the
 * "## Archived" heading, or end of file. Nothing is ever regenerated from
 * parsed fields, which is what makes hand-edited entries safe.
 */
function parseEntries(text) {
  if (!text) return { head: '', entries: [], tail: '' };
  const archiveAt = text.indexOf(`\n${ARCHIVE_HEADING}`);
  const body = archiveAt === -1 ? text : text.slice(0, archiveAt);
  const tail = archiveAt === -1 ? '' : text.slice(archiveAt);

  // Anchored to the start of a line on purpose: prose that quotes the marker
  // inline (this project's own templates do) must not register as an entry.
  const starts = [];
  const anchor = new RegExp(`^${MARKER.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'gm');
  for (let m = anchor.exec(body); m !== null; m = anchor.exec(body)) starts.push(m.index);
  if (starts.length === 0) return { head: body, entries: [], tail };

  const entries = starts.map((start, n) => {
    const raw = body.slice(start, n + 1 < starts.length ? starts[n + 1] : body.length);
    const close = raw.indexOf('-->');
    const meta = {};
    for (const line of (close === -1 ? '' : raw.slice(MARKER.length, close)).split('\n')) {
      const m = line.match(/^\s*([a-zA-Z_]+)\s*:\s*(.*?)\s*$/);
      if (m) meta[m[1]] = m[2];
    }
    const title = raw.match(/^#{2,4}\s+(.+)$/m);
    return {
      raw,
      id: meta.id || null,
      date: meta.date || null,
      tags: parseList(meta.tags),
      severity: meta.severity || null,
      files: parseList(meta.files),
      title: title ? title[1].trim() : '(untitled)',
    };
  });
  return { head: body.slice(0, starts[0]), entries, tail };
}

/** The prose under a `**Name.**` heading, with placeholders stripped. */
function sectionBody(raw, name) {
  const needle = `**${name}.**`;
  const at = raw.indexOf(needle);
  if (at === -1) return null;
  const rest = raw.slice(at + needle.length);
  const next = rest.search(/\n\*\*[A-Z]/);
  return (next === -1 ? rest : rest.slice(0, next))
    .replace(STRIP_FILL_ME, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Every entry anywhere — live logs and the archive. */
function allEntries(cfg) {
  const out = [];
  for (const file of Object.keys(cfg.logs)) {
    for (const e of parseEntries(read(file)).entries) out.push({ ...e, file, archived: false });
  }
  const dir = join(ROOT, cfg.archiveDir);
  if (existsSync(dir)) {
    for (const f of readdirSync(dir).filter((f) => f.endsWith('.md'))) {
      const rel = `${cfg.archiveDir}/${f}`;
      for (const e of parseEntries(readFileSync(join(dir, f), 'utf8')).entries) {
        out.push({ ...e, file: rel, archived: true });
      }
    }
  }
  return out;
}

/** Next free id, counting the archive so a manual deletion cannot cause reuse. */
function nextId(prefix, cfg) {
  let max = 0;
  for (const e of allEntries(cfg)) {
    const m = (e.id || '').match(new RegExp(`^${prefix}-(\\d+)$`));
    if (m) max = Math.max(max, parseInt(m[1], 10));
  }
  return `${prefix}-${String(max + 1).padStart(4, '0')}`;
}

// ------------------------------------------------------------------ doctor

function doctor(cfg, args) {
  const errors = [];
  const warnings = [];

  // 1. Required files exist.
  for (const f of cfg.required) {
    if (!existsSync(join(ROOT, f))) errors.push(`missing required file: ${f}`);
  }

  // 2. Template placeholders outside entries (entry-level ones are reported by id below).
  for (const f of cfg.required) {
    const t = read(f);
    if (!t) continue;
    const { head } = parseEntries(t);
    if (HAS_FILL_ME.test(withoutCode(cfg.logs[f] ? head : t))) {
      errors.push(`${f} still has a bitacora:fill-me placeholder — write the real thing or delete the block`);
    }
  }

  // 3. Context budget.
  for (const [file, spec] of Object.entries(cfg.logs)) {
    const t = read(file);
    if (t === null) continue;
    const n = t.split('\n').length;
    if (n > spec.maxLines) {
      errors.push(
        `${file} is ${n} lines, budget is ${spec.maxLines} — run "rotate", which archives from the bottom until it fits` +
          ` (if it is already down to ${plural(MIN_KEEP, 'entry')}, raise maxLines in bitacora.config.json instead)`
      );
    } else if (n > spec.maxLines * 0.85) {
      warnings.push(
        `${file} is at ${Math.round((n / spec.maxLines) * 100)}% of its ${spec.maxLines}-line budget` +
          ' — rotate starts archiving from the bottom once it crosses, and does nothing before that'
      );
    }
  }

  // 4. Entries: metadata, unique ids across live and archive, and real content.
  const seen = new Map();
  for (const e of allEntries(cfg)) {
    if (e.id && !seen.has(e.id)) seen.set(e.id, e.file);
    else if (e.id) errors.push(`duplicate id ${e.id} (${seen.get(e.id)} and ${e.file})`);
  }

  for (const [file, spec] of Object.entries(cfg.logs)) {
    const { entries } = parseEntries(read(file));
    let previous = null;

    entries.forEach((e, n) => {
      const where = `${file} ${e.id || `entry #${n + 1}`} ("${e.title}")`;

      if (!e.id) errors.push(`${where} has no id`);
      else if (!new RegExp(`^${spec.prefix}-\\d{4}$`).test(e.id))
        errors.push(`${where} has id "${e.id}", expected ${spec.prefix}-0000 form`);

      const age = e.date ? daysSince(e.date) : null;
      if (age === null) errors.push(`${where} has an unparseable date: "${e.date}"`);
      else if (age < 0) errors.push(`${where} is dated in the future ("${e.date}") — probably a typo`);

      if (e.tags.length === 0) errors.push(`${where} has no tags — recall cannot find it`);
      else if (e.tags.includes('example'))
        warnings.push(`${where} is still tagged "example" — delete the template entry once you have a real one`);

      if (spec.severity) {
        if (!e.severity) errors.push(`${where} has no severity (${SEVERITIES.join(' | ')})`);
        else if (!SEVERITIES.includes(e.severity))
          errors.push(`${where} has severity "${e.severity}", expected one of ${SEVERITIES.join(' | ')}`);
      }

      for (const f of e.files) {
        if (!existsSync(join(ROOT, f))) warnings.push(`${where} references ${f}, which no longer exists`);
      }

      // The check the whole method rests on: an entry with a missing or
      // gestural Guardrail is a complaint, and a log of complaints compounds
      // into nothing.
      if (HAS_FILL_ME.test(withoutCode(e.raw))) {
        errors.push(`${where} still has unfilled bitacora:fill-me blocks`);
      } else {
        for (const name of spec.sections || []) {
          const body = sectionBody(e.raw, name);
          if (body === null) errors.push(`${where} has no "**${name}.**" section`);
          else if (body.length < MIN_SECTION_CHARS)
            errors.push(
              `${where} has a ${body.length ? 'near-empty' : 'blank'} "${name}" section` +
                (name === 'Guardrail'
                  ? ' — name the check, test, type or refusal that makes this impossible to repeat, not an intention to be careful'
                  : ` — under ${MIN_SECTION_CHARS} characters is a gesture, not a thought`)
            );
        }
      }

      // rotate retires from the bottom, so the ordering invariant is load-bearing.
      if (previous && e.date && previous.date && Date.parse(e.date) > Date.parse(previous.date)) {
        errors.push(`${where} is dated after ${previous.id} above it — entries run newest first, and rotate archives from the bottom`);
      }
      previous = e;
    });
  }

  // 5. STATE.md is a fresh snapshot, not a diary.
  const stateText = read(cfg.state.file);
  if (stateText) {
    const m = stateText.match(/^updated:\s*(\S+)/m);
    if (!m) {
      errors.push(`${cfg.state.file} has no "updated: YYYY-MM-DD" line`);
    } else {
      const age = daysSince(m[1]);
      if (age === null) errors.push(`${cfg.state.file} updated date is unparseable: "${m[1]}"`);
      else if (age < 0) errors.push(`${cfg.state.file} is dated in the future ("${m[1]}")`);
      else if (age > cfg.state.maxAgeDays)
        warnings.push(
          `${cfg.state.file} was last updated ${plural(age, 'day')} ago (limit ${cfg.state.maxAgeDays})` +
            ' — a stale snapshot is worse than none, because your agent cannot tell'
        );
    }
    const n = stateText.split('\n').length;
    if (n > cfg.state.maxLines)
      errors.push(
        `${cfg.state.file} is ${n} lines, budget is ${cfg.state.maxLines} — it is a snapshot, not a diary.` +
          ' Move the history into the logs, where recall can find it by tag.'
      );
  }

  // 6. The context leak: one eager import quietly undoes the whole design.
  const claude = read('CLAUDE.md');
  if (claude) {
    for (const file of Object.keys(cfg.logs)) {
      if (new RegExp(`^\\s*@${file.replace('.', '\\.')}\\s*$`, 'm').test(claude)) {
        errors.push(`CLAUDE.md does "@${file}" — that loads the whole log into every session. Let the agent recall by tag instead.`);
      }
    }
  }

  for (const w of warnings) console.log(`${C.yellow('warn')}  ${w}`);
  for (const e of errors) console.log(`${C.red('error')} ${e}`);

  if (errors.length === 0) {
    const counts = Object.keys(cfg.logs)
      .map((f) => plural(parseEntries(read(f)).entries.length, kindOf(f)))
      .join(', ');
    console.log(`${C.green('ok')}    bitacora is healthy ${C.dim(`(${counts})`)}`);
  }

  const strictFail = args.strict && warnings.length > 0;
  if (strictFail) console.log(C.dim('--strict: failing on warnings'));
  process.exit(errors.length > 0 || strictFail ? 1 : 0);
}

// --------------------------------------------------------------------- new

const BODY = {
  mistake: [
    '**What happened.** <!-- bitacora:fill-me one paragraph, concrete, no blame -->',
    '',
    '**Root cause.** <!-- bitacora:fill-me why it was possible, not just what broke -->',
    '',
    '**Guardrail.** <!-- bitacora:fill-me the check, test, type or refusal that makes this impossible to repeat. Not "be careful" -->',
  ],
  learning: [
    '**What worked.** <!-- bitacora:fill-me -->',
    '',
    '**Why it worked.** <!-- bitacora:fill-me the transferable part -->',
    '',
    '**Reuse it when.** <!-- bitacora:fill-me the trigger that should bring this back -->',
  ],
  decision: [
    '**Context.** <!-- bitacora:fill-me the forces in play at the time -->',
    '',
    '**Decision.** <!-- bitacora:fill-me -->',
    '',
    '**Consequences.** <!-- bitacora:fill-me what this makes easy, and what it makes expensive -->',
  ],
};

function newEntry(cfg, args) {
  const kind = args._[0];
  const title = args._.slice(1).join(' ');
  if (!KIND_TO_FILE[kind] || !title) {
    fail('usage: new <mistake|learning|decision> "<title>" [--tags a,b] [--severity low|medium|high] [--files a.ts,b.ts]');
  }

  const file = KIND_TO_FILE[kind];
  const spec = cfg.logs[file];
  const text = read(file);
  if (text === null) fail(`${file} does not exist. Run the installer first.`);

  const tags = parseList(args.tags);
  if (tags.length === 0) fail('--tags is required: an untagged entry is an entry nobody will ever recall.');

  const severity = args.severity || 'medium';
  if (spec.severity && !SEVERITIES.includes(severity)) {
    fail(`--severity must be one of ${SEVERITIES.join(' | ')}`);
  }

  const meta = [`id: ${nextId(spec.prefix, cfg)}`, `date: ${today()}`, `tags: [${tags.join(', ')}]`];
  if (spec.severity) meta.push(`severity: ${severity}`);
  const files = parseList(args.files);
  if (files.length) meta.push(`files: [${files.join(', ')}]`);

  const { head, entries, tail } = parseEntries(text);
  const entry = [MARKER, ...meta, '-->', `### ${title}`, '', ...BODY[kind], '', ''].join('\n');

  // Newest first: the agent reads top-down and stops early.
  writeFileSync(join(ROOT, file), head + entry + entries.map((e) => e.raw).join('') + tail);

  const id = meta[0].slice(4);
  console.log(`${C.green('added')} ${id} to ${file} ${C.dim(`[${tags.join(', ')}]`)}`);
  console.log(C.dim('Now fill the blocks. doctor fails while they are unwritten, and again if the'));
  console.log(C.dim(`${kind === 'mistake' ? 'Guardrail' : (cfg.logs[file].sections || []).slice(-1)[0]} section says nothing.`));
}

// ------------------------------------------------------------------ recall

function recall(cfg, args) {
  const needle = (args._[0] || '').toLowerCase();
  if (!needle) fail('usage: recall <tag-or-path-or-phrase> [--brief]');

  // Ranked, because an exact tag match is a different kind of hit from a word
  // that happens to appear in someone's prose.
  const hits = [];
  for (const e of allEntries(cfg)) {
    let score = 0;
    if (e.tags.some((t) => t.toLowerCase() === needle)) score = 3;
    else if (e.files.some((f) => f.toLowerCase().includes(needle))) score = 2;
    else if (e.title.toLowerCase().includes(needle)) score = 2;
    else if (e.raw.toLowerCase().includes(needle)) score = 1;
    if (score) hits.push({ ...e, score });
  }
  hits.sort((a, b) => b.score - a.score || String(b.date).localeCompare(String(a.date)));

  if (hits.length === 0) {
    // The loop closes here or nowhere. A miss is not a dead end: it is the
    // exact moment a future entry is born, so hand over the command rather
    // than leaving the agent to remember that it exists.
    console.log(C.dim(`nothing logged under "${needle}" yet.`));
    console.log(C.dim('An area with no history is one where the first mistake has not been made yet,'));
    console.log(C.dim('which makes it more likely to happen here, not less. When it does:'));
    console.log('');
    console.log(C.dim(`  node .bitacora/cli.mjs new mistake "<what happened>" --tags ${needle} --severity high`));
    console.log('');
    return;
  }

  const brief = Boolean(args.brief);
  const full = brief ? [] : hits.slice(0, RECALL_FULL);
  const listed = brief ? hits : hits.slice(RECALL_FULL);

  for (const h of full) {
    console.log(C.dim(`─── ${h.file}${h.archived ? ' (archived)' : ''} ───`));
    console.log(h.raw.replace(/^<!-- bitacora:entry[\s\S]*?-->\n/, '').trim());
    console.log(C.dim(`      ${h.id} · ${h.date} · [${h.tags.join(', ')}]${h.severity ? ` · ${h.severity}` : ''}`));
    console.log('');
  }

  if (listed.length) {
    if (full.length) console.log(C.dim(`${plural(listed.length, 'further match')}, titles only:`));
    for (const h of listed) {
      console.log(`${C.bold(`${h.id}  ${h.title}`)}${h.archived ? C.dim(' (archived)') : ''}`);
      console.log(C.dim(`      ${h.file} · ${h.date} · [${h.tags.join(', ')}]`));
    }
    console.log('');
  }

  console.log(C.dim(`${plural(hits.length, 'entry')} for "${needle}". Honour the guardrails; if one looks wrong, say so rather than routing around it.`));
}

// ------------------------------------------------------------------ rotate

function rotate(cfg, args) {
  const dry = Boolean(args['dry-run']);
  let moved = 0;

  for (const [file, spec] of Object.entries(cfg.logs)) {
    const text = read(file);
    if (text === null) continue;
    const { head, entries, tail } = parseEntries(text);

    // Defensive: doctor enforces newest-first, so this sort is normally a
    // no-op. It stops a hand-inserted entry from being archived by position.
    const ordered = [...entries].sort((a, b) => String(b.date).localeCompare(String(a.date)));

    // Two budgets, and the line budget is the one that bites first: entries
    // long enough to be worth keeping blow through maxLines well before they
    // reach keepEntries. Retire on whichever binds (M-0012).
    const lines = (n) =>
      (head + ordered.slice(0, n).map((e) => e.raw).join('') + `\n${ARCHIVE_HEADING}\n\n\n\n` + '-\n'.repeat(ordered.length - n))
        .split('\n').length;

    let keepCount = Math.min(ordered.length, spec.keepEntries);
    while (keepCount > MIN_KEEP && lines(keepCount) > spec.maxLines) keepCount--;
    if (keepCount >= ordered.length) continue;

    const keep = ordered.slice(0, keepCount);
    const retire = ordered.slice(keepCount);

    const byYear = new Map();
    for (const e of retire) {
      const year = (e.date || today()).slice(0, 4);
      if (!byYear.has(year)) byYear.set(year, []);
      byYear.get(year).push(e);
    }

    const fresh = [];
    for (const [year, group] of byYear) {
      const rel = `${cfg.archiveDir}/${kindOf(file)}s-${year}.md`;
      const target = join(ROOT, rel);
      if (!dry) {
        const header = `# ${file.replace('.md', '')} — ${year}\n\n> Archived by bitacora. Still searchable with "recall".\n\n`;
        mkdirSync(dirname(target), { recursive: true });
        writeFileSync(target, (existsSync(target) ? readFileSync(target, 'utf8') : header) + group.map((e) => e.raw).join(''));
      }
      for (const e of group) fresh.push({ id: e.id, line: `- \`${e.id}\` ${e.title} — [${e.tags.join(', ')}] → \`${rel}\`` });
      moved += group.length;
    }

    // Rebuild the index from the ids it already lists, so repeated rotates
    // cannot duplicate a line.
    const previous = tail
      .split('\n')
      .map((l) => ({ id: (l.match(/^- `([A-Z]-\d{4})`/) || [])[1], line: l }))
      .filter((x) => x.id);
    const index = [...fresh, ...previous].filter((x, i, all) => all.findIndex((y) => y.id === x.id) === i);

    const newTail = [
      `\n${ARCHIVE_HEADING}`,
      '',
      'Older entries, one line each. `recall` still searches them in full.',
      '',
      ...index.map((x) => x.line),
      '',
    ].join('\n');

    if (!dry) writeFileSync(join(ROOT, file), head + keep.map((e) => e.raw).join('') + newTail);
    console.log(`${dry ? C.yellow('would move') : C.green('moved')} ${plural(retire.length, 'entry')} out of ${file}`);
  }

  if (moved === 0) console.log(C.dim('nothing to rotate — every log is within its entry budget'));
  else if (!dry) console.log(C.dim(`\n${plural(moved, 'entry')} archived. Run doctor to confirm the live files are back inside budget.`));
}

// ------------------------------------------------------------------- stats

function stats(cfg) {
  const tally = new Map();
  let total = 0;
  let recent = 0;

  for (const e of allEntries(cfg).filter((e) => !e.archived)) {
    total++;
    const age = e.date ? daysSince(e.date) : null;
    const isRecent = age !== null && age >= 0 && age <= RECENT_DAYS;
    if (isRecent) recent++;
    for (const t of e.tags) {
      const row = tally.get(t) || { all: 0, recent: 0 };
      row.all++;
      if (isRecent) row.recent++;
      tally.set(t, row);
    }
  }

  if (total === 0) return console.log(C.dim('no entries yet'));

  const ranked = [...tally.entries()].sort((a, b) => b[1].recent - a[1].recent || b[1].all - a[1].all).slice(0, 12);
  const width = Math.max(...ranked.map(([t]) => t.length));
  const top = Math.max(...ranked.map(([, r]) => r.all));

  console.log(C.bold(`\n${plural(total, 'entry')} in the live logs, ${recent} from the last ${RECENT_DAYS} days. Where the friction is:\n`));
  for (const [tag, row] of ranked) {
    const bar = '█'.repeat(Math.max(1, Math.round((row.all / top) * 26)));
    const note = row.recent === row.all ? '' : C.dim(` (${row.recent} recent)`);
    console.log(`  ${tag.padEnd(width)}  ${bar} ${row.all}${note}`);
  }
  console.log(C.dim('\nRanked by recent activity. The top tag is usually one missing abstraction, told n times.\n'));
}

// -------------------------------------------------------------------- main

function parseArgv(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const [k, v] = a.slice(2).split('=');
      out[k] = v !== undefined ? v : argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
    } else out._.push(a);
  }
  return out;
}

const [, , cmd, ...rest] = process.argv;
const args = parseArgv(rest);
const cfg = loadConfig();

switch (cmd) {
  case 'doctor': doctor(cfg, args); break;
  case 'new': newEntry(cfg, args); break;
  case 'recall': recall(cfg, args); break;
  case 'rotate': rotate(cfg, args); break;
  case 'stats': stats(cfg); break;
  default:
    console.log(`bitacora — the logbook your coding agent keeps

  doctor [--strict]                   structure, freshness, context budget, and whether
                                      entries actually say anything
  new <kind> "<title>" --tags a,b     add an entry (kind: mistake | learning | decision)
  recall <tag> [--brief]              pull only the entries that matter right now
  rotate [--dry-run]                  archive old entries, keep the live files lean
  stats                               where your friction actually is
`);
    process.exit(cmd ? 1 : 0);
}
