"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path

from api.config import (
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, CLI_TOOLSETS,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    resolve_model_provider,
)

# Global lock for os.environ writes. Per-session locks (_agent_lock) prevent
# concurrent runs of the SAME session, but two DIFFERENT sessions can still
# interleave their os.environ writes. This global lock serializes the env
# save/restore around the entire agent run.
_ENV_LOCK = threading.Lock()

# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
try:
    from run_agent import AIAgent
except ImportError:
    AIAgent = None
from api.models import get_session, title_from
from api.workspace import set_last_workspace

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal'}


def _sanitize_messages_for_api(messages):
    """Return a deep copy of messages with only API-safe fields.

    The webui stores extra metadata on messages (attachments, timestamp, _ts)
    for display purposes. Some providers (e.g. Z.AI/GLM) reject unknown fields
    instead of ignoring them, causing HTTP 400 errors on subsequent messages.
    """
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get('role'):
            clean.append(sanitized)
    return clean


def _run_direct_llm_streaming(session_id, model, msg_text, put, on_token, s):
    """
    Direct LLM streaming fallback when AIAgent is not installed.
    Supports Ollama, LM Studio, vLLM, and any OpenAI-compatible API.
    """
    from api.config import resolve_model_provider, get_config
    import urllib.request
    import json

    resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model)
    cfg = get_config()
    model_cfg = cfg.get('model', {})

    base_url = resolved_base_url or (model_cfg.get('base_url') if isinstance(model_cfg, dict) else '')
    if not base_url:
        if resolved_provider == 'ollama':
            base_url = 'http://localhost:11434/v1'
        elif resolved_provider == 'lmstudio':
            base_url = 'http://localhost:1234/v1'
        elif resolved_provider == 'vllm':
            base_url = 'http://localhost:8000/v1'
        else:
            base_url = 'http://localhost:11434/v1'

    base_url = base_url.strip().rstrip('/')
    if base_url.endswith('/v1'):
        chat_url = base_url + '/chat/completions'
    elif '/v1/' in base_url:
        chat_url = base_url.split('/v1/')[0] + '/v1/chat/completions'
    else:
        chat_url = base_url + '/v1/chat/completions'

    # Build messages
    messages = []
    messages.append({'role': 'system', 'content': f'Active workspace at session start: {s.workspace}'})
    for m in _sanitize_messages_for_api(s.messages):
        if isinstance(m, dict) and m.get('role') in ('user', 'assistant', 'system'):
            messages.append({'role': m['role'], 'content': m.get('content', '')})

    messages.append({'role': 'user', 'content': f'[Workspace: {s.workspace}]\n' + msg_text})

    target_model = resolved_model or (model_cfg.get('default') if isinstance(model_cfg, dict) else '') or 'default'

    payload = {
        'model': target_model,
        'messages': messages,
        'stream': True,
    }

    headers = {'Content-Type': 'application/json'}
    api_key = os.getenv('LOCAL_API_KEY') or os.getenv('OPENAI_API_KEY') or os.getenv('HERMES_API_KEY')
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    req = urllib.request.Request(chat_url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')

    full_reply = ""
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for line in resp:
                line_str = line.decode('utf-8').strip()
                if not line_str or line_str.startswith(':'):
                    continue
                if line_str.startswith('data: '):
                    data_str = line_str[6:].strip()
                    if data_str == '[DONE]':
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                        content = delta.get('content')
                        if content:
                            full_reply += content
                            on_token(content)
                    except Exception:
                        pass
    except Exception as e:
        err_msg = f"로컬 LLM 연결 오류 ({chat_url}): {str(e)}"
        on_token(f"\n[{err_msg}]\n")
        full_reply += f"\n[{err_msg}]\n"

    # Save to session messages with deduplication check
    already_has_user_msg = False
    if s.messages:
        last_m = s.messages[-1]
        if isinstance(last_m, dict) and last_m.get('role') == 'user':
            c = str(last_m.get('content', ''))
            if c == msg_text or c.endswith(msg_text):
                already_has_user_msg = True

    now_ts = int(time.time())
    if not already_has_user_msg:
        s.messages.append({'role': 'user', 'content': msg_text, 'timestamp': now_ts})

    s.messages.append({'role': 'assistant', 'content': full_reply, 'timestamp': now_ts})
    s.title = title_from(s.messages, s.title)
    s.save()

    usage = {
        'input_tokens': len(msg_text) // 4,
        'output_tokens': len(full_reply) // 4,
        'estimated_cost': 0.0,
    }
    put('done', {
        'session': s.compact() | {'messages': s.messages, 'tool_calls': getattr(s, 'tool_calls', []) or []},
        'usage': usage,
    })


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id, attachments=None):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id]."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    # Sprint 10: create a cancel event for this stream
    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event

    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and event not in ('cancel', 'error'):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            pass

    try:
        s = get_session(session_id)
        s.workspace = str(Path(workspace).expanduser().resolve())
        s.model = model

        _agent_lock = _get_session_agent_lock(session_id)
        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            put('cancel', {'message': 'Cancelled before start'})
            return

        # Resolve profile home for this agent run (snapshot at start)
        try:
            from api.profiles import get_active_hermes_home
            _profile_home = str(get_active_hermes_home())
        except ImportError:
            _profile_home = os.environ.get('HERMES_HOME', '')

        _set_thread_env(
            TERMINAL_CWD=str(s.workspace),
            HERMES_EXEC_ASK='1',
            HERMES_SESSION_KEY=session_id,
            HERMES_HOME=_profile_home,
        )
        # Still set process-level env as fallback for tools that bypass thread-local
        with _ENV_LOCK:
          old_cwd = os.environ.get('TERMINAL_CWD')
          old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
          old_session_key = os.environ.get('HERMES_SESSION_KEY')
          old_hermes_home = os.environ.get('HERMES_HOME')
          os.environ['TERMINAL_CWD'] = str(s.workspace)
          os.environ['HERMES_EXEC_ASK'] = '1'
          os.environ['HERMES_SESSION_KEY'] = session_id
          if _profile_home:
              os.environ['HERMES_HOME'] = _profile_home

          try:
            def on_token(text):
                if text is None:
                    return  # end-of-stream sentinel
                put('token', {'text': text})

            def on_tool(name, preview, args):
                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:4]:
                        s2 = str(v); args_snap[k] = s2[:120]+('...' if len(s2)>120 else '')
                put('tool', {'name': name, 'preview': preview, 'args': args_snap})
                # also check for pending approval and surface it immediately
                from tools.approval import has_pending as _has_pending, _pending, _lock
                if _has_pending(session_id):
                    with _lock:
                        p = dict(_pending.get(session_id, {}))
                    if p:
                        put('approval', p)

            if AIAgent is None:
                _run_direct_llm_streaming(session_id, model, msg_text, put, on_token, s)
                return
            resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model)

            # Resolve API key via Hermes runtime provider (matches gateway behaviour)
            resolved_api_key = None
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider
                _rt = resolve_runtime_provider(requested=resolved_provider)
                resolved_api_key = _rt.get("api_key")
                rt_provider = _rt.get("provider")
                rt_base_url = _rt.get("base_url")
                if not resolved_provider or str(resolved_provider).startswith('custom:'):
                    resolved_provider = rt_provider
                if not resolved_base_url or str(resolved_provider).startswith('custom'):
                    resolved_base_url = rt_base_url
            except Exception as _e:
                print(f"[webui] WARNING: resolve_runtime_provider failed: {_e}", flush=True)

            # Read per-profile config at call time (not module-level snapshot)
            from api.config import get_config as _get_config
            _cfg = _get_config()

            # Per-profile toolsets (fall back to module-level CLI_TOOLSETS)
            _pt = _cfg.get('platform_toolsets', {})
            _toolsets = _pt.get('cli', CLI_TOOLSETS) if isinstance(_pt, dict) else CLI_TOOLSETS

            # Fallback model from profile config (e.g. for rate-limit recovery)
            _fallback = _cfg.get('fallback_model') or None
            if _fallback:
                # Resolve the fallback through our provider logic too
                fb_model = _fallback.get('model', '')
                fb_provider = _fallback.get('provider', '')
                fb_base_url = _fallback.get('base_url')
                _fallback_resolved = {
                    'model': fb_model,
                    'provider': fb_provider,
                    'base_url': fb_base_url,
                }
            else:
                _fallback_resolved = None

            print(f"[webui-debug] resolved_model={resolved_model} resolved_provider={resolved_provider} resolved_base_url={resolved_base_url}", flush=True)
            try:
                agent = AIAgent(
                    model=resolved_model,
                    provider=resolved_provider,
                    base_url=resolved_base_url,
                    api_key=resolved_api_key,
                    platform='cli',
                    quiet_mode=True,
                    enabled_toolsets=_toolsets,
                    fallback_model=_fallback_resolved,
                    session_id=session_id,
                    stream_delta_callback=on_token,
                    tool_progress_callback=on_tool,
                )
                workspace_ctx = f"[Workspace: {s.workspace}]\n"
                workspace_system_msg = (
                    f"Active workspace at session start: {s.workspace}\n"
                    "Every user message is prefixed with [Workspace: /absolute/path] indicating the "
                    "workspace the user has selected in the web UI at the time they sent that message. "
                    "This tag is the single authoritative source of the active workspace and updates "
                    "with every message. It overrides any prior workspace mentioned in this system "
                    "prompt, memory, or conversation history. Always use the value from the most recent "
                    "[Workspace: ...] tag as your default working directory for ALL file operations: "
                    "write_file, read_file, search_files, terminal workdir, and patch. "
                    "Never fall back to a hardcoded path when this tag is present."
                )
                result = agent.run_conversation(
                    user_message=workspace_ctx + msg_text,
                    system_message=workspace_system_msg,
                    conversation_history=_sanitize_messages_for_api(s.messages),
                    task_id=session_id,
                    persist_user_message=msg_text,
                )
                err_text = str(result.get('error') or '')
                last_msgs = result.get('messages') or []
                last_content = str(last_msgs[-1].get('content', '')) if last_msgs else ''
                if 'does not support tools' in err_text or 'does not support tools' in last_content or 'context window' in err_text or 'context window' in last_content or not result.get('completed', True):
                    if 'does not support tools' in err_text or 'does not support tools' in last_content or 'context window' in err_text or 'context window' in last_content:
                        print(f"[webui] AIAgent returned tool/context error, falling back to direct LLM streaming", flush=True)
                        _run_direct_llm_streaming(session_id, model, msg_text, put, on_token, s)
                        return
                s.messages = result.get('messages') or s.messages
            except Exception as _agent_err:
                print(f"[webui] AIAgent failed ({_agent_err}), falling back to direct LLM streaming", flush=True)
                _run_direct_llm_streaming(session_id, model, msg_text, put, on_token, s)
                return

            # ── Handle context compression side effects ──
            # If compression fired inside run_conversation, the agent may have
            # rotated its session_id. Detect and fix the mismatch so the WebUI
            # continues writing to the correct session file.
            _agent_sid = getattr(agent, 'session_id', None)
            _compressed = False
            if _agent_sid and _agent_sid != session_id:
                old_sid = session_id
                new_sid = _agent_sid
                # Rename the session file
                old_path = SESSION_DIR / f'{old_sid}.json'
                new_path = SESSION_DIR / f'{new_sid}.json'
                s.session_id = new_sid
                with LOCK:
                    if old_sid in SESSIONS:
                        SESSIONS[new_sid] = SESSIONS.pop(old_sid)
                if old_path.exists() and not new_path.exists():
                    try:
                        old_path.rename(new_path)
                    except OSError:
                        pass
                _compressed = True
            # Also detect compression via the result dict or compressor state
            if not _compressed:
                _compressor = getattr(agent, 'context_compressor', None)
                if _compressor and getattr(_compressor, 'compression_count', 0) > 0:
                    _compressed = True
            # Notify the frontend that compression happened
            if _compressed:
                put('compressed', {
                    'message': 'Context auto-compressed to continue the conversation',
                })

            # Stamp 'timestamp' on any messages that don't have one yet
            _now = time.time()
            for _m in s.messages:
                if isinstance(_m, dict) and not _m.get('timestamp') and not _m.get('_ts'):
                    _m['timestamp'] = int(_now)
            s.title = title_from(s.messages, s.title)
            # Read token/cost usage from the agent object (if available)
            input_tokens = getattr(agent, 'session_prompt_tokens', 0) or 0
            output_tokens = getattr(agent, 'session_completion_tokens', 0) or 0
            estimated_cost = getattr(agent, 'session_estimated_cost_usd', None)
            s.input_tokens = (s.input_tokens or 0) + input_tokens
            s.output_tokens = (s.output_tokens or 0) + output_tokens
            if estimated_cost:
                s.estimated_cost = (s.estimated_cost or 0) + estimated_cost
            # Extract tool call metadata grouped by assistant message index
            # Each tool call gets assistant_msg_idx so the client can render
            # cards inline with the assistant bubble that triggered them.
            tool_calls = []
            pending_names = {}   # tool_call_id -> name
            pending_args = {}    # tool_call_id -> args dict
            pending_asst_idx = {} # tool_call_id -> index in s.messages
            for msg_idx, m in enumerate(s.messages):
                if m.get('role') == 'assistant':
                    c = m.get('content', '')
                    if isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get('type') == 'tool_use':
                                tid = p.get('id', '')
                                pending_names[tid] = p.get('name', '')
                                pending_args[tid] = p.get('input', {})
                                pending_asst_idx[tid] = msg_idx
                elif m.get('role') == 'tool':
                    tid = m.get('tool_call_id') or m.get('tool_use_id', '')
                    name = pending_names.get(tid, '')
                    if not name or name == 'tool':
                        continue  # skip unresolvable tool entries
                    asst_idx = pending_asst_idx.get(tid, -1)
                    args = pending_args.get(tid, {})
                    raw = str(m.get('content', ''))
                    try:
                        rd = json.loads(raw)
                        snippet = str(rd.get('output') or rd.get('result') or rd.get('error') or raw)[:200]
                    except Exception:
                        snippet = raw[:200]
                    # Truncate args values for storage
                    args_snap = {}
                    if isinstance(args, dict):
                        for k, v in list(args.items())[:6]:
                            s2 = str(v)
                            args_snap[k] = s2[:120] + ('...' if len(s2) > 120 else '')
                    tool_calls.append({
                        'name': name, 'snippet': snippet, 'tid': tid,
                        'assistant_msg_idx': asst_idx, 'args': args_snap,
                    })
            s.tool_calls = tool_calls
            # Tag the matching user message with attachment filenames for display on reload
            # Only tag a user message whose content relates to this turn's text
            # (msg_text is the full message including the [Attached files: ...] suffix)
            if attachments:
                for m in reversed(s.messages):
                    if m.get('role') == 'user':
                        content = str(m.get('content', ''))
                        # Match if content is part of the sent message or vice-versa
                        base_text = msg_text.split('\n\n[Attached files:')[0].strip()
                        if base_text[:60] in content or content[:60] in msg_text:
                            m['attachments'] = attachments
                            break
            s.save()
            # Sync to state.db for /insights (opt-in setting)
            try:
                from api.config import load_settings as _load_settings
                if _load_settings().get('sync_to_insights'):
                    from api.state_sync import sync_session_usage
                    sync_session_usage(
                        session_id=s.session_id,
                        input_tokens=s.input_tokens or 0,
                        output_tokens=s.output_tokens or 0,
                        estimated_cost=s.estimated_cost,
                        model=model,
                        title=s.title,
                    )
            except Exception:
                pass  # never crash the stream for sync failures
            usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'estimated_cost': estimated_cost}
            # Include context window data from the agent's compressor for the UI indicator
            _cc = getattr(agent, 'context_compressor', None)
            if _cc:
                usage['context_length'] = getattr(_cc, 'context_length', 0) or 0
                usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            put('done', {'session': s.compact() | {'messages': s.messages, 'tool_calls': tool_calls}, 'usage': usage})
          finally:
            if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
            else: os.environ['TERMINAL_CWD'] = old_cwd
            if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
            else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
            if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
            else: os.environ['HERMES_SESSION_KEY'] = old_session_key
            if old_hermes_home is None: os.environ.pop('HERMES_HOME', None)
            else: os.environ['HERMES_HOME'] = old_hermes_home

    except Exception as e:
        print('[webui] stream error:\n' + traceback.format_exc(), flush=True)
        err_str = str(e)
        # Detect rate limit errors specifically so the client can show a helpful card
        # rather than the generic "Connection lost" message
        is_rate_limit = 'rate limit' in err_str.lower() or '429' in err_str or 'RateLimitError' in type(e).__name__
        if is_rate_limit:
            put('apperror', {
                'message': err_str,
                'type': 'rate_limit',
                'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
            })
        else:
            put('apperror', {'message': err_str, 'type': 'error'})
    finally:
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if the stream existed."""
    with STREAMS_LOCK:
        if stream_id not in STREAMS:
            return False
        flag = CANCEL_FLAGS.get(stream_id)
        if flag:
            flag.set()
        # Put a cancel sentinel into the queue so the SSE handler wakes up
        q = STREAMS.get(stream_id)
        if q:
            try:
                q.put_nowait(('cancel', {'message': 'Cancelled by user'}))
            except Exception:
                pass
    return True
