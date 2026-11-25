import base64

import pytest
from fastapi.testclient import TestClient

from app.api import get_service
from app.main import app
from app.services import MessengerService
from app.storage import SqliteStorage

PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG = "data:image/png;base64," + base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a") + b"fake-image-body"
).decode()


@pytest.fixture
def svc(tmp_path):
    service = MessengerService(SqliteStorage(tmp_path / "data.db"))
    app.dependency_overrides[get_service] = lambda: service
    yield service
    app.dependency_overrides.clear()


@pytest.fixture
def client(svc):
    return TestClient(app)


def register(client, name, tag=None, password="secret1", repeat=None):
    return client.post("/api/users", json={
        "username": name, "tag": tag or name + "_tag",
        "password": password, "password_repeat": repeat or password,
    })


def login(client, name):
    """Registers a user and returns (id, auth headers)."""
    data = register(client, name).json()
    return data["id"], {"Authorization": f"Bearer {data['token']}"}


def chat(client, n=2):
    people = [login(client, f"user{i}") for i in range(n)]
    ids = [i for i, _ in people]
    cid = client.post("/api/conversations", json={"participant_ids": ids[1:]},
                      headers=people[0][1]).json()["id"]
    return ids, [h for _, h in people], cid


def convs(client, headers):
    """Conversation list without the automatic chat with the bot."""
    all_ = client.get("/api/conversations", headers=headers).json()
    return [c for c in all_ if not c["is_bot_chat"]]


def send(client, cid, headers, text="hi", **extra):
    return client.post(f"/api/conversations/{cid}/messages",
                       json={"text": text, **extra}, headers=headers)


# ---- authentication ----

def test_requests_without_valid_token_are_rejected(client):
    assert client.get("/api/conversations").status_code == 401
    bad = {"Authorization": "Bearer nonsense"}
    assert client.get("/api/users", headers=bad).status_code == 401
    assert client.get("/api/me", headers=bad).status_code == 401


def test_register_login_logout_flow(client):
    assert register(client, "alice", password="secret1", repeat="secret2").status_code == 400
    ok = register(client, "alice")
    assert ok.status_code == 201
    body = ok.json()
    assert body["tag"] == "alice_tag" and body["token"] and "password" not in str(body)

    good = client.post("/api/login", json={"tag": "Alice_Tag", "password": "secret1"})
    assert good.status_code == 200 and good.json()["id"] == body["id"]
    assert client.post("/api/login", json={"tag": "alice_tag", "password": "nope-nope"}).status_code == 401

    headers = {"Authorization": f"Bearer {good.json()['token']}"}
    assert client.get("/api/me", headers=headers).json()["id"] == body["id"]
    assert client.get("/api/users", headers=headers).json()[0].keys() == {"id", "username", "tag", "is_bot"}
    assert client.post("/api/logout", headers=headers).status_code == 204
    assert client.get("/api/me", headers=headers).status_code == 401


def test_token_stored_only_as_hash_and_survives_restart(client, svc, tmp_path):
    data = register(client, "alice").json()
    assert data["token"].encode() not in (tmp_path / "data.db").read_bytes()
    reloaded = MessengerService(SqliteStorage(tmp_path / "data.db"))
    assert reloaded.user_for_token(data["token"]).id == data["id"]


def test_duplicate_tag_rejected(client):
    register(client, "alice")
    assert register(client, "alice2", tag="alice_tag").status_code == 400


# ---- chats ----

def test_group_chat_send_history_search(client):
    ids, hs, cid = chat(client, 3)
    assert send(client, cid, hs[0], "Привіт усім").status_code == 201
    history = client.get(f"/api/conversations/{cid}/messages", headers=hs[1]).json()
    assert [m["text"] for m in history] == ["Привіт усім"]
    found = client.get("/api/search", params={"q": "привіт"}, headers=hs[2]).json()
    assert [m["text"] for m in found if m["conversation_id"] == cid] == ["Привіт усім"]


def test_sender_comes_from_token_and_outsiders_have_no_access(client):
    ids, hs, cid = chat(client, 2)
    sent = send(client, cid, hs[1]).json()
    assert sent["sender_id"] == ids[1]
    _, outsider = login(client, "mallory")
    assert send(client, cid, outsider).status_code == 400
    assert client.get(f"/api/conversations/{cid}/messages", headers=outsider).status_code == 400
    assert convs(client, outsider) == []
    assert client.get("/api/search", params={"q": "hi"}, headers=outsider).json() == []


def test_validation_errors_return_400(client):
    _, hs, cid = chat(client)
    assert send(client, cid, hs[0], "  ").status_code == 400
    assert send(client, cid, hs[0], "x" * 2001).status_code == 400
    assert client.get("/api/search", params={"q": " "}, headers=hs[0]).status_code == 400


def test_photo_upload_serve_and_reject_fake(client):
    _, hs, cid = chat(client)
    r = send(client, cid, hs[0], "", image_data=PNG)
    assert r.status_code == 201
    assert client.get(r.json()["image_url"]).content.endswith(b"fake-image-body")
    fake = "data:image/png;base64," + base64.b64encode(b"just text").decode()
    assert send(client, cid, hs[0], "", image_data=fake).status_code == 400
    assert client.get("/api/uploads/..%2Fdata.db").status_code == 404


def test_block_prevents_messages_both_ways(client):
    ids, hs, cid = chat(client)
    assert client.post("/api/blocks", json={"target_id": ids[1]}, headers=hs[0]).status_code == 204
    assert client.get("/api/blocks", headers=hs[0]).json() == [ids[1]]
    assert send(client, cid, hs[0]).status_code == 400
    assert send(client, cid, hs[1]).status_code == 400
    client.delete("/api/blocks", params={"target_id": ids[1]}, headers=hs[0])
    assert send(client, cid, hs[0]).status_code == 201


def test_clear_history_and_delete_direct_chat(client):
    ids, hs, cid = chat(client)
    url = send(client, cid, hs[0], "x", image_data=PNG).json()["image_url"]
    assert client.delete(f"/api/conversations/{cid}/messages", headers=hs[0]).status_code == 204
    assert client.get(f"/api/conversations/{cid}/messages", headers=hs[0]).json() == []
    assert client.get(url).status_code == 404
    _, outsider = login(client, "mallory")
    assert client.delete(f"/api/conversations/{cid}", headers=outsider).status_code == 400
    assert client.delete(f"/api/conversations/{cid}", headers=hs[1]).status_code == 204
    assert convs(client, hs[0]) == []


# ---- group admin ----

def test_group_creator_is_admin_and_only_admin_removes(client):
    ids, hs, cid = chat(client, 4)
    conv = convs(client, hs[0])[0]
    assert conv["is_group"] and conv["admin_id"] == ids[0]
    url = f"/api/conversations/{cid}/members/{ids[2]}"
    assert client.delete(url, headers=hs[1]).status_code == 400  # not admin
    assert client.delete(url, headers=hs[0]).status_code == 204
    assert convs(client, hs[2]) == []
    own = f"/api/conversations/{cid}/members/{ids[0]}"
    assert client.delete(own, headers=hs[0]).status_code == 400  # admin can't remove self


def test_group_clear_and_delete_for_all_admin_only(client):
    ids, hs, cid = chat(client, 3)
    send(client, cid, hs[1])
    assert client.delete(f"/api/conversations/{cid}/messages", headers=hs[1]).status_code == 400
    assert client.delete(f"/api/conversations/{cid}", params={"everyone": True},
                         headers=hs[1]).status_code == 400
    assert client.delete(f"/api/conversations/{cid}/messages", headers=hs[0]).status_code == 204
    assert client.delete(f"/api/conversations/{cid}", params={"everyone": True},
                         headers=hs[0]).status_code == 204
    assert convs(client, hs[1]) == []


def test_admin_leaves_and_role_passes_on(client):
    ids, hs, cid = chat(client, 3)
    client.delete(f"/api/conversations/{cid}", headers=hs[0])
    conv = convs(client, hs[1])[0]
    assert conv["admin_id"] == ids[1] and conv["is_group"]
    client.delete(f"/api/conversations/{cid}", headers=hs[2])  # last two: one leaves, group ends
    assert convs(client, hs[1]) == []


# ---- duplicates, rename, previews ----

def test_second_direct_chat_with_same_person_is_rejected(client):
    ids, hs, cid = chat(client, 2)
    again = client.post("/api/conversations", json={"participant_ids": [ids[1]]}, headers=hs[0])
    assert again.status_code == 400 and "уже існує" in again.json()["detail"]
    reverse = client.post("/api/conversations", json={"participant_ids": [ids[0]]}, headers=hs[1])
    assert reverse.status_code == 400
    assert len(convs(client, hs[0])) == 1


def test_several_groups_with_same_people_are_allowed(client):
    ids, hs, _ = chat(client, 3)
    r = client.post("/api/conversations", json={"participant_ids": ids[1:], "title": "other"},
                    headers=hs[0])
    assert r.status_code == 201
    assert len(convs(client, hs[0])) == 2


def test_rename_group_admin_only_with_validation(client):
    ids, hs, cid = chat(client, 3)
    url = f"/api/conversations/{cid}"
    ok = client.patch(url, json={"title": "  Нова назва  "}, headers=hs[0])
    assert ok.status_code == 200 and ok.json()["title"] == "Нова назва"
    assert client.patch(url, json={"title": "x"}, headers=hs[1]).status_code == 400  # not admin
    assert client.patch(url, json={"title": "   "}, headers=hs[0]).status_code == 400
    assert client.patch(url, json={"title": "x" * 65}, headers=hs[0]).status_code == 400
    (p_id, p_h), (q_id, _) = login(client, "pp"), login(client, "qq")
    direct = client.post("/api/conversations", json={"participant_ids": [q_id]}, headers=p_h).json()["id"]
    assert client.patch(f"/api/conversations/{direct}", json={"title": "x"},
                        headers=p_h).status_code == 400  # a direct chat can't be renamed


def test_conversation_list_has_last_message(client):
    _, hs, cid = chat(client)
    assert convs(client, hs[0])[0]["last_message"] is None
    send(client, cid, hs[0], "перше")
    send(client, cid, hs[1], "друге")
    last = convs(client, hs[0])[0]["last_message"]
    assert last["text"] == "друге"


# ---- unread counters ----

def unread(client, headers):
    return {c["id"]: c["unread"] for c in convs(client, headers)}


def test_unread_counts_and_mark_read(client):
    ids, hs, cid = chat(client, 2)
    assert unread(client, hs[1])[cid] == 0
    send(client, cid, hs[0], "one")
    send(client, cid, hs[0], "two")
    send(client, cid, hs[1], "my own reply")
    assert unread(client, hs[1])[cid] == 2  # own messages never count
    assert unread(client, hs[0])[cid] == 1
    assert client.post(f"/api/conversations/{cid}/read", headers=hs[1]).status_code == 204
    assert unread(client, hs[1])[cid] == 0
    send(client, cid, hs[0], "three")
    assert unread(client, hs[1])[cid] == 1
    assert client.post(f"/api/conversations/{cid}/read", headers=hs[0]).status_code == 204
    _, outsider = login(client, "mallory")
    assert client.post(f"/api/conversations/{cid}/read", headers=outsider).status_code == 400


def test_old_chats_without_read_markers_start_as_read(client, svc, tmp_path):
    ids, hs, cid = chat(client, 2)
    send(client, cid, hs[0], "old message")
    import sqlite3
    path = tmp_path / "data.db"
    with sqlite3.connect(path) as conn:  # simulate data saved before read markers existed
        conn.execute("UPDATE participants SET last_read = NULL")
    reloaded = MessengerService(SqliteStorage(path))
    assert reloaded.unread_counts(ids[1])[cid] == 0


# ---- adding people to a group ----

def test_admin_adds_members_to_group(client):
    ids, hs, cid = chat(client, 3)
    (new_id, new_h), (other_id, _) = login(client, "newbie"), login(client, "other")
    send(client, cid, hs[1], "старе повідомлення")
    url = f"/api/conversations/{cid}/members"
    added = client.post(url, json={"user_ids": [new_id, other_id]}, headers=hs[0])
    assert added.status_code == 201
    assert added.json()["participants"][-2:] == [new_id, other_id] and added.json()["admin_id"] == ids[0]
    # the newcomer sees the history, but it is not counted as unread
    history = client.get(f"/api/conversations/{cid}/messages", headers=new_h).json()
    assert [m["text"] for m in history] == ["старе повідомлення"]
    assert unread(client, new_h)[cid] == 0
    assert send(client, cid, new_h, "привіт, я новий").status_code == 201
    assert unread(client, hs[0])[cid] == 2


def test_add_members_rules(client):
    ids, hs, cid = chat(client, 3)
    new_id, _ = login(client, "newbie")
    url = f"/api/conversations/{cid}/members"
    assert client.post(url, json={"user_ids": [new_id]}, headers=hs[1]).status_code == 400  # not admin
    assert client.post(url, json={"user_ids": [ids[1]]}, headers=hs[0]).status_code == 400  # already in
    assert client.post(url, json={"user_ids": ["nobody"]}, headers=hs[0]).status_code == 400
    assert client.post(url, json={"user_ids": []}, headers=hs[0]).status_code == 400
    (p_id, p_h), (q_id, _) = login(client, "pp"), login(client, "qq")
    direct = client.post("/api/conversations", json={"participant_ids": [q_id]}, headers=p_h).json()["id"]
    assert client.post(f"/api/conversations/{direct}/members", json={"user_ids": [new_id]},
                       headers=p_h).status_code == 400  # a direct chat is not a group


# ---- CalorieBot ----

def bot_chat(client, headers):
    return next(c for c in client.get("/api/conversations", headers=headers).json() if c["is_bot_chat"])


def test_new_user_has_bot_chat_with_greeting(client):
    _, h = login(client, "alice")
    chat_ = bot_chat(client, h)
    assert chat_["last_message"]["text"].startswith("Привіт! Я CalorieBot")
    assert chat_["unread"] == 1 and not chat_["is_group"]
    users = client.get("/api/users", headers=h).json()
    assert [u["tag"] for u in users if u["is_bot"]] == ["calorie_bot"]


def test_cannot_log_in_as_bot(client):
    login(client, "alice")
    assert client.post("/api/login", json={"tag": "calorie_bot", "password": ""}).status_code == 401
    assert register(client, "evil", tag="calorie_bot").status_code == 400


def test_bot_answers_photo_with_calories(client, monkeypatch):
    seen = {}

    def fake_reply(image, ext, caption):
        seen.update(image=image, ext=ext, caption=caption)
        return "Яблуко 52 ккал"

    monkeypatch.setattr("app.api.bot.reply", fake_reply)
    _, h = login(client, "alice")
    cid = bot_chat(client, h)["id"]
    assert send(client, cid, h, "100 г", image_data=PNG).status_code == 201
    history = client.get(f"/api/conversations/{cid}/messages", headers=h).json()
    assert history[-1]["text"] == "Яблуко 52 ккал"
    assert history[-1]["sender_id"] != history[-2]["sender_id"]
    assert seen["ext"] == "png" and seen["caption"] == "100 г" and seen["image"].endswith(b"fake-image-body")


def test_bot_asks_for_photo_on_text_and_ignores_other_chats(client):
    _, hs, cid = chat(client)
    send(client, cid, hs[0], "привіт")
    assert [m["text"] for m in client.get(f"/api/conversations/{cid}/messages", headers=hs[0]).json()] == ["привіт"]
    bot_cid = bot_chat(client, hs[0])["id"]
    send(client, bot_cid, hs[0], "що їсти?")
    last = client.get(f"/api/conversations/{bot_cid}/messages", headers=hs[0]).json()[-1]
    assert "фото" in last["text"]


def test_bot_reply_when_ollama_is_down_is_friendly(monkeypatch):
    from app import bot

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(bot, "OLLAMA_URL", "http://127.0.0.1:1")  # nothing listens here
    assert bot.reply(b"x", "png", "") == bot.NOT_RUNNING


def test_bot_uses_gemini_when_key_is_set(monkeypatch):
    import httpx
    from app import bot

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, key=headers["x-goog-api-key"], body=json)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Яблуко 78 ккал"}]}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(bot.httpx, "post", fake_post)
    assert bot.reply(PNG_BYTES, "png", "150 г") == "Яблуко 78 ккал"
    assert seen["key"] == "test-key" and ":generateContent" in seen["url"]
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg" and "150 г" in parts[1]["text"]


def test_bot_reports_bad_gemini_key(monkeypatch):
    import httpx
    from app import bot

    monkeypatch.setenv("GEMINI_API_KEY", "wrong")
    monkeypatch.setattr(
        bot.httpx, "post", lambda *a, **k: httpx.Response(400, request=httpx.Request("POST", "http://x"))
    )
    assert bot.reply(PNG_BYTES, "png", "") == bot.BAD_KEY
