"""Token-efficient rendering of UIA snapshots — the model-facing presentation layer.

Everything here is pure: a plain dict in, a plain ``str`` out.  No UIA, no I/O,
so the formatter is unit-testable without touching the desktop.

Format (compaction v2 — lead-approved, tuned against real Edge numbers):

    win #8788190 "题目收集平台…Edge" 2560x1600 @-11,-11 max [focus]
    obs 312n/100 shown 219ms controls
    [8712] tabitem "对话" [s] *
     [8713] edit "搜索" val="dsh" *
     [8714] button "保存" [i]
      ›工具栏 [8715] button "刷新" [i]

* ``[id] ct "name"`` + optional ``val=`` / one-letter capability / state chars
* capability letters (only the strongest one):
  ``e`` expand > ``t`` toggle > ``s`` selection > ``v`` value > ``i`` invoke > ``c`` focusable
* state chars: ``*`` focused, ``+`` selected, ``-`` disabled, ``~`` offscreen
* names squeezed to one line (newlines -> ``⏎``), truncated at 48 chars with ``…``
* indentation: one space per level, at most three; deeper levels are flattened
  behind a ``›parent`` breadcrumb
* homogeneous siblings (same parent, same type) fold at 8: first 6 + ``…+N more
  (filter: kw)``.  Focused / selected siblings are never folded.
* ``view=controls`` drops window chrome (title-bar buttons, scrollbars) unless
  an explicit filter asks for it
* hard cap 10830 characters; overflow is cropped interactive-elements-first in
  screen reading order and reports ``…(+N hidden)``
"""
from __future__ import annotations

# --- budgets (lead-calibrated against windows-mcp baseline) -------------------
MAX_TEXT = 10830         # hard cap for one tool text block
CONTROL_TEXT_TARGET = 3500   # controls view target (lead acceptance: <=3500)
NAME_MAX = 48            # per-name character budget
VAL_MAX = 40             # per-value character budget
TITLE_MAX = 60           # window title budget
INDENT = " "             # one space per hierarchy level
INDENT_MAX = 3           # levels rendered as indentation
DETAIL_MAX = 160         # failure detail budget in cu_do output
HIDDEN_RESERVE = 28      # room reserved for the "…(+N hidden)" trailer
CUT_RESERVE = 44         # room reserved for the "cut=budget(+N hidden)" token

# --- sibling folding ----------------------------------------------------------
SIBLING_FOLD_MIN = 12    # fold a run of same-parent same-ct siblings at >= 12
SIBLING_FOLD_KEEP = 8    # keep this many (plus any active/focused members)
# Only these repetitive content lists fold.  Navigation/selection elements
# (treeitem / tabitem / checkbox / radiobutton, buttons) are the model's primary
# targets and are never folded: T3 measured recall 81.7% when tab strips folded.
FOLDABLE_CTS = frozenset({"listitem", "menuitem", "text"})

# --- element classes ----------------------------------------------------------
# Elements a model can act on; kept first when the text has to be cropped.
INTERACTIVE = frozenset({
    "button", "edit", "checkbox", "radiobutton", "combobox", "hyperlink",
    "menuitem", "tabitem", "listitem", "treeitem", "scrollbar", "slider",
    "spinner", "splitbutton", "datagrid", "dataitem",
})
# Elements that carry content rather than behaviour.
TEXTUAL = frozenset({"text", "document"})
# Pure layout containers: cheap to drop, useful for orientation only.
STRUCTURAL = frozenset({
    "pane", "window", "group", "toolbar", "menubar", "tab", "other", "image",
    "titlebar", "statusbar", "separator", "tooltip", "header",
})

# capability priority: strongest signal only, one letter each
PATTERN_CODES = (("expand", "e"), ("toggle", "t"), ("selection", "s"),
                 ("value", "v"), ("invoke", "i"), ("focusable", "c"))
# window chrome that is noise for control picking (view=all keeps it)
CHROME_CTS = frozenset({"scrollbar", "titlebar"})
CHROME_NAMES = frozenset({
    "最小化", "最大化", "还原", "恢复", "关闭", "系统菜单", "默认窗口",
    "工作区按钮", "全部最小化", "minimize", "maximize", "restore", "close",
    "system menu", "default window", "workspace button", "scroll bar", "滚动条",
})
# name-less structural containers that only add a level of indentation
CONTAINER_CTS = frozenset({
    "pane", "group", "document", "other", "list", "tab", "toolbar", "menubar",
    "table", "custom",
})


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def squeeze(value, limit: int = NAME_MAX) -> str:
    """Flatten arbitrary text to one short line (newlines -> ``⏎``)."""
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "⏎")
    s = s.replace("\t", " ")
    s = " ".join(s.split())
    s = s.replace('"', "'")
    if limit > 0 and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _rect(node) -> list:
    r = node.get("rect") if isinstance(node, dict) else None
    if not isinstance(r, (list, tuple)) or len(r) < 4:
        return [0, 0, 0, 0]
    out = []
    for v in r[:4]:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            out.append(0)
    return out


def _depth(node, default=0) -> int:
    try:
        return int(node.get("depth") or default)
    except (TypeError, ValueError):
        return default


def cap_text(text: str, max_chars: int = MAX_TEXT) -> str:
    """Hard-cap a finished text block (used after appending notes)."""
    if text is None:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _state_token(snap: dict) -> str:
    state = snap.get("state")
    if isinstance(state, str) and state:
        return {"maximized": "max", "minimized": "min",
                "normal": "norm"}.get(state.lower(), state)
    if snap.get("maximized"):
        return "max"
    if snap.get("minimized"):
        return "min"
    return ""


def _priority(node: dict) -> int:
    """Crop priority: 0 = must have … higher = dropped first.

    Ordering (recall-first, B3-audit tuned):
    0 visible interactive · 1 offscreen interactive · 2 disabled interactive
    3 visible text · 4 visible structural · 5 offscreen non-interactive
    6 disabled non-interactive

    Offscreen interactive elements are never discarded — they only rank after
    visible ones, so a tight budget drops them first while they stay clickable
    whenever there is room.
    """
    interactive = (bool(node.get("patterns"))
                   or node.get("ct") in INTERACTIVE)
    enabled = node.get("enabled", True) is not False
    offscreen = bool(node.get("offscreen"))
    if interactive:
        if not enabled:
            return 2
        if offscreen:
            return 1
        return 0
    if not enabled:
        return 6
    if offscreen:
        return 5
    if node.get("ct") in TEXTUAL:
        return 3
    if node.get("ct") in STRUCTURAL:
        return 4
    return 4


def _is_chrome(node: dict) -> bool:
    ct = (node.get("ct") or "").lower()
    if ct in CHROME_CTS:
        return True
    if ct == "button":
        name = (node.get("name") or "").strip().lower()
        if name in CHROME_NAMES:
            return True
    return False


def _pattern_code(node: dict):
    present = {str(p).lower() for p in (node.get("patterns") or [])}
    for pattern, code in PATTERN_CODES:
        if pattern in present:
            return code
    return None


def _state_chars(node: dict) -> str:
    out = ""
    if node.get("focused"):
        out += "*"
    if node.get("selected"):
        out += "+"
    if node.get("enabled", True) is False:
        out += "-"
    if node.get("offscreen"):
        out += "~"
    return out


def _kids_present(nodes: list) -> dict:
    """Count how many direct children each node actually has in this list."""
    counts: dict = {}
    stack: list = []  # [(depth, id)]
    for node in nodes:
        depth = _depth(node)
        nid = node.get("id")
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            counts[parent] = counts.get(parent, 0) + 1
        stack.append((depth, nid))
    return counts


def _node_line(node: dict, indent: str, hidden_kids: dict,
               breadcrumb: str = "") -> str:
    nid = node.get("id")
    ct = squeeze(node.get("ct") or "other", 24)
    parts = [f"[{nid}] {ct}"]
    name = squeeze(node.get("name"))
    if name:
        parts.append(f'"{name}"')
    val = squeeze(node.get("val"), VAL_MAX)
    if val:
        parts.append(f'val="{val}"')
    code = _pattern_code(node)
    if code:
        parts.append(f"[{code}]")
    state = _state_chars(node)
    if state:
        parts.append(state)
    try:
        declared = int(node.get("kids") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0 and declared - hidden_kids.get(nid, 0) > 0:
        parts.append(f"kids={declared}")
    prefix = indent
    if breadcrumb:
        prefix += f"›{breadcrumb} "
    return prefix + " ".join(parts)


# --------------------------------------------------------------------------- #
# sibling folding
# --------------------------------------------------------------------------- #
def _parent_ids(nodes: list) -> list:
    """Nearest surviving ancestor id for every node (DFS order)."""
    parents: list = []
    stack: list = []  # [(depth, id)]
    for node in nodes:
        depth = _depth(node)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parents.append(stack[-1][1] if stack else None)
        stack.append((depth, node.get("id")))
    return parents


def _common_keyword(names: list) -> str:
    """Best filter hint shared by folded siblings."""
    cleaned = [n for n in (str(x).strip() for x in names) if n]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return squeezed_keyword(cleaned[0])
    prefix = _common_prefix(cleaned)
    if len(prefix) >= 2:
        return squeezed_keyword(prefix)
    # fall back to a short n-gram shared by most of the folded names
    first = cleaned[0]
    best = ""
    best_score = (0, 0)
    need = max(2, int(len(cleaned) * 0.7))
    for gram_len in (2, 3, 4):
        for start in range(len(first) - gram_len + 1):
            gram = first[start:start + gram_len]
            if not gram.strip():
                continue
            if gram_len == 2 and gram.isascii():
                continue  # 2 ASCII letters ("er") make a useless filter hint
            count = sum(1 for name in cleaned if gram in name)
            if count >= need and (count, gram_len) > best_score:
                best_score = (count, gram_len)
                best = gram
    return squeezed_keyword(best)


def _common_prefix(strings: list) -> str:
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, char in enumerate(shortest):
        for other in strings:
            if other[i] != char:
                return shortest[:i]
    return shortest


def squeezed_keyword(value: str) -> str:
    return squeeze(value, 24)


# --------------------------------------------------------------------------- #
# noise removal (lead-approved rules, view=controls only)
# --------------------------------------------------------------------------- #
def _index_parents(nodes: list):
    """Return (parents, index_by_id, child_counts) for DFS-ordered nodes."""
    parents: list = []
    stack: list = []  # [(depth, id)]
    children: dict = {}
    for node in nodes:
        depth = _depth(node)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else None
        parents.append(parent)
        if parent is not None:
            children[parent] = children.get(parent, 0) + 1
        stack.append((depth, node.get("id")))
    index_by_id = {node.get("id"): index for index, node in enumerate(nodes)}
    return parents, index_by_id, children


def _ancestors(index, parents, index_by_id, limit):
    """Yield ancestor indices, nearest first, up to ``limit`` hops."""
    parent = parents[index]
    hops = 0
    while parent is not None and hops < limit:
        ancestor_index = index_by_id.get(parent)
        if ancestor_index is None:
            return
        yield ancestor_index
        parent = parents[ancestor_index]
        hops += 1


def _is_chrome_button(nodes, index, parents, index_by_id):
    """Window chrome only: title-bar buttons, never page-level controls."""
    node = nodes[index]
    if (node.get("ct") or "").lower() != "button":
        return False
    cls = (node.get("cls") or "").lower()
    if "captionbutton" in cls or cls == "microsoft.ui.xaml.controls.button":
        # UIA exposes Win32 caption buttons (minimize/maximize/close) as
        # ordinary buttons at depth 5, so class beats name/depth here.
        return True
    name = (node.get("name") or "").strip().lower()
    if name not in CHROME_NAMES:
        return False
    if _depth(node) <= 2:
        return True
    for ancestor_index in _ancestors(index, parents, index_by_id, 3):
        if (nodes[ancestor_index].get("ct") or "").lower() == "titlebar":
            return True
    return False


def _drop_noise(nodes: list) -> list:
    """Remove shadow text, offscreen text, chrome and empty containers."""
    if not nodes:
        return nodes
    parents, index_by_id, _children = _index_parents(nodes)

    drop: set = set()
    for index, node in enumerate(nodes):
        ct = (node.get("ct") or "").lower()
        if ct in CHROME_CTS or _is_chrome_button(nodes, index, parents,
                                                 index_by_id):
            drop.add(index)
            continue
        if ct != "text":
            continue
        # 2) offscreen pure text: unreadable and unclickable
        if node.get("offscreen"):
            drop.add(index)
            continue
        # 1) shadow text: duplicates a nearby interactive ancestor's name
        name = (node.get("name") or "").strip()
        if not name:
            continue
        for ancestor_index in _ancestors(index, parents, index_by_id, 2):
            ancestor = nodes[ancestor_index]
            ancestor_name = (ancestor.get("name") or "").strip()
            if not ancestor_name:
                continue
            interactive = (ancestor.get("ct") in INTERACTIVE
                           or bool(ancestor.get("patterns")))
            if interactive and (ancestor_name == name or name in ancestor_name):
                drop.add(index)
                break

    remaining = [node for index, node in enumerate(nodes) if index not in drop]

    # 3) name-less containers with at most one child: drop and hoist children
    actionable = {"invoke", "setvalue", "toggle", "expand", "collapse",
                  "value", "selection", "checkbox", "radiobutton"}
    for _ in range(5):
        parents, _by_id, children = _index_parents(remaining)
        empty: set = set()
        for index, node in enumerate(remaining):
            ct = (node.get("ct") or "").lower()
            if ct not in CONTAINER_CTS:
                continue
            if node.get("name") or node.get("val"):
                continue
            if set(str(p).lower() for p in (node.get("patterns") or [])) & actionable:
                continue
            if children.get(node.get("id"), 0) <= 1:
                empty.add(index)
        if not empty:
            break
        remaining = [node for index, node in enumerate(remaining)
                     if index not in empty]

    return _recompute_depths(remaining)


def _recompute_depths(nodes: list) -> list:
    """Renumber depths from the surviving hierarchy (dropped levels collapse)."""
    if not nodes:
        return nodes
    parents, index_by_id, _children = _index_parents(nodes)
    depths: list = [0] * len(nodes)
    out: list = []
    for index, node in enumerate(nodes):
        parent = parents[index]
        if parent is not None and parent in index_by_id:
            depth = depths[index_by_id[parent]] + 1
        else:
            depth = 0
        depths[index] = depth
        copy = dict(node)
        copy["depth"] = depth
        out.append(copy)
    return out


def _clean_nodes(nodes: list, view: str) -> list:
    if view != "controls":
        return nodes
    return _drop_noise(nodes)


# --------------------------------------------------------------------------- #
# snapshot rendering
# --------------------------------------------------------------------------- #
def _header_lines(snap: dict, view: str, name_filter, matched=None,
                  cut_extra: str = "") -> list:
    rect = snap.get("rect") or [0, 0, 0, 0]
    try:
        rect = [int(v) for v in list(rect)[:4]]
    except (TypeError, ValueError):
        rect = [0, 0, 0, 0]
    while len(rect) < 4:
        rect.append(0)
    w, h = rect[2] - rect[0], rect[3] - rect[1]

    head = [f"win #{snap.get('hwnd')}"]
    title = squeeze(snap.get("title"), TITLE_MAX)
    if title:
        head.append(f'"{title}"')
    head.append(f"{w}x{h} @{rect[0]},{rect[1]}")
    state = _state_token(snap)
    if state:
        head.append(state)
    if snap.get("focused"):
        head.append("[focus]")
    line1 = " ".join(head)

    nodes = snap.get("nodes") or []
    total = snap.get("total")
    shown = snap.get("shown")
    if total is None:
        total = len(nodes)
    if shown is None:
        shown = len(nodes)
    obs = [f"obs {total}n/{shown} shown",
           f"{int(snap.get('elapsed_ms') or 0)}ms", str(view or "controls")]
    if name_filter:
        obs.append(f'filter="{squeeze(name_filter, 30)}"')
        if matched is not None:
            obs.append(f"{matched} matched")
    if snap.get("truncated") and cut_extra:
        obs.append(f"cut=tree,{cut_extra}")
    elif snap.get("truncated"):
        obs.append("cut=tree")
    elif cut_extra:
        obs.append(f"cut={cut_extra}")
    return [line1, " ".join(obs)]


def _fit(header: list, body: list, max_chars: int) -> str:
    text = "\n".join(header + body)
    if len(text) <= max_chars:
        return text
    body = list(body)
    while body and len(text) > max_chars:
        body.pop()
        text = "\n".join(header + body)
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


def _entry(node, line, priority, depth):
    rect = _rect(node)
    return {"line": line, "priority": priority, "top": rect[1],
            "left": rect[0], "depth": depth}


def _render_entries(nodes: list, *, flat: bool, hidden_kids: dict) -> list:
    """Turn nodes into render lines, folding homogeneous sibling runs."""
    if not nodes:
        return []
    depths = [_depth(node) for node in nodes]
    base = min(depths)
    parents = _parent_ids(nodes)

    def breadcrumb_for(node, rel):
        if rel <= INDENT_MAX:
            return ""
        path = node.get("path") or ""
        segment = str(path).split("/")[-1].strip() if path else ""
        return squeeze(segment, 18) if segment else ""

    def indent_for(rel):
        return INDENT * min(max(0, rel), INDENT_MAX)

    def make_line(index):
        node = nodes[index]
        rel = 0 if flat else max(0, depths[index] - base)
        crumb = "" if flat else breadcrumb_for(node, rel)
        if crumb and crumb == last_crumb["value"]:
            crumb = ""  # don't repeat the same ›breadcrumb on adjacent lines
        elif crumb:
            last_crumb["value"] = crumb
        if not crumb and rel <= INDENT_MAX:
            last_crumb["value"] = None
        indent = "" if flat else indent_for(rel)
        return _entry(node, _node_line(node, indent, hidden_kids, crumb),
                      _priority(node), rel)

    last_crumb = {"value": None}

    entries: list = []
    total = len(nodes)
    i = 0
    while i < total:
        ct = nodes[i].get("ct")
        parent = parents[i]
        j = i
        while j < total and parents[j] == parent and nodes[j].get("ct") == ct:
            j += 1
        run = list(range(i, j))
        fold = (not flat and len(run) >= SIBLING_FOLD_MIN
                and str(ct).lower() in FOLDABLE_CTS)
        if fold:
            keep = set(run[:SIBLING_FOLD_KEEP])
            for index in run:
                node = nodes[index]
                if node.get("focused") or node.get("selected"):
                    keep.add(index)
            ordered_keep = [index for index in run if index in keep]
            dropped = [index for index in run if index not in keep]
            for index in ordered_keep:
                entries.append(make_line(index))
            if dropped:
                names = [nodes[index].get("name") or "" for index in dropped]
                keyword = _common_keyword(names)
                rel = 0 if flat else max(0, depths[dropped[0]] - base)
                marker = INDENT * min(rel, INDENT_MAX) + f"…+{len(dropped)} more"
                if keyword:
                    marker += f" (filter: {keyword})"
                anchor = nodes[dropped[0]]
                entries.append({"line": marker, "priority": 2,
                                "top": _rect(anchor)[1],
                                "left": _rect(anchor)[0], "depth": rel})
        else:
            for index in run:
                entries.append(make_line(index))
        i = j
    return entries


def _fill_stats(stats, *, tree_cut, hidden=0, lines=0, cap=None,
                line_limit=None) -> None:
    if stats is None:
        return
    stats.update({
        "tree_cut": bool(tree_cut),
        "budget_cropped": int(hidden) > 0,
        "hidden": int(hidden),
        "rendered_lines": int(lines),
        "cap": cap,
        "max_lines": line_limit,
    })


def format_snapshot(snap: dict, *, view: str = "controls", filter: str | None = None,
                    max_chars: int = MAX_TEXT,
                    max_lines: int | None = None,
                    stats: dict | None = None) -> str:
    """Render a bridge snapshot dict as the compact text block.

    ``max_lines`` caps the number of rendered element lines (higher-priority
    interactive elements win), independent of the character budget.

    ``stats`` (optional, out-parameter) receives the crop result so the server
    can report why content is missing:
    ``{"tree_cut": bool, "budget_cropped": bool, "hidden": int,
       "rendered_lines": int, "cap": int, "max_lines": int}``.
    ``tree_cut`` mirrors the bridge's ``truncated`` (bounded DFS), while
    ``budget_cropped`` means this presentation layer dropped entries for the
    character/line budget.  Both appear in the obs line as
    ``cut=tree`` / ``cut=budget(+N hidden)`` / ``cut=tree,budget(+N)``.
    """
    snap = snap or {}
    raw_nodes = [n for n in (snap.get("nodes") or []) if isinstance(n, dict)]
    # child counts before noise removal, so "kids=N" only reports real cropping
    hidden_kids = _kids_present(raw_nodes)
    nodes = raw_nodes

    matched = None
    if filter:
        needle = str(filter).lower()
        nodes = [
            n for n in nodes
            if needle in str(n.get("name") or "").lower()
            or needle in str(n.get("val") or "").lower()
            or needle in str(n.get("aid") or "").lower()
        ]
        matched = len(nodes)
    else:
        nodes = _clean_nodes(nodes, view)

    tree_cut = bool(snap.get("truncated"))
    header = _header_lines(snap, view, filter, matched)
    if filter and not nodes:
        header.append(
            f'filter "{squeeze(filter, 40)}" 没有匹配任何元素（共 '
            f'{snap.get("total", 0)} 个）：换个子串、去掉 filter，'
            f'或用 view=text 看文本内容')
        _fill_stats(stats, tree_cut=tree_cut, cap=max_chars)
        return _fit(header, [], max_chars)

    if not nodes:
        header.append(
            "该窗口没有可用的 UIA 元素（可能是 canvas/自绘应用）："
            "用 screenshot=true 并改用 @n:ux,uy 坐标")
        _fill_stats(stats, tree_cut=tree_cut, cap=max_chars)
        return _fit(header, [], max_chars)

    entries = _render_entries(nodes, flat=bool(filter), hidden_kids=hidden_kids)
    cap = max_chars
    if view == "controls" and not filter:
        # lead acceptance: default observation must stay cheap and predictable
        cap = min(cap, CONTROL_TEXT_TARGET)
    lines = [entry["line"] for entry in entries]
    total_len = sum(len(line) + 1 for line in lines)
    line_limit = len(lines)
    if max_lines is not None and max_lines > 0:
        line_limit = min(line_limit, int(max_lines))

    # reserve room for the honest cut= token before deciding the crop
    budget = cap - len("\n".join(header)) - 1 - CUT_RESERVE
    hidden = 0
    if total_len <= budget and len(lines) <= line_limit:
        body_lines = lines
    else:
        order = sorted(range(len(entries)),
                       key=lambda idx: (entries[idx]["priority"],
                                        entries[idx]["top"],
                                        entries[idx]["left"]))
        chosen: set = set()
        used = 0
        for index in order:
            if len(chosen) >= line_limit:
                break
            cost = len(entries[index]["line"]) + 1
            if used + cost + HIDDEN_RESERVE > budget:
                continue
            chosen.add(index)
            used += cost
        body_lines = [lines[index] for index in range(len(lines))
                      if index in chosen]
        hidden = len(lines) - len(chosen)

    def make_header(hidden_count):
        extra = (f"budget(+{hidden_count} hidden)"
                 if hidden_count else "")
        return _header_lines(snap, view, filter, matched, cut_extra=extra)

    # the cut= token grows the header; if that pushes us over the cap, trim one
    # more body line and report it as hidden instead of silently truncating
    while True:
        header_final = make_header(hidden)
        marker = f"…(+{hidden} hidden)" if hidden else ""
        body = body_lines + ([marker] if marker else [])
        text = "\n".join(header_final + body)
        if len(text) <= cap:
            break
        if body_lines:
            body_lines = body_lines[:-1]
            hidden += 1
            continue
        text = _fit(header_final, body, cap)
        break

    _fill_stats(stats, tree_cut=tree_cut, hidden=hidden,
                lines=len(body_lines), cap=cap, line_limit=line_limit)
    return text


def format_windows(windows: list, *, limit: int = 12, elapsed_ms: int = 0,
                   max_chars: int = MAX_TEXT) -> str:
    """Render ``scope=desktop``: the top N windows, ``#hwnd`` selectable."""
    wins = [w for w in (windows or []) if isinstance(w, dict)]
    head = f"desktop {len(wins)} windows {int(elapsed_ms or 0)}ms"
    lines = [head]
    for win in wins[: max(1, int(limit or 12))]:
        rect = _rect(win)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        parts = [f"#{win.get('hwnd')}", str(win.get("proc") or "?")]
        title = squeeze(win.get("title"), TITLE_MAX)
        if title:
            parts.append(f'"{title}"')
        parts.append(f"{w}x{h}")
        if win.get("focused"):
            parts.append("[focus]")
        if win.get("minimized"):
            parts.append("min")
        lines.append(" ".join(parts))
    if len(wins) > limit:
        lines.append(f"…(+{len(wins) - limit} more windows)")
    return _fit([], lines, max_chars)


def format_shot_note(shot: dict, nbytes: int = 0) -> str:
    """Tell the model which coordinate syntax to use (server converts)."""
    shot = shot or {}
    w, h = shot.get("img_w") or 0, shot.get("img_h") or 0
    sw, sh = shot.get("screen_w") or w, shot.get("screen_h") or h
    kb = (nbytes or len(shot.get("jpeg_bytes") or shot.get("png_bytes") or b"")) / 1024.0
    return (f"img {w}x{h} (screen {sw}x{sh}) {kb:.0f}KB — 用 @n:0.42,0.31 "
            f"归一化坐标指位置（服务端换算；也可 @img:x,y 用本图像素）")


# --------------------------------------------------------------------------- #
# cu_do rendering
# --------------------------------------------------------------------------- #
def step_ok(index: int, label: str, via: str, ms: int, extra: str = "") -> str:
    line = f"{index} ok {label} {via or '-'} {int(ms or 0)}ms"
    if extra:
        line += f" {extra}"
    return line


def step_fail(index: int, label: str, detail: str) -> str:
    return (f'{index} FAIL {label} '
            f'detail="{squeeze(detail, DETAIL_MAX)}"')


def fail_block(index: int, label: str, detail: str, advice: str,
               skipped: int = 0) -> list:
    lines = [step_fail(index, label, detail)]
    tail = ""
    if skipped > 0:
        tail = f"（未执行的第 {index + 1}–{index + skipped} 步已跳过）"
    lines.append(f"→ {advice}{tail}")
    return lines
