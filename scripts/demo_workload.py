#!/usr/bin/env python3
import argparse
import asyncio
import os
import random
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://localhost:8080"

QUESTIONS = [
    "What's a fun fact about octopuses?",
    "In one sentence, why is the sky blue?",
    "What's the tallest mountain in Africa?",
    "Give me a quick tip for staying focused while coding.",
    "What year did the Berlin Wall fall?",
    "Explain photosynthesis in one sentence.",
    "What's a good substitute for buttermilk in baking?",
    "How do bees communicate with each other?",
    "What's the capital of New Zealand?",
    "Why do cats purr?",
    "What's one benefit of drinking green tea?",
    "How does a jet engine work, briefly?",
    "What's the origin of the word 'quarantine'?",
    "Name a famous painting by Vincent van Gogh.",
    "What causes a volcano to erupt?",
    "What's a simple way to reduce plastic waste at home?",
    "How far is the Moon from Earth, roughly?",
    "What's the difference between a crocodile and an alligator?",
    "Why do leaves change color in autumn?",
    "What's a common ingredient in Thai green curry?",
    "How do airplanes stay in the air?",
    "What's the boiling point of water at sea level?",
    "Name one benefit of regular exercise.",
    "What's the largest desert in the world?",
    "How do vaccines work, briefly?",
    "What's a fun fact about the Great Wall of China?",
    "Why does the ocean look blue?",
    "What's the fastest land animal?",
    "How do solar panels generate electricity?",
    "What's one tip for better sleep?",
]

NODE_COLORS = ["\033[36m", "\033[35m", "\033[33m", "\033[32m", "\033[34m", "\033[91m"]
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
STATUS_COLORS = {"online": GREEN, "offline": RED, "unknown": YELLOW}

USE_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if USE_COLOR else text


def _truncate(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fire a burst of concurrent chat completion requests at the orchestrator, so you "
            "can watch requests spill across nodes in the TUI (`llm-home-lab-tui`) as any one "
            "node fills up."
        )
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("ORCHESTRATOR_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument("--api-key", default=os.environ.get("ORCHESTRATOR_API_KEY"))
    parser.add_argument("--model", required=True, help="Model name registered/loaded on your nodes")
    parser.add_argument(
        "--concurrency", type=int, default=6, help="Simultaneous in-flight requests"
    )
    parser.add_argument("--count", type=int, default=30, help="Total requests to send")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Fixed user message for every request (default: rotate through a pool of varied "
        "questions so responses aren't served from a prompt cache)",
    )
    return parser.parse_args(argv)


async def _list_nodes(client: httpx.AsyncClient) -> list[dict[str, object]]:
    response = await client.get("/v1/nodes")
    response.raise_for_status()
    return list(response.json()["nodes"])


async def _trigger_health_check(client: httpx.AsyncClient) -> None:
    # /v1/nodes only reports online/offline for a host with a recorded probe, and nothing
    # records one except a call to /health/ready — trigger it so the node list printed below
    # isn't stuck showing "unknown" for a host nothing has probed yet.
    try:
        await client.get("/health/ready")
    except httpx.TransportError:
        pass


def _next_prompt(fixed_prompt: str | None, pool: list[str], index: int) -> str:
    if fixed_prompt is not None:
        return fixed_prompt
    if index < len(pool):
        return pool[index]
    return random.choice(QUESTIONS)


async def _post_with_retry(
    client: httpx.AsyncClient, model: str, prompt: str, max_retries: int = 2
) -> httpx.Response:
    for attempt in range(max_retries + 1):
        try:
            return await client.post(
                "/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            )
        except httpx.TransportError:
            if attempt == max_retries:
                raise
            # A burst of requests opening new connections at the exact same instant can hit a
            # transient connect/reset error — a short backoff clears it without user-visible noise.
            await asyncio.sleep(0.3 * (attempt + 1))
    raise AssertionError("unreachable")  # loop always returns or raises above


async def _send_one(
    client: httpx.AsyncClient, model: str, prompt: str, index: int, node_colors: dict[str, str]
) -> tuple[str, bool]:
    start = time.perf_counter()
    try:
        response = await _post_with_retry(client, model, prompt)
    except httpx.TransportError as exc:
        # httpx transport errors (timeouts especially) often stringify to "" with no detail,
        # so fall back to the exception's type name rather than printing a blank message.
        detail = str(exc) or type(exc).__name__
        print(f"{_c('✗', RED)} #{index:03d}  {_c('connection error: ' + detail, RED)}")
        return "?", False

    elapsed_ms = (time.perf_counter() - start) * 1000
    backend_id = response.headers.get("x-backend-id", "?")
    node_label = _c(f"{backend_id:<20}", node_colors.get(backend_id, ""))

    if response.status_code == 200:
        answer = response.json()["choices"][0]["message"]["content"]
        print(f"{_c('✓', GREEN)} #{index:03d}  {node_label}  {elapsed_ms:6.0f}ms")
        print(f"    {_c('Q', DIM)} {_truncate(prompt)}")
        print(f"    {_c('A', DIM)} {_truncate(answer)}")
        return backend_id, True

    detail = response.json().get("error", {}).get("code", response.text)
    print(
        f"{_c('✗', RED)} #{index:03d}  {node_label}  {elapsed_ms:6.0f}ms  "
        f"{_c(f'status={response.status_code} ({detail})', RED)}"
    )
    return backend_id, False


async def _worker(
    client: httpx.AsyncClient,
    model: str,
    fixed_prompt: str | None,
    pool: list[str],
    counter: list[int],
    total: int,
    per_node: dict[str, list[bool]],
    node_colors: dict[str, str],
    start_delay: float = 0.0,
) -> None:
    if start_delay:
        await asyncio.sleep(start_delay)
    while counter[0] < total:
        index = counter[0]
        counter[0] += 1
        prompt = _next_prompt(fixed_prompt, pool, index)
        backend_id, success = await _send_one(client, model, prompt, index, node_colors)
        per_node.setdefault(backend_id, []).append(success)


def _print_summary(
    node_ids: list[str], node_colors: dict[str, str], per_node: dict[str, list[bool]]
) -> None:
    print(f"\n{_c('Per-node summary:', BOLD)}")
    for node_id in [*node_ids, "?"]:
        outcomes = per_node.get(node_id, [])
        if not outcomes:
            continue
        ok = sum(outcomes)
        label = _c(f"{node_id:<20}", node_colors.get(node_id, ""))
        print(
            f"  {label}  {len(outcomes):3d} requests   {ok:3d} ok   {len(outcomes) - ok:3d} failed"
        )


async def run(args: argparse.Namespace) -> int:
    if not args.api_key:
        print("error: no API key. Pass --api-key or set ORCHESTRATOR_API_KEY.", file=sys.stderr)
        return 1

    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=120.0,
        headers={"Authorization": f"Bearer {args.api_key}"},
    ) as client:
        try:
            await _trigger_health_check(client)
            nodes = await _list_nodes(client)
        except httpx.HTTPError as exc:
            print(f"error: could not reach orchestrator at {args.base_url}: {exc}", file=sys.stderr)
            return 1

        if not nodes:
            print("error: no nodes registered on the orchestrator", file=sys.stderr)
            return 1

        node_ids = [str(node["host_id"]) for node in nodes]
        node_colors = {
            node_id: NODE_COLORS[i % len(NODE_COLORS)] for i, node_id in enumerate(node_ids)
        }

        print(f"🚀 Registered nodes ({len(nodes)}):")
        for node in nodes:
            host_id = str(node["host_id"])
            status = str(node["status"])
            status_emoji = {"online": "🟢", "offline": "🔴"}.get(status, "⚪")
            label = _c(f"{host_id:<20}", node_colors[host_id])
            status_text = _c(f"status={status:<8}", STATUS_COLORS.get(status, ""))
            print(
                f"  {status_emoji} {label} {status_text} "
                f"max_concurrent={node['max_concurrent_requests']}"
            )
        print(
            f"\n📡 Sending {args.count} requests with {args.concurrency} concurrent in flight — "
            "watch `llm-home-lab-tui` to see them spill across nodes.\n"
        )

        pool = QUESTIONS.copy()
        random.shuffle(pool)
        counter = [0]
        per_node: dict[str, list[bool]] = {}
        start = time.perf_counter()
        await asyncio.gather(
            *(
                _worker(
                    client,
                    args.model,
                    args.prompt,
                    pool,
                    counter,
                    args.count,
                    per_node,
                    node_colors,
                    start_delay=i * 0.05,
                )
                for i in range(args.concurrency)
            )
        )
        elapsed = time.perf_counter() - start

        total = sum(len(outcomes) for outcomes in per_node.values())
        succeeded = sum(sum(outcomes) for outcomes in per_node.values())
        _print_summary(node_ids, node_colors, per_node)
        rate = total / elapsed if elapsed else 0.0
        print(f"\n✅ Done: {succeeded}/{total} succeeded in {elapsed:.1f}s ({rate:.1f} req/s)")

    return 0


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        exit_code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped")
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
