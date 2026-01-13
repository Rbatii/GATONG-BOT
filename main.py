import os
import re
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

def openai_summarize_with_image_url(image_url: str) -> str:
    # ✅ 이미지 URL을 OpenAI에 그대로 전달 (다운로드/base64 없음)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    )
    out = (resp.choices[0].message.content or "").strip()
    return out if out else "요약 결과가 비어있어요. 사진을 더 선명하게 다시 보내주세요."

async def run_with_deadline(image_url: str, callback_url: str, callback_token: str | None) -> None:
    """
    callbackUrl은 1분/1회.
    - 55초 안에 요약되면 결과 전송
    - 55초 넘으면 안내 메시지 전송(무응답 방지)
    """
    try:
        summary = await asyncio.wait_for(
            asyncio.to_thread(lambda: openai_summarize_with_image_url(image_url)),
            timeout=55.0
        )
        await post_callback(callback_url, callback_token, summary)

    except asyncio.TimeoutError:
        await post_callback(
            callback_url,
            callback_token,
            "요약에 시간이 조금 더 걸리고 있어요.\n사진을 한 번만 더 보내주시면 바로 이어서 처리할게요."
        )

    except Exception as e:
        err = repr(e)
        print("❌ openai error:", err)
        # ✅ 사용자에게 원인 힌트를 아주 짧게(민감정보 없이)
        await post_callback(
            callback_url,
            callback_token,
            "요약 오류가 발생했어요.\n사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
        )

@app.get("/")
async def health():
    return {"status": "alive", "version": "v9-image-url"}

@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v9)")

    user_request = body.get("userRequest", {})
    callback_url = user_request.get("callbackUrl")
    callback_token = req.headers.get("x-kakao-callback-token")

    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    print("callbackUrl=", callback_url)
    print("callbackTokenPresent=", bool(callback_token))

    if not image_url:
        return JSONResponse(kakao_simple_text("사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요."))

    if not callback_url:
        return JSONResponse(kakao_simple_text(
            "callbackUrl이 요청에 포함되지 않았어요.\n오픈빌더에서 콜백 설정이 해당 블록에 적용됐는지 확인 후 운영 배포해주세요."
        ))

    # 콜백 모드: 즉시 useCallback 반환
    asyncio.create_task(run_with_deadline(image_url, callback_url, callback_token))
    return JSONResponse(kakao_use_callback())
