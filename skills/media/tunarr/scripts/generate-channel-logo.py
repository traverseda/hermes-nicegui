#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["httpx"]
# ///
"""
Generate a channel logo via OpenRouter's FLUX.2 Pro.

Usage:
  python3 generate-channel-logo.py "Channel Name" "Mission/description for the prompt"

Saves the image to /tmp/{name-slug}-logo.png and prints the path.
Requires HINDSIGHT_LLM_API_KEY in the environment (~$0.005/logo).
"""

import base64, json, os, sys, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

OPENROUTER_KEY = os.environ.get("HINDSIGHT_LLM_API_KEY", "")
if not OPENROUTER_KEY:
    print("ERROR: HINDSIGHT_LLM_API_KEY not set", file=sys.stderr)
    sys.exit(1)
MODEL = "black-forest-labs/flux.2-pro"


def slug(name):
    return name.lower().replace(" ", "-").replace(":", "").replace("'", "")


def main():
    if len(sys.argv) < 2:
        print("Usage: generate-channel-logo.py <channel_name> [description]", file=sys.stderr)
        sys.exit(1)

    name = sys.argv[1]
    description = sys.argv[2] if len(sys.argv) > 2 else ""

    prompt = f"A professional TV channel logo for \"{name}\""
    if description:
        prompt += f" — {description}"
    prompt += ". Clean modern design, dark background, recognizable icon, bold text. 400x400 square."

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hermes.nousresearch.com",
    }

    req = Request(
        "https://openrouter.ai/api/v1/images/generations",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )

    try:
        resp = urlopen(req, timeout=120)
        data = json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode()[:1000] if e.fp else ""
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    b64 = data.get("data", [{}])[0].get("b64_json")
    if not b64:
        print(f"ERROR: no image in response: {json.dumps(data)[:500]}", file=sys.stderr)
        sys.exit(1)

    import base64
    img_data = base64.b64decode(b64)

    outpath = f"/tmp/{slug(name)}-logo.png"
    with open(outpath, "wb") as f:
        f.write(img_data)

    print(outpath)


if __name__ == "__main__":
    main()