"""Lays out Map.canvas: projects as a ring of groups, prompts to try beside it.

The file is Obsidian's canvas format (JSON Canvas): positioned nodes and the
edges between them. Whatever the user moved keeps its place when the map is
rewritten, and nodes or edges the user added (ids without PREFIX) are kept.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field

PREFIX = "pl-"
CARD_W, CARD_H = 320, 220
IDEA_W, IDEA_H = 440, 360
GAP, PAD = 60, 40
LEGEND_W, LEGEND_H = 640, 220
# Obsidian's preset colours: 1 red, 2 orange, 3 yellow, 4 green, 5 cyan, 6 purple.
PROJECT_COLORS = ["4", "5", "1", "2"]
HIDDEN, IDEA = "6", "3"


def node_id(kind: str, key: str) -> str:
    return f"{PREFIX}{kind}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


@dataclass
class Card:
    id: str
    text: str


@dataclass
class Edge:
    a: str  # card ids
    b: str
    label: str = ""
    color: str = ""


@dataclass
class Group:
    label: str
    cards: list[Card] = field(default_factory=list)


def _grid(count: int, width: int, height: int) -> tuple[int, int, int]:
    cols = 1 if count <= 1 else 2 if count <= 4 else 3 if count <= 9 else 4
    rows = math.ceil(max(count, 1) / cols)
    return (cols, cols * width + (cols - 1) * GAP + 2 * PAD,
            rows * height + (rows - 1) * GAP + 2 * PAD)


def _ring_order(groups: list[Group], edges: list[Edge]) -> list[Group]:
    """Neighbours on the ring are the projects with the most links between them."""
    home = {card.id: group.label for group in groups for card in group.cards}
    weight: Counter = Counter()
    for edge in edges:
        a, b = home.get(edge.a), home.get(edge.b)
        if a and b and a != b:
            weight[frozenset((a, b))] += 1
    total = Counter()
    for pair, n in weight.items():
        for label in pair:
            total[label] += n
    left = list(groups)
    order = [max(left, key=lambda g: total[g.label])] if left else []
    if order:
        left.remove(order[0])
    while left:
        last = order[-1].label
        best = max(left, key=lambda g: weight[frozenset((last, g.label))])
        order.append(best)
        left.remove(best)
    return order


def _sides(a: dict, b: dict) -> tuple[str, str]:
    dx = (b["x"] + b["width"] / 2) - (a["x"] + a["width"] / 2)
    dy = (b["y"] + b["height"] / 2) - (a["y"] + a["height"] / 2)
    if abs(dx) > abs(dy):
        return ("right", "left") if dx > 0 else ("left", "right")
    return ("bottom", "top") if dy > 0 else ("top", "bottom")


def build(groups: list[Group], edges: list[Edge], ideas: list[Card], legend: str,
          old: dict | None = None) -> dict:
    """The canvas document."""
    old = old if isinstance(old, dict) else {}
    previous = {n["id"]: n for n in old.get("nodes", []) if isinstance(n, dict) and "id" in n}

    # Sizes first, then a ring wide enough that no two groups overlap. Its middle
    # stays empty for the lines that cross it.
    order = _ring_order([g for g in groups if g.cards], edges)
    sized = [(g, *_grid(len(g.cards), CARD_W, CARD_H)) for g in order]
    idea_cols, ideas_w, ideas_h = _grid(len(ideas), IDEA_W, IDEA_H)
    spans = [math.hypot(w, h) for _, _, w, h in sized]
    arc = sum(s + GAP for s in spans)
    radius = max(arc / (2 * math.pi), max(spans, default=0))

    nodes: list[dict] = []
    cards: list[dict] = []

    def keep(node: dict, grow: bool = False) -> tuple[int, int]:
        """Reuse a position the user chose. Returns how far the node moved."""
        prev = previous.get(node["id"])
        if not prev or not all(isinstance(prev.get(k), (int, float)) for k in ("x", "y")):
            return 0, 0
        shift = (round(prev["x"] - node["x"]), round(prev["y"] - node["y"]))
        node["x"], node["y"] = round(prev["x"]), round(prev["y"])
        for dim in ("width", "height"):
            if isinstance(prev.get(dim), (int, float)):
                # A group must still fit the cards it has gained since.
                node[dim] = max(node[dim], round(prev[dim])) if grow else round(prev[dim])
        return shift

    def lay_cards(group_node: dict, group_cards: list[Card], cols: int, width: int,
                  height: int, shift: tuple[int, int], color: str = "") -> None:
        for i, card in enumerate(group_cards):
            row, col = divmod(i, cols)
            node = {"id": card.id, "type": "text", "text": card.text,
                    "x": group_node["x"] - shift[0] + PAD + col * (width + GAP),
                    "y": group_node["y"] - shift[1] + PAD + row * (height + GAP),
                    "width": width, "height": height}
            if color:
                node["color"] = color
            if card.id in previous:
                keep(node)
            else:  # a new card follows its group to wherever the user moved it
                node["x"] += shift[0]
                node["y"] += shift[1]
            cards.append(node)

    angle = -math.pi / 2  # start at the top and go clockwise
    for (group, cols, width, height), span, color in zip(
            sized, spans, (PROJECT_COLORS * len(sized))[:len(sized)]):
        step = 2 * math.pi * (span + GAP) / arc
        cx = radius * math.cos(angle + step / 2)
        cy = radius * math.sin(angle + step / 2)
        angle += step
        node = {"id": node_id("g", group.label), "type": "group", "label": group.label,
                "x": round(cx - width / 2), "y": round(cy - height / 2),
                "width": width, "height": height, "color": color}
        shift = keep(node, grow=True)
        nodes.append(node)
        lay_cards(node, group.cards, cols, CARD_W, CARD_H, shift)

    if ideas:
        right = max([n["x"] + n["width"] for n in nodes], default=0)
        node = {"id": f"{PREFIX}ideas", "type": "group", "label": "Prompts to try",
                "x": right + 3 * GAP, "y": -ideas_h // 2, "width": ideas_w, "height": ideas_h,
                "color": IDEA}
        shift = keep(node, grow=True)
        nodes.append(node)
        lay_cards(node, ideas, idea_cols, IDEA_W, IDEA_H, shift, IDEA)

    top = min([n["y"] for n in nodes + cards], default=0)
    note = {"id": f"{PREFIX}legend", "type": "text", "text": legend,
            "x": -LEGEND_W // 2, "y": top - LEGEND_H - GAP, "width": LEGEND_W, "height": LEGEND_H}
    keep(note)
    nodes.append(note)
    nodes += cards

    by_id = {n["id"]: n for n in nodes}
    out_edges = []
    for line in edges:
        if line.a not in by_id or line.b not in by_id:
            continue
        from_side, to_side = _sides(by_id[line.a], by_id[line.b])
        edge = {"id": node_id("e", f"{line.a}>{line.b}"), "fromNode": line.a,
                "fromSide": from_side, "toNode": line.b, "toSide": to_side}
        if line.label:
            edge["label"] = line.label
        if line.color:
            edge["color"] = line.color
        out_edges.append(edge)

    # The user's own cards and arrows stay, as long as both ends still exist.
    for node in old.get("nodes", []):
        if isinstance(node, dict) and not str(node.get("id", "")).startswith(PREFIX):
            nodes.append(node)
            by_id[node["id"]] = node
    for edge in old.get("edges", []):
        if (isinstance(edge, dict) and not str(edge.get("id", "")).startswith(PREFIX)
                and edge.get("fromNode") in by_id and edge.get("toNode") in by_id):
            out_edges.append(edge)
    return {"nodes": nodes, "edges": out_edges}
