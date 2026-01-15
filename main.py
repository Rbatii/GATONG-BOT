import os
import re
import time
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI

app = FastAPI()
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

PROMPT = """너는 맞벌이 부모를 위한 가정통신문 요약 비서다.
사진 속 가정통신문을 읽고, 부모가 지금 해야 할 행동을 판단해라.

[판단 규칙]
- 신청/회신/제출 등 부모의 행동이 필요하면 → “신청 필요”
- 안내 확인만 하면 되면 → “확인만”
- 둘 중 하나는 반드시 선택할 것 (회피 금지)

[요약 규칙]
- 핵심만 3~5줄
- 인사말·배경 설명 제거
- 추측 금지, 문서에 있는 내용만 사용

[반드시 포함]
1) 해야 할 것
2) 기한(있으면 **굵게**)
3) 돈(금액/방식)
4) 준비물·주의
5) 링크/QR(있으면)

[체크 포인트 출력 규칙]
- 신청 필요 → 신청 ☑️ / 확인 ⬜
- 확인만 → 신청 ⬜ / 확인 ☑️
- 둘 다 필요 → 신청 ☑️ / 확인 ☑️
(☑️, ⬜ 외 이모지 사용 금지)

[주의]
- 선택에 따라 금액·날짜가 달라지면 단일값으로 요약하지 말고
  “가정통신문 표 참고”라고 쓸 것.

[출력 형식]
📌 가정통신문 핵심
- 해야 할 일:
- 기한:
- 돈:
- 준비물/주의:
- 링크/QR:

👉 체크 포인트:
- 신청 ⬜ / 확인 ⬜
"""

RATE_LIMIT_MIN_INTERVAL_SEC = 60
_openai_lock = asyncio.Lock()
_last_openai_call_time = 0.0
_cooldown_until = 0.0

FREE_STAGE_LIMIT_MESSAGE = (
    "현재 무료 제공 단계라 요청 수가 제한되어 있어요.\n\n"
    "⏱️ 1분에 1건씩만 처리할 수 있으니\n"
    "조금만 기다렸다가 다시 사진을 보내주세요.\n"
    "불편을 드려 죄송해요 🙏"
)

TODAY_CLOSED_MESSAGE = (
    "현재 무료 제공 단계에서 오늘 사용 가능한 AI 처리량을 모두 사용했어요.\n\n"
    "📅 내일 다시 시도해주시면 정상적으로 이용하실 수 있어요.\n"
    "불편을 드려 죄송해요 🙏"
)


def kakao_simple_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def kakao_use_callback() -> dict:
    return {"version": "2.0", "useCallback": True}


def extract_first_url(value) -> str | None:
    if value is None:
        return None

    if isinstance(value, dict):
        if "secureUrls" in value:
            return extract_first_url(value.get("secureUrls"))
        for v in value.values():
            url = extract_first_url(v)
            if url:
                return url
        return None

    if isinstance(value, (list, tuple)):
        return extract_first_url(value[0]) if value else None

    s = value if isinstance(value, str) else str(value)
    m = re.search(r"https?://[^\s)]+", s)
    return m.group(0) if m else None


async def post_callback(callback_url: str, callback_token: str | None, text: str) -> None:
    payload = kakao_simple_text(text)
    headers = {}
    if callback_token:
        headers["x-kakao-callback-token"] = callback_token

    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(callback_url, json=payload, headers=headers)
        print("📮 callback status:", r.status_code, flush=True)
        if r.status_code >= 400:
            print("📮 callback body:", r.text[:500], flush=True)


async def download_image_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


def guess_mime(b: bytes) -> str:
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if b.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/jpeg"


def _openai_summarize_with_base64(image_bytes: bytes) -> str:
    mime = guess_mime(image_bytes)
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("utf-8")

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    out = (resp.choices[0].message.content or "").strip()
    return out if out else "요약 결과가 비어있어요. 사진을 더 선명하게 다시 보내주세요."


def _parse_wait_seconds_from_error(err_text: str) -> int | None:
    m = re.search(r"try again in ([0-9]+)s", err_text)
    if m:
        return int(m.group(1))

    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)(?:\.\d+)?)s", err_text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s

    return None


async def run_and_callback(image_url: str, callback_url: str, callback_token: str | None) -> None:
    global _last_openai_call_time, _cooldown_until

    # ✅ 백그라운드가 진짜 돌기 시작했는지 확인용
    print("🚀 run_and_callback START", flush=True)

    try:
        img = await download_image_bytes(image_url)
        print("🖼️ downloaded bytes:", len(img), flush=True)

        if len(img) > 2_500_000:
            await post_callback(
                callback_url,
                callback_token,
                "사진 용량이 조금 커서 요약이 실패할 수 있어요.\n"
                "카톡에서 ‘일반 화질’로 다시 보내주시면 더 잘 돼요."
            )
            return

        async with _openai_lock:
            now = time.time()

            if now < _cooldown_until:
                remaining = int(_cooldown_until - now)
                h = remaining // 3600
                m = (remaining % 3600) // 60
                s = remaining % 60
                print(f"⛔ cooldown active. Remaining ≈ {h}h {m}m {s}s (skip openai)", flush=True)
                await post_callback(callback_url, callback_token, TODAY_CLOSED_MESSAGE)
                return

            wait = RATE_LIMIT_MIN_INTERVAL_SEC - (now - _last_openai_call_time)
            if wait > 0:
                print(f"⏸️ local pacing active: wait≈{int(wait)}s", flush=True)
                await post_callback(callback_url, callback_token, FREE_STAGE_LIMIT_MESSAGE)
                return

            _last_openai_call_time = time.time()

            try:
                summary = await asyncio.wait_for(
                    asyncio.to_thread(lambda: _openai_summarize_with_base64(img)),
                    timeout=55.0
                )
                await post_callback(callback_url, callback_token, summary)
                return

            except Exception as e:
                err = repr(e)
                print("❌ openai error:", err, flush=True)

                if "rate_limit" in err.lower() or "429" in err:
                    wait_sec = _parse_wait_seconds_from_error(err) or 60
                    h = wait_sec // 3600
                    m = (wait_sec % 3600) // 60
                    s = wait_sec % 60
                    print(f"⏳ OpenAI rate limit. Remaining wait ≈ {h}h {m}m {s}s", flush=True)

                    if wait_sec >= 3600:
                        _cooldown_until = time.time() + wait_sec
                        await post_callback(callback_url, callback_token, TODAY_CLOSED_MESSAGE)
                        return

                    await post_callback(callback_url, callback_token, FREE_STAGE_LIMIT_MESSAGE)
                    return

                await post_callback(
                    callback_url,
                    callback_token,
                    "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
                )
                return

    except asyncio.TimeoutError:
        await post_callback(
            callback_url,
            callback_token,
            "요약에 시간이 조금 더 걸리고 있어요.\n사진을 한 번만 더 보내주시면 바로 이어서 처리할게요."
        )
    except Exception as e:
        print("❌ final error:", repr(e), flush=True)
        await post_callback(
            callback_url,
            callback_token,
            "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
        )


@app.get("/")
async def health():
    return {"status": "ok"}


@app.head("/")
async def head_health():
    return Response(status_code=200)


@app.post("/kakao-skill")
async def kakao_skill(req: Request, background_tasks: BackgroundTasks):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (stable)", flush=True)

    user_request = body.get("userRequest", {})
    callback_url = user_request.get("callbackUrl")
    callback_token = req.headers.get("x-kakao-callback-token")

    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        return JSONResponse(kakao_simple_text("사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요."))

    if not callback_url:
        return JSONResponse(kakao_simple_text(
            "callbackUrl이 요청에 포함되지 않았어요.\n"
            "오픈빌더에서 콜백 설정이 해당 블록에 적용됐는지 확인 후 운영 배포해주세요."
        ))

    # ✅ create_task 대신 BackgroundTasks
    background_tasks.add_task(run_and_callback, image_url, callback_url, callback_token)

    return JSONResponse(kakao_use_callback())
