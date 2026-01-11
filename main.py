import os
import re
import base64
import time
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


@app.get("/")
async def health():
    # 서버 살아있는지 브라우저에서 확인용
    return {"status": "alive", "version": "v5-timing"}


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    t_total = time.time()
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v5-timing)")

    # API 키 확인
    if not os.environ.get("OPENAI_API_KEY"):
        return JSONResponse(
            kakao_simple_text("v5) OPENAI_API_KEY가 설정되지 않았어요. Render 환경변수에 추가해주세요.")
        )

    # 이미지 URL 추출
    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        return JSONResponse(
            kakao_simple_text("v5) 사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂")
        )

    # 1) 이미지 다운로드 + 시간 측정
    try:
        t_dl = time.time()
        image_bytes = await download_image_bytes(image_url)
        print(f"⏱️ download sec = {time.time() - t_dl:.3f} (bytes={len(image_bytes)})")
    except Exception as e:
        print("❌ image download error:", repr(e))
        return JSONResponse(
            kakao_simple_text("v5) 사진을 불러오지 못했어요. 사진을 다시 보내주시거나, 조금 후에 다시 시도해주세요.")
        )

    mime = guess_mime(image_bytes)
    data_url = f"data:{mime};base64," + base64.b64encode(image_bytes).decode("utf-8")

    # 2) OpenAI 호출 + 시간 측정
    try:
        t_ai = time.time()
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
        print(f"⏱️ openai sec = {time.time() - t_ai:.3f}")

        summary = (resp.choices[0].message.content or "").strip()
        if not summary:
            summary = "v5) 요약 결과가 비어있어요. 사진을 조금 더 선명하게 찍어 다시 보내주세요 🙂"

    except Exception as e:
        err = repr(e)
        print("❌ openai error:", err)
        return JSONResponse(
            kakao_simple_text("v5) 요약 중 오류가 발생했어요. 잠시 후 다시 시도해주세요.")
        )

    print(f"⏱️ total sec = {time.time() - t_total:.3f}")
    return JSONResponse(kakao_simple_text(summary))
