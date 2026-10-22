"""HealthUp 웹 애플리케이션 2모드 화면 검사 (webapp_plan 작업 A2).

화면 검사 10개 항목:
1. 데모 모드 기본 표시: 자리표시 영역(#avatarCard) 노출, 예외 3개(#aiNotice 'AI 마음 건강 도우미', 배지, 연결상태) 노출, 개발 패널 hidden, #avatarHint '대화' 문구
2. 데모 모드 onHello 뒤 헤더 숨김 유지 (필수 1): 세션 시작 및 hello 수신 후에도 #engines·#voiceWrap·#sttWrap·#epWrap 숨김 유지
3. 개발 모드 표시: 개발 패널(#micCard, #textCard, #logCard, #latCard) 및 헤더 옵션 노출, #avatarCard 숨김
4. 모드 전환 시 config 전송: demo <-> dev 토글 시 WebSocket으로 {"type": "config", "mode": ...} 프레임 전송 확인
5. 새로고침 뒤 모드 유지: localStorage에 저장된 모드가 reload 후에도 유지되는지 확인
6. AI 고지 해제 경고 모달: #prefAiNotice 해제 시 confirm 취소 시 유지, 승인 시 숨김 확인
7. 자리표시 터치 시 start() 및 중지 상태 갱신 (권장 1·2): #avatarCard 터치 시 시작, stopAll() 시 #avatarStatusText '중지됨' 갱신
8. 설정 패널 6개 개별 토글 및 카메라 배지 연동 (계획서 ③ & 필수 3): show_dev_info, #cameraBadge 문구(카메라 켜짐/마이크 대기 중) 및 숨김 조건 연동
9. 개별 토글 변경 시 config 전송 (계획서 ⑤): wake, face, show_dev_info 등 토글 시 {"type": "config", ...} 프레임 전송 확인
10. 새로고침 뒤 개별 토글 유지 및 ws.onopen config 전송 (계획서 ⑥ & 필수 2): reload 후 localStorage 유지 및 ws.onopen 재연결 시 wake/face 포함 config 전송 확인
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

# Ensure src is in python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aiohttp import web
from playwright.async_api import async_playwright, Dialog

from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import build_engines


async def run_server(port: int = 8795) -> tuple[web.AppRunner, web.TCPSite]:
    """Mock 엔진 기반 데모 웹 서버 실행 (8790이 아닌 독립 포트)."""
    engines = build_engines("mock", "mock", "mock", default_mode="demo")
    app = create_app(engines)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site


async def check_1_demo_default(page, base_url: str) -> None:
    """1. 데모 모드 기본 표시 및 문구 검사."""
    await page.goto(base_url)
    await page.wait_for_selector("#avatarCard")

    # 자리표시 카드 노출 확인
    assert await page.is_visible("#avatarCard"), "데모 모드에서 #avatarCard가 보여야 함"

    # 권장 1: #avatarHint "상담" -> "대화" 확인
    avatar_hint = await page.text_content("#avatarHint")
    assert "상담" not in avatar_hint, f"#avatarHint에 '상담' 문구가 남아있음: {avatar_hint}"
    assert "화면을 터치하여 대화를 시작하세요" in avatar_hint, f"#avatarHint 기본 문구 불일치: {avatar_hint}"

    # 예외 1: AI 마음 건강 도우미 고지 확인
    ai_notice = await page.text_content("#aiNotice")
    assert "AI 마음 건강 도우미" in ai_notice, f"AI 고지 문구 불일치: {ai_notice}"
    assert await page.is_visible("#aiNotice"), "#aiNotice가 보여야 함"

    # 예외 3: 연결 상태 표시 노출
    assert await page.is_visible("#connStatus"), "#connStatus가 보여야 함"

    # 개발 패널 hidden 확인
    assert not await page.is_visible("#logCard"), "데모 모드에서 #logCard는 숨겨져야 함"
    assert not await page.is_visible("#latCard"), "데모 모드에서 #latCard는 숨겨져야 함"
    assert not await page.is_visible("#micCard"), "데모 모드에서 #micCard는 숨겨져야 함"
    assert not await page.is_visible("#textCard"), "데모 모드에서 #textCard는 숨겨져야 함"

    # 초기 헤더 엔진 정보 숨김 확인
    assert not await page.is_visible("#engines"), "데모 모드에서 #engines는 숨겨져야 함"

    # DOM에는 여전히 존재함을 확인 (web_e2e.py 호환)
    assert await page.query_selector("#lat") is not None, "#lat 테이블이 DOM에 있어야 함"
    assert await page.query_selector("#log") is not None, "#log 요소가 DOM에 있어야 함"


async def check_2_demo_onhello_hidden(page) -> None:
    """2. 데모 모드 onHello 수신 뒤 헤더 숨김 유지 검사 (필수 1)."""
    # 세션 시작 -> WS 연결 및 onHello 수신
    await page.click("#start")
    await asyncio.sleep(0.3)

    # 실행 중 힌트 문구 확인 (권장 1)
    running_hint = await page.text_content("#avatarHint")
    assert "상담" not in running_hint, f"실행 중 #avatarHint에 '상담' 포함: {running_hint}"
    assert "대화 진행 중입니다" in running_hint, f"실행 중 #avatarHint 불일치: {running_hint}"

    # onHello 수신 후 엔진 텍스트가 채워졌으나 데모 모드이므로 헤더 요소들은 숨김 유지 (필수 1)
    await page.wait_for_selector("#engines", state="attached")
    engines_text = await page.text_content("#engines")
    assert "llm=mock" in engines_text, f"#engines 텍스트 미수신: {engines_text}"

    assert not await page.is_visible("#engines"), "onHello 후 데모 모드에서 #engines가 숨겨져야 함"
    assert not await page.is_visible("#voiceWrap"), "onHello 후 데모 모드에서 #voiceWrap이 숨겨져야 함"
    assert not await page.is_visible("#sttWrap"), "onHello 후 데모 모드에서 #sttWrap이 숨겨져야 함"
    assert not await page.is_visible("#epWrap"), "onHello 후 데모 모드에서 #epWrap이 숨겨져야 함"


async def check_3_dev_mode(page) -> None:
    """3. 개발 모드 표시 검사."""
    # 설정 패널 열기
    await page.click("#settingsBtn")
    await page.wait_for_selector("#settingsPanel:not([hidden])")

    # 개발 모드 선택
    await page.click("#modeDev")
    await asyncio.sleep(0.1)

    # 개발 패널 노출 확인
    assert await page.is_visible("#logCard"), "개발 모드에서 #logCard가 보여야 함"
    assert await page.is_visible("#latCard"), "개발 모드에서 #latCard가 보여야 함"
    assert await page.is_visible("#micCard"), "개발 모드에서 #micCard가 보여야 함"
    assert await page.is_visible("#textCard"), "개발 모드에서 #textCard가 보여야 함"

    # 헤더 개발 엔진/선택기 노출 확인 (Mock 엔진에서 stt, endpoint는 옵션 존재)
    assert await page.is_visible("#engines"), "개발 모드에서 #engines가 보여야 함"
    assert await page.is_visible("#sttWrap"), "개발 모드에서 #sttWrap이 보여야 함"
    assert await page.is_visible("#epWrap"), "개발 모드에서 #epWrap이 보여야 함"

    # 데모 전용 자리표시 카드는 숨김 확인
    assert not await page.is_visible("#avatarCard"), "개발 모드에서 #avatarCard는 숨겨져야 함"


async def check_4_config_on_toggle(page, ws_sent: list[str]) -> None:
    """4. 모드 전환 시 config 전송 검사."""
    def has_config_mode(mode_val: str) -> bool:
        for s in ws_sent:
            try:
                m = json.loads(s)
                if m.get("type") == "config" and m.get("mode") == mode_val:
                    return True
            except Exception:
                pass
        return False

    # ws_sent 초기화 후 dev -> demo 전환
    ws_sent.clear()
    await page.click("#modeDemo")
    await asyncio.sleep(0.1)

    assert has_config_mode("demo"), f"demo 모드 전환 config 메시지가 WS로 전송되지 않음: {ws_sent}"

    # demo -> dev 전환
    ws_sent.clear()
    await page.click("#modeDev")
    await asyncio.sleep(0.1)

    assert has_config_mode("dev"), f"dev 모드 전환 config 메시지가 WS로 전송되지 않음: {ws_sent}"


async def check_5_reload_persistence(page, base_url: str) -> None:
    """5. 새로고침 및 재연결 뒤 모드 유지 검사."""
    # 현재 dev 모드 상태에서 페이지 새로고침
    await page.reload()
    await page.wait_for_selector("#settingsBtn")

    # 새로고침 후에도 dev 모드 유지 확인 (localStorage 기반)
    mode_in_ls = await page.evaluate("() => localStorage.getItem('healthup_mode')")
    assert mode_in_ls == "dev", f"새로고침 후 localStorage 모드 불일치: {mode_in_ls}"

    # 개발 패널이 여전히 보이는지 확인
    assert await page.is_visible("#logCard"), "새로고침 후에도 dev 모드 패널이 유지되어야 함"
    assert not await page.is_visible("#avatarCard"), "새로고침 후에도 dev 모드에서는 #avatarCard가 숨겨져야 함"

    # 다시 demo 모드로 원복
    await page.click("#settingsBtn")
    await page.click("#modeDemo")
    await asyncio.sleep(0.1)


async def check_6_ai_notice_warning(page) -> None:
    """6. AI 마음 건강 도우미 고지 및 해제 경고 검사."""
    # 설정 패널 열기
    if not await page.is_visible("#settingsPanel"):
        await page.click("#settingsBtn")
        await page.wait_for_selector("#settingsPanel:not([hidden])")

    # (1) 취소 시나리오: 다이얼로그 dismiss -> 체크 유지 및 배지 유지
    dialog_handled = asyncio.Event()

    async def handle_dismiss(dialog: Dialog):
        assert "AI 기본법 제31조" in dialog.message, f"경고 메시지 불일치: {dialog.message}"
        await dialog.dismiss()
        dialog_handled.set()

    page.once("dialog", handle_dismiss)
    await page.click("#prefAiNotice")
    await asyncio.wait_for(dialog_handled.wait(), timeout=3.0)

    # 취소했으므로 체크박스는 여전히 checked, 배지도 노출 유지
    is_checked = await page.is_checked("#prefAiNotice")
    assert is_checked, "취소 후에도 #prefAiNotice는 checked 상태여야 함"
    assert await page.is_visible("#aiNotice"), "취소 후에도 #aiNotice가 보여야 함"

    # (2) 승인 시나리오: 다이얼로그 accept -> 체크 해제 및 배지 숨김
    dialog_handled2 = asyncio.Event()

    async def handle_accept(dialog: Dialog):
        assert "AI 기본법 제31조" in dialog.message
        await dialog.accept()
        dialog_handled2.set()

    page.once("dialog", handle_accept)
    await page.click("#prefAiNotice")
    await asyncio.wait_for(dialog_handled2.wait(), timeout=3.0)

    # 승인했으므로 체크박스 해제, 배지 숨김
    is_checked2 = await page.is_checked("#prefAiNotice")
    assert not is_checked2, "승인 후에는 #prefAiNotice가 해제되어야 함"
    assert not await page.is_visible("#aiNotice"), "승인 후에는 #aiNotice가 숨겨져야 함"

    # 복구 (다시 켜기 - 켤 때는 confirm 없이 바로 켜짐)
    await page.click("#prefAiNotice")
    assert await page.is_visible("#aiNotice"), "다시 켜면 #aiNotice가 보여야 함"


async def check_7_avatar_touch_start(page) -> None:
    """7. 데모 모드 자리표시 영역 터치 시 start() 및 stopAll 시 상태 갱신 검사 (권장 1·2)."""
    # 설정 패널 닫기
    if await page.is_visible("#settingsPanel"):
        await page.click("#settingsBtn")

    # 데모 모드 자리표시 카드 노출 확인
    assert await page.is_visible("#avatarCard")

    # 미실행 상태 보장
    if "대화 중지" in await page.text_content("#start"):
        await page.click("#start")
        await asyncio.sleep(0.2)

    # 미실행 상태 확인: #start 버튼이 "시작"
    start_text_before = await page.text_content("#start")
    assert "시작" in start_text_before

    # #avatarCard 클릭 -> start() 트리거
    await page.click("#avatarCard")
    await asyncio.sleep(0.3)

    # 실행 상태 확인
    start_text_after = await page.text_content("#start")
    assert "대화 중지" in start_text_after or await page.evaluate(
        "() => document.getElementById('avatarCard').classList.contains('running')"
    ), "자리표시 카드 클릭 시 start()가 트리거되어 실행 상태가 되어야 함"

    # 실행 중 힌트 확인 (권장 1)
    hint_running = await page.text_content("#avatarHint")
    assert "대화 진행 중입니다 (편하게 말씀하세요)" in hint_running, f"실행 중 힌트 불일치: {hint_running}"

    # 중지 버튼 클릭 -> stopAll() 실행 및 권장 2 검사
    await page.click("#start")
    await asyncio.sleep(0.3)

    # #avatarStatusText 가 "중지됨"으로 갱신되었는지 확인 (권장 2)
    status_text = await page.text_content("#avatarStatusText")
    assert status_text == "중지됨", f"stopAll() 후 #avatarStatusText 불일치: {status_text}"

    # 힌트 복구 확인
    hint_stopped = await page.text_content("#avatarHint")
    assert "화면을 터치하여 대화를 시작하세요" in hint_stopped, f"중지 후 힌트 불일치: {hint_stopped}"


async def check_8_pref_toggles_ui(page) -> None:
    """8. 설정 패널 6개 개별 토글 및 카메라 배지 연동 검사 (계획서 ③ & 필수 3)."""
    # 설정 패널 열기
    if not await page.is_visible("#settingsPanel"):
        await page.click("#settingsBtn")
        await page.wait_for_selector("#settingsPanel:not([hidden])")

    # 데모 모드 확인
    await page.click("#modeDemo")
    await asyncio.sleep(0.1)

    # 1) show_dev_info: 데모 모드에서 켜면 #latCard와 #logCard가 보이고, 끄면 숨김
    assert not await page.is_visible("#latCard"), "기본 데모에서 #latCard는 숨겨져야 함"
    assert not await page.is_visible("#logCard"), "기본 데모에서 #logCard는 숨겨져야 함"
    await page.click("#prefDevInfo")  # ON
    await asyncio.sleep(0.1)
    assert await page.is_visible("#latCard"), "show_dev_info 켜면 #latCard가 보여야 함"
    assert await page.is_visible("#logCard"), "show_dev_info 켜면 #logCard가 보여야 함"
    await page.click("#prefDevInfo")  # OFF
    await asyncio.sleep(0.1)
    assert not await page.is_visible("#latCard"), "show_dev_info 끄면 #latCard가 숨겨져야 함"
    assert not await page.is_visible("#logCard"), "show_dev_info 끄면 #logCard가 숨겨져야 함"

    # 2) show_conn_status: 끄면 #connStatus 숨김, 켜면 보임
    assert await page.is_visible("#connStatus"), "기본 상태에서 #connStatus 노출"
    await page.click("#prefConnStatus")  # OFF
    await asyncio.sleep(0.1)
    assert not await page.is_visible("#connStatus"), "show_conn_status 끄면 #connStatus 숨겨져야 함"
    await page.click("#prefConnStatus")  # ON
    await asyncio.sleep(0.1)
    assert await page.is_visible("#connStatus"), "show_conn_status 켜면 #connStatus 보여야 함"

    # 3) #cameraBadge 문구 및 face/wake 연동 (필수 3)
    # 기본값: wake=True, face=False -> "마이크 대기 중" 표시 및 노출
    assert await page.is_visible("#cameraBadge"), "기본 상태에서 #cameraBadge 노출"
    b_text1 = (await page.text_content("#cameraBadge")).strip()
    assert b_text1 == "마이크 대기 중", f"기본 배지 텍스트 불일치: {b_text1}"

    # face=True, wake=True -> "카메라 켜짐 · 마이크 대기 중" 노출
    await page.click("#prefFace")  # ON
    await asyncio.sleep(0.1)
    b_text2 = (await page.text_content("#cameraBadge")).strip()
    assert "카메라 켜짐" in b_text2 and "마이크 대기 중" in b_text2, f"face+wake 배지 불일치: {b_text2}"
    assert await page.is_visible("#cameraBadge")

    # face=True, wake=False -> "카메라 켜짐" 노출
    await page.click("#prefWake")  # OFF
    await asyncio.sleep(0.1)
    b_text3 = (await page.text_content("#cameraBadge")).strip()
    assert b_text3 == "카메라 켜짐", f"face only 배지 불일치: {b_text3}"
    assert await page.is_visible("#cameraBadge")

    # face=False, wake=False -> 둘 다 꺼짐 -> 배지 숨김 (parts.length === 0)
    await page.click("#prefFace")  # OFF
    await asyncio.sleep(0.1)
    assert not await page.is_visible("#cameraBadge"), "wake/face 둘 다 꺼지면 #cameraBadge 숨겨져야 함"

    # wake=True 복구 -> "마이크 대기 중" 다시 노출
    await page.click("#prefWake")  # ON
    await asyncio.sleep(0.1)
    assert await page.is_visible("#cameraBadge")

    # show_camera_badge=False -> 배지 숨김
    await page.click("#prefCameraBadge")  # OFF
    await asyncio.sleep(0.1)
    assert not await page.is_visible("#cameraBadge"), "show_camera_badge 꺼지면 #cameraBadge 숨겨져야 함"
    await page.click("#prefCameraBadge")  # ON 복구
    await asyncio.sleep(0.1)
    assert await page.is_visible("#cameraBadge")


async def check_9_pref_config_frames(page, ws_sent: list[str]) -> None:
    """9. 개별 토글 변경 시 config 웹소켓 프레임 전송 검사 (계획서 ⑤)."""
    # WS 연결 상태 보장
    if "시작" in await page.text_content("#start"):
        await page.click("#start")
        await asyncio.sleep(0.3)

    def has_config_kv(key: str, val: Any) -> bool:
        for s in ws_sent:
            try:
                m = json.loads(s)
                if m.get("type") == "config" and key in m and m[key] == val:
                    return True
            except Exception:
                pass
        return False

    # (1) wake 토글 시 config 전송
    ws_sent.clear()
    await page.click("#prefWake")  # False로 변경
    await asyncio.sleep(0.1)
    assert has_config_kv("wake", False), f"wake: False config 프레임 누락: {ws_sent}"

    ws_sent.clear()
    await page.click("#prefWake")  # True로 복구
    await asyncio.sleep(0.1)
    assert has_config_kv("wake", True), f"wake: True config 프레임 누락: {ws_sent}"

    # (2) face 토글 시 config 전송
    ws_sent.clear()
    await page.click("#prefFace")  # True로 변경
    await asyncio.sleep(0.1)
    assert has_config_kv("face", True), f"face: True config 프레임 누락: {ws_sent}"

    ws_sent.clear()
    await page.click("#prefFace")  # False로 복구
    await asyncio.sleep(0.1)
    assert has_config_kv("face", False), f"face: False config 프레임 누락: {ws_sent}"

    # (3) show_dev_info 토글 시 config 전송
    ws_sent.clear()
    await page.click("#prefDevInfo")  # True로 변경
    await asyncio.sleep(0.1)
    assert has_config_kv("show_dev_info", True), f"show_dev_info: True config 프레임 누락: {ws_sent}"

    ws_sent.clear()
    await page.click("#prefDevInfo")  # False로 복구
    await asyncio.sleep(0.1)
    assert has_config_kv("show_dev_info", False), f"show_dev_info: False config 프레임 누락: {ws_sent}"


async def check_10_pref_reload_persistence(page, base_url: str, ws_sent: list[str]) -> None:
    """10. 새로고침 뒤 개별 토글 유지 및 ws.onopen 재연결 config 전송 검사 (계획서 ⑥ & 필수 2)."""
    # 기본값과 다른 조합으로 설정: show_dev_info=True, face=True, show_conn_status=False
    if not await page.is_visible("#settingsPanel"):
        await page.click("#settingsBtn")
        await page.wait_for_selector("#settingsPanel:not([hidden])")

    if not await page.is_checked("#prefDevInfo"):
        await page.click("#prefDevInfo")
    if not await page.is_checked("#prefFace"):
        await page.click("#prefFace")
    if await page.is_checked("#prefConnStatus"):
        await page.click("#prefConnStatus")
    await asyncio.sleep(0.1)

    # 새로고침
    ws_sent.clear()
    await page.reload()
    await page.wait_for_selector("#settingsBtn")

    # localStorage 값 검증
    ls_prefs_str = await page.evaluate("() => localStorage.getItem('healthup_prefs')")
    assert ls_prefs_str is not None, "localStorage에 healthup_prefs가 있어야 함"
    ls_prefs = json.loads(ls_prefs_str)
    assert ls_prefs.get("show_dev_info") is True, f"show_dev_info 유지 실패: {ls_prefs}"
    assert ls_prefs.get("face") is True, f"face 유지 실패: {ls_prefs}"
    assert ls_prefs.get("show_conn_status") is False, f"show_conn_status 유지 실패: {ls_prefs}"

    # UI 노출 및 체크박스 상태 검증
    await page.click("#settingsBtn")
    await page.wait_for_selector("#settingsPanel:not([hidden])")
    assert await page.is_checked("#prefDevInfo"), "#prefDevInfo가 checked 유지되어야 함"
    assert await page.is_checked("#prefFace"), "#prefFace가 checked 유지되어야 함"
    assert not await page.is_checked("#prefConnStatus"), "#prefConnStatus가 unchecked 유지되어야 함"

    # UI 효과 검증: show_conn_status=False이므로 #connStatus는 숨겨짐
    assert not await page.is_visible("#connStatus"), "#connStatus 숨김 유지 실패"
    # show_dev_info=True이므로 데모 모드에서도 #latCard, #logCard 보임
    assert await page.is_visible("#latCard"), "#latCard 노출 유지 실패"
    assert await page.is_visible("#logCard"), "#logCard 노출 유지 실패"

    # 재연결 시 ws.onopen의 config 프레임에 wake·face·show_dev_info 포함 확인 (필수 2)
    ws_sent.clear()
    await page.click("#start")
    await asyncio.sleep(0.3)

    def find_initial_config() -> dict[str, Any] | None:
        for s in ws_sent:
            try:
                m = json.loads(s)
                if m.get("type") == "config" and "wake" in m and "face" in m:
                    return m
            except Exception:
                pass
        return None

    init_cfg = find_initial_config()
    assert init_cfg is not None, f"ws.onopen 재연결 config 메시지 누락: {ws_sent}"
    assert init_cfg.get("wake") is True, f"init_cfg.wake 불일치: {init_cfg}"
    assert init_cfg.get("face") is True, f"init_cfg.face 불일치: {init_cfg}"
    assert init_cfg.get("show_dev_info") is True, f"init_cfg.show_dev_info 불일치: {init_cfg}"

    # 세션 중지
    await page.click("#start")
    await asyncio.sleep(0.2)

    # 기본값 복구
    await page.click("#prefDevInfo")
    await page.click("#prefFace")
    await page.click("#prefConnStatus")
    await asyncio.sleep(0.1)


async def main() -> None:
    port = 8795
    base_url = f"http://127.0.0.1:{port}"
    print("=" * 70)
    print("  HealthUp 웹 애플리케이션 화면 2모드 및 개별 토글 검사 시작 (A2)")
    print(f"  테스트 서버 포트: {port}")
    print("=" * 70)

    t0 = time.perf_counter()
    runner, site = await run_server(port)

    checks = [
        ("1. 데모 기본 표시 및 대화 힌트", lambda p: check_1_demo_default(p, base_url)),
        ("2. 데모 onHello 뒤 헤더 숨김 (필수 1)", check_2_demo_onhello_hidden),
        ("3. 개발 모드 패널 및 헤더 표시", check_3_dev_mode),
        ("4. 모드 전환 시 config 전송", lambda p: check_4_config_on_toggle(p, ws_sent)),
        ("5. 새로고침 뒤 모드 유지", lambda p: check_5_reload_persistence(p, base_url)),
        ("6. AI 고지 해제 경고 모달", check_6_ai_notice_warning),
        ("7. 자리표시 터치 및 중지 갱신 (권장 1·2)", check_7_avatar_touch_start),
        ("8. 개별 토글 및 카메라 배지 (③, 필수 3)", check_8_pref_toggles_ui),
        ("9. 개별 토글 config 전송 (⑤)", lambda p: check_9_pref_config_frames(p, ws_sent)),
        ("10. 새로고침 유지 및 재연결 config (⑥, 필수 2)", lambda p: check_10_pref_reload_persistence(p, base_url, ws_sent)),
    ]

    passed = 0
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=os.environ.get("CHROME_PATH") or None,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = await browser.new_page()

            ws_sent: list[str] = []
            page.on(
                "websocket",
                lambda ws: ws.on(
                    "framesent",
                    lambda f: ws_sent.append(f) if isinstance(f, str) else None,
                ),
            )

            for title, check_fn in checks:
                print(f"  실행 중: {title} ... ", end="", flush=True)
                await check_fn(page)
                print("OK")
                passed += 1

            await browser.close()
    finally:
        await runner.cleanup()

    dt = time.perf_counter() - t0
    print("=" * 70)
    print(f"  화면 검사 완료: {passed}/{len(checks)} 통과 ({dt:.2f}초)")
    print(f"============================== {passed} passed in {dt:.2f}s ==============================")


if __name__ == "__main__":
    asyncio.run(main())
