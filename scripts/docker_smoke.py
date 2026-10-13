"""Exercise the real Docker image, HTTP/CSRF flow and persistent SQLite volume.

Only creates and removes Docker resources with a fresh, unique smoke-test name.
"""
import argparse
import csv
import io
import json
import os
import re
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar


def docker(*args, capture=True, env=None):
    return subprocess.run(["docker", *args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, env=env).stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="campaign-action:test")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if args.build:
        docker("build", "-t", args.image, ".", capture=False)
    name = "campaign-action-smoke-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    environment = dict(os.environ, ENCYPTION_SECRET=secrets.token_urlsafe(48),
                       ADMIN_USERNAME="smoke-admin", ADMIN_PASSWORD=secrets.token_urlsafe(24), PUBLIC_BASE_URL="http://localhost:8080")

    def start():
        docker("run", "-d", "--name", name, "-p", "127.0.0.1::8080", "-v", volume + ":/data",
               "-e", "ENCYPTION_SECRET", "-e", "ADMIN_USERNAME", "-e", "ADMIN_PASSWORD", "-e", "PUBLIC_BASE_URL", args.image, env=environment)
        port = docker("port", name, "8080/tcp").strip().rsplit(":", 1)[1]
        origin = "http://127.0.0.1:" + port
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(origin + "/healthz", timeout=2) as response:
                    if json.load(response)["status"] == "ok":
                        assert docker("exec", name, "id", "-u").strip() == "10001"
                        return origin
            except (OSError, ValueError):
                time.sleep(.25)
        raise RuntimeError("Container did not become healthy")

    def browser(origin):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        def get(path):
            with opener.open(origin + path, timeout=15) as response:
                return response.read()
        def post(path, fields):
            payload = urllib.parse.urlencode(fields, doseq=True).encode()
            with opener.open(origin + path, payload, timeout=15) as response:
                return response.read(), urllib.parse.urlsplit(response.url).path
        csrf = lambda html: re.search(rb'name="csrf_token" value="([^"]+)"', html).group(1).decode()
        post("/admin/login", {"csrf_token": csrf(get("/admin/login")), "username": environment["ADMIN_USERNAME"], "password": environment["ADMIN_PASSWORD"]})
        return opener, get, post, csrf

    try:
        origin = start()
        opener, get, post, csrf = browser(origin)
        _, path = post("/admin/campaigns/new", {"csrf_token": csrf(get("/admin/campaigns/new")), "name": "Docker smoke", "identity_type": "email", "key_column": "key", "confirmation_mode": "visit", "confirmation_text": "Danke!", "active": "1"})
        campaign_path = path
        boundary = "smoke-" + uuid.uuid4().hex
        token = csrf(get(path + "/import"))
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n{token}\r\n',
                 f'--{boundary}\r\nContent-Disposition: form-data; name="delimiter"\r\n\r\n,\r\n',
                 f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="subscribers.csv"\r\nContent-Type: text/csv\r\n\r\nemail,name\r\nsmoke@example.com,Smoke\r\n', f'--{boundary}--\r\n']
        request = urllib.request.Request(origin + path + "/import", "".join(parts).encode(), {"Content-Type": "multipart/form-data; boundary=" + boundary})
        with opener.open(request, timeout=15) as response:
            preview, preview_path = response.read(), urllib.parse.urlsplit(response.url).path
        post(preview_path, {"csrf_token": csrf(preview)})
        data, _ = post(path + "/export/links", {"csrf_token": csrf(get(path)), "format": "simple"})
        link = next(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))["link"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", link.rsplit("/r/", 1)[1])
        get(urllib.parse.urlsplit(link).path)
        before = next(csv.DictReader(io.StringIO(get(path + "/export/results").decode("utf-8-sig"))))
        assert before["confirmed_at"] and before["email"] == "smoke@example.com"
        docker("rm", "-f", name)
        origin = start()
        _, get, _, _ = browser(origin)
        after = next(csv.DictReader(io.StringIO(get(campaign_path + "/export/results").decode("utf-8-sig"))))
        assert before == after, "Data changed after replacing the container"
        get(urllib.parse.urlsplit(link).path)
        print("Docker smoke passed: HTTP flow, non-root user, persistent data and stable links.")
    except Exception:
        subprocess.run(["docker", "logs", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", volume], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
