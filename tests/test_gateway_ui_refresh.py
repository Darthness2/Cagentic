"""Regression guards for the responsive gateway workspace refresh."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = _ROOT / "cagentic" / "gateway_assets"
_HTML = (_ASSETS / "index.html").read_text(encoding="utf-8")
_CSS = (_ASSETS / "app.css").read_text(encoding="utf-8")
_JS = (_ASSETS / "app.js").read_text(encoding="utf-8")


def test_rich_outputs_use_a_responsive_accessible_workspace() -> None:
    assert '<aside id="workspacePane"' in _HTML
    assert 'aria-labelledby="workspaceTitle"' in _HTML
    assert "body.workspace-open #app" in _CSS
    assert "@media (max-width: 767px)" in _CSS
    assert "workspaceOverlay" in _JS
    assert "app.inert=dialogOpen||drawerOpen||workspaceOverlay" in _JS
    assert "ownedFocus=win.contains(document.activeElement)" in _JS
    assert "visible.length>0&&!(mobileSheet&&state.busy)" in _JS
    assert "pane.setAttribute('role',mobileSheet?'dialog':'region')" in _JS


def test_compact_header_menu_exposes_state_and_keyboard_navigation() -> None:
    assert 'id="headerMenu" role="menu"' in _HTML
    assert 'id="themeState"' in _HTML
    assert 'id="voiceOutState"' in _HTML
    assert "e.key==='ArrowDown'||e.key==='ArrowUp'" in _JS
    assert "closeHeaderMenu(true)" in _JS


def test_connection_and_settings_failures_are_visible() -> None:
    assert 'id="gatewayStatusText">Connecting' in _HTML
    assert 'id="settingsStatus"' in _HTML
    assert "setConnectionState('offline'" in _JS
    assert "showBootState('error')" in _JS
    assert "Settings could not be saved" in _JS


def test_streaming_markdown_is_painted_at_most_once_per_frame() -> None:
    assert "requestAnimationFrame(paintLiveBody)" in _JS
    assert "cancelAnimationFrame(_liveRenderFrame)" in _JS


def test_voice_input_never_sends_without_review() -> None:
    result_handler = _JS[_JS.index("recog.onresult") : _JS.index("function toggleMic")]
    assert "input.value=left+before+txt.trim()+after+right" in result_handler
    assert "input.focus()" in result_handler
    assert "submit()" not in result_handler
    assert "send(" not in result_handler
    assert "text.slice(0,600)" not in _JS
    assert "const chunks=[]" in _JS


def test_user_facing_messages_hide_internal_upload_paths() -> None:
    assert "function _splitUserPayload" in _JS
    assert "user-attachment-name" in _JS
    assert "displayText" in _JS
    assert "r._attachments=parsed.attachments" in _JS
    assert "newRaw=mentions?newText" in _JS


def test_streams_require_a_terminal_event_and_keep_handler_failures_visible() -> None:
    assert "if(!ended) throw new Error('Response stream ended before completion.')" in _JS
    assert "onEvent(event);" in _JS
    assert "try{onEvent" not in _JS
    assert "settleLiveBody(true)" in _JS
    assert "return {hadError,errorText}" in _JS


def test_boot_gates_conversation_actions_and_restores_unsent_drafts() -> None:
    assert "bootReady: false" in _JS
    assert "if(!state.bootReady||state.busy) return" in _JS
    assert "restoreComposerDraft(text,presentation)" in _JS
    assert "Your draft was restored" in _JS


def test_new_chat_is_serialized_and_stops_an_active_turn_first() -> None:
    assert "creatingChat: false" in _JS
    assert "if(!state.bootReady||state.creatingChat) return" in _JS
    assert "await abortGeneration({announce:false,refresh:false})" in _JS
    assert "e&&e.status===409" in _JS


def test_rejected_busy_send_restores_the_draft_and_optimistic_bubble() -> None:
    send = _JS[_JS.index("async function send(") : _JS.index("// ---- ATTACHMENTS")]
    assert "const conflict=!!res&&res.status===409" in send
    assert "removeOptimisticTurn(optimistic); restoreComposerDraft(text,presentation)" in send
    assert "Your message was not sent and your draft was restored" in send
    assert "if(outcome.hadError)" in send
    assert "await reconcileCurrent" in send


def test_regenerate_preserves_the_live_thinking_indicator() -> None:
    stream_edit = _JS[_JS.index("async function streamEdit") : _JS.index("function editMsg")]
    assert "const thinking=live.thinking" in stream_edit
    assert "truncateAfter(target,false)" in stream_edit
    assert "getThread().appendChild(thinking)" in stream_edit
    assert stream_edit.index("truncateAfter(target,false)") < stream_edit.index(
        "getThread().appendChild(thinking)"
    )


def test_user_message_toolbar_only_offers_copy_and_edit() -> None:
    toolbar = _JS[_JS.index("const MSG_ACTIONS_HTML") : _JS.index("const REPLY_ACTIONS_HTML")]
    assert 'data-act="copy"' in toolbar
    assert 'data-act="edit"' in toolbar
    assert 'data-act="resend"' not in toolbar
    assert 'data-act="delete"' not in toolbar


def test_composer_accepts_the_next_draft_while_a_response_streams() -> None:
    composer = _JS[_JS.index("function syncComposerState") : _JS.index("function setBusy")]
    assert "input.disabled=!state.bootReady" in composer
    assert "input.disabled=state.busy" not in composer
    assert "sendBtn.disabled=state.busy" in composer
    assert "$('#attachBtn').disabled=state.busy" in composer
    assert "$('#micBtn').disabled=state.busy" in composer


def test_tool_activity_is_nested_inside_the_assistant_response() -> None:
    assistant = _JS[_JS.index("function addAssistant") : _JS.index("function wireReplyActions")]
    assert 'class="assistant-content"' in assistant
    assert 'class="tool-activity hidden"' in assistant
    assert "addToolRow(typeof t==='string'?{name:t}:t,true,r)" in assistant
    assert "function syncToolActivity" in _JS
    assert "const row=ensureLiveAssistant()" in _JS
    sync = _JS[_JS.index("function syncToolActivity") : _JS.index("function addToolRow")]
    assert ".open=pending" not in sync
    assert ".tool-activity" in _CSS
    assert ".tool-list" in _CSS


def test_assistant_actions_follow_the_per_turn_usage_line() -> None:
    done = _JS[_JS.index("} else if(k==='done')") : _JS.index("// Clipboard helper")]
    assert "const actions=live.row&&live.row.querySelector('.msg-actions.reply')" in done
    assert "if(actions) actions.before(row)" in done
    assert ".msg-row.assistant .done-stats { padding-left: 0; }" in _CSS


def test_internal_token_routing_note_is_not_added_to_the_transcript() -> None:
    handler = _JS[_JS.index("function handle(ev)") : _JS.index("function removeOptimisticTurn")]
    assert "const internalTokenNote=k==='info'" in handler
    assert "if(!internalTokenNote){ addNote(d.text,false)" in handler
    assert "if(m) live.tokensIn=" in handler


def test_touch_layouts_keep_controls_at_least_44_pixels() -> None:
    coarse = _CSS[_CSS.rindex("@media (pointer: coarse)") :]
    assert ".icon-btn" in coarse
    assert ".hud-win-close" in coarse
    assert "min-width: 44px; min-height: 44px" in coarse


def test_explicit_themes_control_native_widgets_and_danger_contrast() -> None:
    assert ':root[data-theme="light"] { color-scheme: light; }' in _CSS
    assert ':root[data-theme="dark"] { color-scheme: dark; }' in _CSS
    assert "--on-hot:   #071018;" in _CSS
    assert "background: var(--hot); color: var(--on-hot)" in _CSS


def test_no_fractional_or_integer_font_size_is_below_twelve_pixels() -> None:
    sizes = [float(value) for value in re.findall(r"font(?:-size)?:\s*([\d.]+)px", _CSS)]
    assert all(size >= 12 for size in sizes), sizes
