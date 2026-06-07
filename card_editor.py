#!/usr/bin/env python3
"""
Card Forge — a visual editor for the Godot card system's deck.json.

Run:
    python3 card_editor.py            # then File > Open your deck.json
    python3 card_editor.py deck.json  # open a deck on startup

No third-party dependencies are required (pure Tkinter). If Pillow is installed
it will be used for image previews of more formats; otherwise PNG/GIF preview
still works via Tkinter.

------------------------------------------------------------------------------
EXTENDING IT
------------------------------------------------------------------------------
The editor is schema-driven. When you add a new effect type to your GDScript
resolver, add one entry to EFFECT_SCHEMA below and the editor gives you a form
for it automatically. Each field is (key, kind, extra):

    "text"          single-line string
    "int" / "num"   number (int, or int/float)
    "bool"          checkbox
    "choice"        dropdown, extra = list of options
    "scope"         editable dropdown of common scope strings
    "op"            add / mul / set
    "list_str"      comma-separated -> list of strings
    "card_ref"      one card id (dropdown of existing ids)
    "card_ref_list" several card ids
    "set_ref"       one set id
    "script_ref"    a function name from card_scripts.gd
    "modifier_list" a list of modifier dicts (nested editor)
    "effect"        a single nested effect (nested editor, e.g. delayed_effect)
    "raw"           free-form JSON (escape hatch for anything)

Any effect type NOT in EFFECT_SCHEMA is still fully editable as raw JSON, and
every form has a "Raw JSON" toggle, so the tool can never block you.
"""

import sys
import os
import re
import json
import copy
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk          # optional, nicer image support
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

CARD_TYPES = ["intel", "event", "decision"]
OP_PRESETS = ["add", "mul", "set"]
POSITION_PRESETS = ["front", "soon", "random", "back"]
SCOPE_PRESETS = [
    "all", "player", "enemy", "neutral", "ground", "combatant", "supplier",
    "city", "train", "type:infantry", "type:artillery", "type:logistics",
    "team:1", "team:2", "tag:",
]

# A modifier sub-dict (used inline by add_modifier and inside set_weather).
MODIFIER_FIELDS = [
    ("id", "text", None),
    ("stat", "text", None),
    ("op", "op", None),
    ("value", "num", None),
    ("scope", "scope", None),
    ("duration_days", "int", None),
    ("source", "text", None),
    ("tags", "list_str", None),
]

# effect type -> list of (key, kind, extra)
EFFECT_SCHEMA = {
    "set_weather": [
        ("name", "text", None),
        ("modifiers", "modifier_list", None),
    ],
    "add_modifier": [
        ("id", "text", None),
        ("stat", "text", None),
        ("op", "op", None),
        ("value", "num", None),
        ("scope", "scope", None),
        ("duration_days", "int", None),
        ("source", "text", None),
        ("tags", "list_str", None),
    ],
    "remove_modifier": [("id", "text", None)],
    "remove_modifiers_by_tag": [("tag", "text", None)],
    "inject_cards": [
        ("ids", "card_ref_list", None),
        ("position", "choice", POSITION_PRESETS),
    ],
    "remove_cards": [("ids", "card_ref_list", None)],
    "add_set": [("set", "set_ref", None)],
    "remove_set": [("set", "set_ref", None)],
    "delayed_card": [
        ("id", "card_ref", None),
        ("after_days", "int", None),
    ],
    "delayed_effect": [
        ("after_days", "int", None),
        ("effect", "effect", None),
    ],
    "spawn_unit": [
        ("unit_type", "text", None),
        ("team", "int", None),
        ("near", "text", None),
        ("count", "int", None),
    ],
    "transform_unit": [
        ("scope", "scope", None),
        ("to", "text", None),
    ],
    "grant_resources": [
        ("scope", "scope", None),
        ("amount", "num", None),
    ],
    "drain_resources": [
        ("scope", "scope", None),
        ("amount", "num", None),
    ],
    "script": [
        ("fn", "script_ref", None),
        ("params", "raw", None),
    ],
    "set_state": [("key", "text", None), ("value", "text", None)],
    "restore_state": [("key", "text", None)],
}

EFFECT_TYPES = sorted(EFFECT_SCHEMA.keys())


# ──────────────────────────────────────────────────────────────────────────────
# Data layer (no GUI — unit-testable)
# ──────────────────────────────────────────────────────────────────────────────

def parse_num(s):
    """'1' -> 1, '0.6' -> 0.6, '' -> 0, anything else returned as-is string."""
    s = str(s).strip()
    if s == "":
        return 0
    try:
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return s


def split_list(s):
    return [x.strip() for x in str(s).split(",") if x.strip()]


def effect_refs(effect):
    """Return (card_ids, set_ids, script_fns) a single effect points at."""
    cards, sets_, fns = [], [], []
    t = effect.get("type")
    if t in ("inject_cards", "remove_cards"):
        cards += list(effect.get("ids", []))
    elif t == "delayed_card":
        if effect.get("id"):
            cards.append(effect["id"])
    elif t in ("add_set", "remove_set"):
        if effect.get("set"):
            sets_.append(effect["set"])
    elif t == "script":
        if effect.get("fn"):
            fns.append(effect["fn"])
    elif t == "delayed_effect":
        c, s, f = effect_refs(effect.get("effect", {}) or {})
        cards += c; sets_ += s; fns += f
    return cards, sets_, fns


def card_effect_lists(card):
    """Yield every effects list belonging to a card (top-level and per-choice)."""
    if isinstance(card.get("effects"), list):
        yield card["effects"]
    for ck in ("choice_a", "choice_b"):
        ch = card.get(ck)
        if isinstance(ch, dict) and isinstance(ch.get("effects"), list):
            yield ch["effects"]


class Deck:
    def __init__(self):
        self.data = {"sets": {}}
        self.path = None

    # ---- io ----
    def load(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "sets" not in data or not isinstance(data["sets"], dict):
            raise ValueError("Not a recognised deck.json (missing top-level 'sets').")
        self.data = data
        self.path = path

    def save(self, path=None, indent=2):
        path = path or self.path
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
        self.path = path

    # ---- queries ----
    @property
    def sets(self):
        return self.data["sets"]

    def set_ids(self):
        return list(self.sets.keys())

    def cards_in(self, set_id):
        return self.sets[set_id].setdefault("cards", [])

    def iter_cards(self):
        for sid, s in self.sets.items():
            for c in s.get("cards", []):
                yield sid, c

    def all_card_ids(self):
        return [c.get("id", "") for _, c in self.iter_cards()]

    def find_card(self, card_id):
        for sid, c in self.iter_cards():
            if c.get("id") == card_id:
                return sid, c
        return None, None

    # ---- mutation ----
    def add_set(self, set_id, label="", startable=False):
        if set_id in self.sets:
            return False
        self.sets[set_id] = {"label": label, "startable": startable, "cards": []}
        return True

    def delete_set(self, set_id):
        self.sets.pop(set_id, None)

    def add_card(self, set_id, card_type="intel", card_id=None):
        if card_id is None:
            card_id = self._unique_id(card_type)
        card = {"id": card_id, "type": card_type, "text": "", "protected": False}
        if card_type == "decision":
            card["choice_a"] = {"label": "", "effects": []}
            card["choice_b"] = {"label": "", "effects": []}
        else:
            card["effects"] = []
        self.cards_in(set_id).append(card)
        return card

    def _unique_id(self, prefix):
        existing = set(self.all_card_ids())
        i = 1
        while f"{prefix}_{i:03d}" in existing:
            i += 1
        return f"{prefix}_{i:03d}"

    def delete_card(self, card_id):
        for s in self.sets.values():
            cards = s.get("cards", [])
            for i, c in enumerate(cards):
                if c.get("id") == card_id:
                    cards.pop(i)
                    return True
        return False

    def duplicate_card(self, card_id):
        sid, c = self.find_card(card_id)
        if not c:
            return None
        clone = copy.deepcopy(c)
        clone["id"] = self._unique_copy_id(c.get("id", "card"))
        self.cards_in(sid).append(clone)
        return clone

    def _unique_copy_id(self, base):
        existing = set(self.all_card_ids())
        cand = base + "_copy"
        i = 2
        while cand in existing:
            cand = f"{base}_copy{i}"
            i += 1
        return cand

    def move_card(self, card_id, to_set):
        sid, c = self.find_card(card_id)
        if not c or sid == to_set or to_set not in self.sets:
            return False
        self.cards_in(sid).remove(c)
        self.cards_in(to_set).append(c)
        return True

    def set_card_type(self, card, new_type):
        card["type"] = new_type
        if new_type == "decision":
            card.pop("effects", None)
            card.setdefault("choice_a", {"label": "", "effects": []})
            card.setdefault("choice_b", {"label": "", "effects": []})
        else:
            card.pop("choice_a", None)
            card.pop("choice_b", None)
            card.setdefault("effects", [])

    # ---- analysis ----
    def references_from(self, card):
        cards, sets_, fns = [], [], []
        for lst in card_effect_lists(card):
            for eff in lst:
                c, s, f = effect_refs(eff)
                cards += c; sets_ += s; fns += f
        return cards, sets_, fns

    def referenced_by(self, target_id):
        out = []
        for _, c in self.iter_cards():
            cards, _, _ = self.references_from(c)
            if target_id in cards and c.get("id") != target_id:
                out.append(c.get("id"))
        return out

    def all_script_fns(self):
        fns = set()
        for _, c in self.iter_cards():
            _, _, f = self.references_from(c)
            fns.update(f)
        return sorted(fns)

    def validate(self, known_scripts=None):
        issues = []
        ids = self.all_card_ids()
        seen = {}
        for cid in ids:
            seen[cid] = seen.get(cid, 0) + 1
        for cid, n in seen.items():
            if cid == "":
                issues.append(("error", "A card has an empty id."))
            elif " " in cid:
                issues.append(("warn", f"Card id '{cid}' contains a space."))
            if n > 1:
                issues.append(("error", f"Duplicate card id '{cid}' ({n} copies)."))
        idset = set(ids)
        setset = set(self.set_ids())
        for sid, c in self.iter_cards():
            cid = c.get("id", "?")
            t = c.get("type")
            if t not in CARD_TYPES:
                issues.append(("warn", f"'{cid}': unknown type '{t}'."))
            if t == "decision":
                for ck in ("choice_a", "choice_b"):
                    ch = c.get(ck)
                    if not isinstance(ch, dict):
                        issues.append(("error", f"'{cid}': decision missing {ck}."))
                    elif not str(ch.get("label", "")).strip():
                        issues.append(("warn", f"'{cid}': {ck} has no label."))
                if "effects" in c:
                    issues.append(("warn", f"'{cid}': decision has top-level 'effects' (ignored at runtime)."))
            else:
                if c.get("choice_a") or c.get("choice_b"):
                    issues.append(("warn", f"'{cid}': {t} card has choices (ignored at runtime)."))
            cards, sets_, fns = self.references_from(c)
            for ref in cards:
                if ref not in idset:
                    issues.append(("error", f"'{cid}' references missing card '{ref}'."))
            for ref in sets_:
                if ref not in setset:
                    issues.append(("error", f"'{cid}' references missing set '{ref}'."))
            if known_scripts is not None:
                for fn in fns:
                    if fn not in known_scripts:
                        issues.append(("warn", f"'{cid}': script '{fn}' not found in card_scripts.gd."))
        return issues


def parse_script_names(path):
    """Extract top-level func names from a card_scripts.gd file."""
    names = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"\s*func\s+([A-Za-z_]\w*)\s*\(", line)
                if m:
                    names.append(m.group(1))
    except Exception:
        pass
    # 'run' is the dispatcher, not a card script
    return [n for n in names if n != "run"]


def append_script_stub(path, fn_name):
    existing = set(parse_script_names(path))
    if fn_name in existing:
        return False
    stub = (
        f"\nfunc {fn_name}(game: Node, ctx: Dictionary) -> void:\n"
        f"\t# TODO: implement '{fn_name}'. 'game' is the Game node, 'ctx' is the\n"
        f"\t# effect dict from the card (so you can read custom params you set).\n"
        f"\tpass\n"
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(stub)
    return True


def effect_summary(eff):
    t = eff.get("type", "?")
    if t == "add_modifier":
        return f"add_modifier: {eff.get('stat','?')} {eff.get('op','?')} {eff.get('value','?')} ({eff.get('scope','?')})"
    if t == "set_weather":
        return f"set_weather: {eff.get('name','?')} (+{len(eff.get('modifiers',[]))} mods)"
    if t in ("inject_cards", "remove_cards"):
        return f"{t}: {eff.get('ids',[])} {eff.get('position','') if t=='inject_cards' else ''}".strip()
    if t == "delayed_card":
        return f"delayed_card: {eff.get('id','?')} in {eff.get('after_days','?')}d"
    if t == "delayed_effect":
        inner = eff.get("effect", {}) or {}
        return f"delayed_effect (+{eff.get('after_days','?')}d): {inner.get('type','?')}"
    if t in ("add_set", "remove_set"):
        return f"{t}: {eff.get('set','?')}"
    if t == "script":
        return f"script: {eff.get('fn','?')}"
    if t in ("grant_resources", "drain_resources"):
        return f"{t}: {eff.get('amount','?')} ({eff.get('scope','?')})"
    if t == "spawn_unit":
        return f"spawn_unit: {eff.get('count',1)}x {eff.get('unit_type','?')} -> team {eff.get('team','?')}"
    if t == "transform_unit":
        return f"transform_unit: {eff.get('scope','?')} -> {eff.get('to','?')}"
    return t


# ──────────────────────────────────────────────────────────────────────────────
# GUI — generic form builder
# ──────────────────────────────────────────────────────────────────────────────

class FormBuilder:
    """Builds widgets from a schema into a frame and reads them back to a dict."""

    def __init__(self, parent, schema, data, app):
        self.parent = parent
        self.schema = schema
        self.data = data
        self.app = app
        self.widgets = {}   # key -> (kind, getter)
        self._build()

    def _build(self):
        for r, (key, kind, extra) in enumerate(self.schema):
            ttk.Label(self.parent, text=key).grid(row=r, column=0, sticky="ne", padx=4, pady=3)
            getter = self._field(self.parent, r, key, kind, extra, self.data.get(key))
            self.widgets[key] = (kind, getter)
        self.parent.columnconfigure(1, weight=1)

    def _field(self, parent, r, key, kind, extra, value):
        if kind in ("text",):
            var = tk.StringVar(value="" if value is None else str(value))
            ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=3)
            return lambda: var.get()
        if kind in ("int", "num"):
            var = tk.StringVar(value="" if value is None else str(value))
            ttk.Entry(parent, textvariable=var, width=14).grid(row=r, column=1, sticky="w", padx=4, pady=3)
            if kind == "int":
                return lambda: int(parse_num(var.get())) if str(var.get()).strip() else 0
            return lambda: parse_num(var.get())
        if kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(parent, variable=var).grid(row=r, column=1, sticky="w", padx=4, pady=3)
            return lambda: bool(var.get())
        if kind == "choice":
            var = tk.StringVar(value="" if value is None else str(value))
            cb = ttk.Combobox(parent, textvariable=var, values=extra or [], state="readonly")
            cb.grid(row=r, column=1, sticky="w", padx=4, pady=3)
            return lambda: var.get()
        if kind in ("scope", "op", "card_ref", "set_ref", "script_ref"):
            return self._combo(parent, r, kind, value)
        if kind == "list_str":
            var = tk.StringVar(value=", ".join(value) if isinstance(value, list) else ("" if value is None else str(value)))
            ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=3)
            ttk.Label(parent, text="comma-separated", foreground="#888").grid(row=r, column=2, sticky="w")
            return lambda: split_list(var.get())
        if kind == "card_ref_list":
            return self._card_ref_list(parent, r, value)
        if kind == "modifier_list":
            return self._modifier_list(parent, r, value)
        if kind == "effect":
            return self._nested_effect(parent, r, value)
        if kind == "raw":
            return self._raw(parent, r, value)
        # fallback
        var = tk.StringVar(value="" if value is None else str(value))
        ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=3)
        return lambda: var.get()

    def _combo(self, parent, r, kind, value):
        var = tk.StringVar(value="" if value is None else str(value))
        if kind == "scope":
            vals = SCOPE_PRESETS
        elif kind == "op":
            vals = OP_PRESETS
        elif kind == "card_ref":
            vals = self.app.deck.all_card_ids()
        elif kind == "set_ref":
            vals = self.app.deck.set_ids()
        elif kind == "script_ref":
            vals = self.app.known_scripts()
        else:
            vals = []
        state = "readonly" if kind == "op" else "normal"
        cb = ttk.Combobox(parent, textvariable=var, values=vals, state=state)
        cb.grid(row=r, column=1, sticky="ew", padx=4, pady=3)
        if kind == "script_ref":
            ttk.Button(parent, text="New stub", width=9,
                       command=lambda: self._new_script_stub(var)).grid(row=r, column=2, padx=2)
        return lambda: var.get()

    def _new_script_stub(self, var):
        name = var.get().strip()
        if not name:
            messagebox.showinfo("Script", "Type a function name first.")
            return
        self.app.create_script_stub(name)

    def _card_ref_list(self, parent, r, value):
        frame = ttk.Frame(parent)
        frame.grid(row=r, column=1, columnspan=2, sticky="ew", padx=4, pady=3)
        items = list(value) if isinstance(value, list) else []
        lb = tk.Listbox(frame, height=3)
        for it in items:
            lb.insert("end", it)
        lb.grid(row=0, column=0, rowspan=3, sticky="ew")
        frame.columnconfigure(0, weight=1)
        pick = tk.StringVar()
        cb = ttk.Combobox(frame, textvariable=pick, values=self.app.deck.all_card_ids())
        cb.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(frame, text="Add", width=7,
                   command=lambda: (lb.insert("end", pick.get().strip()) if pick.get().strip() else None)
                   ).grid(row=1, column=1, padx=4, sticky="ew")
        ttk.Button(frame, text="Remove", width=7,
                   command=lambda: [lb.delete(i) for i in reversed(lb.curselection())]
                   ).grid(row=2, column=1, padx=4, sticky="ew")
        return lambda: list(lb.get(0, "end"))

    def _modifier_list(self, parent, r, value):
        editor = ListEditor(parent, self.app, list(value) if isinstance(value, list) else [],
                            summary=lambda m: f"{m.get('stat','?')} {m.get('op','?')} {m.get('value','?')} ({m.get('scope','?')})",
                            on_add=lambda: ModifierDialog(self.app, {}).result,
                            on_edit=lambda m: ModifierDialog(self.app, m).result)
        editor.grid(row=r, column=1, columnspan=2, sticky="ew", padx=4, pady=3)
        return editor.get_items

    def _nested_effect(self, parent, r, value):
        holder = {"eff": dict(value) if isinstance(value, dict) else {}}
        lbl = tk.StringVar(value=effect_summary(holder["eff"]) if holder["eff"] else "(none)")

        def edit():
            res = EffectDialog(self.app, holder["eff"]).result
            if res is not None:
                holder["eff"] = res
                lbl.set(effect_summary(res))

        frame = ttk.Frame(parent)
        frame.grid(row=r, column=1, columnspan=2, sticky="ew", padx=4, pady=3)
        ttk.Label(frame, textvariable=lbl, foreground="#555").grid(row=0, column=0, sticky="w")
        ttk.Button(frame, text="Edit nested effect…", command=edit).grid(row=0, column=1, padx=6)
        return lambda: holder["eff"]

    def _raw(self, parent, r, value):
        txt = tk.Text(parent, height=4, width=40, wrap="word")
        txt.grid(row=r, column=1, columnspan=2, sticky="ew", padx=4, pady=3)
        if value not in (None, ""):
            txt.insert("1.0", json.dumps(value, indent=2, ensure_ascii=False))

        def get():
            raw = txt.get("1.0", "end-1c").strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:
                return raw
        return get

    def read(self):
        out = {}
        for key, (kind, getter) in self.widgets.items():
            val = getter()
            if kind == "raw" and val is None:
                continue
            out[key] = val
        return out


class ListEditor(ttk.Frame):
    """Reusable listbox + Add/Edit/Remove/Up/Down bound to a python list."""

    def __init__(self, parent, app, items, summary, on_add, on_edit):
        super().__init__(parent)
        self.app = app
        self.items = items
        self.summary = summary
        self.on_add = on_add
        self.on_edit = on_edit
        self.lb = tk.Listbox(self, height=5)
        self.lb.grid(row=0, column=0, rowspan=5, sticky="nsew")
        self.lb.bind("<Double-Button-1>", lambda e: self._edit())
        self.columnconfigure(0, weight=1)
        ttk.Button(self, text="Add", width=8, command=self._add).grid(row=0, column=1, padx=4, pady=1, sticky="ew")
        ttk.Button(self, text="Edit", width=8, command=self._edit).grid(row=1, column=1, padx=4, pady=1, sticky="ew")
        ttk.Button(self, text="Remove", width=8, command=self._remove).grid(row=2, column=1, padx=4, pady=1, sticky="ew")
        ttk.Button(self, text="Up", width=8, command=lambda: self._move(-1)).grid(row=3, column=1, padx=4, pady=1, sticky="ew")
        ttk.Button(self, text="Down", width=8, command=lambda: self._move(1)).grid(row=4, column=1, padx=4, pady=1, sticky="ew")
        self._refresh()

    def _refresh(self):
        self.lb.delete(0, "end")
        for it in self.items:
            self.lb.insert("end", self.summary(it))

    def _sel(self):
        s = self.lb.curselection()
        return s[0] if s else None

    def _add(self):
        res = self.on_add()
        if res is not None:
            self.items.append(res)
            self._refresh()

    def _edit(self):
        i = self._sel()
        if i is None:
            return
        res = self.on_edit(self.items[i])
        if res is not None:
            self.items[i] = res
            self._refresh()

    def _remove(self):
        i = self._sel()
        if i is None:
            return
        self.items.pop(i)
        self._refresh()

    def _move(self, d):
        i = self._sel()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self._refresh()
            self.lb.selection_set(j)

    def get_items(self):
        return self.items


# ──────────────────────────────────────────────────────────────────────────────
# GUI — dialogs
# ──────────────────────────────────────────────────────────────────────────────

class ModalDialog(tk.Toplevel):
    def __init__(self, app, title):
        super().__init__(app)
        self.app = app
        self.result = None
        self.title(title)
        self.transient(app)
        self.resizable(True, False)
        self.body = ttk.Frame(self, padding=8)
        self.body.pack(fill="both", expand=True)

    def add_buttons(self):
        bar = ttk.Frame(self, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="OK", command=self._ok).pack(side="right", padx=6)
        self.bind("<Escape>", lambda e: self.destroy())

    def _ok(self):
        raise NotImplementedError

    def wait(self):
        self.grab_set()
        self.wait_window()
        return self.result


class ModifierDialog(ModalDialog):
    def __init__(self, app, modifier):
        super().__init__(app, "Modifier")
        self.form = FormBuilder(self.body, MODIFIER_FIELDS, modifier or {}, app)
        self.add_buttons()
        self.wait()

    def _ok(self):
        d = self.form.read()
        # drop empties so weather modifiers stay tidy
        d = {k: v for k, v in d.items() if v not in ("", [], None)}
        self.result = d
        self.destroy()


class EffectDialog(ModalDialog):
    def __init__(self, app, effect):
        super().__init__(app, "Effect")
        effect = effect or {}
        self.type_var = tk.StringVar(value=effect.get("type", EFFECT_TYPES[0]))
        self.raw_mode = tk.BooleanVar(value=effect.get("type") not in EFFECT_SCHEMA and bool(effect))
        self._current = dict(effect)

        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Label(top, text="type").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.type_var, values=EFFECT_TYPES, width=22)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._rebuild())
        ttk.Checkbutton(top, text="Raw JSON", variable=self.raw_mode,
                        command=self._rebuild).pack(side="right")

        self.form_frame = ttk.Frame(self.body)
        self.form_frame.pack(fill="both", expand=True, pady=6)
        self.form = None
        self.raw_widget = None
        self._rebuild()
        self.add_buttons()
        self.wait()

    def _rebuild(self):
        for w in self.form_frame.winfo_children():
            w.destroy()
        t = self.type_var.get()
        data = dict(self._current)
        data["type"] = t
        if self.raw_mode.get() or t not in EFFECT_SCHEMA:
            self.form = None
            self.raw_widget = tk.Text(self.form_frame, height=12, width=50, wrap="word")
            self.raw_widget.pack(fill="both", expand=True)
            self.raw_widget.insert("1.0", json.dumps(data, indent=2, ensure_ascii=False))
        else:
            self.raw_widget = None
            self.form = FormBuilder(self.form_frame, EFFECT_SCHEMA[t], data, self.app)

    def _ok(self):
        if self.raw_widget is not None:
            try:
                obj = json.loads(self.raw_widget.get("1.0", "end-1c"))
            except Exception as ex:
                messagebox.showerror("Invalid JSON", str(ex))
                return
            if "type" not in obj:
                obj["type"] = self.type_var.get()
            self.result = obj
        else:
            d = self.form.read()
            d = {k: v for k, v in d.items() if v not in ("", [], None)}
            d["type"] = self.type_var.get()
            self.result = d
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# GUI — main window
# ──────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self, start_path=None):
        super().__init__()
        self.title("Card Forge")
        self.geometry("1080x720")
        self.deck = Deck()
        self.scripts_path = None
        self.assets_root = None
        self._scripts_cache = []
        self.current_id = None
        self._preview_img = None
        self._choice_vars = {}   # ck -> StringVar (GUI-only, never stored in deck data)

        self._build_menu()
        self._build_layout()

        if start_path and os.path.exists(start_path):
            self._open(start_path)
        else:
            self.deck.add_set("new_set", label="New Set", startable=True)
            self._refresh_tree()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- menu ----
    def _build_menu(self):
        m = tk.Menu(self)
        filem = tk.Menu(m, tearoff=0)
        filem.add_command(label="New deck", command=self._new_deck)
        filem.add_command(label="Open deck.json…", command=self._open_dialog, accelerator="Ctrl+O")
        filem.add_command(label="Save", command=self._save, accelerator="Ctrl+S")
        filem.add_command(label="Save As…", command=self._save_as)
        filem.add_separator()
        filem.add_command(label="Link card_scripts.gd…", command=self._link_scripts)
        filem.add_command(label="Set assets root…", command=self._set_assets_root)
        filem.add_separator()
        filem.add_command(label="Exit", command=self._on_close)
        m.add_cascade(label="File", menu=filem)

        toolm = tk.Menu(m, tearoff=0)
        toolm.add_command(label="Validate deck", command=self._validate)
        toolm.add_command(label="Story map", command=self._story_map)
        m.add_cascade(label="Tools", menu=toolm)
        self.config(menu=m)
        self.bind("<Control-s>", lambda e: self._save())
        self.bind("<Control-o>", lambda e: self._open_dialog())

    # ---- layout ----
    def _build_layout(self):
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # left: tree + toolbar
        left = ttk.Frame(paned)
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        ttk.Button(bar, text="+ Set", width=7, command=self._new_set).pack(side="left", padx=1, pady=2)
        ttk.Button(bar, text="+ Card", width=7, command=self._new_card).pack(side="left", padx=1, pady=2)
        ttk.Button(bar, text="Dup", width=5, command=self._dup_card).pack(side="left", padx=1, pady=2)
        ttk.Button(bar, text="Del", width=5, command=self._del_selected).pack(side="left", padx=1, pady=2)
        self.tree = ttk.Treeview(left, show="tree")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        paned.add(left, weight=1)

        # right: editor
        right = ttk.Frame(paned, padding=8)
        paned.add(right, weight=3)
        self.editor = right
        self._build_editor(right)

    def _build_editor(self, parent):
        meta = ttk.LabelFrame(parent, text="Card", padding=8)
        meta.pack(fill="x")
        meta.columnconfigure(1, weight=1)

        self.v_id = tk.StringVar()
        self.v_type = tk.StringVar()
        self.v_set = tk.StringVar()
        self.v_image = tk.StringVar()
        self.v_protected = tk.BooleanVar()

        ttk.Label(meta, text="id").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        ttk.Entry(meta, textvariable=self.v_id).grid(row=0, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(meta, text="type").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        tcb = ttk.Combobox(meta, textvariable=self.v_type, values=CARD_TYPES, state="readonly", width=14)
        tcb.grid(row=1, column=1, sticky="w", padx=4, pady=3)
        tcb.bind("<<ComboboxSelected>>", lambda e: self._on_type_change())

        ttk.Label(meta, text="set").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.set_cb = ttk.Combobox(meta, textvariable=self.v_set, state="readonly", width=22)
        self.set_cb.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        self.set_cb.bind("<<ComboboxSelected>>", lambda e: self._on_set_change())

        ttk.Label(meta, text="text").grid(row=3, column=0, sticky="ne", padx=4, pady=3)
        self.txt = tk.Text(meta, height=4, wrap="word")
        self.txt.grid(row=3, column=1, sticky="ew", padx=4, pady=3)

        ttk.Label(meta, text="image").grid(row=4, column=0, sticky="e", padx=4, pady=3)
        imgrow = ttk.Frame(meta)
        imgrow.grid(row=4, column=1, sticky="ew", padx=4, pady=3)
        imgrow.columnconfigure(0, weight=1)
        ttk.Entry(imgrow, textvariable=self.v_image).grid(row=0, column=0, sticky="ew")
        ttk.Button(imgrow, text="Browse…", command=self._browse_image).grid(row=0, column=1, padx=4)
        ttk.Button(imgrow, text="Preview", command=self._show_preview).grid(row=0, column=2)
        ttk.Checkbutton(meta, text="protected (won't be drawn randomly)",
                        variable=self.v_protected).grid(row=5, column=1, sticky="w", padx=4)
        self.preview = ttk.Label(meta)
        self.preview.grid(row=0, column=2, rowspan=4, padx=8)

        # effects / choices area (rebuilt per type)
        self.body = ttk.Frame(parent)
        self.body.pack(fill="both", expand=True, pady=8)

        # links panel
        links = ttk.LabelFrame(parent, text="Story links", padding=6)
        links.pack(fill="x")
        self.lbl_links_to = ttk.Label(links, text="Draws / links to: —", wraplength=900, justify="left")
        self.lbl_links_to.pack(anchor="w")
        self.lbl_ref_by = ttk.Label(links, text="Referenced by: —", wraplength=900, justify="left")
        self.lbl_ref_by.pack(anchor="w")

        self.status = ttk.Label(parent, text="", foreground="#666")
        self.status.pack(anchor="w", pady=(4, 0))

    # ---- script linkage ----
    def known_scripts(self):
        return self._scripts_cache

    def create_script_stub(self, name):
        if not self.scripts_path:
            messagebox.showinfo("No script file",
                                "Link your card_scripts.gd first (File > Link card_scripts.gd).")
            return
        if append_script_stub(self.scripts_path, name):
            self._scripts_cache = parse_script_names(self.scripts_path)
            messagebox.showinfo("Script", f"Added stub for '{name}' to card_scripts.gd.")
        else:
            messagebox.showinfo("Script", f"'{name}' already exists.")

    def _link_scripts(self):
        p = filedialog.askopenfilename(title="Select card_scripts.gd",
                                       filetypes=[("GDScript", "*.gd"), ("All", "*.*")])
        if p:
            self.scripts_path = p
            self._scripts_cache = parse_script_names(p)
            self._set_status(f"Linked {os.path.basename(p)} ({len(self._scripts_cache)} scripts).")

    def _set_assets_root(self):
        d = filedialog.askdirectory(title="Project folder that res:// points to")
        if d:
            self.assets_root = d
            self._set_status(f"Assets root: {d}")

    # ---- file ops ----
    def _new_deck(self):
        if not self._confirm_discard():
            return
        self.deck = Deck()
        self.deck.add_set("new_set", label="New Set", startable=True)
        self.current_id = None
        self._refresh_tree()

    def _open_dialog(self):
        p = filedialog.askopenfilename(title="Open deck.json",
                                       filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self._open(p)

    def _open(self, path):
        try:
            self.deck.load(path)
        except Exception as ex:
            messagebox.showerror("Open failed", str(ex))
            return
        self.current_id = None
        self._refresh_tree()
        self._set_status(f"Opened {path}")

    def _save(self):
        self._commit_current()
        if not self.deck.path:
            return self._save_as()
        try:
            self.deck.save()
            self._set_status(f"Saved {self.deck.path}")
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex))

    def _save_as(self):
        self._commit_current()
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                         filetypes=[("JSON", "*.json")])
        if p:
            try:
                self.deck.save(p)
                self._set_status(f"Saved {p}")
            except Exception as ex:
                messagebox.showerror("Save failed", str(ex))

    # ---- tree ----
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for sid, s in self.deck.sets.items():
            star = "★ " if s.get("startable") else ""
            node = self.tree.insert("", "end", iid="set:" + sid,
                                    text=f"{star}{sid}", open=True)
            for c in s.get("cards", []):
                cid = c.get("id", "?")
                self.tree.insert(node, "end", iid="card:" + cid,
                                 text=f"  [{c.get('type','?')[:3]}] {cid}")
        self.set_cb.configure(values=self.deck.set_ids())

    def _on_tree_select(self, _evt):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("card:"):
            self._commit_current()
            self._load_card(iid[5:])
        elif iid.startswith("set:"):
            self._commit_current()
            self._edit_set(iid[4:])

    # ---- set editing ----
    def _edit_set(self, sid):
        self.current_id = None
        s = self.deck.sets.get(sid, {})
        # lightweight set editor inline via dialog
        dlg = ModalDialog(self, f"Set: {sid}")
        v_label = tk.StringVar(value=s.get("label", ""))
        v_start = tk.BooleanVar(value=s.get("startable", False))
        ttk.Label(dlg.body, text="label").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(dlg.body, textvariable=v_label, width=30).grid(row=0, column=1, padx=4, pady=4)
        ttk.Checkbutton(dlg.body, text="startable (loaded into a fresh deck)",
                        variable=v_start).grid(row=1, column=1, sticky="w", padx=4)

        def ok():
            s["label"] = v_label.get()
            s["startable"] = bool(v_start.get())
            dlg.result = True
            dlg.destroy()
        dlg._ok = ok
        dlg.add_buttons()
        dlg.wait()
        self._refresh_tree()

    # ---- card editing ----
    def _load_card(self, card_id):
        sid, c = self.deck.find_card(card_id)
        if not c:
            return
        self.current_id = card_id
        self.v_id.set(c.get("id", ""))
        self.v_type.set(c.get("type", "intel"))
        self.v_set.set(sid)
        self.v_image.set(c.get("image", ""))
        self.v_protected.set(bool(c.get("protected", False)))
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", c.get("text", ""))
        self._build_body(c)
        self._refresh_links(c)
        self._show_preview()

    def _build_body(self, card):
        for w in self.body.winfo_children():
            w.destroy()
        self._choice_vars = {}
        if card.get("type") == "decision":
            for ck, title in (("choice_a", "Choice A"), ("choice_b", "Choice B")):
                ch = card.setdefault(ck, {"label": "", "effects": []})
                lf = ttk.LabelFrame(self.body, text=title, padding=6)
                lf.pack(fill="both", expand=True, pady=3)
                row = ttk.Frame(lf); row.pack(fill="x")
                ttk.Label(row, text="label").pack(side="left")
                v = tk.StringVar(value=ch.get("label", ""))
                ttk.Entry(row, textvariable=v).pack(side="left", fill="x", expand=True, padx=6)
                self._choice_vars[ck] = v
                self._effects_editor(lf, ch.setdefault("effects", []))
        else:
            lf = ttk.LabelFrame(self.body, text="Effects (applied when drawn)", padding=6)
            lf.pack(fill="both", expand=True)
            self._effects_editor(lf, card.setdefault("effects", []))

    def _effects_editor(self, parent, effects_list):
        ed = ListEditor(parent, self, effects_list,
                        summary=effect_summary,
                        on_add=lambda: EffectDialog(self, {}).result,
                        on_edit=lambda e: EffectDialog(self, e).result)
        ed.pack(fill="both", expand=True, pady=3)

    def _commit_current(self):
        if not self.current_id:
            return
        sid, c = self.deck.find_card(self.current_id)
        if not c:
            return
        new_id = self.v_id.get().strip()
        c["type"] = self.v_type.get()
        c["text"] = self.txt.get("1.0", "end-1c")
        c["protected"] = bool(self.v_protected.get())
        img = self.v_image.get().strip()
        if img:
            c["image"] = img
        else:
            c.pop("image", None)
        # commit choice labels (held in GUI-side vars, not in the deck data)
        for ck, var in self._choice_vars.items():
            if isinstance(c.get(ck), dict):
                c[ck]["label"] = var.get()
        # id change
        if new_id and new_id != self.current_id:
            old = self.current_id
            c["id"] = new_id
            self._repoint_refs(old, new_id)
            self.current_id = new_id
        self._refresh_tree()

    def _repoint_refs(self, old_id, new_id):
        for _, c in self.deck.iter_cards():
            for lst in card_effect_lists(c):
                for eff in lst:
                    if eff.get("type") in ("inject_cards", "remove_cards"):
                        eff["ids"] = [new_id if x == old_id else x for x in eff.get("ids", [])]
                    elif eff.get("type") == "delayed_card" and eff.get("id") == old_id:
                        eff["id"] = new_id

    def _on_type_change(self):
        if not self.current_id:
            return
        _, c = self.deck.find_card(self.current_id)
        if c:
            self.deck.set_card_type(c, self.v_type.get())
            self._build_body(c)

    def _on_set_change(self):
        if self.current_id and self.deck.move_card(self.current_id, self.v_set.get()):
            self._refresh_tree()
            self.tree.selection_set("card:" + self.current_id)

    def _refresh_links(self, card):
        cards, sets_, fns = self.deck.references_from(card)
        bits = []
        if cards: bits.append("cards: " + ", ".join(cards))
        if sets_: bits.append("sets: " + ", ".join(sets_))
        if fns: bits.append("scripts: " + ", ".join(fns))
        self.lbl_links_to.config(text="Draws / links to: " + (" | ".join(bits) if bits else "—"))
        rb = self.deck.referenced_by(card.get("id", ""))
        self.lbl_ref_by.config(text="Referenced by: " + (", ".join(rb) if rb else "—"))

    # ---- tree actions ----
    def _selected_set(self):
        sel = self.tree.selection()
        if sel:
            iid = sel[0]
            if iid.startswith("set:"):
                return iid[4:]
            if iid.startswith("card:"):
                sid, _ = self.deck.find_card(iid[5:])
                return sid
        return self.deck.set_ids()[0] if self.deck.set_ids() else None

    def _new_set(self):
        name = _ask_string(self, "New set", "Set id (e.g. western_front_intel):")
        if name:
            if self.deck.add_set(name, label=name):
                self._refresh_tree()
            else:
                messagebox.showinfo("Set", "That set id already exists.")

    def _new_card(self):
        sid = self._selected_set()
        if not sid:
            messagebox.showinfo("Card", "Create a set first.")
            return
        self._commit_current()
        c = self.deck.add_card(sid, "intel")
        self._refresh_tree()
        self.tree.selection_set("card:" + c["id"])

    def _dup_card(self):
        if not self.current_id:
            return
        self._commit_current()
        clone = self.deck.duplicate_card(self.current_id)
        if clone:
            self._refresh_tree()
            self.tree.selection_set("card:" + clone["id"])

    def _del_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("card:"):
            if messagebox.askyesno("Delete", f"Delete card '{iid[5:]}'?"):
                self.deck.delete_card(iid[5:])
                self.current_id = None
                self._refresh_tree()
        elif iid.startswith("set:"):
            sid = iid[4:]
            n = len(self.deck.cards_in(sid))
            if messagebox.askyesno("Delete", f"Delete set '{sid}' and its {n} card(s)?"):
                self.deck.delete_set(sid)
                self.current_id = None
                self._refresh_tree()

    # ---- image ----
    def _browse_image(self):
        p = filedialog.askopenfilename(title="Card image",
                                       filetypes=[("Images", "*.png *.gif *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if not p:
            return
        if self.assets_root and p.startswith(self.assets_root):
            rel = os.path.relpath(p, self.assets_root).replace(os.sep, "/")
            self.v_image.set("res://" + rel)
        else:
            self.v_image.set(p)
        self._show_preview()

    def _resolve_image_path(self, val):
        if not val:
            return None
        if val.startswith("res://"):
            if self.assets_root:
                return os.path.join(self.assets_root, val[len("res://"):])
            return None
        return val

    def _show_preview(self):
        path = self._resolve_image_path(self.v_image.get().strip())
        self._preview_img = None
        if path and os.path.exists(path):
            try:
                if HAVE_PIL:
                    im = Image.open(path)
                    im.thumbnail((140, 140))
                    self._preview_img = ImageTk.PhotoImage(im)
                else:
                    self._preview_img = tk.PhotoImage(file=path)
                    # crude downscale for big PNGs
                    w = self._preview_img.width()
                    if w > 160:
                        self._preview_img = self._preview_img.subsample(max(1, w // 140))
            except Exception:
                self._preview_img = None
        self.preview.config(image=self._preview_img or "",
                            text="" if self._preview_img else "(no preview)")

    # ---- tools ----
    def _validate(self):
        self._commit_current()
        known = set(self._scripts_cache) if self.scripts_path else None
        issues = self.deck.validate(known_scripts=known)
        win = tk.Toplevel(self)
        win.title("Validation")
        win.geometry("620x420")
        txt = tk.Text(win, wrap="word")
        txt.pack(fill="both", expand=True)
        if not issues:
            txt.insert("end", "✓ No problems found.\n")
        else:
            errs = [m for s, m in issues if s == "error"]
            warns = [m for s, m in issues if s == "warn"]
            if errs:
                txt.insert("end", f"ERRORS ({len(errs)}):\n")
                for m in errs:
                    txt.insert("end", f"  • {m}\n")
                txt.insert("end", "\n")
            if warns:
                txt.insert("end", f"Warnings ({len(warns)}):\n")
                for m in warns:
                    txt.insert("end", f"  • {m}\n")
        txt.config(state="disabled")

    def _story_map(self):
        self._commit_current()
        win = tk.Toplevel(self)
        win.title("Story map")
        win.geometry("640x480")
        txt = tk.Text(win, wrap="none")
        txt.pack(fill="both", expand=True)
        for sid, s in self.deck.sets.items():
            star = " (startable)" if s.get("startable") else ""
            txt.insert("end", f"[{sid}]{star}\n")
            for c in s.get("cards", []):
                cards, sets_, fns = self.deck.references_from(c)
                arrow = ""
                links = cards + [f"set:{x}" for x in sets_]
                if links:
                    arrow = "  ──▶  " + ", ".join(links)
                txt.insert("end", f"   {c.get('id')} [{c.get('type','?')}]{arrow}\n")
            txt.insert("end", "\n")
        txt.config(state="disabled")

    # ---- misc ----
    def _confirm_discard(self):
        return messagebox.askyesno("Discard?", "Discard current deck and start a new one?")

    def _set_status(self, msg):
        self.status.config(text=msg)

    def _on_close(self):
        self._commit_current()
        self.destroy()


def _ask_string(parent, title, prompt):
    """Minimal string prompt (avoids importing simpledialog quirks)."""
    dlg = ModalDialog(parent, title)
    var = tk.StringVar()
    ttk.Label(dlg.body, text=prompt).pack(anchor="w", pady=(0, 4))
    ent = ttk.Entry(dlg.body, textvariable=var, width=36)
    ent.pack(fill="x")
    ent.focus_set()

    def ok():
        dlg.result = var.get().strip()
        dlg.destroy()
    dlg._ok = ok
    ent.bind("<Return>", lambda e: ok())
    dlg.add_buttons()
    return dlg.wait()


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else None
    App(start).mainloop()


if __name__ == "__main__":
    main()
