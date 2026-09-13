# -*- coding: utf-8 -*-
"""이 Mac에 로그인된 Claude Code를 불러 쓴다 (2026-09).

API 키(별도 결제) 대신 지금 쓰는 Claude 구독으로 돌린다 — 사용량은 구독 한도에서
차감된다. 인증은 .env의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로 발급)을 쓴다.
터미널 로그인은 만료되면 자동 실행이 갱신하지 못하고 3분 가까이 기다리다 거절됐다(실측).

프로젝트 폴더 밖에서 실행한다 — 안에서 돌리면 이 저장소의 작업 지침·기억·훅이 매 호출에
딸려 들어가 사용량이 불어난다(실측: 사진 한 장 판독에 6만 토큰).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

_SYSTEM = ("너는 쇼핑몰 상세페이지 작업 도우미다. 지시된 이미지 파일이 있으면 Read 도구로 "
           "읽고, 요청한 JSON만 출력한다. 설명·인사말 없이 JSON만.")


class ClaudeUnavailable(Exception):
    """화면에 그대로 보여줄 수 있는 사유를 담는다."""


def unavailable_reason() -> str:
    """지금 부를 수 없으면 그 이유, 부를 수 있으면 빈 문자열."""
    if not shutil.which("claude"):
        return "이 컴퓨터에 Claude Code가 설치돼 있지 않습니다"
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ".env에 CLAUDE_CODE_OAUTH_TOKEN이 없습니다 — 터미널에서 claude setup-token으로 발급하세요"
    return ""


def _explain(msg: str) -> str:
    low = msg.lower()
    if "oauth" in low or "authenticat" in low or "401" in low:
        return ("Claude 로그인 토큰이 만료됐거나 잘못됐습니다 — 터미널에서 claude setup-token으로 "
                "다시 발급해 .env의 CLAUDE_CODE_OAUTH_TOKEN을 바꾸세요")
    if "limit" in low or "429" in low:
        return "Claude 구독 사용 한도에 걸렸습니다 — 한도가 풀린 뒤 다시 누르세요"
    return f"Claude 호출 실패: {msg[:200]}"


def ask(prompt: str, *, model: str, files: Iterable[Path] = (), timeout: int = 300,
        think: bool = True) -> str:
    """Claude Code에 한 번 묻고 답 글자를 돌려준다. 실패하면 ClaudeUnavailable.

    think=False면 답하기 전에 속으로 생각하는 단계를 끈다 — 짧은 문구 JSON 하나에 생각이
    1만3천 토큰 붙어 114초 걸리던 것이 9초·사용량 1/10이 됐다(실측)."""
    why = unavailable_reason()
    if why:
        raise ClaudeUnavailable(why)

    files = [Path(f).resolve() for f in files]
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
           "--system-prompt", _SYSTEM, "--no-session-persistence", "--strict-mcp-config",
           "--tools", "Read" if files else ""]
    if files:
        cmd += ["--allowedTools", "Read"]
        for d in sorted({str(f.parent) for f in files}):
            cmd += ["--add-dir", d]
    # API 키가 환경에 있으면 Claude Code가 그걸 먼저 써서 유료 과금으로 넘어간다
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    if not think:
        env["MAX_THINKING_TOKENS"] = "0"

    try:
        out = subprocess.run(cmd, input="", capture_output=True, text=True, timeout=timeout,
                             cwd=tempfile.gettempdir(), env=env)
    except subprocess.TimeoutExpired:
        raise ClaudeUnavailable(f"Claude 응답이 {timeout}초 안에 오지 않았습니다")
    try:
        d = json.loads(out.stdout)
    except Exception:
        raise ClaudeUnavailable(f"Claude Code 실행 실패: {(out.stderr or out.stdout).strip()[:200]}")
    if d.get("is_error"):
        raise ClaudeUnavailable(_explain(str(d.get("result", ""))))
    return str(d.get("result", ""))


def _demo() -> None:
    """자체 점검 — 실제 호출 없음."""
    assert "만료" in _explain('API Error: 401 {"message":"OAuth access token has expired."}')
    assert "한도" in _explain("Claude AI usage limit reached")
    saved = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        if shutil.which("claude"):
            assert "CLAUDE_CODE_OAUTH_TOKEN" in unavailable_reason(), "토큰 없이 부를 수 있다고 판단함"
        try:
            ask("x", model="claude-haiku-4-5-20251001")
            raise AssertionError("토큰 없이 호출을 시도함")
        except ClaudeUnavailable:
            pass
    finally:
        if saved is not None:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = saved
    print("claude_cli._demo self-check OK")


if __name__ == "__main__":
    _demo()
