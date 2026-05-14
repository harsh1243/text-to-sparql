import re
import json
from rdflib import URIRef, Literal
from rdflib.term import Variable
from rdflib.plugins.sparql import parser, algebra
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.paths import Path

from lcquad_utils_new import (
    normalize_lcquad_query,
    walk, local_name, entity_surface, safe_var,
    is_entity_uri, is_type_pred, is_label_pred,
    is_statement_link_pred, is_qualifier_pred, is_statement_value_pred,
    collect_bgp_triples, collect_filters, collect_union_branches,
    parse_filter_expr, extract_order_limit, extract_group_by,
    extract_agg_info, extract_agg_info_from_algebra, extract_subquery_plan,
    extract_bind, extract_values,
    has_union, has_minus, has_distinct, has_subquery,
    OWLIndex,
)

PREFIXES = """
PREFIX dbo:  <http://dbpedia.org/ontology/>
PREFIX dbr:  <http://dbpedia.org/resource/>
PREFIX dbp:  <http://dbpedia.org/property/>
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX dct:  <http://purl.org/dc/terms/>
PREFIX geo:  <http://www.w3.org/2003/01/geo/wgs84_pos#>
PREFIX wd:   <http://www.wikidata.org/entity/>
PREFIX wdt:  <http://www.wikidata.org/prop/direct/>
PREFIX p:    <http://www.wikidata.org/prop/>
PREFIX ps:   <http://www.wikidata.org/prop/statement/>
PREFIX pq:   <http://www.wikidata.org/prop/qualifier/>
"""


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
        if action == "distinct":                                            return 7.5
        if action in ("limit", "sort_and_limit"):                           return 8
        return 10

    sorted_steps = sorted(steps, key=lambda s: get_weight(s.get("action", "")))

    def _topo_resolve(steps):
        HOP_ACTIONS = {
            "find_by_type", "find_object", "find_subjects", "property_path",
            "find_statement", "filter_statement",
            "find_qualifier", "optional_find", "left_join", "optional_expand"
        }
        pre  = [s for s in steps if get_weight(s.get("action", "")) <  4]
        hops = [s for s in steps if s.get("action") in HOP_ACTIONS]
        post = [s for s in steps if get_weight(s.get("action", "")) >  4]

        if len(hops) <= 1:
            return steps

        bound = set()
        for s in pre:
            v = s.get("output_variable")
            if v: bound.add(v)

        _hop_inputs  = set()
        _hop_outputs = set()
        for s in hops:
            inp = (s.get("subject_variable") or s.get("object_variable") or s.get("filter_variable"))
            if inp: _hop_inputs.add(inp)
            out = s.get("output_variable")
            if out: _hop_outputs.add(out)
        for v in _hop_inputs:
            if v not in _hop_outputs and v not in bound:
                bound.add(v)

        ordered   = []
        remaining = list(hops)
        max_iter  = len(hops) ** 2 + 1
        iters     = 0

        def step_priority(step):
            action = step.get("action")
            if action == "find_entity":     return 0
            if action in ("find_subjects", "find_object") and not step.get("is_qualifier"): return 1
            if action == "find_statement":  return 2
            if action == "filter_statement":return 3
            if step.get("is_qualifier"):    return 4
            if action == "filter":          return 5
            return 6

        while remaining and iters < max_iter:
            iters += 1
            progress = False
            remaining.sort(key=step_priority)
            for s in list(remaining):
                inputs = [v for v in [
                    s.get("subject_variable"),
                    s.get("object_variable"),
                    s.get("filter_variable")
                ] if v is not None]
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


def build_plan(sparql_query: str, question: str = "", owl: OWLIndex = None) -> dict:
    sparql_query, _ = normalize_lcquad_query(sparql_query)

    try:
        full_query = sparql_query if re.match(r'\s*PREFIX', sparql_query, re.IGNORECASE) else PREFIXES + sparql_query.strip()
        parsed = parser.parseQuery(full_query)
        alg    = algebra.translateQuery(parsed).algebra
    except Exception as e:
        raise ValueError(f"SPARQL parse error: {e}") from e

    steps    = []
    step_num = [0]
    def next_step():
        step_num[0] += 1
        return step_num[0]

    query_form = "ASK" if re.search(r"\bASK\b", sparql_query.upper()) else "SELECT"

    regular_triples, optional_triples, minus_clauses = collect_bgp_triples(alg)
    order_info    = extract_order_limit(alg)

    def _strip_nested_selects(q):
        result = []
        i = 0
        while i < len(q):
            m = re.search(r'\{\s*SELECT\b', q[i:], re.IGNORECASE)
            if not m:
                result.append(q[i:])
                break
            result.append(q[i : i + m.start()])
            depth = 1
            j = i + m.start() + 1
            while j < len(q) and depth > 0:
                if   q[j] == '{': depth += 1
                elif q[j] == '}': depth -= 1
                j += 1
            result.append(' ')
            i = j
        return ''.join(result)

    _outer_query  = _strip_nested_selects(sparql_query)
    group_info    = extract_group_by(alg, _outer_query)
    agg_list      = extract_agg_info(_outer_query)
    bind_list     = extract_bind(alg)
    values_blocks = extract_values(alg)
    union         = has_union(alg)
    minus         = has_minus(alg)
    distinct      = has_distinct(sparql_query)
    subquery      = has_subquery(alg)

    union_branches_data = []
    union_triple_keys   = set()
    union_filter_ids    = set()
    if union:
        union_branches_data = collect_union_branches(alg)
        for bt, bf in union_branches_data:
            for t in bt:
                union_triple_keys.add((str(t[0]), str(t[1]), str(t[2])))
            for f_expr in bf:
                union_filter_ids.add(id(f_expr))

    regular_triples = [t for t in regular_triples
                       if (str(t[0]), str(t[1]), str(t[2])) not in union_triple_keys]

    raw_filters_all  = collect_filters(alg)
    raw_filters_flat = [f for f in raw_filters_all if id(f) not in union_filter_ids]
    all_filters      = [f for f in (parse_filter_expr(f) for f in raw_filters_flat)
                        if f and f.get("type") != "unknown"]

    if subquery:
        for sp in extract_subquery_plan(alg, question, owl):
            steps.append({"step": next_step(), "action": "subquery",
                           "description": "execute inner subquery",
                           "sub_steps": sp["steps"], "semantic_type": "MODIFIER"})

    _select_clause = re.search(r'SELECT\s+(.*?)\s*WHERE', sparql_query, re.IGNORECASE | re.DOTALL)
    _select_vars   = set()
    if _select_clause:
        sel_text = _select_clause.group(1).strip()
        if sel_text == '*':
            _select_vars = {'*'}
        else:
            as_vars   = set(re.findall(r'AS\s+\?(\w+)', sel_text, re.IGNORECASE))
            sel_clean = re.sub(r'(?:COUNT|MAX|MIN|SUM|AVG|SAMPLE|GROUP_CONCAT)\s*\([^)]*\)', '', sel_text, flags=re.IGNORECASE)
            bare_vars = set(re.findall(r'\?(\w+)', sel_clean))
            _select_vars = {f"?{v}" for v in (as_vars | bare_vars)}

    entity_uri_to_var = {}

    def ensure_entity_step(uri_node):
        key = str(uri_node)
        if key in entity_uri_to_var: return entity_uri_to_var[key]
        surface  = entity_surface(uri_node)
        var_name = safe_var(key)
        steps.append({"step": next_step(), "action": "find_entity",
                       "description": f"locate {surface} in the knowledge graph",
                       "surface_form": surface, "semantic_type": "ENTITY",
                       "output_variable": var_name})
        entity_uri_to_var[key] = var_name
        return var_name

    def process_triple(s, p, o, is_optional=False):
        p_full    = str(p)
        prop_name = local_name(p_full)
        action    = "left_join" if is_optional else "find_object"

        if isinstance(p, (CompValue, Path)):
            steps.append({"step": next_step(), "action": "property_path",
                           "description": f"follow property path {p_full}",
                           "property_path": p_full,
                           "subject_variable": f"?{str(s)}" if isinstance(s, Variable) else None,
                           "object_variable":  f"?{str(o)}" if isinstance(o, Variable) else None,
                           "semantic_type": "PROPERTY"})
            return

        if is_statement_link_pred(p):
            subj_var = ensure_entity_step(s) if is_entity_uri(s) else f"?{str(s)}"
            steps.append({"step": next_step(), "action": "find_statement",
                           "description": f"find {prop_name} statement of {entity_surface(s) if is_entity_uri(s) else str(s)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "subject_variable": subj_var, "output_variable": f"?{str(o)}"})
            return

        if is_qualifier_pred(p):
            if isinstance(s, Variable) and isinstance(o, Variable):
                steps.append({"step": next_step(), "action": "find_object",
                               "description": f"get qualifier {prop_name} from ?{str(s)}",
                               "property": prop_name, "semantic_type": "QUALIFIER",
                               "subject_variable": f"?{str(s)}", "output_variable": f"?{str(o)}",
                               "is_qualifier": True})
            elif isinstance(s, Variable) and is_entity_uri(o):
                steps.append({"step": next_step(), "action": "filter_statement",
                               "description": f"restrict ?{str(s)} where qualifier {prop_name} = {entity_surface(o)}",
                               "property": prop_name, "semantic_type": "QUALIFIER",
                               "filter_variable": f"?{str(s)}", "value_variable": ensure_entity_step(o),
                               "is_qualifier": True})
            elif isinstance(s, Variable) and isinstance(o, Literal):
                lit_val = str(o).split("^^")[0].strip('"\' ')
                vtype   = "date" if ("date" in str(getattr(o, "datatype", "") or "").lower() or re.match(r'\d{4}-\d{2}-\d{2}', lit_val)) else "literal"
                steps.append({"step": next_step(), "action": "filter_statement",
                               "description": f"restrict ?{str(s)} where qualifier {prop_name} = {lit_val}",
                               "property": prop_name, "semantic_type": "QUALIFIER",
                               "filter_variable": f"?{str(s)}", "value": lit_val,
                               "value_type": vtype, "is_qualifier": True})
            return

        if is_statement_value_pred(p):
            if isinstance(s, Variable) and is_entity_uri(o):
                steps.append({"step": next_step(), "action": "filter_statement",
                               "description": f"restrict ?{str(s)} to where {prop_name} = {entity_surface(o)}",
                               "property": prop_name, "semantic_type": "PROPERTY",
                               "filter_variable": f"?{str(s)}", "value_variable": ensure_entity_step(o)})
            elif isinstance(s, Variable) and isinstance(o, Literal):
                lit_val = str(o).split("^^")[0].strip('"\' ')
                steps.append({"step": next_step(), "action": "filter_statement",
                               "description": f"restrict ?{str(s)} where {prop_name} = {lit_val}",
                               "property": prop_name, "semantic_type": "PROPERTY",
                               "filter_variable": f"?{str(s)}", "value": lit_val, "value_type": "literal"})
            elif isinstance(s, Variable) and isinstance(o, Variable):
                steps.append({"step": next_step(), "action": "find_object",
                               "description": f"get {prop_name} value from ?{str(s)}",
                               "property": prop_name, "semantic_type": "PROPERTY",
                               "subject_variable": f"?{str(s)}", "output_variable": f"?{str(o)}"})
            return

        if is_type_pred(p):
            if isinstance(s, Variable) and isinstance(o, URIRef):
                target_var    = f"?{str(s)}"
                already_bound = any(target_var in (st.get("output_variable"), st.get("subject_variable"),
                                                   st.get("object_variable"), st.get("filter_variable"))
                                    for st in steps)
                if not already_bound:
                    steps.append({"step": next_step(), "action": "find_by_type",
                                   "description": f"find all entities of type {local_name(str(o))}",
                                   "type": local_name(str(o)), "semantic_type": "CLASS",
                                   "output_variable": target_var})
                else:
                    steps.append({"step": next_step(), "action": "filter_type",
                                   "description": f"constrain ?{str(s)} to type {local_name(str(o))}",
                                   "type": local_name(str(o)), "filter_variable": target_var,
                                   "semantic_type": "CLASS"})
            elif is_entity_uri(s) and isinstance(o, Variable):
                steps.append({"step": next_step(), "action": "find_object",
                               "description": f"get type of {entity_surface(s)}",
                               "property": "type", "semantic_type": "CLASS",
                               "subject_variable": ensure_entity_step(s), "output_variable": f"?{str(o)}"})
            elif is_entity_uri(s) and isinstance(o, URIRef):
                steps.append({"step": next_step(), "action": "verify_fact",
                               "description": f"check if {entity_surface(s)} is of type {local_name(str(o))}",
                               "property": "type", "semantic_type": "CLASS",
                               "subject_variable": ensure_entity_step(s), "object_type": local_name(str(o))})
            elif isinstance(s, Variable) and isinstance(o, Variable):
                steps.append({"step": next_step(), "action": "find_object",
                               "description": f"get type of ?{str(s)}",
                               "property": "type", "semantic_type": "CLASS",
                               "subject_variable": f"?{str(s)}", "output_variable": f"?{str(o)}"})
            return

        if is_label_pred(p):
            if isinstance(s, Variable) and isinstance(o, Literal):
                steps.append({"step": next_step(), "action": "find_entity",
                               "description": f"locate {str(o).replace('@en','').strip()} via label",
                               "surface_form": str(o).replace("@en", "").strip(),
                               "semantic_type": "ENTITY", "output_variable": f"?{str(s)}"})
            elif isinstance(o, Variable):
                subj_var = ensure_entity_step(s) if is_entity_uri(s) else f"?{str(s)}"
                steps.append({"step": next_step(), "action": "find_object",
                               "description": f"get label of {entity_surface(s) if is_entity_uri(s) else str(s)}",
                               "property": "label", "semantic_type": "PROPERTY",
                               "subject_variable": subj_var, "output_variable": f"?{str(o)}"})
            return

        if is_entity_uri(s) and isinstance(o, Variable):
            steps.append({"step": next_step(), "action": action,
                           "description": f"get {prop_name} of {entity_surface(s)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "subject_variable": ensure_entity_step(s), "output_variable": f"?{str(o)}"})
            return

        if isinstance(s, Variable) and is_entity_uri(o):
            steps.append({"step": next_step(), "action": "find_subjects",
                           "description": f"find subjects with {prop_name} = {entity_surface(o)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "object_variable": ensure_entity_step(o), "output_variable": f"?{str(s)}"})
            return

        if isinstance(s, Variable) and isinstance(o, Variable):
            steps.append({"step": next_step(), "action": action,
                           "description": f"get {prop_name} of ?{str(s)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "subject_variable": f"?{str(s)}", "output_variable": f"?{str(o)}"})
            return

        if isinstance(s, Variable) and isinstance(o, Literal):
            steps.append({"step": next_step(), "action": "filter",
                           "description": f"filter where {prop_name} equals {str(o)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "filter_variable": f"?{str(s)}", "operator": "equals",
                           "value": str(o), "value_type": "string"})
            return

        if is_entity_uri(s) and is_entity_uri(o):
            steps.append({"step": next_step(), "action": "verify_fact",
                           "description": f"check if {entity_surface(s)} has {prop_name} {entity_surface(o)}",
                           "property": prop_name, "semantic_type": "PROPERTY",
                           "subject_variable": ensure_entity_step(s), "object_variable": ensure_entity_step(o)})
            return

        if isinstance(o, URIRef) and not is_entity_uri(o):
            val_str = str(o)
            vtype   = "date" if re.match(r'\d{4}-\d{1,2}-\d{1,2}', val_str) else "uri"
            fvar    = ensure_entity_step(s) if is_entity_uri(s) else (f"?{str(s)}" if isinstance(s, Variable) else "?x")
            steps.append({"step": next_step(), "action": "filter",
                           "description": f"filter where {prop_name} equals {val_str}",
                           "filter_variable": fvar, "operator": "equals",
                           "value": val_str, "value_type": vtype, "semantic_type": "LITERAL"})

    for s, p, o in regular_triples:  process_triple(s, p, o, False)
    for s, p, o in optional_triples: process_triple(s, p, o, True)

    _COLLISION_ACTIONS = {"find_subjects", "find_object", "left_join", "optional_find", "optional_expand"}
    _produced = {}
    for _i, _st in enumerate(steps):
        _ov = _st.get("output_variable")
        if _ov and _st["action"] not in ("find_by_type", "filter_type"):
            if _ov not in _produced:
                _produced[_ov] = _i

    for _i, _st in enumerate(steps):
        if _st["action"] not in _COLLISION_ACTIONS: continue
        _ov = _st.get("output_variable")
        if _ov is None: continue
        _first_idx = _produced.get(_ov)
        if _first_idx is not None and _first_idx < _i:
            _subj = _st.get("subject_variable")
            if _st["action"] == "find_object" and _subj and _subj not in _produced:
                _st["action"] = "find_subjects"
                _st["output_variable"] = _subj
                _st["object_variable"] = _ov
                _st.pop("subject_variable", None)
                _st["description"] = f"find subjects where {_st.get('property','?')} = {_ov}"
                _produced[_subj] = _i
            else:
                _st.pop("output_variable")
                _st["filter_variable"] = _ov
                _st["join_type"]       = "intersect"
                _st["description"]     = _st.get("description", "").replace("find subjects with", "constrain to").replace("get ", "join on ")

    other_outs = {st.get("output_variable") for st in steps if st["action"] not in ("find_by_type", "filter_type")}
    for st in steps:
        if st["action"] == "find_by_type" and st.get("output_variable") in other_outs:
            st["action"] = "filter_type"
            st["filter_variable"] = st.pop("output_variable")
            st["semantic_type"]   = "CLASS"
            st["description"]     = st.get("description", "").replace("find all entities of type", "constrain to type")

    fo_out_vars = {st.get("output_variable"): i for i, st in enumerate(steps)
                   if st["action"] in ("find_object", "left_join") and st.get("output_variable")}
    reordered = list(steps)
    moved = set()
    for i, st in enumerate(steps):
        if st["action"] == "find_subjects" and i not in moved:
            out = st.get("output_variable")
            if out and out in fo_out_vars and fo_out_vars[out] > i:
                reordered.remove(st)
                reordered.insert(reordered.index(steps[fo_out_vars[out]]) + 1, st)
                moved.add(i)
    steps = reordered

    def emit_filter(f):
        ftype = f.get("type", "")
        if ftype == "not_exists":
            expr_str = str(f.get("expr", ""))
            prop_m   = (re.search(r'dbo:(\w+)', expr_str) or re.search(r'wdt:(\w+)', expr_str) or
                        re.search(r'\bps:(\w+)', expr_str) or re.search(r'\bpq:(\w+)', expr_str) or
                        re.search(r'\bp:(\w+)', expr_str) or re.search(r'dbpedia\.org/ontology/(\w+)', expr_str) or
                        re.search(r'wikidata\.org/prop/[^/]*/(\w+)', expr_str))
            pname  = prop_m.group(1) if prop_m else "unknown"
            var_m  = re.search(r'\?(\w+)', expr_str) or re.search(r"Variable\('(\w+)'\)", expr_str)
            steps.append({"step": next_step(), "action": "exclude",
                           "description": f"remove results where {pname} exists",
                           "property": pname, "semantic_type": "PROPERTY",
                           "filter_variable": f"?{var_m.group(1)}" if var_m else "?x",
                           "operator": "not_exists"})
        elif ftype == "exists":
            expr_str = str(f.get("expr", ""))
            prop_m   = (re.search(r'dbo:(\w+)', expr_str) or re.search(r'wdt:(\w+)', expr_str) or
                        re.search(r'\bps:(\w+)', expr_str) or re.search(r'\bpq:(\w+)', expr_str) or
                        re.search(r'\bp:(\w+)', expr_str) or re.search(r'dbpedia\.org/ontology/(\w+)', expr_str) or
                        re.search(r'wikidata\.org/prop/[^/]*/(\w+)', expr_str))
            pname  = prop_m.group(1) if prop_m else "unknown"
            var_m  = re.search(r'\?(\w+)', expr_str)
            steps.append({"step": next_step(), "action": "exists",
                           "description": f"keep results where {pname} exists",
                           "property": pname, "semantic_type": "PROPERTY",
                           "filter_variable": f"?{var_m.group(1)}" if var_m else "?x"})
        elif ftype == "date_filter":
            step = {"step": next_step(), "action": "filter",
                    "description": f"keep results where {f['variable']} {f['operator']} {f['value']}",
                    "filter_variable": f["variable"], "operator": f["operator"],
                    "value": f["value"], "value_type": f.get("value_type", "date"), "semantic_type": "LITERAL"}
            if f.get("apply_fn"):
                step["apply_fn"]    = f["apply_fn"]
                step["description"] = f"apply {f['apply_fn']}({f['variable']}) {f['operator']} {f['value']}"
            steps.append(step)
        elif ftype in ("numeric_filter", "string_filter", "regex_filter"):
            fvar = f.get("variable", "?value")
            fval = f.get("value", "")
            if fvar in ("?value", "?val"):
                for st in steps:
                    if st.get("action") == "find_object" and st.get("property") == "label":
                        fvar = st.get("output_variable", fvar)
            if fval in ("other", "") and question:
                q_lower = question.lower()
                quoted  = re.findall(r'["\']{1}(.{2,}?)["\']{1}', q_lower)
                fval    = quoted[-1].strip() if quoted else fval
            op = "regex" if ftype == "regex_filter" else f.get("operator", "contains")
            val = f.get("pattern", fval) if ftype == "regex_filter" else fval
            steps.append({"step": next_step(), "action": "filter",
                           "description": f"filter where {fvar} {op} {val}",
                           "filter_variable": fvar, "operator": op,
                           "value": val, "value_type": "string", "semantic_type": "LITERAL"})
        elif ftype == "lang_filter":
            steps.append({"step": next_step(), "action": "filter",
                           "description": f"restrict {f.get('variable','?label')} to language '{f.get('value','en')}'",
                           "filter_variable": f.get("variable", "?label"), "operator": "lang_equals",
                           "value": f.get("value", "en"), "value_type": "lang", "semantic_type": "LITERAL"})
        elif ftype == "in_filter":
            steps.append({"step": next_step(), "action": "filter",
                           "description": f"filter where {f['variable']} in {f['values']}",
                           "filter_variable": f["variable"], "operator": "in",
                           "values": f["values"], "value_type": "set", "semantic_type": "LITERAL"})
        elif ftype == "compound":
            if f.get("logic") == "OR":
                parts = []
                for _p in f.get("parts", []):
                    if not _p: continue
                    pt = _p.get("type", "")
                    d  = {"action": "filter", "filter_variable": _p.get("variable", "?value"),
                          "operator": _p.get("operator", "equals" if pt != "string_filter" else "contains"),
                          "value": _p.get("value", ""), "value_type": _p.get("value_type", "literal")}
                    if _p.get("apply_fn"): d["apply_fn"] = _p["apply_fn"]
                    parts.append(d)
                steps.append({"step": next_step(), "action": "or_filter",
                               "description": "keep results matching any OR condition",
                               "parts": parts, "semantic_type": "LITERAL"})
            else:
                for part in f.get("parts", []):
                    if part: emit_filter(part)

    if union:
        branch_plans = []
        for branch_triples, branch_filter_exprs in union_branches_data:
            saved_cache = dict(entity_uri_to_var)
            entity_uri_to_var.clear()
            mark = len(steps)

            for s, p, o in branch_triples:
                process_triple(s, p, o, False)

            branch_outs = {st.get("output_variable") for st in steps[mark:]
                           if st["action"] not in ("find_by_type", "filter_type")}
            for st in steps[mark:]:
                if st["action"] == "find_by_type" and st.get("output_variable") in branch_outs:
                    st["action"] = "filter_type"
                    st["filter_variable"] = st.pop("output_variable")
                    st["semantic_type"]   = "CLASS"
                    st["description"]     = st.get("description", "").replace("find all entities of type", "constrain to type")

            for fe in branch_filter_exprs:
                pf = parse_filter_expr(fe)
                if pf and pf.get("type") != "unknown": emit_filter(pf)

            branch_step_list = steps[mark:]
            for i, bs in enumerate(branch_step_list): bs["step"] = i + 1
            branch_plans.append(list(branch_step_list))
            del steps[mark:]
            entity_uri_to_var.clear()
            entity_uri_to_var.update(saved_cache)

        steps.append({"step": next_step(), "action": "union",
                       "description": f"combine results from {len(branch_plans)} UNION branches",
                       "branches": branch_plans, "semantic_type": "MODIFIER"})

        union_outs = {bs.get("output_variable") for bp in branch_plans for bs in bp}
        for st in steps:
            if st.get("action") == "find_by_type" and st.get("output_variable") in union_outs:
                st["action"] = "filter_type"
                st["filter_variable"] = st.pop("output_variable")
                st["semantic_type"]   = "CLASS"
                st["description"]     = st.get("description", "").replace("find all entities of type", "constrain to type")

    for b in bind_list:
        step = {"step": next_step(), "action": "bind",
                "description": f"compute {b['variable']} from {b['expr_type']}",
                "output_variable": b["variable"], "expr_type": b["expr_type"], "semantic_type": "MODIFIER"}
        if b.get("source_variable"): step["source_variable"] = b["source_variable"]
        steps.append(step)

    for v in values_blocks:
        steps.append({"step": next_step(), "action": "seed_values",
                       "description": "initialize variables from VALUES clause",
                       "variables": v["variables"], "rows": v["rows"], "semantic_type": "MODIFIER"})

    for f in all_filters:
        emit_filter(f)

    if minus:
        for clause in minus_clauses:
            props = [local_name(str(mp)) for ms, mp, mo in clause if not isinstance(mp, CompValue)]
            var   = next((f"?{str(ms)}" for ms, mp, mo in clause if isinstance(ms, Variable)),
                         next((f"?{str(mo)}" for ms, mp, mo in clause if isinstance(mo, Variable)), "?x"))
            steps.append({"step": next_step(), "action": "exclude",
                           "description": f"subtract results matching MINUS pattern ({', '.join(props)})",
                           "semantic_type": "MODIFIER", "operator": "minus",
                           "properties": props, "filter_variable": var})

    if distinct and agg_list:
        steps.append({"step": next_step(), "action": "distinct",
                       "description": "remove duplicate inputs before aggregation",
                       "semantic_type": "MODIFIER"})

    for agg in agg_list:
        step = {"step": next_step(), "action": "aggregate",
                "description": f"apply {agg['function']}{'(DISTINCT)' if agg.get('is_distinct') else ''} over {agg['target']}",
                "function": agg["function"], "target_variable": agg["target"],
                "output_variable": agg["out_var"], "semantic_type": "AGGREGATION"}
        if agg.get("is_distinct"): step["is_distinct"] = True
        steps.append(step)

    if group_info["has_group_by"]:
        step = {"step": next_step(), "action": "group_by",
                "description": f"group results by {', '.join('?'+v for v in group_info['group_vars'])}",
                "group_variables": [f"?{v}" for v in group_info["group_vars"]],
                "semantic_type": "MODIFIER"}
        if group_info["has_having"]: step["having"] = group_info["having_raw"]
        steps.append(step)
        if group_info["has_having"]:
            hv   = group_info["having_raw"]
            hv_m = re.search(r'\?(\w+)', hv)
            op_m = re.search(r'([><=!]+)\s*([\d.]+)', hv)
            if hv_m and op_m:
                op_map = {"=":"equals","!=":"not_equals",">":"greater_than","<":"less_than",">=":"gte","<=":"lte"}
                hvar   = f"?{hv_m.group(1)}"
                steps.append({"step": next_step(), "action": "having_filter",
                               "description": f"apply HAVING: {hvar} {op_m.group(1)} {op_m.group(2)}",
                               "filter_variable": hvar, "operator": op_map.get(op_m.group(1), op_m.group(1)),
                               "value": op_m.group(2), "value_type": "number",
                               "semantic_type": "LITERAL", "source": "HAVING"})

    if order_info["has_order"] and order_info["has_limit"]:
        steps.append({"step": next_step(), "action": "sort_and_limit",
                       "description": f"sort by {order_info['sort_var']} {order_info['direction']} take top {order_info['limit_value']}",
                       "sort_variable": order_info["sort_var"], "sort_expression": order_info.get("sort_expr"),
                       "direction": order_info["direction"].lower(), "n": order_info["limit_value"],
                       "semantic_type": "MODIFIER"})
    elif order_info["has_order"]:
        steps.append({"step": next_step(), "action": "sort",
                       "description": f"sort by {order_info['sort_var']} {order_info['direction']}",
                       "sort_variable": order_info["sort_var"], "sort_expression": order_info.get("sort_expr"),
                       "direction": order_info["direction"].lower(), "semantic_type": "MODIFIER"})
    elif order_info["has_limit"]:
        steps.append({"step": next_step(), "action": "limit",
                       "description": f"take first {order_info['limit_value']} results",
                       "n": order_info["limit_value"], "semantic_type": "MODIFIER"})

    if distinct and not agg_list:
        steps.append({"step": next_step(), "action": "distinct",
                       "description": "remove duplicate results", "semantic_type": "MODIFIER"})

    steps = reorder_steps(steps)

    _sel_ordered = re.findall(r'\?(\w+)', (_select_clause.group(1) if _select_clause else ''))
    if len(_sel_ordered) >= 2:
        HOP_TYPES = {"find_object", "left_join", "find_subjects"}
        def _reorder_siblings(step_list):
            result, i = [], 0
            while i < len(step_list):
                s = step_list[i]
                if s.get("action") not in HOP_TYPES or not s.get("subject_variable"):
                    result.append(s); i += 1; continue
                subj = s["subject_variable"]
                run, j = [], i
                while j < len(step_list) and step_list[j].get("action") in HOP_TYPES and step_list[j].get("subject_variable") == subj:
                    run.append(step_list[j]); j += 1
                if len(run) > 1:
                    run.sort(key=lambda st: _sel_ordered.index(st.get("output_variable","").lstrip("?"))
                             if st.get("output_variable","").lstrip("?") in _sel_ordered else 999)
                result.extend(run); i = j
            return result
        steps = _reorder_siblings(steps)
        for idx, st in enumerate(steps): st["step"] = idx + 1

    q_lower = (question or "").lower()
    if any(q_lower.startswith(w) or f" {w} " in q_lower
           for w in ("when", "what date", "which year", "what year", "what time", "since when", "how long ago")):
        for st in steps:
            if st.get("action") == "verify_fact":
                st["action"]             = "find_qualifier"
                st["qualifier_property"] = "point in time"
                st["semantic_type"]      = "PROPERTY"
                st["description"]        = st.get("description", "").replace("check if", "get date when")

    _FILTER_ACTIONS = {"filter", "filter_type", "filter_statement", "exclude", "exists", "having_filter"}
    has_filter_flag = any(s["action"] in _FILTER_ACTIONS for s in steps)

    return {
        "steps": steps,
        "sparql_hints": {
            "query_form": query_form,
            "has_filter": has_filter_flag,
        }
    }


if __name__ == "__main__":
    QUERY = """
    SELECT ?value WHERE { wd:Q30971 p:P1082 ?s . ?s ps:P1082 ?x filter(contains(?x,'223734.0')) . ?s pq:P585 ?value}
    """
    plan = build_plan(QUERY)
    print(json.dumps(plan, indent=2))
