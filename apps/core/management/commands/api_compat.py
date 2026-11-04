"""
Spec 15 §4.11: compare a new OpenAPI schema with the committed one. Removals and type changes are breaking; each must be
named in docs/api-changes.md (a line containing "METHOD /path" or the component name) or the command fails.

    python manage.py api_compat --base old.json --new docs/openapi.json
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

METHODS = ("get", "put", "post", "patch", "delete")


def _resolve(schema, root):
    while isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = root.get("components", {}).get("schemas", {}).get(name, {})
    return schema or {}


def _type(s):
    t = s.get("type")
    return tuple(t) if isinstance(t, list) else t


def _props(old, new, old_root, new_root, where, out, seen):
    o, n = _resolve(old, old_root), _resolve(new, new_root)
    key = (where, id(o), id(n))
    if key in seen:
        return
    seen.add(key)
    if _type(o) and _type(n) and _type(o) != _type(n):
        out.append(f"{where}: type {_type(o)} → {_type(n)}")
        return
    for name, sub in (o.get("properties") or {}).items():
        if name not in (n.get("properties") or {}):
            if not n.get("additionalProperties"):
                out.append(f"{where}.{name}: removed")
            continue
        _props(sub, n["properties"][name], old_root, new_root, f"{where}.{name}", out, seen)
    if o.get("items") and n.get("items"):
        _props(o["items"], n["items"], old_root, new_root, f"{where}[]", out, seen)
    if o.get("enum") and n.get("enum"):
        gone = set(o["enum"]) - set(n["enum"])
        if gone:
            out.append(f"{where}: enum values removed {sorted(map(str, gone))}")


def _required_added(old, new, old_root, new_root):
    o, n = _resolve(old, old_root), _resolve(new, new_root)
    return set(n.get("required") or []) - set(o.get("required") or []) - (set(n.get("properties") or {}) - set(o.get("properties") or {}))


def breaking(old: dict, new: dict) -> list[tuple[str, str]]:
    """[(operation, problem)]"""
    out = []
    for path, ops in old.get("paths", {}).items():
        for m in METHODS:
            if m not in ops:
                continue
            op = f"{m.upper()} {path}"
            nops = new.get("paths", {}).get(path)
            if not nops or m not in nops:
                out.append((op, "operation removed"))
                continue
            o, n = ops[m], nops[m]
            for status, resp in (o.get("responses") or {}).items():
                if not status.startswith("2"):
                    continue
                os_ = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
                ns_ = (((n.get("responses") or {}).get(status) or {}).get("content") or {}).get("application/json", {}).get("schema")
                if os_ and not ns_ and status in (n.get("responses") or {}):
                    continue
                if os_ and ns_:
                    problems = []
                    _props(os_, ns_, old, new, f"response {status}", problems, set())
                    out += [(op, p) for p in problems]
            ob = (((o.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            nb = (((n.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            if ob and nb:
                req = _required_added(ob, nb, old, new)
                if req:
                    out.append((op, f"request fields now required: {sorted(req)}"))
            oparams = {(p.get("in"), p.get("name")): p for p in o.get("parameters") or []}
            for p in n.get("parameters") or []:
                if p.get("required") and (p.get("in"), p.get("name")) not in oparams and p.get("in") != "path":
                    out.append((op, f"new required parameter {p.get('name')}"))
    return out


class Command(BaseCommand):
    help = "Fail on breaking API changes not explained in docs/api-changes.md (spec 15 §4.11)."

    def add_arguments(self, parser):
        parser.add_argument("--base", required=True, help="The previous schema (e.g. from the main branch).")
        parser.add_argument("--new", default=str(Path(settings.BASE_DIR) / "docs" / "openapi.json"))
        parser.add_argument("--changes", default=str(Path(settings.BASE_DIR) / "docs" / "api-changes.md"))

    def handle(self, *args, base, new, changes, **opts):
        old_s, new_s = json.loads(Path(base).read_text()), json.loads(Path(new).read_text())
        log = Path(changes).read_text() if Path(changes).exists() else ""
        found = breaking(old_s, new_s)
        unexplained = [(op, p) for op, p in found if op not in log]
        for op, p in found:
            self.stdout.write(f"{'explained' if (op, p) not in unexplained else 'BREAKING'}: {op} — {p}")
        if unexplained:
            raise CommandError(f"{len(unexplained)} breaking change(s) not in docs/api-changes.md. Add a parallel endpoint, "
                               "or explain each (\"METHOD /path\", date, reason) there.")
        self.stdout.write(f"API compatible ({len(found)} explained change(s)).")
