"""Small OpenAI-compatible API client; configuration stays beside this script."""
import argparse
import json
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AIClient:
    def __init__(self, config_path=None):
        path = Path(config_path) if config_path else Path(__file__).with_name("ai.json")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("AI configuration must be private (chmod 600)")
        self.config = json.loads(path.read_text())
        self.base_url = self.config["base_url"].rstrip("/")
        url = urllib.parse.urlsplit(self.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("API base URL must be HTTPS without credentials/query/fragment")
        if not self.config.get("api_key") or not self.config.get("model"):
            raise ValueError("Missing API key or model")
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, endpoint, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=body,
            headers={"Authorization": "Bearer " + self.config["api_key"],
                     "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not log request headers, prompts, keys, or response bodies.
            raise RuntimeError(f"API HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("API connection failed or timed out") from None

    def models(self):
        return [item["id"] for item in self.request("/models")["data"]]

    def complete(self, messages, max_tokens=None):
        payload = {"model": self.config["model"], "messages": messages,
                   "stream": False,
                   "max_tokens": max_tokens or self.config.get("max_tokens", 1024)}
        if "thinking" in self.config:
            payload["thinking"] = {"type": self.config["thinking"]}
        result = self.request("/chat/completions", payload)
        reply = result["choices"][0]["message"].get("content")
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError("API returned no text reply")
        return reply, result.get("usage", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "smoke"])
    args = parser.parse_args()
    client = AIClient()
    available = client.config["model"] in client.models()
    print(json.dumps({"model": client.config["model"], "available": available}))
    if not available:
        raise RuntimeError("Configured model is unavailable")
    if args.command == "smoke":
        reply, usage = client.complete(
            [{"role": "user", "content": "连接测试，请只回复：连接成功"}], max_tokens=32)
        print(json.dumps({"reply": reply, "usage": usage}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
