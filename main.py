import os
import re
import time
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI

app = FastAPI()

@app.get("/")
async def health():
    return {"status": "ok"}

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# =========================
# PROMPT
# =========================
PROMPT = """너는 맞벌이 부모를 위한 가정통신문 요약 비서야.
사진 속 내용을 읽고 부모가 바로 해야 할 행동만 정리해줘.

규칙:
- 해야 할 일 / 기한 / 돈이 있으면 우선
- 없으면 '학부모가 따로 할 일 없음'
- 추측 금지
- 선택에 따라 값이 달라지면 '표 참고'

출력:
- 해야 할 일:
- 기한:
- 돈:
- 준비물/주의:
- 링크/QR:

체크:
- 신청 ⬜ / 확인 ⬜
(필요한 경우만 ☑️)
"""

# =========================
# FREE STAGE POLICY
# =========================
RATE_LIMIT_INTERVAL = 60  # 1분 1건
_openai_lock = asyncio.Lock()
_last_call = 0.0
_block_until = 0.0        # 장시간 차단 시각(epoch)
_block_day = None         # 날짜 단위 차단용

FREE_LIMIT_MSG = (
    "현재 무료 제공 단계라 요청 수가 제한되어 있어요.\n\n"
    "⏱️ 1분에 1건씩만 처리할 수 있으니\n"
    "조금만 기다렸다가 다시 사진을 보내주세요.\n"
    "불편을 드려 죄송해요 🙏"
)

TODAY_CLOSED_MSG = (
    "현재 무료 제공 단계에서 오늘 사용 가능한 AI 처리량을 모두 사용했어요.\n\n"
    "내일 다시 시도해주시면 정상적으로 이용하실 수 있어요.\n"
    "불편을 드려 죄송해요 🙏"
)

# =========================
# Kakao helpers
# =========================
def kakao_simple_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def kakao_use_callback() -> dict:
    return {"version": "2.0", "useCallback": True}

def extract_first_url(value):
    if isinstance(value, dict):
        for v in value.values():
            u = extract_first_url(v)
            if u:
                return u
    if isinstance(value, list) and value:
        return extract_first_url(value[0])
    if isinstance(value, str):
        m = re.search(r"https?://[^\s)]+", value)
        return m.group(0) if m else None
    return None

async def post_callback(url, token, text):
    headers = {}
    if token:
        headers["x-kakao-callback-token"] = token
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(url, json=kakao_simple_text(text), headers=headers)

# =========================
# Image / OpenAI
# =========================
async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content

def guess_mime(b: bytes) -> str:
    if b.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if b.startswith(b"\x89PNG"):
        return "image/png"
    return "image/jpeg"

def call_openai(image_bytes: bytes) -> str:
    mime = guess_mime(image_bytes)
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode()

    res = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    )
    return res.choices[0].message.content.strip()

def parse_wait_seconds(err: str):
    m = re.search(r"in (\d+)h", err)
    if m:
        return int(m.group(1)) * 3600
    m = re.search(r"in (\d+)s", err)
    if m:
        return int(m.group(1))
    return None

# =========================
# Core logic
# =========================
async def run_and_callback(image_url, callback_url, callback_token):
    global _last_call, _block_until, _block_day

    today = time.strftime("%Y-%m-%d")

    # 날짜 바뀌면 차단 해제
    if _block_day and _block_day != today:
        _block_day = None
        _block_until = 0

    # 오늘 차단 상태
    if _block_day == today:
        await post_callback(callback_url, callback_token, TODAY_CLOSED_MSG)
        return

    async with _openai_lock:
        now = time.time()

        if now < _block_until:
            await post_callback(callback_url, callback_token, TODAY_CLOSED_MSG)
            return

        if now - _last_call < RATE_LIMIT_INTERVAL:
            await post_callback(callback_url, callback_token, FREE_LIMIT_MSG)
            return

        _last_call = now

        try:
            img = await download_image(image_url)
            if len(img) > 2_500_000:
                await post_callback(
                    callback_url,
                    callback_token,
                    "사진 용량이 커서 처리하기 어려워요.\n일반 화질로 다시 보내주세요."
                )
                return

            result = await asyncio.wait_for(
                asyncio.to_thread(call_openai, img),
                timeout=55
            )
            await post_callback(callback_url, callback_token, result)

        except Exception as e:
            err = repr(e)
            print("❌ openai error:", err)

            if "rate_limit" in err.lower() or "429" in err:
                wait = parse_wait_seconds(err) or 3600
                if wait >= 3600:
                    _block_until = time.time() + wait
                    _block_day = today
                    await post_callback(callback_url, callback_token, TODAY_CLOSED_MSG)
                else:
                    await post_callback(callback_url, callback_token, FREE_LIMIT_MSG)
                return

            await post_callback(
                callback_url,
                callback_token,
                "요약 중 오류가 발생했어요.\n잠시 후 다시 시도해주세요."
            )

# =========================
# Routes
# =========================
@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()

    user_req = body.get("userRequest", {})
    callback_url = user_req.get("callbackUrl")
    callback_token = req.headers.get("x-kakao-callback-token")

    detail = body.get("action", {}).get("detailParams", {})
    image_val = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(image_val)

    if not image_url:
        return JSONResponse(kakao_simple_text("가정통신문 사진을 1장 보내주세요."))

    asyncio.create_task(run_and_callback(image_url, callback_url, callback_token))
    return JSONResponse(kakao_use_callback())
