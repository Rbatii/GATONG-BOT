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

PROMPT = """너는 맞벌이 부모를 위한 가정통신문 행동 요약 비서다.
사진 속 내용을 읽고 부모가 바로 해야 할 행동만 정리하라.

규칙:
- 해야 할 일 / 기한 / 돈이 있으면 우선 표시
- 없으면 “학부모가 따로 할 일 없음” 명시
- 날짜·금액은 원문 그대로
- 추측 금지, 불확실하면 (확인 필요)
- 선택에 따라 값이 달라지면 요약하지 말고 “표 참고”로 처리

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


def kakao_simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def kakao_use_callback() -> dict:
    # 콜백 모드로 동작하려면 useCallback=true 를 반환해야 함 (template 사용 X)
    # (카카오 가이드 명시) :contentReference[oaicite:3]{index=3}
    return {
        "version": "2.0",
        "useCallback": True
    }


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
        if not value:
            return None
        return extract_first_url(value[0])

    s = value if isinstance(value, str) else str(value)
    m = re.search(r"https?://[^\s)]+", s)
    return m.group(0) if m else None


async def download_image_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(25.0)
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
    """
    callbackUrl로 최종 응답 전송.
    콜백 토큰 헤더(x-kakao-callback-token)가 오는 환경에서는 같이 넣어주는 게 안전함.
    (테스트 환경에서 토큰 이슈가 있다는 안내도 있음) :contentReference[oaicite:4]{index=4}
    """
    payload = kakao_simple_text(text)
    headers = {}
    if callback_token:
        headers["x-kakao-callback-token"] = callback_token

    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(callback_url, json=payload, headers=headers)
        print("📮 callback status:", r.status_code)
        if r.status_code >= 400:
            print("📮 callback body:", r.text[:500])


async def run_summary_and_callback(image_url: str, callback_url: str, callback_token: str | None) -> None:
    try:
        image_bytes = await download_image_bytes(image_url)
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

        summary = (resp.choices[0].message.content or "").strip()
        if not summary:
            summary = "요약 결과가 비어있어요. 사진을 조금 더 선명하게 찍어 다시 보내주세요 🙂"

        await post_callback(callback_url, callback_token, summary)

    except Exception as e:
        err = repr(e)
    print("❌ openai error:", err)
    return JSONResponse(
        kakao_simple_text("요약 오류: " + err[:150])
        )


@app.get("/")
async def health():
    return {"status": "alive", "version": "v7-callback-fixed"}


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v7)")

    if not os.environ.get("OPENAI_API_KEY"):
        return JSONResponse(kakao_simple_text("OPENAI_API_KEY가 설정되지 않았어요. Render 환경변수에 추가해주세요."))

    # ✅ callbackUrl은 userRequest 안에 들어감 :contentReference[oaicite:5]{index=5}
    user_request = body.get("userRequest", {})
    callback_url = user_request.get("callbackUrl")

    # 콜백 토큰 (있을 수도/없을 수도)
    callback_token = req.headers.get("x-kakao-callback-token")
    print("callbackUrl=", callback_url)
    print("callbackTokenPresent=", bool(callback_token))

    # 이미지 URL 추출
    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        return JSONResponse(kakao_simple_text("사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂"))

    # callbackUrl이 없으면 아직 콜백이 적용되지 않은 요청(또는 테스트 한계)일 수 있음
    if not callback_url:
        return JSONResponse(kakao_simple_text(
            "callbackUrl이 요청에 포함되지 않았어요.\n"
            "1) 운영 채널에서 테스트 중인지 확인\n"
            "2) '가정통신문 요약' 블록에 콜백 설정 ON + 운영 배포 확인"
        ))

    # ✅ 5초 내에 useCallback=true로 응답해야 콜백 모드로 동작 :contentReference[oaicite:6]{index=6}
    asyncio.create_task(run_summary_and_callback(image_url, callback_url, callback_token))
    return JSONResponse(kakao_use_callback())
