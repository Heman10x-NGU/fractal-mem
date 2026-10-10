from __future__ import annotations

import asyncio
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

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


def test_custom_tools_json_literal_credentials_sanitized_in_both_payloads() -> None:
    """Verify that literal secrets in tool descriptions, defaults, and examples are scrubbed

    while JSON schema structure, property keys (e.g. api_key), and types remain intact.
    """
    dummy_sk_live = "sk-live98765432109876543210987654321098"
    dummy_ghp = "ghp_1234567890abcdefABCDEF1234567890abcdef"
    dummy_sk_ant = "sk-ant-12345678901234567890"

    tools = [
        {
            "name": "configure_service",
            "description": f"Service configuration helper using {dummy_sk_ant}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "api_key": {
                        "type": "string",
                        "description": "API key for service",
                        "default": dummy_sk_live,
                        "examples": [dummy_sk_live, "harmless_sample_value"],
                    },
                    "nested_settings": {
                        "type": "object",
                        "properties": {
                            "token": {
                                "type": "string",
                                "default": dummy_ghp,
                            },
                            "endpoint": {
                                "type": "string",
                                "default": "http://localhost:8080/v1",
                                "examples": ["http://localhost:8080/v1"],
                            },
                        },
                    },
                },
                "required": ["api_key"],
            },
        }
    ]

    prefix = PromptPrefix(tools_json=json.dumps(tools))

    # Verify canonical prefix text and constant bytes do not contain raw secrets
    assert dummy_sk_live not in prefix.tools_json
    assert dummy_ghp not in prefix.tools_json
    assert dummy_sk_ant not in prefix.tools_json
    assert dummy_sk_live.encode() not in prefix.constant_bytes
    assert dummy_ghp.encode() not in prefix.constant_bytes
    assert dummy_sk_ant.encode() not in prefix.constant_bytes

    # Verify structural schema integrity is preserved
    parsed = json.loads(prefix.tools_json)
    tool = parsed[0]
    assert tool["name"] == "configure_service"
    assert "REDACTED_SECRET" in tool["description"]
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert "api_key" in schema["properties"]
    assert schema["properties"]["api_key"]["type"] == "string"
    assert schema["properties"]["api_key"]["description"] == "API key for service"
    assert "REDACTED_SECRET" in schema["properties"]["api_key"]["default"]
    assert "REDACTED_SECRET" in schema["properties"]["api_key"]["examples"][0]
    assert "REDACTED_SECRET" in schema["properties"]["api_key"]["examples"][1]
    assert "REDACTED_SECRET" in schema["properties"]["nested_settings"]["properties"]["token"]["default"]
    assert schema["properties"]["nested_settings"]["properties"]["endpoint"]["default"] == "http://localhost:8080/v1"
    assert schema["properties"]["nested_settings"]["properties"]["endpoint"]["examples"][0] == "http://localhost:8080/v1"
    assert schema["required"] == ["api_key"]

    # Verify both OpenAI and Anthropic provider payloads share the exact same sanitized schemas
    openai_payload = prefix.payload("openai", "gpt-4o", "0+1|view\n", "task")
    anthropic_payload = prefix.payload("anthropic", "claude-3-7-sonnet", "0+1|view\n", "task")

    openai_json = json.dumps(openai_payload)
    anthropic_json = json.dumps(anthropic_payload)

    for secret in (dummy_sk_live, dummy_ghp, dummy_sk_ant):
        assert secret not in openai_json
        assert secret not in anthropic_json

    assert "REDACTED_SECRET" in openai_json
    assert "REDACTED_SECRET" in anthropic_json
    assert "api_key" in openai_json and "api_key" in anthropic_json


def test_custom_tools_json_structural_credential_rejections() -> None:
    """Verify that credentials in structural fields or semantic constraints are rejected

    with static errors, and that str(error) NEVER reflects the secret token.
    """
    secret = "sk-ant-12345678901234567890"

    # 1. Secret in $ref
    ref_tools = [{"name": "tool1", "input_schema": {"$ref": f"#/defs/{secret}"}}]
    with pytest.raises(ValueError) as exc1:
        PromptPrefix(tools_json=json.dumps(ref_tools))
    assert secret not in str(exc1.value)
    assert str(exc1.value) == "tool schema contains sensitive credentials in structural field '$ref'"

    # 2. Secret in pattern
    pat_tools = [{"name": "tool2", "input_schema": {"pattern": f"^{secret}$"}}]
    with pytest.raises(ValueError) as exc2:
        PromptPrefix(tools_json=json.dumps(pat_tools))
    assert secret not in str(exc2.value)
    assert str(exc2.value) == "tool schema contains sensitive credentials in structural field 'pattern'"

    # 3. Secret in property name
    prop_tools = [{"name": "tool3", "input_schema": {"properties": {secret: {"type": "string"}}}}]
    with pytest.raises(ValueError) as exc3:
        PromptPrefix(tools_json=json.dumps(prop_tools))
    assert secret not in str(exc3.value)
    assert str(exc3.value) == "tool schema contains sensitive credentials in property name"

    # 4. Secret in required list
    req_tools = [{"name": "tool4", "input_schema": {"properties": {"foo": {"type": "string"}}, "required": [secret]}}]
    with pytest.raises(ValueError) as exc4:
        PromptPrefix(tools_json=json.dumps(req_tools))
    assert secret not in str(exc4.value)
    assert str(exc4.value) == "tool schema contains sensitive credentials in required field"

    # 5. Secret in tool name
    name_tools = [{"name": secret, "input_schema": {"type": "object"}}]
    with pytest.raises(ValueError) as exc5:
        PromptPrefix(tools_json=json.dumps(name_tools))
    assert secret not in str(exc5.value)
    assert str(exc5.value) == "tool name contains sensitive credentials"

    # 6. Secret in semantic constraint (enum / const)
    enum_tools = [{"name": "tool6", "input_schema": {"properties": {"env": {"enum": [secret, "prod"]}}}}]
    with pytest.raises(ValueError) as exc6:
        PromptPrefix(tools_json=json.dumps(enum_tools))
    assert secret not in str(exc6.value)
    assert str(exc6.value) == "tool schema contains sensitive credentials in semantic constraint 'enum'"

    const_tools = [{"name": "tool7", "input_schema": {"properties": {"token": {"const": secret}}}}]
    with pytest.raises(ValueError) as exc7:
        PromptPrefix(tools_json=json.dumps(const_tools))
    assert secret not in str(exc7.value)
    assert str(exc7.value) == "tool schema contains sensitive credentials in semantic constraint 'const'"

    # 7. Secret in malformed JSON
    bad_json = f'[{{"name": "{secret}", "input_schema":'
    with pytest.raises(ValueError) as exc8:
        PromptPrefix(tools_json=bad_json)
    assert secret not in str(exc8.value)
    assert str(exc8.value) == "tools_json must be valid JSON"

    # 8. Secret in dependentRequired key
    dep_tools = [
        {
            "name": "tool8",
            "input_schema": {
                "type": "object",
                "properties": {"auth": {"type": "string"}},
                "dependentRequired": {secret: ["auth"]},
            },
        }
    ]
    with pytest.raises(ValueError) as exc9:
        PromptPrefix(tools_json=json.dumps(dep_tools))
    assert secret not in str(exc9.value)
    assert str(exc9.value) == "tool schema contains sensitive credentials in required field"

    # 9. Sensitive property with semantic constraint (enum / const) rejected statically
    sensitive_enum_tools = [
        {
            "name": "tool9",
            "input_schema": {
                "type": "object",
                "properties": {
                    "api_key": {
                        "type": "string",
                        "enum": ["allowed_key_1", "allowed_key_2"],
                    }
                },
            },
        }
    ]
    with pytest.raises(ValueError) as exc10:
        PromptPrefix(tools_json=json.dumps(sensitive_enum_tools))
    assert "allowed_key_1" not in str(exc10.value)
    assert str(exc10.value) == "tool schema contains sensitive credentials in semantic constraint 'enum'"

    sensitive_const_tools = [
        {
            "name": "tool10",
            "input_schema": {
                "type": "object",
                "properties": {
                    "password": {
                        "type": "string",
                        "const": "fixed_secret",
                    }
                },
            },
        }
    ]
    with pytest.raises(ValueError) as exc11:
        PromptPrefix(tools_json=json.dumps(sensitive_const_tools))
    assert "fixed_secret" not in str(exc11.value)
    assert str(exc11.value) == "tool schema contains sensitive credentials in semantic constraint 'const'"


def test_custom_tools_json_sensitive_property_context_and_complex_schemas() -> None:
    """Verify that low-entropy values under sensitive properties are sanitized via context,

    and that harmless complex schemas (enum, const, pattern, $ref, required) survive 100% intact.
    """
    # 1. Low-entropy default under sensitive property name (password, api_key)
    sensitive_tools = [
        {
            "name": "login",
            "description": "User login tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "password": {
                        "type": "string",
                        "description": "User login password",
                        "default": "tiny secret",
                        "examples": ["another password"],
                    },
                    "username": {
                        "type": "string",
                        "default": "alice",
                    },
                },
                "required": ["username", "password"],
            },
        }
    ]

    prefix_sensitive = PromptPrefix(tools_json=json.dumps(sensitive_tools))
    parsed_sensitive = json.loads(prefix_sensitive.tools_json)[0]
    props = parsed_sensitive["input_schema"]["properties"]

    # Low-entropy password default/example is scrubbed
    assert "tiny secret" not in prefix_sensitive.tools_json
    assert "another password" not in prefix_sensitive.tools_json
    assert "REDACTED_SECRET" in props["password"]["default"]
    assert "REDACTED_SECRET" in props["password"]["examples"][0]
    # Harmless username default is untouched
    assert props["username"]["default"] == "alice"
    # Schema structure and description remain intact
    assert props["password"]["type"] == "string"
    assert props["password"]["description"] == "User login password"
    assert parsed_sensitive["input_schema"]["required"] == ["username", "password"]

    # 2. Harmless complex schema with enum, const, pattern, $ref, dependentRequired survives 100% unchanged
    complex_tools = [
        {
            "name": "manage_cluster",
            "description": "Cluster management tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["fast", "slow", "turbo"],
                        "pattern": "^[a-z]+$",
                    },
                    "version": {
                        "const": "2.1",
                    },
                    "target": {
                        "$ref": "#/$defs/TargetNode",
                    },
                },
                "$defs": {
                    "TargetNode": {
                        "type": "string",
                    },
                },
                "required": ["mode"],
                "dependentRequired": {
                    "mode": ["version"],
                },
            },
        }
    ]

    prefix_complex = PromptPrefix(tools_json=json.dumps(complex_tools))
    assert json.loads(prefix_complex.tools_json) == complex_tools


def test_loopback_http_request_captures_sanitized_custom_tools() -> None:
    """Verify over a real loopback TCP connection that custom tool credentials never appear in dispatched HTTP requests."""
    captured_requests: list[dict[str, object]] = []
    dummy_secret = "sk-live98765432109876543210987654321098"

    class CapturingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            captured_requests.append(json.loads(body))
            resp = json.dumps(
                {
                    "choices": [{"message": {"content": "summary fact"}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    custom_tools = [
        {
            "name": "deploy_job",
            "description": f"Deploy runner using {dummy_secret}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "api_key": {
                        "type": "string",
                        "default": dummy_secret,
                    }
                },
            },
        }
    ]
    prefix = PromptPrefix(tools_json=json.dumps(custom_tools))

    async def scenario() -> None:
        provider = HTTPTextProvider(
            ProviderConfig("openai", f"http://127.0.0.1:{server.server_port}/v1", "fixture", airgap=True),
            prefix=prefix,
        )
        try:
            reply = await provider.complete("0+1|view\n", "Compaction: extract")
            assert reply.text == "summary fact"
        finally:
            await provider.aclose()

    try:
        asyncio.run(scenario())
        assert len(captured_requests) == 1
        raw_body_text = json.dumps(captured_requests[0])
        # Physical proof: secret is absent, redacted marker is present, api_key property is present
        assert dummy_secret not in raw_body_text
        assert "REDACTED_SECRET" in raw_body_text
        assert "api_key" in raw_body_text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_budget_sanitized_summary_regressions() -> None:
    """Comprehensive regressions for budget_sanitized_summary:

    1. expansion525B: Post-redaction expansion (500B -> 525B) budgeted to <= 512B and stable.
    2. Multibyte Unicode: Boundary integrity without malformed code points or slicing partial runes.
    3. Marker boundary cuts: Cutting text across [REDACTED_SECRET_...] boundaries never leaves partial markers.
    4. Repeated sink redaction: Calling redactor.redact() multiple times on the result yields exact identity (fixed-point).
    5. Internal parent nodes: Tree nodes at levels 0, 1, 2, 3 enforce <= 512B invariants and redaction stability.
    """
    from fractalmem.core.node import HMRNode
    from fractalmem.core.summary import budget_sanitized_summary
    from fractalmem.security.redact import Redactor

    redactor = Redactor()

    # 1. Synthetic expansion fixture (495B -> 526B): Pre-redaction string 495 bytes ('A'*480 + ' password="abc"'),
    # expands to 526 bytes upon redaction with exact marker '[REDACTED_SECRET_ba7816bf8f01cfea]'.
    # Distinguish from historical LongMemEval corpus case 60d45044 (525 bytes from longmemeval_s_cleaned.json).
    raw_exp = "A" * 480 + " password=\"abc\""
    assert len(raw_exp.encode("utf-8")) == 495
    assert len(redactor.redact(raw_exp).encode("utf-8")) == 526
    assert "[REDACTED_SECRET_ba7816bf8f01cfea]" in redactor.redact(raw_exp)
    budgeted_exp = budget_sanitized_summary(raw_exp, budget=512, redactor=redactor)
    exp_bytes = len(budgeted_exp.encode("utf-8"))
    assert exp_bytes <= 512
    assert redactor.redact(budgeted_exp) == budgeted_exp
    assert "REDACTED_SECRET" in budgeted_exp

    # 2. Multibyte Unicode: Complex Japanese, accented characters, and 4-byte emoji
    unicode_source = "こんにちは世界 " * 50 + " password=\"my_pass\" " + "🚀🌟🔥" * 30
    budgeted_uni = budget_sanitized_summary(unicode_source, budget=512, redactor=redactor)
    uni_bytes = len(budgeted_uni.encode("utf-8"))
    assert uni_bytes <= 512
    assert redactor.redact(budgeted_uni) == budgeted_uni
    # Ensure UTF-8 decodable and valid code points
    assert budgeted_uni.encode("utf-8").decode("utf-8") == budgeted_uni

    # 3. Marker cuts across various budget sizes (from 32 to 200 bytes)
    marker_str = (
        "prefix "
        + "[REDACTED_SECRET_0123456789abcdef]"
        + " middle "
        + "[REDACTED_SECRET_fedcba9876543210]"
        + " suffix"
    )
    for b in range(32, len(marker_str.encode("utf-8")) + 5):
        budgeted_m = budget_sanitized_summary(marker_str, budget=b, redactor=redactor)
        b_bytes = len(budgeted_m.encode("utf-8"))
        assert b_bytes <= b
        assert redactor.redact(budgeted_m) == budgeted_m
        # Every instance of marker prefix in output must be a full, complete 34-byte marker
        if "[REDACTED_SECRET_" in budgeted_m:
            for part in budgeted_m.split("[REDACTED_SECRET_")[1:]:
                assert len(part) >= 17 and part[16] == "]", f"Cut marker at budget {b}: {budgeted_m}"

    # 4. Repeated sink redaction (fixed-point idempotence)
    sink_text = "Task summary: " + "X" * 400 + " password=\"xyz123\" " + "Y" * 100
    s1 = budget_sanitized_summary(sink_text, budget=512, redactor=redactor)
    s2 = redactor.redact(s1)
    s3 = redactor.redact(s2)
    assert s1 == s2 == s3
    assert len(s1.encode("utf-8")) <= 512

    # 5. Internal parent nodes across tree levels (L0, L1, L2, L3)
    for level in range(4):
        node_text = f"Level {level} summary: " + "Z" * 550 + " api_key=\"AKIAIOSFODNN7EXAMPLE\""
        budgeted_node = budget_sanitized_summary(node_text, budget=512, redactor=redactor)
        node = HMRNode.create(
            level=level,
            index_in_level=0,
            summary_text=budgeted_node,
            project_id="test_regressions",
        )
        assert node.level == level
        assert node.byte_size <= 512
        assert len(node.summary_text.encode("utf-8")) == node.byte_size
        assert redactor.redact(node.summary_text) == node.summary_text


def test_tool_schema_nested_inheritance_and_annotation_data_regressions() -> None:
    """Regressions for tool schema sanitization:

    1. nested credentials.properties.value.default inherits sensitivity and is scrubbed.
    2. root / object examples and defaults scrub sensitive keys while preserving harmless data.
    3. semantic constraints (enum / const) under nested sensitive scope are statically rejected.
    """
    # 1. Nested credentials.properties.value.default inherits sensitivity
    nested_tool = [
        {
            "name": "connect_service",
            "input_schema": {
                "type": "object",
                "properties": {
                    "credentials": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "string",
                                "default": "hunter2",
                            }
                        },
                    }
                },
            },
        }
    ]
    p_nested = PromptPrefix(tools_json=json.dumps(nested_tool))
    parsed_nested = json.loads(p_nested.tools_json)[0]
    val_default = parsed_nested["input_schema"]["properties"]["credentials"]["properties"]["value"]["default"]
    assert "hunter2" not in p_nested.tools_json
    assert "REDACTED_SECRET" in val_default

    # 2. Root examples / defaults with nested data payloads
    root_examples_tool = [
        {
            "name": "login_batch",
            "input_schema": {
                "type": "object",
                "properties": {
                    "tenant": {"type": "string"},
                },
                "examples": [
                    {"password": "raw_pass_1", "tenant": "acme"},
                    {"password": "raw_pass_2", "tenant": "beta"},
                ],
                "default": {
                    "password": "default_pass",
                    "safe_count": 10,
                },
            },
        }
    ]
    p_root = PromptPrefix(tools_json=json.dumps(root_examples_tool))
    parsed_root = json.loads(p_root.tools_json)[0]
    schema = parsed_root["input_schema"]
    assert "raw_pass_1" not in p_root.tools_json
    assert "raw_pass_2" not in p_root.tools_json
    assert "default_pass" not in p_root.tools_json
    assert "REDACTED_SECRET" in schema["examples"][0]["password"]
    assert schema["examples"][0]["tenant"] == "acme"
    assert "REDACTED_SECRET" in schema["default"]["password"]
    assert schema["default"]["safe_count"] == 10

    # 3. Semantic constraint (enum) under nested sensitive scope statically rejected
    nested_enum_tool = [
        {
            "name": "auth_provider",
            "input_schema": {
                "type": "object",
                "properties": {
                    "credentials": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "string",
                                "enum": ["static_token_1", "static_token_2"],
                            }
                        },
                    }
                },
            },
        }
    ]
    with pytest.raises(ValueError) as exc:
        PromptPrefix(tools_json=json.dumps(nested_enum_tool))
    assert "static_token" not in str(exc.value)
    assert str(exc.value) == "tool schema contains sensitive credentials in semantic constraint 'enum'"


def test_prompt_prefix_public_payloads_root_schema_and_wrapper_annotations() -> None:
    """Verify that PromptPrefix roots schema at input_schema, redacts wrapper annotations,

    and sanitizes local references in both OpenAI and Anthropic public payloads.
    """
    tools = [
        {
            "name": "login_service",
            "description": "Log into service with API key: sk-ant-secret12345678901234567890",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"$ref": "#/$defs/SharedCredential"},
                    "query": {"type": "string"},
                },
                "$defs": {
                    "SharedCredential": {
                        "type": "string",
                        "default": "low_entropy_secret_123",
                    }
                },
                "required": ["token"],
            },
        }
    ]
    prefix = PromptPrefix(tools_json=json.dumps(tools))

    # Constant bytes & tools_json verification
    assert "sk-ant-secret12345678901234567890" not in prefix.tools_json
    assert "low_entropy_secret_123" not in prefix.tools_json
    assert "REDACTED_SECRET" in prefix.tools_json

    # OpenAI public payload verification
    openai_payload = prefix.payload("openai", "gpt-4o", "", "task")
    openai_tools = cast(list[dict[str, object]], openai_payload["tools"])
    fn = cast(dict[str, object], openai_tools[0]["function"])
    assert fn["name"] == "login_service"
    assert "sk-ant-secret" not in str(fn["description"])
    assert "REDACTED_SECRET" in str(fn["description"])
    params = cast(dict[str, object], fn["parameters"])
    defs = cast(dict[str, object], params["$defs"])
    shared_def = cast(dict[str, object], defs["SharedCredential"])
    assert shared_def["default"] != "low_entropy_secret_123"
    assert "REDACTED_SECRET" in str(shared_def["default"])
    props = cast(dict[str, object], params["properties"])
    query_prop = cast(dict[str, object], props["query"])
    assert query_prop["type"] == "string"
    assert params["required"] == ["token"]

    # Anthropic public payload verification
    anthropic_payload = prefix.payload("anthropic", "claude-3-5-sonnet", "", "task")
    anthropic_tools = cast(list[dict[str, object]], anthropic_payload["tools"])
    a_tool = anthropic_tools[0]
    assert a_tool["name"] == "login_service"
    assert "sk-ant-secret" not in str(a_tool["description"])
    assert "REDACTED_SECRET" in str(a_tool["description"])
    a_input = cast(dict[str, object], a_tool["input_schema"])
    a_defs = cast(dict[str, object], a_input["$defs"])
    a_shared = cast(dict[str, object], a_defs["SharedCredential"])
    assert a_shared["default"] != "low_entropy_secret_123"
    assert "REDACTED_SECRET" in str(a_shared["default"])


def test_schema_unresolved_external_and_depth_limits_rejected() -> None:
    """Verify that unresolvable references, external URLs, and recursion depth limit (>32)

    are statically rejected with static ValueError messages.
    """
    # 1. Unresolvable reference
    unresolvable_tool = [
        {
            "name": "fetch",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"$ref": "#/$defs/DoesNotExist"},
                },
            },
        }
    ]
    with pytest.raises(ValueError) as exc1:
        PromptPrefix(tools_json=json.dumps(unresolvable_tool))
    assert str(exc1.value) == "tool schema contains unresolvable reference"

    # 2. External reference URL
    external_tool = [
        {
            "name": "fetch_ext",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token": {"$ref": "https://example.com/schema.json#/def"},
                },
            },
        }
    ]
    with pytest.raises(ValueError) as exc2:
        PromptPrefix(tools_json=json.dumps(external_tool))
    assert str(exc2.value) == "tool schema contains external reference which is forbidden"

    # 3. Recursion depth limit > 32
    deep_schema: dict[str, object] = {"type": "string"}
    for i in range(35):
        deep_schema = {"type": "object", "properties": {f"level_{i}": deep_schema}}
    deep_tool = [
        {
            "name": "deep_tool",
            "input_schema": deep_schema,
        }
    ]
    with pytest.raises(ValueError) as exc3:
        PromptPrefix(tools_json=json.dumps(deep_tool))
    assert str(exc3.value) == "tool schema exceeds maximum recursion depth"


def test_schema_cyclic_references_and_nested_resource_scope() -> None:
    """Verify that cyclic definitions are handled safely without infinite recursion."""
    # 1. Cyclic schema without secrets
    cyclic_tool = [
        {
            "name": "parse_ast",
            "input_schema": {
                "type": "object",
                "properties": {
                    "root": {"$ref": "#/$defs/Node"},
                },
                "$defs": {
                    "Node": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "child": {"$ref": "#/$defs/Node"},
                        },
                    }
                },
            },
        }
    ]
    prefix_cyclic = PromptPrefix(tools_json=json.dumps(cyclic_tool))
    assert "parse_ast" in prefix_cyclic.tools_json

    # 2. Cyclic schema with sensitive credential
    cyclic_sec_tool = [
        {
            "name": "auth_chain",
            "input_schema": {
                "type": "object",
                "properties": {
                    "credentials": {"$ref": "#/$defs/SecNode"},
                },
                "$defs": {
                    "SecNode": {
                        "type": "object",
                        "properties": {
                            "secret_token": {"type": "string", "default": "chain_secret_value"},
                            "next": {"$ref": "#/$defs/SecNode"},
                        },
                    }
                },
            },
        }
    ]
    prefix_sec = PromptPrefix(tools_json=json.dumps(cyclic_sec_tool))
    assert "chain_secret_value" not in prefix_sec.tools_json
    assert "REDACTED_SECRET" in prefix_sec.tools_json


def test_schema_nested_id_resolver_scope() -> None:
    """Verify that descent into a subschema with $id enters that resource's resolver scope.

    Root $defs.Value has default 'public'; nested child has $id 'child', its own
    $defs.Value with default 'hunter2', and password referencing #/$defs/Value.
    The public PromptPrefix scrubs 'hunter2' in child while preserving root 'public'.
    """
    nested_res_tool = [
        {
            "name": "nested_id_tool",
            "input_schema": {
                "$id": "https://example.com/root",
                "type": "object",
                "$defs": {
                    "Value": {"type": "string", "default": "public"}
                },
                "properties": {
                    "child": {
                        "$id": "child",
                        "type": "object",
                        "$defs": {
                            "Value": {"type": "string", "default": "hunter2"}
                        },
                        "properties": {
                            "password": {"$ref": "#/$defs/Value"}
                        }
                    }
                }
            }
        }
    ]
    prefix = PromptPrefix(tools_json=json.dumps(nested_res_tool))
    parsed = json.loads(prefix.tools_json)[0]
    # Root Value default is UNTOUCHED
    assert parsed["input_schema"]["$defs"]["Value"]["default"] == "public"
    # Child Value default is SCRUBBED
    child_def = parsed["input_schema"]["properties"]["child"]["$defs"]["Value"]
    assert "hunter2" not in prefix.tools_json
    assert "REDACTED_SECRET" in child_def["default"]

    # Public payload verification
    openai_payload = prefix.payload("openai", "gpt-4o", "", "task")
    anthropic_payload = prefix.payload("anthropic", "claude-3-5-sonnet", "", "task")
    assert "hunter2" not in json.dumps(openai_payload)
    assert "hunter2" not in json.dumps(anthropic_payload)
    assert "public" in json.dumps(openai_payload)
    assert "public" in json.dumps(anthropic_payload)


def test_schema_unsupported_dynamic_ref_and_declared_drafts() -> None:
    """Verify that dynamic references and unsupported drafts are statically rejected,

    while supported draft 7 with tuple items is handled properly.
    """
    # 1. $dynamicRef rejected
    tool_dyn = [{"name": "dyn", "input_schema": {"properties": {"x": {"$dynamicRef": "#dyn"}}}}]
    with pytest.raises(ValueError) as exc_dyn:
        PromptPrefix(tools_json=json.dumps(tool_dyn))
    assert str(exc_dyn.value) == "tool schema contains unsupported dynamic reference"

    # 2. $recursiveRef rejected
    tool_rec = [{"name": "rec", "input_schema": {"properties": {"x": {"$recursiveRef": "#"}}}}]
    with pytest.raises(ValueError) as exc_rec:
        PromptPrefix(tools_json=json.dumps(tool_rec))
    assert str(exc_rec.value) == "tool schema contains unsupported dynamic reference"

    # 3. Unsupported schema draft rejected
    tool_draft = [{"name": "d", "input_schema": {"$schema": "https://unknown.org/draft", "type": "object"}}]
    with pytest.raises(ValueError) as exc_draft:
        PromptPrefix(tools_json=json.dumps(tool_draft))
    assert str(exc_draft.value) == "tool schema contains unsupported schema draft"

    # 4. Supported Draft 7 with tuple items
    tool_tuple = [
        {
            "name": "tuple_tool",
            "input_schema": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "credentials": {
                        "type": "array",
                        "items": [
                            {"type": "string", "default": "tuple_secret_pwd"},
                            {"type": "number"}
                        ]
                    }
                }
            }
        }
    ]
    p_tuple = PromptPrefix(tools_json=json.dumps(tool_tuple))
    assert "tuple_secret_pwd" not in p_tuple.tools_json
    assert "REDACTED_SECRET" in p_tuple.tools_json


def test_schema_defs_named_enum_or_const_treated_as_definitions() -> None:
    """Verify that $defs entries named 'enum' or 'const' are treated as definition identifiers,

    preserving the definition name and thoroughly scrubbing child descriptions and defaults.
    """
    tool_map_names = [
        {
            "name": "defs_naming_tool",
            "input_schema": {
                "type": "object",
                "$defs": {
                    "enum": {
                        "type": "string",
                        "description": "Secret token is sk-ant-secret12345678901234567890",
                        "default": "harmless_val"
                    },
                    "const": {
                        "type": "object",
                        "properties": {
                            "password": {
                                "type": "string",
                                "default": "secret_in_const_def"
                            }
                        }
                    }
                }
            }
        }
    ]
    p_map = PromptPrefix(tools_json=json.dumps(tool_map_names))
    parsed_map = json.loads(p_map.tools_json)[0]
    defs = parsed_map["input_schema"]["$defs"]
    assert "enum" in defs
    assert "const" in defs
    assert "sk-ant-secret" not in p_map.tools_json
    assert "REDACTED_SECRET" in defs["enum"]["description"]
    assert defs["enum"]["default"] == "harmless_val"
    assert "secret_in_const_def" not in p_map.tools_json
    assert "REDACTED_SECRET" in defs["const"]["properties"]["password"]["default"]


def test_static_rejection_suppresses_secret_exception_chaining() -> None:
    """Verify that static errors suppress exception chaining so secret tokens in unresolvable

    references are NEVER present in str(error) or formatted exception tracebacks.
    Also verifies boolean schemas are preserved.
    """
    secret_token = "sk-live-secrettoken1234567890123456"
    unresolvable_secret_tool = [
        {
            "name": "leak_test",
            "input_schema": {
                "properties": {
                    "token": {"$ref": f"#/$defs/{secret_token}"}
                }
            }
        }
    ]
    with pytest.raises(ValueError) as exc_leak:
        PromptPrefix(tools_json=json.dumps(unresolvable_secret_tool))
    assert secret_token not in str(exc_leak.value)
    formatted_exc = "".join(traceback.format_exception(exc_leak.value))
    assert secret_token not in formatted_exc
    assert exc_leak.value.__cause__ is None
    assert exc_leak.value.__suppress_context__ is True

    # Boolean schemas
    bool_tool = [
        {
            "name": "bool_tool",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "valid_flag": True
                }
            }
        }
    ]
    p_bool = PromptPrefix(tools_json=json.dumps(bool_tool))
    parsed_bool = json.loads(p_bool.tools_json)[0]
    assert parsed_bool["input_schema"]["additionalProperties"] is False
    assert parsed_bool["input_schema"]["properties"]["valid_flag"] is True


def test_pattern_properties_and_unevaluated_properties_reference_sensitivity() -> None:
    """Verify that patternProperties and unevaluatedProperties correctly propagate sensitivity

    to local references and reject forbidden external references.
    """
    # 1. patternProperties sensitive reference propagation
    pattern_tool = [
        {
            "name": "pattern_tool",
            "input_schema": {
                "type": "object",
                "patternProperties": {
                    "^password$": {"$ref": "#/$defs/PatternSecret"}
                },
                "$defs": {
                    "PatternSecret": {
                        "type": "string",
                        "default": "hunter2_pattern_secret"
                    }
                }
            }
        }
    ]
    p_pat = PromptPrefix(tools_json=json.dumps(pattern_tool))
    assert "hunter2_pattern_secret" not in p_pat.tools_json
    assert "REDACTED_SECRET" in p_pat.tools_json

    # 2. unevaluatedProperties sensitive reference propagation
    uneval_tool = [
        {
            "name": "uneval_tool",
            "input_schema": {
                "type": "object",
                "properties": {
                    "credentials": {
                        "type": "object",
                        "unevaluatedProperties": {"$ref": "#/$defs/UnevalSecret"}
                    }
                },
                "$defs": {
                    "UnevalSecret": {
                        "type": "string",
                        "default": "hunter2_uneval_secret"
                    }
                }
            }
        }
    ]
    p_uneval = PromptPrefix(tools_json=json.dumps(uneval_tool))
    assert "hunter2_uneval_secret" not in p_uneval.tools_json
    assert "REDACTED_SECRET" in p_uneval.tools_json

    # 3. Forbidden external reference inside unevaluatedProperties is rejected
    uneval_ext_tool = [
        {
            "name": "uneval_ext_tool",
            "input_schema": {
                "type": "object",
                "unevaluatedProperties": {"$ref": "https://example.com/forbidden"}
            }
        }
    ]
    with pytest.raises(ValueError) as exc_ext:
        PromptPrefix(tools_json=json.dumps(uneval_ext_tool))
    assert str(exc_ext.value) == "tool schema contains external reference which is forbidden"


def test_dependent_schemas_trigger_name_does_not_taint_mode_enum() -> None:
    """Verify that dependentSchemas trigger property names (e.g. password)

    do not taint nested harmless schemas or reject legitimate mode enums.
    """
    tool = [
        {
            "name": "login_config",
            "input_schema": {
                "type": "object",
                "properties": {
                    "password": {"type": "string"},
                },
                "dependentSchemas": {
                    "password": {
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["fast", "secure"],
                            }
                        }
                    }
                },
            },
        }
    ]
    prefix = PromptPrefix(tools_json=json.dumps(tool))
    parsed = json.loads(prefix.tools_json)[0]
    dep_mode = parsed["input_schema"]["dependentSchemas"]["password"]["properties"]["mode"]
    assert dep_mode["enum"] == ["fast", "secure"]


def test_marker_spans_linear_incremental_pass_and_tail_code_preservation() -> None:
    """Verify incremental linear _marker_spans calculation and that tail marker cuts

    do not delete complete code identifiers in bracketed expressions like cache[keep_old_setting].
    """
    from fractalmem.core.summary import _marker_spans, budget_sanitized_summary
    from fractalmem.security.redact import Redactor

    redactor = Redactor()

    # 1. Exact spans on Unicode and multi-marker text
    text = "🚀 prefix [REDACTED_SECRET_0123456789abcdef] middle 🌟 [REDACTED_SECRET_fedcba9876543210] suffix"
    spans = _marker_spans(text)
    assert len(spans) == 2
    raw = text.encode("utf-8")
    assert raw[spans[0][0]:spans[0][1]].decode("utf-8") == "[REDACTED_SECRET_0123456789abcdef]"
    assert raw[spans[1][0]:spans[1][1]].decode("utf-8") == "[REDACTED_SECRET_fedcba9876543210]"

    # 2. Bracketed code preservation in extractive tail
    code_text = "start " * 80 + "cache[keep_old_setting] + later_decision"
    res = budget_sanitized_summary(code_text, budget=128, redactor=redactor)
    assert len(res.encode("utf-8")) <= 128
    assert "keep_old_setting" in res
    assert "later_decision" in res


def test_summary_budget_32_bytes_boundary_and_error() -> None:
    """Verify strict 32..512 byte budget boundaries and SummaryBudgetError behavior."""
    from fractalmem.core.summary import SummaryBudgetError, budget_sanitized_summary
    from fractalmem.security.redact import Redactor

    redactor = Redactor()

    # Valid minimum budget 32B
    short_text = "Short text summary line"
    res32 = budget_sanitized_summary(short_text, budget=32, redactor=redactor)
    assert len(res32.encode("utf-8")) <= 32
    assert redactor.redact(res32) == res32

    # Out-of-bounds budgets
    with pytest.raises(ValueError, match="summary budget must be 32..512 bytes"):
        budget_sanitized_summary("text", budget=31, redactor=redactor)
    with pytest.raises(ValueError, match="summary budget must be 32..512 bytes"):
        budget_sanitized_summary("text", budget=513, redactor=redactor)

    # Deliberate empty-input policy: empty or whitespace-only inputs violate the
    # non-empty candidate contract and raise SummaryBudgetError
    with pytest.raises(SummaryBudgetError, match="unable to budget summary"):
        budget_sanitized_summary("", budget=512, redactor=redactor)
    with pytest.raises(SummaryBudgetError, match="unable to budget summary"):
        budget_sanitized_summary("   ", budget=512, redactor=redactor)

    assert isinstance(SummaryBudgetError("test"), RuntimeError)


def test_disk_expansion_and_parent_completion_lifecycle(tmp_path: Path) -> None:
    """Verify physical Database lifecycle with expansion fixtures, parent node scheduling,

    idempotency/retries, conflict detection, and restart persistence across connections.
    Fixture accuracy note:
    - Synthetic expansion fixture: 495 bytes ('A'*480 + ' password="abc"') expands to 526 bytes.
    - Corpus case 60d45044: 525 bytes from longmemeval_s_cleaned.json (receipt line 37).
    """
    from fractalmem.core.node import HMRNode
    from fractalmem.protocol import Event
    from fractalmem.security.redact import Redactor
    from fractalmem.storage.db import Database, EventConflictError

    db_path = tmp_path / "test_lifecycle.db"
    redactor = Redactor()

    # 1. Complete Leaf 0 with synthetic 495B -> 526B expansion fixture
    with Database(db_path) as db:
        db.append_event(Event("p", "s", "e0", "user", "msg0 " * 200))
        db.append_event(Event("p", "s", "e1", "user", "msg1 " * 200))
        jobs = db.claim_jobs()
        assert len(jobs) == 2
        leaf0_job, leaf1_job = jobs[0], jobs[1]
        assert leaf0_job.node_id == "L0_P0_M0_0"
        assert leaf1_job.node_id == "L0_P1_M1_1"

        raw_leaf0 = "A" * 480 + " password=\"abc\""
        assert len(raw_leaf0.encode("utf-8")) == 495
        assert len(redactor.redact(raw_leaf0).encode("utf-8")) == 526
        leaf0 = HMRNode.create_leaf(leaf0_job.start_seq, raw_leaf0, project_id="p")
        db.complete_node(leaf0)
        stored_leaf0 = db.get_node("p", leaf0_job.node_id)
        assert stored_leaf0 is not None
        assert stored_leaf0.status == "ready"
        assert stored_leaf0.byte_size <= 512

        # Complete sibling Leaf 1 to schedule parent job L1_P0_M0_1
        leaf1 = HMRNode.create_leaf(leaf1_job.start_seq, "leaf 1 summary", project_id="p")
        db.complete_node(leaf1)
        stored_leaf1 = db.get_node("p", leaf1_job.node_id)
        assert stored_leaf1 is not None
        assert stored_leaf1.status == "ready"

        # 2. Both children ready -> Parent job scheduled with canonical ID L1_P0_M0_1
        parent_jobs = db.claim_jobs()
        assert len(parent_jobs) == 1
        parent_job = parent_jobs[0]
        assert parent_job.level == 1
        assert parent_job.node_id == "L1_P0_M0_1"

        # Complete Parent with oversized summary crossing 512B upon redaction (495B -> 526B)
        raw_parent = "Parent Level 1 summary " * 20 + " " * 20 + " password=\"abc\""
        assert len(raw_parent.encode("utf-8")) == 495
        assert len(redactor.redact(raw_parent).encode("utf-8")) == 526
        parent_node = HMRNode.create(
            level=parent_job.level,
            index_in_level=0,
            summary_text=raw_parent,
            project_id="p",
        )
        db.complete_node(parent_node)
        stored_parent = db.get_node("p", parent_job.node_id)
        assert stored_parent is not None
        assert stored_parent.status == "ready"
        assert stored_parent.byte_size <= 512
        assert redactor.redact(stored_parent.summary_text) == stored_parent.summary_text
        assert "[REDACTED_SECRET_ba7816bf8f01cfea]" in stored_parent.summary_text

        # 3. Idempotency / Retry: completing already-ready node returns False without error
        retry_result = db.complete_node(stored_parent)
        assert retry_result is False

        # 4. Conflict detection: conflicting ready node content raises EventConflictError
        conflict_node = HMRNode.create(1, 0, "Conflicting parent summary", project_id="p")
        with pytest.raises(EventConflictError, match="ready node content cannot change"):
            db.complete_node(conflict_node)

    # 5. Restart persistence: re-open Database from disk and verify immutable stored bytes
    with Database(db_path) as db_reopened:
        r_l0 = db_reopened.get_node("p", leaf0_job.node_id)
        r_l1 = db_reopened.get_node("p", leaf1_job.node_id)
        r_p = db_reopened.get_node("p", parent_job.node_id)
        assert r_l0 is not None and r_l0.status == "ready" and r_l0.byte_size <= 512
        assert r_l1 is not None and r_l1.status == "ready" and r_l1.byte_size <= 512
        assert r_p is not None and r_p.status == "ready" and r_p.byte_size <= 512
        assert r_p.summary_text == stored_parent.summary_text



def test_semantic_constraints_zero_false_empty_and_null() -> None:
    """Verify semantic constraint policy (const, enum):

    - In sensitive scope: 0, False, non-empty values are rejected; empty strings, empty containers, None are permitted.
    - In harmless scope: 0, False, non-sensitive enums/consts are permitted.
    """
    # 1. const: 0 under sensitive scope is rejected
    t_zero = [
        {
            "name": "login",
            "input_schema": {
                "type": "object",
                "properties": {"password": {"const": 0}},
            },
        }
    ]
    with pytest.raises(ValueError, match="sensitive credentials in semantic constraint 'const'"):
        PromptPrefix(tools_json=json.dumps(t_zero))

    # 2. const: False under sensitive scope is rejected
    t_false = [
        {
            "name": "login",
            "input_schema": {
                "type": "object",
                "properties": {"password": {"const": False}},
            },
        }
    ]
    with pytest.raises(ValueError, match="sensitive credentials in semantic constraint 'const'"):
        PromptPrefix(tools_json=json.dumps(t_false))

    # 3. const: "" (empty string) under sensitive scope is permitted
    t_empty_str = [
        {
            "name": "login",
            "input_schema": {
                "type": "object",
                "properties": {"password": {"const": ""}},
            },
        }
    ]
    p_empty = PromptPrefix(tools_json=json.dumps(t_empty_str))
    assert '{"const":""}' in p_empty.tools_json

    # 4. default: [] and default: {} and default: None under sensitive scope are permitted
    t_empty_containers = [
        {
            "name": "auth",
            "input_schema": {
                "type": "object",
                "properties": {
                    "credentials": {
                        "type": "object",
                        "default": {},
                    },
                    "tokens": {
                        "type": "array",
                        "default": [],
                    },
                    "api_key": {
                        "type": "string",
                        "default": None,
                    },
                },
            },
        }
    ]
    p_containers = PromptPrefix(tools_json=json.dumps(t_empty_containers))
    assert "credentials" in p_containers.tools_json

    # 5. Harmless scope: const: 0, const: False, enum: [0, 1] are permitted
    t_harmless = [
        {
            "name": "counter",
            "input_schema": {
                "type": "object",
                "properties": {
                    "step": {"const": 0},
                    "enabled": {"const": False},
                    "mode": {"enum": [0, 1, 2]},
                },
            },
        }
    ]
    p_harmless = PromptPrefix(tools_json=json.dumps(t_harmless))
    assert '{"const":0}' in p_harmless.tools_json
    assert '{"const":false}' in p_harmless.tools_json


def test_writer_worker_summary_budget_error_isolation(tmp_path: Path) -> None:
    """Verify that WriterWorker._compact catches SummaryBudgetError, marks the job failed,

    retains the source, and allows the worker to continue running rather than setting fatal_reason.
    """
    from fractalmem.core.node import HMRNode
    from fractalmem.core.summary import SummaryBudgetError
    from fractalmem.daemon.ring_buffer import RingBuffer
    from fractalmem.daemon.worker import WriterWorker
    from fractalmem.protocol import Event
    from fractalmem.storage.db import Database

    class BudgetFailingCompactor(Compactor):
        async def build_node(
            self, node: HMRNode, source: str, view_buffer: str = ""
        ) -> tuple[HMRNode, CompactionResult]:
            raise SummaryBudgetError("simulated per-node budget failure")

    db_path = tmp_path / "worker_budget_test.db"
    queue: RingBuffer[object] = RingBuffer(32)

    with Database(db_path) as db:
        db.append_event(Event("proj", "s", "e0", "user", "msg0 " * 200))
        db.append_event(Event("proj", "s", "e1", "user", "msg1 " * 200))
        jobs = db.claim_jobs()
        assert len(jobs) >= 1
        job = jobs[0]

    worker = WriterWorker(db_path, queue, compactor=BudgetFailingCompactor())  # type: ignore[arg-type]

    async def scenario() -> None:
        await worker.start()
        try:
            await worker._compact(job)
            # Worker fatal_reason remains None (daemon did not crash or halt)
            assert worker.fatal_reason is None
            # Durable compaction job in SQLite is recorded as failed with reason SummaryBudgetError
            def check(db: Database) -> tuple[str, str]:
                row = db.connection.execute(
                    "SELECT status, error FROM compaction_jobs WHERE node_id=?", (job.node_id,)
                ).fetchone()
                return str(row[0]), str(row[1])

            status, err = await worker.call(check)
            assert status == "failed"
            assert err == "SummaryBudgetError"

            # Source retention: source messages e0 and e1 are physically retained in SQLite
            def check_source(db: Database) -> int:
                row = db.connection.execute(
                    "SELECT count(*) FROM messages WHERE project_id=? AND event_id IN ('e0', 'e1')",
                    ("proj",),
                ).fetchone()
                return int(row[0]) if row else 0

            source_count = await worker.call(check_source)
            assert source_count == 2

            # Continuation: ingest subsequent event e2 and verify worker continues operating normally
            def add_and_claim(db: Database) -> int:
                db.append_event(Event("proj", "s", "e2", "user", "msg2 " * 200))
                return len(db.claim_jobs())

            subsequent_jobs = await worker.call(add_and_claim)
            assert subsequent_jobs >= 1
            assert worker.fatal_reason is None
        finally:
            await worker.close()

    asyncio.run(scenario())


def test_compactor_verbatim_and_provider_validation_rejects_unstable_candidates() -> None:
    """Verify common fixed-point candidate validation across verbatim and provider paths."""
    from fractalmem.compaction import ProviderReply, Usage

    # 1. Verbatim path: string is <= 512 bytes but has trailing quote that expands on re-redaction
    # Candidate fails validate_summary_candidate, proceeding to offline summary
    compactor_offline = Compactor(None)

    async def test_verbatim() -> None:
        res = await compactor_offline.compact("Task with normal length message\n")
        assert res.mode == "verbatim"
        assert res.text == "Task with normal length message"

    asyncio.run(test_verbatim())

    # 2. Provider path: attempt 1 exceeds budget (520 bytes), attempt 2 meets budget (200 bytes)
    attempts_called = 0

    class RetryProvider:
        async def complete(self, view_buffer: str, task: str) -> ProviderReply:
            nonlocal attempts_called
            if "Compaction: extract" in task:
                return ProviderReply("extracted facts", Usage(provider="p", input_tokens=10, output_tokens=10))
            attempts_called += 1
            if attempts_called == 1:
                # Oversized candidate
                return ProviderReply("A" * 520, Usage(provider="p", input_tokens=10, output_tokens=10))
            else:
                # Valid candidate
                return ProviderReply("Valid summary", Usage(provider="p", input_tokens=10, output_tokens=10))

        async def aclose(self) -> None:
            pass

    async def test_retry() -> None:
        compactor = Compactor(RetryProvider())
        oversized = "0+1|" + "source log message " * 30 + "\n"
        res = await compactor.compact(oversized)
        assert res.mode == "dual_llm"
        assert res.text == "Valid summary"
        assert attempts_called == 2

    asyncio.run(test_retry())


def test_compactor_scrubs_first_stage_facts_before_nonce_framing() -> None:
    """Verify that Compactor.compact scrubs first-stage facts with redactor.redact

    before passing them into second-stage nonce framing.
    """
    captured_payloads: list[str] = []
    secret = "sk-live-firststage98765432109876543210"

    class CapturingProvider:
        async def complete(self, view_buffer: str, task: str) -> object:
            from fractalmem.compaction import ProviderReply, Usage

            captured_payloads.append(task)
            if "Compaction: extract" in task:
                return ProviderReply(
                    text=f"First stage facts: token={secret}",
                    usage=Usage(provider="custom", input_tokens=10, output_tokens=10),
                )
            else:
                return ProviderReply(
                    text="Final sanitized summary",
                    usage=Usage(provider="custom", input_tokens=10, output_tokens=10),
                )

        async def aclose(self) -> None:
            pass

    async def scenario() -> None:
        compactor = Compactor(CapturingProvider())  # type: ignore[arg-type]
        oversized_source = "0+1|" + "source log message requiring compaction " * 25 + "\n"
        res = await compactor.compact(oversized_source)
        assert res.text == "Final sanitized summary"
        assert len(captured_payloads) == 2
        second_stage_task = captured_payloads[1]
        assert "Compaction: compress" in second_stage_task
        assert secret not in second_stage_task
        assert "REDACTED_SECRET" in second_stage_task

    asyncio.run(scenario())


def test_draft7_schema_valued_dependencies_traversed() -> None:
    """Verify Draft 7 schema-valued dependencies are traversed and propagate sensitivity."""
    from fractalmem.security.redact import Redactor
    from fractalmem.security.tool_schema import sanitize_tool_schema

    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "definitions": {
            "Value": {"type": "string", "default": "hunter2"}
        },
        "dependencies": {
            "trigger": {
                "properties": {
                    "password": {"$ref": "#/definitions/Value"}
                }
            }
        }
    }
    redactor = Redactor()
    res = cast(dict[str, object], sanitize_tool_schema(schema, redactor))
    defs = cast(dict[str, object], res["definitions"])
    val = cast(dict[str, object], defs["Value"])
    assert val["default"] != "hunter2"
    assert "REDACTED_SECRET" in str(val["default"])

    # Also verify via PromptPrefix and provider payloads
    tool = [{"name": "test_tool", "input_schema": schema}]
    prefix = PromptPrefix(tools_json=json.dumps(tool))
    for provider in ("openai", "anthropic"):
        payload = prefix.payload(provider, "model", "view", "task")
        payload_str = json.dumps(payload)
        assert "hunter2" not in payload_str
        assert "REDACTED_SECRET" in payload_str


def test_draft7_additional_items_traversed() -> None:
    """Verify Draft 7 additionalItems is traversed as a subschema propagating sensitivity."""
    from fractalmem.security.redact import Redactor
    from fractalmem.security.tool_schema import sanitize_tool_schema

    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "definitions": {
            "Value": {"type": "string", "default": "hunter2"}
        },
        "properties": {
            "password": {
                "type": "array",
                "items": [],
                "additionalItems": {"$ref": "#/definitions/Value"}
            }
        }
    }
    redactor = Redactor()
    res = cast(dict[str, object], sanitize_tool_schema(schema, redactor))
    defs = cast(dict[str, object], res["definitions"])
    val = cast(dict[str, object], defs["Value"])
    assert val["default"] != "hunter2"
    assert "REDACTED_SECRET" in str(val["default"])

    # Also verify via PromptPrefix and provider payloads
    tool = [{"name": "test_tool", "input_schema": schema}]
    prefix = PromptPrefix(tools_json=json.dumps(tool))
    for provider in ("openai", "anthropic"):
        payload = prefix.payload(provider, "model", "view", "task")
        payload_str = json.dumps(payload)
        assert "hunter2" not in payload_str
        assert "REDACTED_SECRET" in payload_str


def test_relative_id_subresource_entered_once_and_decoy_untouched() -> None:
    """Verify relative $id sub-resource is entered exactly once and does not resolve decoy."""
    from fractalmem.security.redact import Redactor
    from fractalmem.security.tool_schema import sanitize_tool_schema

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://schema.example/root.json",
        "type": "object",
        "$defs": {
            "Child": {
                "$id": "nested/",
                "type": "object",
                "$defs": {
                    "Value": {"type": "string", "default": "hunter2"}
                },
                "properties": {
                    "password": {"$ref": "#/$defs/Value"}
                }
            },
            "Decoy": {
                "$id": "nested/nested/",
                "type": "object",
                "$defs": {
                    "Value": {"type": "string", "default": "public"}
                }
            }
        },
        "properties": {
            "profile": {"$ref": "#/$defs/Child"}
        }
    }
    redactor = Redactor()
    res = cast(dict[str, object], sanitize_tool_schema(schema, redactor))
    defs = cast(dict[str, object], res["$defs"])
    child = cast(dict[str, object], defs["Child"])
    child_defs = cast(dict[str, object], child["$defs"])
    child_val = cast(dict[str, object], child_defs["Value"])
    decoy = cast(dict[str, object], defs["Decoy"])
    decoy_defs = cast(dict[str, object], decoy["$defs"])
    decoy_val = cast(dict[str, object], decoy_defs["Value"])

    # Child's Value.default must be scrubbed
    assert child_val["default"] != "hunter2"
    assert "REDACTED_SECRET" in str(child_val["default"])

    # Decoy's Value.default must remain untouched ("public")
    assert decoy_val["default"] == "public"

    # Also verify via PromptPrefix across provider payloads
    tool = [{"name": "test_tool", "input_schema": schema}]
    prefix = PromptPrefix(tools_json=json.dumps(tool))
    for provider in ("openai", "anthropic"):
        payload = prefix.payload(provider, "model", "view", "task")
        payload_str = json.dumps(payload)
        assert "hunter2" not in payload_str
        assert "REDACTED_SECRET" in payload_str
        assert "public" in payload_str
