from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from fractalmem.compaction import (
    CompactionError,
    CompactionResult,
    Compactor,
    HTTPTextProvider,
    PromptPrefix,
    ProviderConfig,
    ProviderError,
    extract_reply,
    extractive_summary,
    parse_usage,
)


def test_shared_constant_prefix_and_anthropic_four_line_blocks() -> None:
    prefix = PromptPrefix()
    main = prefix.render("0+1|one\n", "Main user task")
    compaction = prefix.render("0+1|one\n", "Compaction: extract")
    assert main.startswith(prefix.constant_bytes)
    assert compaction.startswith(prefix.constant_bytes)
    for provider in ("openai", "anthropic"):
        a = prefix.payload(provider, "model", "0+1|one\n", "turn")
        b = prefix.payload(provider, "model", "0+1|one\n", "Compaction: one")
        assert a["tools"] == b["tools"]
        if provider == "anthropic":
            assert a["system"] == b["system"]
        else:
            assert a["messages"][0] == b["messages"][0]  # type: ignore[index]
    payload = prefix.payload(
        "anthropic", "model", "".join(f"{i}+1|line\n" for i in range(9)), "task"
    )
    blocks = payload["messages"][0]["content"]  # type: ignore[index]
    assert blocks[1]["text"].count("\n") == 4
    assert blocks[2]["cache_control"] == {"type": "ephemeral"}
    assert blocks[-1]["text"].startswith("8+1|line\n")


def test_tools_keep_schema_fields_that_look_like_secret_names() -> None:
    tools = [{"name": "configure", "input_schema": {"properties": {"api_key": {"type": "string"}}}}]
    prefix = PromptPrefix(tools_json=json.dumps(tools))
    assert json.loads(prefix.tools_json) == tools


def test_explicit_offline_summary_has_both_ends_and_preserves_source() -> None:
    source = "HEAD_decision " + "middle " * 1000 + "TAIL_error"
    summary = extractive_summary(source)
    assert len(summary.encode("utf-8")) <= 512
    assert "HEAD_decision" in summary and "TAIL_error" in summary
    assert "middle " * 1000 in source
    result = asyncio.run(Compactor().compact(source))
    assert result.mode == "offline_extractive" and result.usage == ()
    short = asyncio.run(Compactor().compact("unchanged source"))
    assert short.mode == "verbatim" and short.text == "unchanged source"


def test_actual_usage_fields_and_unknowns() -> None:
    a = parse_usage(
        "anthropic",
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 100,
        },
    )
    assert a.input_tokens == 1000 and a.cache_read_rate == 0.8
    o = parse_usage(
        "openai",
        {
            "prompt_tokens": 1000,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    )
    assert o.cache_read_rate == 0.8
    r = parse_usage(
        "openai", {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 900}}
    )
    assert r.cache_read_rate == 0.9
    unknown = parse_usage("openai", {})
    assert unknown.input_tokens is None and unknown.cache_read_rate is None
    assert unknown.ttft_ms is None


def test_provider_contract_rejects_tool_calls_and_scrubs_output() -> None:
    secret = "ghp_" + "x" * 36
    assert (
        secret not in extract_reply("openai", {"choices": [{"message": {"content": secret}}]}).text
    )
    assert (
        extract_reply("anthropic", {"content": [{"type": "text", "text": "facts"}]}).text == "facts"
    )
    with pytest.raises(ProviderError, match="tool call"):
        extract_reply(
            "openai", {"choices": [{"message": {"content": "x", "tool_calls": [{"name": "run"}]}}]}
        )
    with pytest.raises(ProviderError, match="tool call"):
        extract_reply("anthropic", {"content": [{"type": "tool_use"}]})


def test_airgap_remote_rejection_and_dynamic_marker(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="airgap"):
        HTTPTextProvider(
            ProviderConfig("openai", "https://remote.example/v1", "model", airgap=True)
        )
    with pytest.raises(ValueError):
        HTTPTextProvider(ProviderConfig("openai", "http://remote.example/v1", "model"))
    marker = tmp_path / ".fractalmem_airgap"
    provider = HTTPTextProvider(
        ProviderConfig("openai", "https://remote.example/v1", "model", airgap_file=marker)
    )
    marker.touch()
    with pytest.raises(ProviderError, match="airgap"):
        asyncio.run(provider.complete("view", "task"))
    HTTPTextProvider(ProviderConfig("openai", "http://127.0.0.1:1234/v1", "local", airgap=True))


def test_two_stages_over_real_http_and_enforcement_retry_budget() -> None:
    """Real HTTP transport using protocol fixtures; this is not a live LLM score."""
    requests: list[dict[str, object]] = []
    replies = ["fact " * 150, "x" * 600, "final summary"]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(json.loads(body))
            text = replies[min(len(requests) - 1, len(replies) - 1)]
            response = json.dumps(
                {
                    "choices": [{"message": {"content": text}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 50},
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            pass  # Fixture server intentionally has no stdout/stderr diagnostics.

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = HTTPTextProvider(
            ProviderConfig(
                "openai", f"http://127.0.0.1:{server.server_port}/v1", "fixture", airgap=True
            )
        )
        source = "secret=lowvalue " + "evidence " * 200

        async def first() -> CompactionResult:
            try:
                return await Compactor(provider).compact(source, "0+1|view\n")
            finally:
                await provider.aclose()

        result = asyncio.run(first())
        assert result.text == "final summary" and result.mode == "dual_llm"
        assert len(requests) == len(result.usage) == 3
        assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
        assert requests[0]["messages"][0] == requests[1]["messages"][0]  # type: ignore[index]
        assert "lowvalue" not in json.dumps(requests)
        assert "EXTERNAL_UNTRUSTED_NONCE" in json.dumps(requests)
        assert result.usage[0].response_latency_ms is not None
        assert result.usage[0].ttft_ms is None
        requests.clear()
        replies[:] = ["facts", "x" * 600]

        async def failing() -> None:
            try:
                await Compactor(provider, max_attempts=5).compact("data " * 200)
            finally:
                await provider.aclose()

        with pytest.raises(CompactionError, match="exhausted"):
            asyncio.run(failing())
        assert len(requests) == 6  # One extraction and five enforcement attempts.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("recent_clock_origin", [False, True])
def test_prefix_flight_first_byte_release_and_client_reuse(recent_clock_origin: bool) -> None:
    """Actual HTTP followers wait for the cold-prefix response's first byte."""
    from http.server import ThreadingHTTPServer

    arrived: list[float] = []
    first_byte_at: list[float] = []
    import time

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            arrived.append(time.monotonic())
            first_request = len(arrived) == 1
            if first_request:
                time.sleep(0.08)
            body = json.dumps({"choices": [{"message": {"content": "summary"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if first_request:
                first_byte_at.append(time.monotonic())
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario() -> None:
        provider = HTTPTextProvider(
            ProviderConfig("openai", f"http://127.0.0.1:{server.server_port}/v1", "fixture")
        )
        if recent_clock_origin:
            # No prefix flight exists yet. A recent timestamp reproduces the
            # startup decision on machines with less than 240 seconds uptime.
            provider._prefix_ready_at = time.monotonic()
        try:
            await asyncio.gather(*(provider.complete("view", f"task{i}") for i in range(4)))
            assert len(arrived) == 4
            assert all(moment >= first_byte_at[0] for moment in arrived[1:])
            client = provider._client
            await provider.complete("view", "another task")
            assert provider._client is client
        finally:
            await provider.aclose()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_provider_transient_retry_is_bounded_and_invalid_prefix_rejected() -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(1)
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def scenario() -> None:
        provider = HTTPTextProvider(
            ProviderConfig("openai", f"http://127.0.0.1:{server.server_port}/v1", "fixture")
        )
        try:
            with pytest.raises(ProviderError):
                await provider.complete("view", "task")
            assert len(requests) == 3
        finally:
            await provider.aclose()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with pytest.raises(ValueError):
        PromptPrefix(tools_json='[{"name":"incomplete"}]')
    assert (
        parse_usage(
            "openai", {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}}
        ).cache_read_rate
        is None
    )
