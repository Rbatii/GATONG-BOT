import os
import re
import base64
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI

app = FastAPI()
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

PROMPT = """너는 맞벌이 부모를 위한 “가정통신문 행동정리 비서”야.
사진 속 내용을 읽고 부모가 바로 해야 할 행동만 정리하라.

규칙:
- 해야 할 일/기한/돈이 있으면 우선 표시
- 없으면 “학부모가 따로 할 일 없음” 명시
- 날짜·금액은 원문 그대로
- 추측 금지, 불확실하면 (확인 필요)
- 선택에 따라 값이 달라지면 단일값으로 요약하지 말고 “표 참고”로 처리

출력:
- 해야 할 일:
- 기한:
- 돈:
- 준비물/주의:
- 링크/QR:

체크:
- 신청 ⬜ / 확인 ⬜
(필요한 경우만 ☑️, 사용 가능한 이모지는 ⬜/☑️만)
"""

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

async def download_image_bytes(url: str) -> bytes:
    # 1분 제한 때문에 다운로드는 빠르게 (최대 10초)
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content

def guess_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/jpeg"

async def post_callback(callback_url: str, callback_token: str | None, text: str) -> None:
    payload = kakao_simple_text(text)
    headers = {}
    if callback_token:
        headers["x-kakao-callback-token"] = callback_token

    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(callback_url, json=payload, headers=headers)
        print("📮 callback status:", r.status_code)
        if r.status_code >= 400:
            print("📮 callback body:", r.text[:500])

async def summarize(image_url: str) -> str:
    image_bytes = await download_image_bytes(image_url)
    mime = guess_mime(image_bytes)
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("utf-8")

    # OpenAI도 1분 제한 때문에 최대 40초로 제한
    resp = await asyncio.to_thread(
        lambda: client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )
    )
    out = (resp.choices[0].message.content or "").strip()
    return out if out else "요약 결과가 비어있어요. 사진을 더 선명하게 다시 보내주세요."

async def run_with_deadline(image_url: str, callback_url: str, callback_token: str | None) -> None:
    """
    콜백 URL(1분/1회) 만료 전에 무조건 한 번은 보내기.
    - 55초 안에 요약 끝나면 요약 전송
    - 55초 넘기면 '지연 안내' 전송 (요약은 취소)
    """
    try:
        # 전체 작업을 55초로 제한
        summary = await asyncio.wait_for(summarize(image_url), timeout=55.0)
        await post_callback(callback_url, callback_token, summary)
    except asyncio.TimeoutError:
        await post_callback(
            callback_url,
            callback_token,
            "요약에 시간이 조금 더 걸리고 있어요.\n"
            "사진을 한 번만 더 보내주시면 바로 이어서 처리할게요 🙂"
        )
    except Exception as e:
        print("❌ async summary error:", repr(e))
        await post_callback(
            callback_url,
            callback_token,
            "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
        )

@app.get("/")
async def health():
    return {"status": "alive", "version": "v8-deadline"}

@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v8)")

    user_request = body.get("userRequest", {})
    callback_url = user_request.get("callbackUrl")
    callback_token = req.headers.get("x-kakao-callback-token")
    print("callbackUrl=", callback_url)
    print("callbackTokenPresent=", bool(callback_token))

    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        return JSONResponse(kakao_simple_text("사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요."))

    if not callback_url:
        return JSONResponse(kakao_simple_text(
            "callbackUrl이 요청에 포함되지 않았어요.\n"
            "오픈빌더에서 콜백 설정이 '가정통신문 요약' 블록에 적용됐는지 확인 후 운영 배포해주세요."
        ))

    # 콜백 모드 진입 (5초 제한 회피)
    asyncio.create_task(run_with_deadline(image_url, callback_url, callback_token))
    return JSONResponse(kakao_use_callback())
