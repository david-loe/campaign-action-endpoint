"""Disposable Listmonk API double used only by HTTP/browser verification."""
import base64
import copy
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class ListmonkTestServer:
    def __init__(self):
        self.username = "test-api"
        self.token = secrets.token_urlsafe(32)
        self.lists = {1: {"id": 1, "name": "Newsletter", "status": "active"},
                      2: {"id": 2, "name": "Andere Liste", "status": "active"},
                      3: {"id": 3, "name": "Archiv", "status": "archived"}}
        self.subscribers = {}
        self.requests = []
        self.fail_pages = set()
        self.patch_failures = {}
        self.patch_entered = threading.Event()
        self.patch_release = None
        self.delay = 0
        self._lock = threading.Lock()
        self.add_subscriber(10, "anna@example.com", "Anna", [1], {"city": "Köln", "customer": {"id": "A-10"}, "campaign_action_links": {"previous": "https://example.com/old"}})
        self.add_subscriber(11, "ben@example.com", "Ben", [1, 2], {"city": "Berlin", "customer": {"id": "B-11"}})
        self.add_subscriber(12, "outside@example.com", "Outside", [2])

    def add_subscriber(self, subscriber_id, email, name, lists, attributes=None):
        self.subscribers[subscriber_id] = {"id": subscriber_id, "uuid": f"00000000-0000-0000-0000-{subscriber_id:012d}",
            "email": email, "name": name, "status": "blocklisted" if subscriber_id == 10 else "enabled",
            "created_at": "2026-01-01T12:00:00Z", "updated_at": "2026-01-02T12:00:00Z", "attribs": attributes or {},
            "lists": [{"id": list_id, "subscription_status": "unsubscribed" if subscriber_id == 10 else "confirmed"} for list_id in lists]}

    def __enter__(self):
        state = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def authorized(self):
                expected = "Basic " + base64.b64encode(f"{state.username}:{state.token}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    self.respond(401, {"message": "Credentials rejected: " + state.token})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if state.delay:
                    time.sleep(state.delay)
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                with state._lock:
                    state.requests.append(("GET", self.path, None))
                    if parts.path == "/api/lists":
                        rows = [copy.deepcopy(row) for row in state.lists.values() if row["status"] == query.get("status", [row["status"]])[0]]
                        if query.get("minimal") == ["true"]:
                            self.respond(200, {"data": rows})
                            return
                    elif parts.path.startswith("/api/lists/"):
                        row = state.lists.get(int(parts.path.rsplit("/", 1)[1]))
                        self.respond(200 if row else 404, {"data": row})
                        return
                    elif parts.path == "/api/subscribers":
                        page = int(query.get("page", [1])[0])
                        list_id = int(query["list_id"][0])
                        if (list_id, page) in state.fail_pages:
                            self.respond(503, {"message": "Remote failure: " + state.token})
                            return
                        rows = [copy.deepcopy(row) for row in state.subscribers.values() if any(entry["id"] == list_id for entry in row["lists"])]
                    elif parts.path.startswith("/api/subscribers/"):
                        row = state.subscribers.get(int(parts.path.rsplit("/", 1)[1]))
                        self.respond(200 if row else 404, {"data": copy.deepcopy(row)})
                        return
                    else:
                        self.respond(404, {"message": "Not found"})
                        return
                page = int(query.get("page", [1])[0])
                per_page = int(query.get("per_page", [100])[0])
                self.respond(200, {"data": {"results": rows[(page - 1) * per_page:page * per_page], "total": len(rows)}})

            def do_PATCH(self):
                if not self.authorized():
                    return
                subscriber_id = int(urlsplit(self.path).path.rsplit("/", 1)[1])
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.patch_entered.set()
                if state.patch_release is not None:
                    state.patch_release.wait(timeout=5)
                with state._lock:
                    state.requests.append(("PATCH", self.path, payload))
                    if state.patch_failures.get(subscriber_id, 0):
                        state.patch_failures[subscriber_id] -= 1
                        self.respond(503, {"message": "Patch failure: " + state.token})
                        return
                    row = state.subscribers.get(subscriber_id)
                    if not row:
                        self.respond(404, {"message": "Not found"})
                        return
                    if set(payload) != {"attribs"}:
                        self.respond(400, {"message": "Only attribute updates are allowed by this fixture"})
                        return
                    row["attribs"].update(payload["attribs"])
                    self.respond(200, {"data": copy.deepcopy(row)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        if self.patch_release:
            self.patch_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
