'use strict';
// Runs the target editor's shipped script (dashboard._EDITOR_SCRIPT, written to a
// file by the caller) against a DOM stub just large enough for it, and prints
// what each scenario observed as JSON. The assertions live in
// tests/test_editor_state.py; this file only drives the page and reports.
//
//   node tests/editor_harness.js <script-file>
//
// The stub is deliberately dumb: no layout, no events beyond the ones the script
// registers. A disabled control ignores click(), as a browser does, and the
// selector engine throws on anything it does not understand, so a selector the
// script starts using fails loudly here instead of silently matching nothing.

const fs = require('fs');
const vm = require('vm');

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.handlers = {};
    this.onclick = null;
    this.className = '';
    this.id = '';
    this.value = '';
    this.textContent = '';
    this.type = '';
    this.checked = false;
    this.disabled = false;
    this.open = false;
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  replaceChildren(...kids) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    kids.forEach((k) => this.appendChild(k));
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  querySelectorAll(selector) { return queryAll(this, selector); }
}

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const part = /^([a-zA-Z][\w-]*)|^#([\w-]+)|^\.([\w-]+)|^\[([\w-]+)(?:=("?)([^\]"]*)\5)?\]/;
  let rest = text;
  while (rest) {
    const m = part.exec(rest);
    if (!m) throw new Error('stub: unsupported selector fragment ' + JSON.stringify(rest));
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else out.attrs.push([m[4], m[6]]);
    rest = rest.slice(m[0].length);
  }
  return out;
}

function attrOf(el, name) {
  if (name.startsWith('data-')) {
    const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return el.dataset[key];
  }
  return el[name];
}

function matches(el, c) {
  if (c.tag && String(el.tagName).toLowerCase() !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  const have = String(el.className).split(/\s+/);
  if (!c.classes.every((k) => have.includes(k))) return false;
  return c.attrs.every(([name, val]) => {
    const got = attrOf(el, name);
    return val === undefined ? (got !== undefined && got !== false && got !== null)
                             : String(got) === val;
  });
}

function matchesChain(el, chain) {
  if (!matches(el, chain[chain.length - 1])) return false;
  let at = chain.length - 2;
  for (let up = el.parentNode; at >= 0 && up; up = up.parentNode) {
    if (matches(up, chain[at])) at--;
  }
  return at < 0;
}

function descendants(root) {
  const out = [];
  (function walk(node) { node.children.forEach((k) => { out.push(k); walk(k); }); })(root);
  return out;
}

function queryAll(root, selector) {
  const chains = selector.split(',').map((g) => g.trim().split(/\s+/).map(parseCompound));
  return descendants(root).filter((el) => chains.some((ch) => matchesChain(el, ch)));
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => { if (body instanceof Error) throw body; return body; },
  };
}

function boot(scriptText) {
  const body = new El('body');
  const byId = {};
  const BUTTONS = new Set(['eadd', 'esave']);
  const document = {
    getElementById(id) {
      if (!byId[id]) {
        const el = new El(BUTTONS.has(id) ? 'button' : id === 'etok' ? 'input' : id === 'editor' ? 'details' : 'div');
        el.id = id;
        byId[id] = el;
        body.appendChild(el);
      }
      return byId[id];
    },
    createElement: (tag) => new El(tag),
    createTextNode: (text) => { const el = new El('#text'); el.textContent = text; return el; },
    querySelectorAll: (selector) => queryAll(body, selector),
  };

  const calls = [];
  const confirms = [];
  let confirmAnswer = true;
  const store = {};
  const sandbox = {
    document,
    fetch: (url, opts) => new Promise((resolve, reject) => {
      calls.push({ url, opts: opts || {}, resolve, reject });
    }),
    sessionStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    confirm: (text) => { confirms.push(text); return confirmAnswer; },
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptText, sandbox);

  const el = (id) => document.getElementById(id);
  const h = {
    sandbox, calls, confirms, el,
    setConfirm: (answer) => { confirmAnswer = answer; },
    setToken: (text) => { el('etok').value = text; },
    rows: () => el('erows').children.filter((c) => c.className === 'erow'),
    urlInput: (row) => row.children[0],
    open() { const e = el('editor'); e.open = true; e.handlers.toggle.call(e); },
    click(id) {
      const target = el(id);
      if (target.disabled) return false;
      if (target.onclick) target.onclick();
      return true;
    },
    async settle(index, status, payload) { calls[index].resolve(response(status, payload)); await tick(); },
    async reject(index, error) { calls[index].reject(error); await tick(); },
    // The headers arrive now and the body later: returns the function that
    // delivers the body.
    async settleJsonLater(index, status) {
      let release;
      const body = new Promise((resolve) => { release = resolve; });
      calls[index].resolve({ ok: status >= 200 && status < 300, status, json: () => body });
      await tick();
      return async (payload) => { release(payload); await tick(); };
    },
    puts: () => calls.filter((c) => (c.opts.method || 'GET') === 'PUT'),
    view() {
      const rows = h.rows();
      const controls = queryAll(el('erows'), 'input,button');
      return {
        save: el('esave').disabled,
        add: el('eadd').disabled,
        urls: rows.map((r) => h.urlInput(r).value),
        controlsDisabled: controls.length > 0 && controls.every((c) => c.disabled),
        controlsEnabled: controls.length > 0 && controls.every((c) => !c.disabled),
        msg: el('emsg').textContent,
        msgClass: el('emsg').className,
        fetches: calls.length,
        puts: h.puts().length,
        putBodies: h.puts().map((c) => JSON.parse(c.opts.body)),
        confirms: confirms.length,
      };
    },
  };
  return h;
}

const listing = (urls, extra) => Object.assign(
  { managed: true, writable: true, seed_error: null, targets: urls.map((url) => ({ url })) },
  extra || {});

const scenarios = {
  async initial(h) { return h.view(); },

  async loading(h) { h.open(); await tick(); return h.view(); },

  async http_error(h) { h.open(); await h.settle(0, 500, {}); return h.view(); },

  // A store that exists but cannot be used: the API answers 500 with the reason
  // and no `targets` key.
  async http_500_with_a_reason(h) {
    h.open();
    await h.settle(0, 500, {
      error: 'data/targets.json is present but unusable',
      store_error: 'invalid JSON at line 3',
    });
    return h.view();
  },

  async http_500_with_a_non_json_body(h) {
    h.open(); await h.settle(0, 500, new SyntaxError('Unexpected token <')); return h.view();
  },

  async http_500_with_a_hostile_reason(h) {
    h.open();
    await h.settle(0, 500, { error: '<img src=x onerror=alert(1)>', store_error: 42 });
    return h.view();
  },

  async stale_500_reason_is_not_painted(h) {
    h.open(); h.open();
    await h.settle(1, 200, listing(['https://newer.example']));
    await h.settle(0, 500, { error: 'old failure', store_error: 'old reason' });
    return h.view();
  },

  async refusal_body_arrives_after_a_newer_load_started(h) {
    h.open();
    const deliver = await h.settleJsonLater(0, 500);
    h.open();
    await deliver({ error: 'old failure', store_error: 'old reason' });
    const during = h.view();
    await h.settle(1, 200, listing(['https://newer.example']));
    return { during, after: h.view() };
  },

  async save_refused_with_a_reason(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example']));
    h.setToken('tok');
    h.click('esave'); await tick();
    await h.settle(1, 409, {
      error: 'data/targets.json is present but unusable',
      store_error: 'invalid JSON at line 3',
    });
    return h.view();
  },

  async network_error(h) {
    h.open(); await h.reject(0, new TypeError('Failed to fetch')); return h.view();
  },

  async not_json(h) { h.open(); await h.settle(0, 200, new SyntaxError('Unexpected token <')); return h.view(); },

  async shape_missing_targets(h) { h.open(); await h.settle(0, 200, { managed: true }); return h.view(); },

  async shape_targets_not_a_list(h) {
    h.open(); await h.settle(0, 200, { targets: 'nope' }); return h.view();
  },

  async shape_null_entry(h) {
    h.open(); await h.settle(0, 200, { targets: [null] }); return h.view();
  },

  async shape_entry_without_url(h) {
    h.open(); await h.settle(0, 200, { targets: [{ timeout_s: 5 }] }); return h.view();
  },

  // The reproduction of the defect: the first load fails, the operator adds a
  // row and presses Save. Both the click and a direct call are tried, because a
  // disabled button is the first line and the function's own guard the second.
  async failed_load_then_add_and_save(h) {
    h.open(); await h.settle(0, 500, {});
    h.setToken('tok');
    const addClicked = h.click('eadd');
    h.el('eadd').onclick();
    const added = h.rows();
    if (added.length) h.urlInput(added[0]).value = 'https://typed.example';
    const saveClicked = h.click('esave');
    h.sandbox.esave();
    await tick();
    return Object.assign(h.view(), { addClicked, saveClicked });
  },

  async success(h) {
    h.open(); await tick();
    const during = h.view();
    await h.settle(0, 200, listing(['https://a.example', 'https://b.example']));
    return { during, after: h.view() };
  },

  async retry_recovers(h) {
    h.open(); await h.settle(0, 500, {});
    const failed = h.view();
    h.open(); await tick();
    const retrying = h.view();
    await h.settle(1, 200, listing(['https://a.example']));
    return { failed, retrying, recovered: h.view() };
  },

  async stale_success_after_newer(h) {
    h.open(); h.open();
    await h.settle(1, 200, listing(['https://newer.example']));
    await h.settle(0, 200, listing(['https://older-1.example', 'https://older-2.example']));
    return h.view();
  },

  async stale_failure_after_newer(h) {
    h.open(); h.open();
    await h.settle(1, 200, listing(['https://newer.example']));
    await h.settle(0, 500, {});
    return h.view();
  },

  async stale_success_while_newer_pending(h) {
    h.open(); h.open();
    await h.settle(0, 200, listing(['https://older.example']));
    return h.view();
  },

  async stale_failure_does_not_poison_newer(h) {
    h.open(); h.open();
    await h.reject(0, new TypeError('Failed to fetch'));
    const between = h.view();
    await h.settle(1, 200, listing(['https://newer.example']));
    return { between, after: h.view() };
  },

  async edits_survive_a_reopen(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example', 'https://b.example']));
    h.urlInput(h.rows()[0]).value = 'https://a-edited.example';
    h.click('eadd');
    h.open(); await tick();
    return h.view();
  },

  async clean_reopen_reloads(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example']));
    h.open(); await tick();
    const during = h.view();
    await h.settle(1, 200, listing(['https://a.example', 'https://c.example']));
    return { during, after: h.view() };
  },

  async reload_failure_blocks_save(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example']));
    h.open(); await h.settle(1, 500, {});
    return h.view();
  },

  async empty_list_declined(h) {
    h.open(); await h.settle(0, 200, listing([]));
    h.setToken('tok'); h.setConfirm(false);
    h.click('esave'); await tick();
    return h.view();
  },

  async empty_list_confirmed(h) {
    h.open(); await h.settle(0, 200, listing([]));
    h.setToken('tok'); h.setConfirm(true);
    h.click('esave'); await tick();
    return h.view();
  },

  async save_sends_the_loaded_list(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example']));
    h.setToken('tok');
    h.click('esave'); await tick();
    return h.view();
  },

  async saved_list_reloads_on_reopen(h) {
    h.open(); await h.settle(0, 200, listing(['https://a.example']));
    h.urlInput(h.rows()[0]).value = 'https://a-edited.example';
    h.setToken('tok');
    h.click('esave'); await tick();
    await h.settle(1, 200, listing(['https://a-edited.example']));
    h.open(); await tick();
    return h.view();
  },
};

(async () => {
  const script = fs.readFileSync(process.argv[2], 'utf8');
  const out = {};
  for (const [name, run] of Object.entries(scenarios)) {
    try {
      out[name] = await run(boot(script));
    } catch (error) {
      out[name] = { error: String(error && error.stack || error) };
    }
  }
  process.stdout.write(JSON.stringify(out));
})();
