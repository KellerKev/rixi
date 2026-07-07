# ruff: noqa
# PyLevate source — compiled to Preact by `pylevate build`, not executed as Python. Names like
# `h`, `state`, `fetch`, `localStorage`, `JSON` are compiler builtins / JS globals.
"""rixi gateway console — written in PyLevate (Python → Preact), served by management.py.

A single-component admin app: paste a JWT, then browse live nodes/resources/sessions, view + edit
the policy (admin), and query the audit log. All data comes from the gateway's /api/* management
API (same origin); the token is sent as `Authorization: Bearer` and kept in localStorage.
"""
from pylevate import Component, css, h, mount, state


class App(Component):

    token = state('')
    tab = state('dashboard')
    nodes = state([])
    resources = state([])
    sessions = state([])
    policy_text = state('')
    audit = state([])
    f_actor = state('')
    f_event = state('')
    f_decision = state('')
    msg = state('')

    style = css("""
        .app { max-width: 1100px; margin: 0 auto; padding: 1.5rem 2rem; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; color: #1f2330; }
        .top { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.25rem; }
        .brand { font-size: 1.4rem; font-weight: 700; color: #5c6bc0; margin-right: auto; }
        .tok { flex: 0 1 360px; padding: 0.5rem 0.7rem; border: 1px solid #ccd; border-radius: 6px; font-family: ui-monospace, monospace; font-size: 0.85rem; }
        .btn { background: #5c6bc0; color: #fff; border: none; border-radius: 6px; padding: 0.5rem 1rem; cursor: pointer; font-size: 0.9rem; }
        .btn:hover { background: #3f51b5; }
        .btn.ghost { background: #eef0f7; color: #3a3f55; }
        .tabs { display: flex; gap: 0.25rem; border-bottom: 1px solid #e3e6f0; margin-bottom: 1rem; }
        .tabbtn { padding: 0.6rem 1rem; border: none; background: none; cursor: pointer; color: #6b7090; font-size: 0.95rem; border-bottom: 2px solid transparent; }
        .tabbtn.active { color: #5c6bc0; border-bottom-color: #5c6bc0; font-weight: 600; }
        .msg { min-height: 1.2rem; color: #c0392b; font-size: 0.85rem; margin-bottom: 0.5rem; }
        h2 { font-size: 1rem; margin: 1.25rem 0 0.5rem; color: #3a3f55; }
        table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 0.5rem; }
        th { text-align: left; color: #8a90ab; font-weight: 600; padding: 0.45rem 0.6rem; border-bottom: 1px solid #eceef6; }
        td { padding: 0.45rem 0.6rem; border-bottom: 1px solid #f3f4fa; font-family: ui-monospace, monospace; }
        .deny { color: #c0392b; }
        .allow { color: #2e7d32; }
        textarea { width: 100%; height: 320px; font-family: ui-monospace, monospace; font-size: 0.82rem; padding: 0.75rem; border: 1px solid #ccd; border-radius: 8px; }
        .hint { color: #8a90ab; font-size: 0.85rem; }
    """)

    def on_mount(self):
        self.token = localStorage.getItem('rixi_tok') or ''
        if self.token:
            self.refresh()

    # -- data ----------------------------------------------------------------
    async def _get(self, path):
        r = await fetch(path, {'headers': {'Authorization': 'Bearer ' + self.token}})
        if not r.ok:
            self.msg = path + ' → HTTP ' + r.status
            return None
        return await r.json()

    async def refresh(self):
        self.msg = ''
        n = await self._get('/api/nodes')
        if n:
            self.nodes = n['nodes']
        rs = await self._get('/api/resources')
        if rs:
            self.resources = rs['resources']
        ss = await self._get('/api/sessions')
        if ss:
            self.sessions = ss['sessions']
        p = await self._get('/api/policy')
        if p:
            self.policy_text = JSON.stringify(p['global'], None, 2)
        await self.load_audit()

    async def load_audit(self):
        q = '/api/audit?limit=200'
        if self.f_actor:
            q += '&actor=' + self.f_actor
        if self.f_event:
            q += '&event=' + self.f_event
        if self.f_decision:
            q += '&decision=' + self.f_decision
        a = await self._get(q)
        if a:
            self.audit = a['events']

    async def teardown(self, name):
        r = await fetch('/api/resources/' + name + '/teardown',
                        {'method': 'POST', 'headers': {'Authorization': 'Bearer ' + self.token}})
        self.msg = ('tore down ' + name) if r.ok else ('teardown failed → HTTP ' + r.status)
        await self.refresh()

    async def provision(self, name):
        r = await fetch('/api/resources/' + name + '/provision',
                        {'method': 'POST', 'headers': {'Authorization': 'Bearer ' + self.token}})
        self.msg = ('provisioning ' + name) if r.ok else ('provision failed → HTTP ' + r.status)
        await self.refresh()

    async def save_policy(self):
        txt = self.policy_text
        r = await fetch('/api/policy', {
            'method': 'PUT',
            'headers': {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
            'body': JSON.stringify({'global': JSON.parse(txt)}),
        })
        self.msg = 'policy saved' if r.ok else ('save failed → HTTP ' + r.status)

    # -- template ------------------------------------------------------------
    template = {
        h.div(Class='app'): {
            h.div(Class='top'): {
                h.span(Class='brand'): '🛡️ rixi gateway',
                h.input(Class='tok', placeholder='paste JWT…', value={'self.token'},
                        onInput={'e => { self.token = e.target.value; localStorage.setItem("rixi_tok", e.target.value); }'}): None,
                h.button(Class='btn', onClick={'self.refresh'}): 'Connect',
            },
            h.div(Class='tabs'): {
                h.button(Class={'self.tab === "dashboard" ? "tabbtn active" : "tabbtn"'},
                         onClick={'() => self.tab = "dashboard"'}): 'Dashboard',
                h.button(Class={'self.tab === "policy" ? "tabbtn active" : "tabbtn"'},
                         onClick={'() => self.tab = "policy"'}): 'Policy',
                h.button(Class={'self.tab === "audit" ? "tabbtn active" : "tabbtn"'},
                         onClick={'() => self.tab = "audit"'}): 'Audit',
            },
            h.div(Class='msg'): '[[self.msg]]',

            # ---- Dashboard ----
            h.Template(If={'self.tab === "dashboard"'}): {
                h.div(): {
                    h.h2(): 'Nodes',
                    h.table(): {
                        h.thead(): {h.tr(): {h.th(): 'node_id', h.th(): 'kind', h.th(): 'port'}},
                        h.tbody(): {
                            h.Template(For='n in self.nodes'): {
                                h.tr(): {h.td(): '[[n["node_id"]]]', h.td(): '[[n["kind"]]]',
                                         h.td(): '[[n["port"]]]'},
                            },
                        },
                    },
                    h.h2(): 'Resources',
                    h.table(): {
                        h.thead(): {h.tr(): {h.th(): 'name', h.th(): 'provider', h.th(): 'reuse',
                                             h.th(): 'status', h.th(): 'up', h.th(): 'actions'}},
                        h.tbody(): {
                            h.Template(For='r in self.resources'): {
                                h.tr(): {h.td(): '[[r["name"]]]', h.td(): '[[r["provider"]]]',
                                         h.td(): '[[r["reuse"]]]', h.td(): '[[r["status"]]]',
                                         h.td(): '[[r["up"]]]',
                                         h.td(): {
                                             h.button(Class='btn ghost', onClick={'() => self.provision(r["name"])'}): 'Provision',
                                             h.button(Class='btn ghost', onClick={'() => self.teardown(r["name"])'}): 'Teardown',
                                         }},
                            },
                        },
                    },
                    h.h2(): 'Sessions',
                    h.table(): {
                        h.thead(): {h.tr(): {h.th(): 'client', h.th(): 'identity', h.th(): 'sessions'}},
                        h.tbody(): {
                            h.Template(For='s in self.sessions'): {
                                h.tr(): {h.td(): '[[s["client"]]]', h.td(): '[[s["identity"]]]',
                                         h.td(): '[[s["sessions"]]]'},
                            },
                        },
                    },
                },
            },

            # ---- Policy ----
            h.Template(If={'self.tab === "policy"'}): {
                h.div(): {
                    h.h2(): 'Global policy (edit + save; admin only)',
                    h.textarea(value={'self.policy_text'},
                               onInput={'e => self.policy_text = e.target.value'}): None,
                    h.div(): {
                        h.button(Class='btn', onClick={'self.save_policy'}): 'Save policy',
                    },
                },
            },

            # ---- Audit ----
            h.Template(If={'self.tab === "audit"'}): {
                h.div(): {
                    h.h2(): 'Audit log (most recent)',
                    h.div(Class='top'): {
                        h.input(Class='tok', placeholder='actor', value={'self.f_actor'},
                                onInput={'e => self.f_actor = e.target.value'}): None,
                        h.input(Class='tok', placeholder='event', value={'self.f_event'},
                                onInput={'e => self.f_event = e.target.value'}): None,
                        h.input(Class='tok', placeholder='decision (allow/deny)', value={'self.f_decision'},
                                onInput={'e => self.f_decision = e.target.value'}): None,
                        h.button(Class='btn', onClick={'self.load_audit'}): 'Apply',
                    },
                    h.table(): {
                        h.thead(): {h.tr(): {h.th(): 'ts', h.th(): 'event', h.th(): 'actor',
                                             h.th(): 'action', h.th(): 'decision', h.th(): 'reason'}},
                        h.tbody(): {
                            h.Template(For='e in self.audit'): {
                                h.tr(): {h.td(): '[[e["ts"]]]', h.td(): '[[e["event"]]]',
                                         h.td(): '[[e["actor"]]]', h.td(): '[[e["action"]]]',
                                         h.td(Class={'e["decision"] === "deny" ? "deny" : "allow"'}): '[[e["decision"]]]',
                                         h.td(): '[[e["reason"]]]'},
                            },
                        },
                    },
                },
            },
        }
    }


mount(App, '#app')
