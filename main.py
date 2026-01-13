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

PROMPT = """너는 맞벌이·워킹맘을 위한 가정통신문 요약 비서야.
아래 사진 속 가정통신문을 읽고,
부모가 바로 행동할 수 있게 핵심만 정리해줘.

[요약 규칙]
- 가장 중요한 내용만 3~5줄
- 인사말, 교육적 설명, 배경 설명은 전부 제거
- 아래 항목이 있으면 반드시 포함:
  1) 해야 할 행동 (신청, 회신 등)
  2) 기한 
  3) 돈 관련 내용 (금액, 출금 방식)
  4) 준비물 / 주의사항
  5) 링크나 QR 코드가 있으면 url 형식으로 변환하여 표시
  - QR/링크가 선명하지 않으면 '링크 확인 필요'로 표시
  6) 체크 포인트에 신청 확인 따라 ☑️이모지 사용
  7) 학부모 선택에 따라 값이 변하는 결과는 반영 x(선택 과목에 따른 가격 상이, 날짜마다 가격 다름 등)
  - 이경우 가정통신문의 참고가능한 표를 다시 반환하여 보낼 것
- 부모에게 말하듯 자연스럽고 친절한 말투
- 이모지는 최대 1개만 사용

[출력 형식]
📌 가정통신문 핵심
➀ 해야 할 것:
➁ 기한:
➂ 돈 관련:
➃ 준비물/주의사항:

👉 체크 포인트:
- 신청 ⬜ / 확인 ⬜
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
        print("❌ async summary error:", err)
        await post_callback(
            callback_url,
            callback_token,
            "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
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
