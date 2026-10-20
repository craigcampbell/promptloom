import os
import subprocess
import sys

from promptloom import canvas, connect
from promptloom.model import Session
from promptloom.store import Record


def record(sid, context, *prompts, updated="2026-01-02T03:04:05Z"):
    session = Session("claude-code", sid, context=context)
    for text in prompts:
        session.add_prompt(updated, text)
    return Record(session)


def test_terms_keep_unusual_words_and_phrases():
    found = connect.terms("I don't want to fix the websocket bridge for sqlite, see "
                          "https://example.com/websocket and the multi-account view")
    assert {"websocket", "bridge", "websocket bridge", "sqlite", "multi-account",
            "account"} <= found
    assert not {"want", "don", "fix", "bridge sqlite", "example", "see"} & found


def test_sessions_in_different_projects_link_through_shared_words():
    shop = record("a", "/x/shop", "push websocket notifications to the shop dashboard",
                  "the websocket notifications reconnect too often")
    game = record("b", "/x/game", "the lobby needs websocket notifications for players")
    notes = record("c", "/x/notes", "write a recipe parser for markdown")
    web = connect.Web([shop, game, notes])

    [link] = web.across()
    assert {link.a.session.session_id, link.b.session.session_id} == {"a", "b"}
    assert link.hidden and "websocket notifications" in link.words
    assert web.similar(notes) == []
    assert ("websocket", [shop, game]) in web.keywords()
    assert [(p.a, p.b) for p in web.projects()] == [("game", "shop")]


def test_bridge_prompt_runs_in_the_app(tmp_path):
    app = tmp_path / "shop"
    (app / ".git").mkdir(parents=True)
    shop = record("a", str(app), "websocket notifications", updated="2026-01-01T00:00:00Z")
    other = record("b", str(tmp_path), "websocket notifications", updated="2026-02-01T00:00:00Z")
    assert connect.is_app(str(app)) and not connect.is_app(str(tmp_path))

    link = connect.Link(shop, other, 0.5, ["websocket", "notifications"])
    here, prompt = connect.bridge_prompt(link)
    assert here is shop  # an app beats a more recent session in a bare folder
    assert f"In {tmp_path.name} I worked on" in prompt
    assert "Here in shop" in prompt and "websocket, notifications" in prompt


def test_map_keeps_what_the_user_moved_and_added():
    groups = [canvas.Group("shop", [canvas.Card("pl-s-1", "one")]),
              canvas.Group("game", [canvas.Card("pl-s-2", "two")])]
    edges = [canvas.Edge("pl-s-1", "pl-s-2", "websocket", canvas.HIDDEN)]
    first = canvas.build(groups, edges, [canvas.Card("pl-i-1", "idea")], "legend")
    nodes = {n["id"]: n for n in first["nodes"]}
    assert {n["type"] for n in first["nodes"]} == {"group", "text"}
    [edge] = first["edges"]
    assert edge["label"] == "websocket" and edge["color"] == canvas.HIDDEN

    # The user drags a card and a group, and adds a card and an arrow of their own.
    nodes["pl-s-1"]["x"] += 500
    shop = nodes[canvas.node_id("g", "shop")]
    shop["x"] += 1000
    first["nodes"].append({"id": "mine", "type": "text", "text": "hm", "x": 0, "y": 0,
                           "width": 10, "height": 10})
    first["edges"].append({"id": "arrow", "fromNode": "mine", "toNode": "pl-s-2"})

    groups[0].cards.append(canvas.Card("pl-s-3", "three"))
    second = canvas.build(groups, edges, [canvas.Card("pl-i-1", "idea")], "legend", first)
    again = {n["id"]: n for n in second["nodes"]}
    assert again["pl-s-1"]["x"] == nodes["pl-s-1"]["x"]
    assert again[shop["id"]]["x"] == shop["x"]
    assert again[shop["id"]]["width"] > shop["width"]  # it grew to take the new card
    new, grown = again["pl-s-3"], again[shop["id"]]
    assert grown["x"] <= new["x"] and new["x"] + new["width"] <= grown["x"] + grown["width"]
    assert "mine" in again and any(e["id"] == "arrow" for e in second["edges"])


def test_the_same_sessions_give_the_same_web_in_any_process():
    # Terms come out of sets, whose order differs between processes; a map that
    # changed on every restart would be rewritten (and reloaded) every time.
    script = """
from promptloom import connect
from promptloom.model import Session
from promptloom.store import Record
records = []
for i, (where, text) in enumerate([
        ("/x/a", "custom validation failed validation ltx audio monitor devtop interface"),
        ("/x/b", "failed validation custom validation ltx audio interface monitor devtop"),
        ("/x/c", "monitor interface devtop ltx audio")]):
    session = Session("claude-code", str(i), context=where)
    session.add_prompt("2026-01-01T00:00:00Z", text)
    records.append(Record(session))
web = connect.Web(records)
print([(l.a.session.key, l.b.session.key, round(l.score, 9), l.words) for l in web.links])
print([term for term, _ in web.keywords()])
"""
    outputs = {subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              check=True, env={**os.environ, "PYTHONHASHSEED": seed}).stdout
               for seed in ("1", "2", "3")}
    assert len(outputs) == 1
