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
  2) 기한 (날짜가 있으면 굵게 강조)
  3) 돈 관련 내용 (금액, 출금 방식)
  4) 준비물 / 주의사항
  5) 링크나 QR 코드가 있으면 url 형식으로 변환하여 표시
  - QR/링크가 선명하지 않으면 '링크 확인 필요'로 표시
- 부모에게 말하듯 자연스럽고 친절한 말투
- 이모지는 최대 1개만 사용

[출력 형식]
📌 가정통신문 핵심
1️⃣ 해야 할 것:
2️⃣ 기한:
3️⃣ 돈 관련:
4️⃣ 준비물/주의사항:

👉 체크 포인트:
- 신청 ⬜ / 확인 ⬜
"""


def kakao_simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def extract_first_url(value) -> str | None:
    """secureimage 값이 dict/list/문자열(List(...))로 와도 URL 1개만 뽑기"""
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


async def post_callback(callback_url: str, text: str) -> None:
    """카카오 callbackUrl로 최종 응답을 보내는 함수(1회용)"""
    payload = kakao_simple_text(text)
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(callback_url, json=payload)
        print("📮 callback status:", r.status_code)


async def run_summary_and_callback(image_url: str, callback_url: str) -> None:
    """느린 작업(다운로드+OpenAI) 후 callbackUrl로 결과 전송"""
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

        await post_callback(callback_url, summary)

    except Exception as e:
        err = repr(e)
        print("❌ async summary error:", err)
        await post_callback(
            callback_url,
            "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
        )


@app.get("/")
async def health():
    return {"status": "alive", "version": "v6-callback-debug"}


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v6-callback-debug)")

    # ✅ callbackUrl이 진짜 내려오는지 확인용 로그 (핵심)
    print("callbackUrl=", body.get("callbackUrl"))
    print("keys=", list(body.keys()))

    if not os.environ.get("OPENAI_API_KEY"):
        return JSONResponse(
            kakao_simple_text("OPENAI_API_KEY가 설정되지 않았어요. Render 환경변수에 추가해주세요.")
        )

    # callbackUrl: 카카오가 요청마다 1회용으로 발급해줌 (최대 1분)
    callback_url = body.get("callbackUrl") or body.get("callback_url")

    # 이미지 URL 추출
    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        return JSONResponse(kakao_simple_text("사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂"))

    # ✅ 콜백이 없다면: 일단 즉시 응답만(디버그용)
    # (콜백이 진짜 켜지면 여기로 오지 않아야 정상)
    if not callback_url:
        return JSONResponse(kakao_simple_text(
            "callbackUrl이 아직 내려오지 않았어요.\n"
            "오픈빌더에서 '콜백 설정'을 '가정통신문 요약' 블록에 켠 뒤 저장+운영배포까지 해주세요."
        ))

    # ✅ 1차 즉시 응답(5초 내) — 타임아웃 방지
    immediate = "사진 확인했어요 🙂\n요약 중입니다... (10~20초 정도 걸릴 수 있어요)"

    # ✅ 백그라운드처럼 요약 후 콜백 전송
    asyncio.create_task(run_summary_and_callback(image_url, callback_url))

    return JSONResponse(kakao_simple_text(immediate))
