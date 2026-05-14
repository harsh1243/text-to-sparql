import re
import json
import urllib.request
import urllib.parse

from rdflib import URIRef, Literal
from rdflib.term import Variable
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.paths import Path

def _cv_get(node, *keys):
    """Safe getter for rdflib CompValue nodes.
    CompValue.get(missing_key) returns the key name as a bare string
    instead of None — this wrapper returns None for those false hits,
    accepting only CompValue, Variable, or Literal values.
    Pass multiple keys to try each in order (first valid hit wins)."""
    from rdflib.term import Literal
    for key in keys:
        val = node.get(key)
        if isinstance(val, (CompValue, Variable, Literal)):
            return val
    return None

from graphviz import Digraph
from IPython.display import Image, display

def visualize_plan(plan, filename="query_plan"):

    g = Digraph()

    for step in plan["steps"]:
        label = f"{step['step']} : {step['action']}"

        if "property" in step:
            label += f"\n{step['property']}"

        if "output_variable" in step:
            label += f"\n→ {step['output_variable']}"
        elif "filter_variable" in step:
            label += f"\n⊓ {step['filter_variable']}"
        if "object_variable" in step:
            label += f"\n← {step['object_variable']}"

        g.node(str(step["step"]), label, shape="box")

    for i in range(len(plan["steps"]) - 1):
        s1 = plan["steps"][i]["step"]
        s2 = plan["steps"][i + 1]["step"]
        g.edge(str(s1), str(s2))

    path = g.render(filename, format="png", cleanup=True)

    display(Image(path))

def extract_question_word(q):
    q = q.strip().lower()
    for p in ["how many", "how much", "how long", "how old"]:
        if q.startswith(p): return p
    first = q.split()[0] if q.split() else ""
    return first if first in KNOWN_QUESTION_WORDS else "unknown"

RANGE_TO_SEMANTIC = {
    "http://dbpedia.org/ontology/Person":         "Person",
    "http://dbpedia.org/ontology/Actor":          "Person",
    "http://dbpedia.org/ontology/Director":       "Person",
    "http://dbpedia.org/ontology/Athlete":        "Person",
    "http://dbpedia.org/ontology/Politician":     "Person",
    "http://dbpedia.org/ontology/Scientist":      "Person",
    "http://dbpedia.org/ontology/Artist":         "Person",
    "http://dbpedia.org/ontology/Place":          "Place",
    "http://dbpedia.org/ontology/Location":       "Place",
    "http://dbpedia.org/ontology/City":           "Place",
    "http://dbpedia.org/ontology/Country":        "Place",
    "http://dbpedia.org/ontology/PopulatedPlace": "Place",
    "http://www.w3.org/2001/XMLSchema#date":      "Date",
    "http://www.w3.org/2001/XMLSchema#gYear":     "Date",
    "http://www.w3.org/2001/XMLSchema#integer":   "Number",
    "http://www.w3.org/2001/XMLSchema#double":    "Number",
    "http://www.w3.org/2001/XMLSchema#float":     "Number",
    "http://www.w3.org/2001/XMLSchema#string":    "String",
}

class OWLIndex:
    def __init__(self, owl_path=None):
        self.range_ = {}
        self.sub    = {}
        if owl_path: self._load(owl_path)

    def _load(self, path):
        from rdflib import Graph
        print(f"[OWL] loading {path} ...")
        g = Graph()
        g.parse(path)
        RDFS = "http://www.w3.org/2000/01/rdf-schema#"
        for s, p, o in g:
            ps = str(p)
            if ps == f"{RDFS}range":        self.range_[str(s)] = str(o)
            elif ps == f"{RDFS}subClassOf": self.sub[str(s)]    = str(o)
        print(f"[OWL] {len(self.range_)} ranges loaded")

    def _is_sub(self, child, parent, depth=0):
        if depth > 10 or not child: return False
        if child == parent:         return True
        return self._is_sub(self.sub.get(child, ""), parent, depth + 1)

    def range_type(self, prop_uri):
        r = self.range_.get(prop_uri)
        if not r: return None
        if r in RANGE_TO_SEMANTIC: return RANGE_TO_SEMANTIC[r]
        for known, sem in RANGE_TO_SEMANTIC.items():
            if self._is_sub(r, known): return sem
        return None

def local_name(uri):
    uri = str(uri)
    return uri.split("#")[-1].split("/")[-1]

def entity_surface(uri):
    return local_name(str(uri)).replace("_", " ")

def safe_var(uri_str):
    """Derive a stable variable name from a URI, preserving underscores so that
    birth_place and birth_date don't collide as the same ?birth token."""
    raw   = local_name(uri_str).lower()
    clean = re.sub(r'[^a-z0-9_]', '', raw)[:20]
    clean = clean.strip('_')
    return f"?{clean}" if clean else "?entity"

def is_entity_uri(node):
    return isinstance(node, URIRef) and any(
        str(node).startswith(p) for p in ENTITY_PREFIXES)

WIKIDATA_INSTANCE_OF = "http://www.wikidata.org/prop/direct/P31"

_WD_PROP_BASE      = "http://www.wikidata.org/prop/"
_WD_PROP_STATEMENT = "http://www.wikidata.org/prop/statement/"
_WD_PROP_QUALIFIER = "http://www.wikidata.org/prop/qualifier/"
_WD_PROP_DIRECT    = "http://www.wikidata.org/prop/direct/"

def is_type_pred(node):
    s = str(node)
    return s == RDF_TYPE or s == WIKIDATA_INSTANCE_OF

def is_label_pred(node): return str(node) == RDFS_LABEL

def is_statement_link_pred(node):
    """True for p:Pxxx  (http://www.wikidata.org/prop/Pxxx) — links entity to statement node.
    Excludes ps:, pq:, wdt: which all share the same base but are sub-namespaces."""
    s = str(node)
    return (
        s.startswith(_WD_PROP_BASE) and
        not s.startswith(_WD_PROP_STATEMENT) and
        not s.startswith(_WD_PROP_QUALIFIER) and
        not s.startswith(_WD_PROP_DIRECT)
    )

def is_qualifier_pred(node):
    """True for pq:Pxxx — retrieves a qualifier from a statement node."""
    return str(node).startswith(_WD_PROP_QUALIFIER)

def is_statement_value_pred(node):
    """True for ps:Pxxx — the value edge coming OUT of a statement node.
    ?stmt ps:Pxxx ?val    → retrieve value  (find_object)
    ?stmt ps:Pxxx wd:Qxxx → constraint      (filter_statement)
    """
    return str(node).startswith(_WD_PROP_STATEMENT)

def walk(node, visitor):
    if isinstance(node, CompValue):
        visitor(node)
        for v in node.values():
            walk(v, visitor)
    elif isinstance(node, list):
        for item in node:
            walk(item, visitor)

def collect_bgp_triples(alg):
    regular, optional, minus_clauses, subquery_triples = [], [], [], []

    def subq_visitor(node):
        if node.name == "ToMultiSet":
            inner = node.get("p")
            if inner:
                def inner_v(n):
                    if n.name == "BGP": subquery_triples.extend(n.get("triples", []))
                walk(inner, inner_v)

    walk(alg, subq_visitor)

    def visitor(node):
        if node.name == "BGP":
            regular.extend(node.get("triples", []))

    def opt_visitor(node):
        if node.name == "LeftJoin":
            right = node.get("p2") or node.get("right")
            if right:
                inner = []
                def inner_v(n):
                    if n.name == "BGP": inner.extend(n.get("triples", []))
                walk(right, inner_v)
                optional.extend(inner)

    def minus_visitor(node):
        if node.name == "Minus":
            right = node.get("p2") or node.get("right")
            if right:
                clause = []
                def inner_v(n, _c=clause):
                    if n.name == "BGP": _c.extend(n.get("triples", []))
                walk(right, inner_v)
                if clause:
                    minus_clauses.append(clause)

    walk(alg, visitor)
    walk(alg, opt_visitor)
    walk(alg, minus_visitor)

    def _tk(t): return (str(t[0]), str(t[1]), str(t[2]))

    opt_set   = {_tk(t) for t in optional}
    minus_set = {_tk(t) for clause in minus_clauses for t in clause}
    subq_set  = {_tk(t) for t in subquery_triples}

    regular_clean = [
        t for t in regular
        if _tk(t) not in opt_set
        and _tk(t) not in minus_set
        and _tk(t) not in subq_set
    ]

    return regular_clean, optional, minus_clauses

def collect_filters(alg):
    filters = []
    seen = set()
    def visitor(node):
        if node.name == "Filter":
            expr = node.get("expr")
            if expr is not None and id(expr) not in seen:

                expr_str = str(expr)
                if "__agg_" in expr_str: return
                seen.add(id(expr))
                filters.append(expr)
    walk(alg, visitor)
    return filters

def parse_filter_expr(expr):
    if expr is None:
        return None
    name = getattr(expr, "name", "") or ""

    if "Exists" in name and "NotExists" not in name:
        return {"type": "exists", "expr": expr}

    if "NotExists" in name or "notexists" in name.lower():
        return {"type": "not_exists", "expr": expr}

    if name == "RelationalExpression":
        op_map = {"=": "equals", "!=": "not_equals", ">": "greater_than",
                  "<": "less_than", ">=": "gte", "<=": "lte"}
        op       = str(expr.get("op", ""))
        lhs      = expr.get("expr")
        rhs      = expr.get("other")
        lhs_name = getattr(lhs, "name", "") or ""

        if "YEAR" in lhs_name.upper():
            arg = _cv_get(lhs, "arg") if isinstance(lhs, CompValue) else None
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?date"

            return {"type": "date_filter", "variable": var,
                    "operator": op_map.get(op, op),
                    "value": str(rhs), "value_type": "year",
                    "apply_fn": "YEAR"}

        if "LANG" in lhs_name.upper():
            arg = _cv_get(lhs, "arg") if isinstance(lhs, CompValue) else None
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?label"
            return {"type": "lang_filter", "variable": var, "value": str(rhs)}

        if "CONTAINS" in lhs_name.upper():
            arg = lhs.get("arg") if isinstance(lhs, CompValue) else None
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?value"
            return {"type": "string_filter", "variable": var,
                    "operator": "contains", "value": str(rhs), "value_type": "string"}

        if isinstance(rhs, Literal):
            rhs_dt  = str(getattr(rhs, "datatype", "") or "")
            rhs_val = str(rhs)
            var     = f"?{str(lhs)}" if isinstance(lhs, Variable) else "?value"

            is_date = ("date" in rhs_dt.lower() or
                       bool(re.match(r'\d{4}-\d{2}-\d{2}', rhs_val)))
            if is_date:
                return {"type": "date_filter", "variable": var,
                        "operator": op_map.get(op, op),
                        "value": rhs_val, "value_type": "date"}
            try:
                float(rhs_val)
                return {"type": "numeric_filter", "variable": var,
                        "operator": op_map.get(op, op),
                        "value": rhs_val, "value_type": "number"}
            except (ValueError, TypeError):
                pass
            return {"type": "string_filter", "variable": var,
                    "operator": op_map.get(op, op),
                    "value": rhs_val, "value_type": "string"}

        if isinstance(rhs, URIRef):
            var = f"?{str(lhs)}" if isinstance(lhs, Variable) else "?value"
            return {"type": "string_filter", "variable": var,
                    "operator": op_map.get(op, op),
                    "value": local_name(str(rhs)), "value_type": "uri"}

    if "Builtin_REGEX" in name or name == "Function_REGEX":

        text    = _cv_get(expr, "text", "arg")
        pattern = _cv_get(expr, "pattern", "other")
        var = f"?{str(text)}" if isinstance(text, Variable) else "?value"
        return {"type": "regex_filter", "variable": var,
                "pattern": str(pattern).strip('"').split("^^")[0] if pattern else "",
                "value_type": "string"}

    if "Builtin_CONTAINS" in name:

        arg = _cv_get(expr, "arg1", "arg")
        val = _cv_get(expr, "arg2", "other", "pattern")

        arg_name = getattr(arg, "name", "") or ""
        if any(fn in arg_name.upper() for fn in ("YEAR", "MONTH", "DAY")):
            inner_arg = _cv_get(arg, "arg") if isinstance(arg, CompValue) else None
            date_var  = f"?{str(inner_arg)}" if isinstance(inner_arg, Variable) else "?date"
            val_str   = str(val).strip("'\"").split("^^")[0] if val else ""
            val_type  = "year" if "YEAR" in arg_name.upper() else "date"

            fn_name = "YEAR" if "YEAR" in arg_name.upper() else ("MONTH" if "MONTH" in arg_name.upper() else "DAY")
            return {"type": "date_filter", "variable": date_var,
                    "operator": "equals", "value": val_str, "value_type": val_type,
                    "apply_fn": fn_name}

        if isinstance(arg, CompValue):
            inner = _cv_get(arg, "arg", "arg1")
            var   = f"?{str(inner)}" if isinstance(inner, Variable) else "?value"
        else:
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?value"
        return {"type": "string_filter", "variable": var,
                "operator": "contains",
                "value": str(val).strip("'\"").split("^^")[0] if val else "",
                "value_type": "string"}

    if "Builtin_STRSTARTS" in name:
        arg = _cv_get(expr, "arg1", "arg")
        val = _cv_get(expr, "arg2", "other")

        if isinstance(arg, CompValue):
            inner = _cv_get(arg, "arg", "arg1")
            var   = f"?{str(inner)}" if isinstance(inner, Variable) else "?value"
        else:
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?value"
        return {"type": "string_filter", "variable": var,
                "operator": "starts_with",
                "value": str(val).strip("'\"").split("^^")[0] if val else "",
                "value_type": "string"}

    if "Builtin_STRENDS" in name:
        arg = _cv_get(expr, "arg1", "arg")
        val = _cv_get(expr, "arg2", "other")

        if isinstance(arg, CompValue):
            inner = _cv_get(arg, "arg", "arg1")
            var   = f"?{str(inner)}" if isinstance(inner, Variable) else "?value"
        else:
            var = f"?{str(arg)}" if isinstance(arg, Variable) else "?value"
        return {"type": "string_filter", "variable": var,
                "operator": "ends_with",
                "value": str(val).strip("'\"").split("^^")[0] if val else "",
                "value_type": "string"}

    if name in ("ConditionalAndExpression", "ConditionalOrExpression"):

        raw_expr = expr.get("expr", [])
        if raw_expr is None:
            raw_expr = []
        parts = list(raw_expr) if isinstance(raw_expr, list) else [raw_expr]

        other = expr.get("other")
        if other is not None:
            parts = parts + (list(other) if isinstance(other, list) else [other])
        results = [parse_filter_expr(p) for p in parts if p is not None]
        results = [r for r in results if r and r.get("type") != "unknown"]
        if len(results) == 0: return {"type": "unknown", "raw": str(expr)}
        if len(results) == 1: return results[0]
        return {"type": "compound",
                "logic": "AND" if "And" in name else "OR",
                "parts": results}

    if name in ("RelationalExpression_IN", "InExpression"):
        lhs  = _cv_get(expr, "expr", "op1")
        vals = expr.get("other") or []
        var  = f"?{str(lhs)}" if isinstance(lhs, Variable) else "?value"
        val_list = [str(v) for v in (vals if isinstance(vals, list) else [vals])]
        return {"type": "in_filter", "variable": var, "values": val_list}

    return {"type": "unknown", "raw": str(expr)}

def has_union(alg):
    found = [False]
    def visitor(node):
        if node.name == "Union": found[0] = True
    walk(alg, visitor)
    return found[0]

def collect_union_branches(alg):
    """
    Return a flat list of (triples, filter_exprs) for each leaf branch of the
    top-level UNION.  rdflib encodes n-way UNION as a left-nested tree:
        UNION(A, B, C)  →  Union(Union(A, B), C)
    so we recurse into p1/p2 until we hit non-Union leaves.

    Returns:
        list of (branch_triples: list, branch_filter_exprs: list)
        one entry per leaf branch, in left-to-right textual order.
    """
    def _branch_content(node):
        """Collect (triples, raw_filter_exprs) from one branch node."""
        triples      = []
        filter_exprs = []
        seen_ids     = set()

        def bgp_v(n):
            if n.name == "BGP":
                triples.extend(n.get("triples", []))

        def filter_v(n):
            if n.name == "Filter":
                expr = n.get("expr")
                if expr is not None and id(expr) not in seen_ids:
                    if "__agg_" not in str(expr):
                        seen_ids.add(id(expr))
                        filter_exprs.append(expr)

        walk(node, bgp_v)
        walk(node, filter_v)
        return triples, filter_exprs

    def _flatten(node, out):
        """Recursively collect leaf branches; do not recurse into nested Unions."""
        if isinstance(node, CompValue) and node.name == "Union":
            p1 = node.get("p1")
            p2 = node.get("p2")
            if p1: _flatten(p1, out)
            if p2: _flatten(p2, out)
        elif node is not None:
            out.append(_branch_content(node))

    branches  = []
    found_top = [False]

    def top_visitor(node):
        if node.name == "Union" and not found_top[0]:
            found_top[0] = True
            _flatten(node, branches)

    walk(alg, top_visitor)
    return branches

def has_minus(alg):

    found = [False]
    def visitor(node):
        if node.name == "Minus": found[0] = True
    walk(alg, visitor)
    return found[0]

def has_distinct(sparql_str):
    return bool(re.search(r'SELECT\s+DISTINCT', sparql_str, re.IGNORECASE))

def has_subquery(alg):

    found = [False]
    def visitor(node):
        if node.name == "ToMultiSet":
            inner = node.get("p")

            if isinstance(inner, CompValue) and inner.name == "values":
                return
            found[0] = True
    walk(alg, visitor)
    return found[0]

def extract_values(alg):
    values_blocks = []

    def visitor(node):

        if node.name in ("Values", "values"):
            res = node.get("res") or []

            if res and isinstance(res[0], dict):
                vars_ = [f"?{str(v)}" for v in res[0].keys()]
                rows  = [[str(row.get(v, "")) for v in res[0].keys()] for row in res]
            else:

                vars_ = [f"?{str(v)}" for v in node.get("var", [])]
                rows  = [[str(x) for x in r] for r in res]

            values_blocks.append({
                "variables": vars_,
                "rows": rows
            })

    walk(alg, visitor)
    return values_blocks

def extract_bind(alg):
    binds = []

    agg_out_vars = set()
    def agg_visitor(node):
        if node.name == "AggregateJoin":
            for agg in node.get("A", []):
                v = agg.get("res")
                if v: agg_out_vars.add(str(v))
    walk(alg, agg_visitor)

    def visitor(node):
        if node.name == "Extend":
            var  = node.get("var")
            expr = node.get("expr")
            if var and expr and str(var) not in agg_out_vars:
                expr_name = getattr(expr, "name", "") or ""

                if isinstance(expr, Variable): return

                source_var = None
                if isinstance(expr, CompValue):
                    for key in ("arg", "arg1", "expr"):
                        inner = expr.get(key)
                        if isinstance(inner, Variable):
                            source_var = f"?{str(inner)}"
                            break
                bind_dict = {
                    "variable": f"?{str(var)}",
                    "expr_type": expr_name,
                    "raw": str(expr)
                }
                if source_var:
                    bind_dict["source_variable"] = source_var
                binds.append(bind_dict)
    walk(alg, visitor)
    return binds

def extract_order_limit(alg):
    info = {
        "has_order": False,
        "direction": "ASC",
        "has_limit": False,
        "limit_value": 0,
        "sort_var": None,
        "sort_expr": None
    }
    def visitor(node):
        if node.name == "OrderBy":
            info["has_order"] = True
            conds = node.get("expr", [])
            cond  = conds[0] if isinstance(conds, list) and conds else conds

            if isinstance(cond, CompValue) and cond.name == "OrderCondition":
                oc_expr  = cond.get("expr")
                oc_order = str(cond.get("order") or "ASC").upper()
                info["direction"] = oc_order
                if isinstance(oc_expr, Variable):
                    vname = str(oc_expr)
                    info["sort_var"]  = f"?{vname}"
                    info["sort_expr"] = f"{oc_order}(?{vname})"
                else:

                    m = re.search(r"Variable\('(\w+)'\)", str(oc_expr)) or re.search(r'\?(\w+)', str(oc_expr))
                    vname = m.group(1) if m else "value"
                    info["sort_var"]  = f"?{vname}"
                    info["sort_expr"] = f"{oc_order}(?{vname})"
            else:

                cond_s = str(cond)
                info["direction"] = "DESC" if "desc" in cond_s.lower() else "ASC"
                m = re.search(r'\?(\w+)', cond_s) or re.search(r"Variable\('(\w+)'\)", cond_s)
                info["sort_var"]  = f"?{m.group(1)}" if m else None
                info["sort_expr"] = f"{info['direction']}(?{m.group(1)})" if m else cond_s
        if node.name == "Slice":
            length = node.get("length")
            if length is not None:
                info["has_limit"]   = True
                info["limit_value"] = int(length)
    walk(alg, visitor)
    return info

def extract_group_by(alg, sparql_str):
    """Detect GROUP BY and HAVING."""
    info = {"has_group_by": False, "group_vars": [], "has_having": False, "having_raw": None}
    gb_m = re.search(r'GROUP\s+BY\s+((?:(?:\([^)]*\)|\?\w+)\s*)+)', sparql_str, re.IGNORECASE)
    if gb_m:
        info["has_group_by"] = True

        info["group_vars"]   = re.findall(r'\?(\w+)', gb_m.group(1))

    hv_m = re.search(r'HAVING\s*\((.+?)\)\s*(?:$|ORDER|LIMIT|GROUP)', sparql_str.strip(),
                      re.IGNORECASE | re.DOTALL)
    if not hv_m:

        hv_m = re.search(r'HAVING\s*\((.+)', sparql_str.strip(), re.IGNORECASE | re.DOTALL)
    if hv_m:
        info["has_having"]  = True
        info["having_raw"]  = hv_m.group(1).strip().rstrip(')')
    return info

def extract_agg_info(sparql_str):
    """Extract all aggregation functions (supports multiple).
    Strips HAVING clause first so COUNT/MAX in HAVING is not double-counted."""

    clean = re.sub(r'HAVING\s*\((?:[^)(]|\([^)]*\))*\)', '', sparql_str, flags=re.IGNORECASE)
    aggs = []
    seen = set()
    for m in re.finditer(
        r'(COUNT|MAX|MIN|SUM|AVG|SAMPLE|GROUP_CONCAT)\s*\(([^)]*?)\)(?:\s+AS\s+(\?\w+))?',
        clean, re.IGNORECASE
    ):
        func    = m.group(1).upper()
        inner   = m.group(2)
        out_var = m.group(3) or f"?{func.lower()}"

        tgt_m   = re.search(r'(\?\w+|\*)', inner)
        tgt     = tgt_m.group(1) if tgt_m else "*"
        key = (func, tgt, out_var)
        if key not in seen:
            seen.add(key)

            is_distinct = bool(re.search(r'\bDISTINCT\b', inner, re.IGNORECASE))
            aggs.append({
                "function": func,
                "target":   tgt if tgt.startswith("?") else f"?{tgt}",
                "out_var":  out_var,
                "is_distinct": is_distinct
            })
    return aggs

def extract_agg_info_from_algebra(inner_alg, agg_alias=None):
    """Extract aggregation info from algebra AggregateJoin node.
    Used inside extract_subquery_plan to avoid regex on partial SPARQL strings.
    agg_alias: optional dict mapping internal __agg_N__ var names to user aliases."""
    if agg_alias is None:
        agg_alias = {}
    aggs = []
    seen = set()
    def visitor(node):
        if node.name == "AggregateJoin":
            for agg_node in node.get("A", []):
                fn_raw = agg_node.name
                fn = fn_raw.split("_")[-1].upper() if "_" in fn_raw else fn_raw.upper()
                res = agg_node.get("res")
                vs  = agg_node.get("vars")
                if fn in ("SAMPLE",):
                    return

                internal_name = str(res) if res else ""
                alias_name    = agg_alias.get(internal_name, internal_name)
                out_var = f"?{alias_name}" if alias_name else f"?{fn.lower()}"
                if isinstance(vs, Variable):
                    tgt = f"?{str(vs)}"
                elif isinstance(vs, list) and vs:
                    tgt = f"?{str(vs[0])}"
                else:
                    tgt = "*"
                key = (fn, tgt, out_var)
                if key not in seen:
                    seen.add(key)
                    aggs.append({"function": fn, "target": tgt, "out_var": out_var})
    walk(inner_alg, visitor)
    return aggs

def extract_subquery_plan(alg, question="", owl=None):
    """
    Walk algebra looking for ToMultiSet nodes (inner SELECT subqueries).
    Return list of subquery plan dicts.
    """
    subqueries = []

    def visitor(node):
        if node.name == "ToMultiSet":
            inner = node.get("p")
            if inner:

                try:
                    inner_steps = []
                    inner_num   = [0]
                    def inner_next():
                        inner_num[0] += 1
                        return inner_num[0]

                    bgps, opts, _inner_minus = collect_bgp_triples(inner)
                    inner_order = extract_order_limit(inner)

                    for s, p, o in bgps:
                        if is_entity_uri(s) and isinstance(o, Variable):
                            inner_steps.append({
                                "step": inner_next(), "action": "find_object",
                                "property": local_name(str(p)),
                                "subject_variable": safe_var(str(s)),
                                "output_variable": f"?{str(o)}",
                                "semantic_type": "PROPERTY"
                            })
                        elif isinstance(s, Variable) and is_entity_uri(o):
                            inner_steps.append({
                                "step": inner_next(), "action": "find_subjects",
                                "property": local_name(str(p)),
                                "object_variable": safe_var(str(o)),
                                "output_variable": f"?{str(s)}",
                                "semantic_type": "PROPERTY"
                            })
                        elif isinstance(s, Variable) and isinstance(o, Variable):
                            inner_steps.append({
                                "step": inner_next(), "action": "find_object",
                                "property": local_name(str(p)),
                                "subject_variable": f"?{str(s)}",
                                "output_variable": f"?{str(o)}",
                                "semantic_type": "PROPERTY"
                            })

                    inner_group_vars = []
                    def _find_group(n):
                        if isinstance(n, CompValue) and n.name == "Group":
                            for v in (n.get("expr") or []):
                                if isinstance(v, Variable):
                                    inner_group_vars.append(str(v))
                        if isinstance(n, CompValue):
                            for val in n.values(): _find_group(val)
                        elif isinstance(n, list):
                            for item in n: _find_group(item)
                    _find_group(inner)

                    _agg_alias = {}
                    def _find_extend(n):
                        if isinstance(n, CompValue) and n.name == "Extend":
                            var  = n.get("var")
                            expr = n.get("expr")
                            if isinstance(var, Variable) and isinstance(expr, Variable):
                                _agg_alias[str(expr)] = str(var)
                        if isinstance(n, CompValue):
                            for val in n.values(): _find_extend(val)
                        elif isinstance(n, list):
                            for item in n: _find_extend(item)
                    _find_extend(inner)

                    inner_gb = {"has_group_by": bool(inner_group_vars),
                                "group_vars": inner_group_vars}
                    inner_aggs = extract_agg_info_from_algebra(inner, _agg_alias)

                    if inner_gb["has_group_by"]:
                        inner_steps.append({
                            "step": inner_next(), "action": "group_by",
                            "description": f"group by {', '.join('?' + v for v in inner_gb['group_vars'])}",
                            "group_variables": [f"?{v}" for v in inner_gb["group_vars"]],
                            "semantic_type": "MODIFIER"
                        })

                    for agg in inner_aggs:
                        inner_steps.append({
                            "step": inner_next(), "action": "aggregate",
                            "description": f"apply {agg['function']} over {agg['target']}",
                            "function": agg["function"],
                            "target_variable": agg["target"],
                            "output_variable": agg["out_var"],
                            "semantic_type": "AGGREGATION"
                        })

                    if inner_order["has_limit"]:
                        inner_steps.append({
                            "step": inner_next(), "action": "limit",
                            "n": inner_order["limit_value"],
                            "semantic_type": "MODIFIER"
                        })

                    subqueries.append({"steps": inner_steps})
                except Exception as e:
                    import warnings
                    warnings.warn(f"[extract_subquery_plan] inner subquery parse failed: {e}")

    walk(alg, visitor)
    return subqueries
def reorder_steps(steps):
    def get_weight(action):

        if action == "seed_values":                                         return 0
        if action in ("find_entity", "subquery"):                           return 1

        if action in ("find_by_type", "find_object", "find_subjects",
                      "find_statement", "filter_statement",
                      "property_path", "verify_fact", "find_qualifier",
                      "optional_find", "left_join", "optional_expand"):     return 4
        if action == "union":                                               return 4.5
        if action in ("filter_type", "filter", "exists",
                      "exclude", "bind"):                                   return 5

        if action == "group_by":                                            return 5.5
        if action == "aggregate":                                           return 6

        if action == "having_filter":                                       return 6.5
        if action == "sort":                                                return 7
        if action in ("limit", "sort_and_limit"):                           return 8
        if action == "distinct":                                            return 7.5
        return 10

    sorted_steps = sorted(steps, key=lambda s: get_weight(s.get("action", "")))

    def _topo_resolve(steps):

        HOP_ACTIONS = {
            "find_by_type",
            "find_object", "find_subjects", "property_path",
            "find_statement", "filter_statement",
            "find_qualifier", "optional_find", "left_join", "optional_expand"
        }

        pre   = [s for s in steps if get_weight(s.get("action","")) <  4]
        hops  = [s for s in steps if s.get("action") in HOP_ACTIONS]
        post  = [s for s in steps if get_weight(s.get("action","")) >  4]

        if len(hops) <= 1:
            return steps

        bound = set()
        for s in pre:
            v = s.get("output_variable")
            if v: bound.add(v)

        _hop_inputs  = set()
        _hop_outputs = set()
        for s in hops:
            inp = (s.get("subject_variable") or s.get("object_variable")
                   or s.get("filter_variable"))
            if inp: _hop_inputs.add(inp)
            out = s.get("output_variable")
            if out: _hop_outputs.add(out)

        for v in _hop_inputs:
            if v not in _hop_outputs and v not in bound:
                bound.add(v)

        ordered = []
        remaining = list(hops)
        max_iter = len(hops) ** 2 + 1
        iters = 0
        while remaining and iters < max_iter:
            iters += 1
            progress = False

            def step_priority(step):
                action = step.get("action")

                if action == "find_entity":
                    return 0

                if action == "find_subjects" or action == "find_object":
                    if not step.get("is_qualifier"):
                        return 1

                if action == "find_statement":
                    return 2

                if action == "filter_statement":
                    return 3

                if step.get("is_qualifier"):
                    return 4

                if action == "filter":
                    return 5

                return 6
            remaining.sort(key=step_priority)
            for s in list(remaining):

                inputs = [
                s.get("subject_variable"),
                s.get("object_variable"),
                s.get("filter_variable")
                     ]

                inputs = [v for v in inputs if v is not None]
                if all(v in bound for v in inputs) or not inputs:

                    ordered.append(s)
                    remaining.remove(s)
                    out = s.get("output_variable")
                    if out: bound.add(out)
                    progress = True
            if not progress:

                ordered.extend(remaining)
                break

        return pre + ordered + post

    sorted_steps = _topo_resolve(sorted_steps)

    for i, s in enumerate(sorted_steps):
        s["step"] = i + 1

    return sorted_steps


import json
import re
import urllib.request
import urllib.parse
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Variable, Literal

def extract_lcquad2(entry):

    question = entry.get("paraphrased_question") or entry.get("question", "")

    sparql   = entry.get("sparql_wikidata", "")
    uid      = entry.get("uid", "")
    return uid, question, sparql

def normalize_lcquad_query(query):

    query = query.replace('\\"', '"').replace("\\'", "'")

    URI_TO_PREFIX = [
        ("http://dbpedia.org/resource/",                    "dbr:"),
        ("http://dbpedia.org/ontology/",                    "dbo:"),
        ("http://dbpedia.org/property/",                    "dbp:"),
        ("http://wikidata.dbpedia.org/resource/",           "dbr:"),
        ("http://www.wikidata.org/entity/",                 "wd:"),
        ("http://www.w3.org/1999/02/22-rdf-syntax-ns#",     "rdf:"),
        ("http://www.w3.org/2000/01/rdf-schema#",           "rdfs:"),
        ("http://www.w3.org/2001/XMLSchema#",               "xsd:"),
        ("http://xmlns.com/foaf/0.1/",                      "foaf:"),
        ("http://purl.org/dc/terms/",                       "dct:"),

        ("http://www.wikidata.org/prop/statement/",         "ps:"),
        ("http://www.wikidata.org/prop/qualifier/",         "pq:"),
        ("http://www.wikidata.org/prop/direct/",            "wdt:"),
        ("http://www.wikidata.org/prop/",                   "p:"),
    ]

    def replace_uri(match, prefix):
        local = match.group(1)

        if re.match(r'^[\w]+$', local):
            return prefix + local
        return match.group(0)

    for uri, prefix in URI_TO_PREFIX:
        query = re.sub(
            r"<" + re.escape(uri) + r"([^>]+)>",
            lambda m, p=prefix: replace_uri(m, p),
            query
        )

    _SUBJ = r'(?:\?\w+|<[^>]+>|\w[\w.-]*:\w[\w.-]*)'

    _OBJ  = r'(?:\?\w+|<[^>]+>|\w[\w-]*:\w+)'
    query = re.sub(
        rf'({_SUBJ})\s+wdt:P31\s+({_OBJ})',
        r'\1 rdf:type \2',
        query
    )
    query = re.sub(
        rf'({_SUBJ})\s+wd:P31\s+({_OBJ})',
        r'\1 rdf:type \2',
        query
    )

    query = re.sub(
        rf'({_SUBJ})\s+dbo:instanceOf\s+({_OBJ})',
        r'\1 rdf:type \2',
        query
    )

    query = re.sub(
        r'SELECT\s+COALESCE\s*\(([^)]+)\)',
        lambda m: 'SELECT ' + (re.findall(r'\?\w+', m.group(1)) or ['?x'])[0],
        query, flags=re.IGNORECASE)

    query = re.sub(
        r'SELECT\s+IF\s*\([^,]+,\s*(\?\w+)',
        r'SELECT \1',
        query, flags=re.IGNORECASE)

    query = re.sub(
        r'SELECT\s+DISTINCT\s+COUNT\s*\(\s*DISTINCT\s+(\?\w+)\s*\)',
        r'SELECT (COUNT(DISTINCT \1) AS ?count)', query, flags=re.IGNORECASE)
    query = re.sub(
        r'SELECT\s+DISTINCT\s+COUNT\s*\(\s*(\?\w+)\s*\)',
        r'SELECT (COUNT(DISTINCT \1) AS ?count)', query, flags=re.IGNORECASE)
    query = re.sub(
        r'SELECT\s+COUNT\s*\(\s*DISTINCT\s+(\?\w+)\s*\)(?!\s+AS)',
        r'SELECT (COUNT(DISTINCT \1) AS ?count)', query, flags=re.IGNORECASE)
    query = re.sub(
        r'SELECT\s+COUNT\s*\(\s*(\?\w+)\s*\)(?!\s+AS)',
        r'SELECT (COUNT(\1) AS ?count)', query, flags=re.IGNORECASE)
    query = re.sub(
        r'SELECT\s+COUNT\s*\(\s*\*\s*\)(?!\s+AS)',
        r'SELECT (COUNT(*) AS ?count)', query, flags=re.IGNORECASE)

    _stripped_filter_bodies: list = []

    def _strip_bad_filters(q):

        out   = []
        i     = 0
        while i < len(q):
            m = re.search(r'(?i)FILTER\s*\(', q[i:])
            if not m:
                out.append(q[i:])
                break
            start = i + m.start()
            paren_start = i + m.end() - 1
            depth  = 1
            j      = paren_start + 1
            while j < len(q) and depth > 0:
                if   q[j] == '(': depth += 1
                elif q[j] == ')': depth -= 1
                j += 1
            filter_body = q[paren_start+1 : j-1]
            is_bad = (
                '\\' in filter_body or
                '{' in filter_body or
                re.search(r'=\s*[a-zA-Z]\d{4,}', filter_body)
            )
            if is_bad:
                out.append(q[i:start])
                _stripped_filter_bodies.append(filter_body)
                i = j
            else:
                out.append(q[i:start + len(m.group())])
                i = paren_start + 1
        return ''.join(out)

    query = _strip_bad_filters(query)

    def _strip_service_blocks(q):
        """Remove SERVICE <uri> { ... } blocks, handling nested braces."""
        result = []
        i = 0
        while i < len(q):
            m = re.search(r'SERVICE\s+\S+\s*\{', q[i:], re.IGNORECASE)
            if not m:
                result.append(q[i:])
                break
            result.append(q[i : i + m.start()])
            depth = 1
            j = i + m.end()
            while j < len(q) and depth > 0:
                if   q[j] == '{': depth += 1
                elif q[j] == '}': depth -= 1
                j += 1
            i = j
        return ''.join(result)

    _had_wikibase_service = bool(
        re.search(r'SERVICE\s+wikibase:', query, re.IGNORECASE))

    query = _strip_service_blocks(query)
    def _rewrite_label_vars(q):
        if not _had_wikibase_service:
            return q
        def _replace_sel(m):
            return re.sub(r'\?([A-Za-z_]\w*)Label\b', r'?\1', m.group(0))
        return re.sub(r'SELECT\s+.*?WHERE', _replace_sel, q,
                      count=1, flags=re.IGNORECASE | re.DOTALL)

    query = _rewrite_label_vars(query)

    query = re.sub(r"\.\s*}", " }", query)
    query = re.sub(r"\s+", " ", query).strip()

    return query, _stripped_filter_bodies

PREFIXES = """
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbr:  <http://dbpedia.org/resource/>
PREFIX dbp:  <http://dbpedia.org/property/>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX dct:  <http://purl.org/dc/terms/>
PREFIX geo:  <http://www.w3.org/2003/01/geo/wgs84_pos
PREFIX wd:   <http://www.wikidata.org/entity/>
PREFIX wdt:  <http://www.wikidata.org/prop/direct/>
PREFIX p:    <http://www.wikidata.org/prop/>
PREFIX ps:   <http://www.wikidata.org/prop/statement/>
PREFIX pq:   <http://www.wikidata.org/prop/qualifier/>
"""

RDF_TYPE   = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ENTITY_PREFIXES = (
    "http://dbpedia.org/resource/",
    "http://wikidata.dbpedia.org/resource/",
    "http://www.wikidata.org/entity/",
)

KNOWN_QUESTION_WORDS = {
    "who", "whom", "whose", "where", "when",
    "how many", "how much", "how long", "how old",
    "which", "what",
}

def _check_variable_flow(steps):
    """Return (True, None) if every step's input variables are either
    produced by a prior step OR are free root variables (appear as input
    but are never produced by any step — these are implicitly bound by
    the KG traversal starting point, e.g. ?film in ?film wdt:P57 ?dir).

    Handles union branches, subquery sub_steps, and seed_values."""

    def _all_outputs(step_list):
        outs = set()
        for s in step_list:
            if s.get("output_variable"):
                outs.add(s["output_variable"])
            for branch in s.get("branches", []):
                outs |= _all_outputs(branch)
            outs |= _all_outputs(s.get("sub_steps", []))
        return outs

    all_produced = _all_outputs(steps)

    def _all_inputs(step_list):
        ins = set()
        for s in step_list:
            for k in ("subject_variable", "object_variable"):
                v = s.get(k)
                if v: ins.add(v)
            for branch in s.get("branches", []):
                ins |= _all_inputs(branch)
            ins |= _all_inputs(s.get("sub_steps", []))
        return ins

    all_inputs = _all_inputs(steps)

    free_vars = all_inputs - all_produced

    produced = set(free_vars)

    for s in steps:
        act = s.get("action", "")
        if act == "seed_values":
            for v in s.get("variables", []):
                produced.add(v)
        elif act == "union":
            for branch in s.get("branches", []):
                for bs in branch:
                    if bs.get("output_variable"):
                        produced.add(bs["output_variable"])
        elif act == "subquery":
            for ss in s.get("sub_steps", []):
                if ss.get("output_variable"):
                    produced.add(ss["output_variable"])

        for key in ("subject_variable", "object_variable"):
            v = s.get(key)
            if v and v not in produced:
                return False, f"step {s['step']} ({act}): unbound {key}={v}"
        if s.get("output_variable"):
            produced.add(s["output_variable"])

    return True, None

def is_valid_training_sample(plan, for_t2=False, sparql_query=None, endpoint_url=None):
    """
    Returns (is_valid: bool, rejection_reason: str | None).

    Parameters
    ----------
    plan          : dict  — output of build_plan()
    for_t2        : bool  — if True, apply Rule 4 (gold SPARQL execution check)
    sparql_query  : str   — original gold SPARQL (needed for Rule 4)
    endpoint_url  : str   — SPARQL endpoint URL (needed for Rule 4), e.g.
                            "https://query.wikidata.org/sparql"

    Usage
    -----
    plan = build_plan(sparql, question=question)
    valid, reason = is_valid_training_sample(plan)
    if not valid:
        continue
    """
    steps = plan.get("steps", [])
    hints = plan.get("sparql_hints", {})

    _HOP_ACTIONS = {
        "find_object", "find_subjects", "find_statement", "filter_statement",
        "find_by_type", "optional_find", "left_join", "optional_expand",
        "verify_fact", "find_qualifier", "property_path"
    }
    hop_count  = sum(1 for s in steps if s.get("action") in _HOP_ACTIONS)
    query_form = hints.get("query_form", "SELECT")
    if hop_count == 0 and query_form != "ASK":
        return False, f"hop_count=0 on a SELECT query: all triples were dropped or misclassified"

    for s in steps:
        for required_key in ("step", "action", "semantic_type"):
            if required_key not in s:
                return False, f"step missing required key '{required_key}': {s}"

    for i, s in enumerate(steps):
        if s["step"] != i + 1:
            return False, f"step numbering broken at position {i}: got step={s['step']}"

    flow_ok, flow_err = _check_variable_flow(steps)
    if not flow_ok:
        return False, f"broken variable flow: {flow_err}"

    for s in steps:
        ov = s.get("output_variable")
        fv = s.get("filter_variable")
        if ov and fv and ov == fv:
            return False, f"step {s['step']}: output_variable == filter_variable == {ov}"

    if for_t2 and endpoint_url and sparql_query:
        try:
            import urllib.request, urllib.parse
            encoded = urllib.parse.urlencode({"query": sparql_query, "format": "json"})
            req = urllib.request.Request(
                f"{endpoint_url}?{encoded}",
                headers={"User-Agent": "sparql-planner-filter/1.0",
                         "Accept": "application/sparql-results+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            if "boolean" in data:
                return True, None
            results = data.get("results", {}).get("bindings", [])
            if not results:
                return False, "gold SPARQL returned empty results (stale entity ID or wrong query)"
        except Exception as e:

            pass

    return True, None

def build_training_dataset(lcquad2_path,
                            output_path=None,
                            dataset="lcquad2",
                            for_t2=False,
                            endpoint_url=None,
                            verbose=True):
    from sparql_planner import build_plan
    """
    Load a LC-QuAD 2 (or LC-QuAD 1) JSON file, run build_plan() on every
    entry, apply all quality filters, and return a list of clean training
    records — optionally writing them to a JSON file.

    Parameters
    ----------
    lcquad2_path : str   — path to the raw LC-QuAD JSON file
    output_path  : str   — if set, write filtered records as JSON Lines here
    dataset      : str   — "lcquad2" (default) or "lcquad1"
    for_t2       : bool  — if True, also apply Rule 4 (gold SPARQL execution)
    endpoint_url : str   — SPARQL endpoint for Rule 4 (only used if for_t2=True)
    verbose      : bool  — print per-reason rejection counts when done

    Returns
    -------
    list of dicts, each:
        {
            "uid":      str,
            "question": str,
            "sparql":   str,
            "plan":     dict
        }

    Rejection reason counters are printed when verbose=True:
        parse_failed          — Rule 1
        zero_hops             — Rule 2
        schema_violation      — Rule 3 (missing keys / numbering / flow / ov==fv)
        empty_results         — Rule 4 (endpoint execution, only if for_t2=True)
    """
    with open(lcquad2_path, encoding="utf-8") as f:
        raw = json.load(f)

    records  = []
    rejected = {"parse_failed": 0, "zero_hops": 0,
                "schema_violation": 0, "empty_results": 0, "other": 0}
    total = len(raw)

    for entry in raw:

        if dataset == "lcquad2":
            uid, question, sparql = extract_lcquad2(entry)
        else:

            uid      = str(entry.get("_id", entry.get("uid", "")))
            question = (entry.get("corrected_question")
                        or entry.get("question", "")).strip()
            sparql   = entry.get("sparql_query", "").strip()

        if not sparql or not question:
            rejected["other"] += 1
            continue

        try:
            plan = build_plan(sparql, question=question)
        except ValueError:
            rejected["parse_failed"] += 1
            continue
        except Exception as e:
            rejected["other"] += 1
            continue

        valid, reason = is_valid_training_sample(
            plan,
            for_t2=for_t2,
            sparql_query=sparql,
            endpoint_url=endpoint_url
        )

        if not valid:

            if "parse_failed"   in (reason or ""): rejected["parse_failed"]       += 1
            elif "hop_count"    in (reason or ""): rejected["zero_hops"]           += 1
            elif "empty result" in (reason or ""): rejected["empty_results"]       += 1
            else:                                  rejected["schema_violation"]     += 1
            continue

        records.append({
            "uid":      uid,
            "question": question,
            "sparql":   sparql,
            "plan":     plan
        })

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if verbose:
        kept = len(records)
        print(f"\nDataset: {lcquad2_path}")
        print(f"  Total entries   : {total:>6}")
        print(f"  Kept            : {kept:>6}  ({100*kept/total:.1f}%)")
        print(f"  Rejected        : {total-kept:>6}  ({100*(total-kept)/total:.1f}%)")
        print(f"    parse_failed  : {rejected['parse_failed']:>6}")
        print(f"    zero_hops     : {rejected['zero_hops']:>6}")
        print(f"    schema_error  : {rejected['schema_violation']:>6}")
        if for_t2:
            print(f"    empty_results : {rejected['empty_results']:>6}")
        print(f"    other         : {rejected['other']:>6}")
        if output_path:
            print(f"  Written to      : {output_path}")

    return records



