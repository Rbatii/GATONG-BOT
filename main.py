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
아래 사진 속 가정통신문을 읽고,
부모가 바로 행동할 수 있게 ‘해야 할 일’ 중심으로 정리해줘.

[최우선 규칙]
- ‘해야 할 일 / 기한 / 돈’이 있으면 맨 위에 배치
- 위 항목이 전혀 없으면,
  → “이번 공지에서 학부모가 따로 해야 할 일은 없습니다.” 를 맨 위에 명시
- 날짜, 기한, 시간, 금액, 장소는 원문 표현을 그대로 정확히 사용
- 불확실하거나 추측이 필요한 내용은 절대 추론하지 말고 “(확인 필요)”로 표기
- 학부모 선택에 따라 결과가 달라지는 내용은 요약에 반영하지 말 것
  (예: 선택 과목에 따른 금액 상이, 날짜별 비용 상이 등)

[선택값에 따라 달라지는 정보 처리 규칙]
- 학부모 선택에 따라 값이 달라지는 경우:
  → 단일 값으로 요약하지 말 것
  → 대신 가정통신문에 포함된 ‘참고 가능한 표(금액표, 일정표 등)’가 있다면
    해당 표 내용을 그대로 정리하여 함께 제공할 것

[추출 항목]
1) 해야 할 일:
   - 신청 / 회신 / 동의 / 제출 / 준비 / 참여 여부 등
2) 기한:
   - 날짜 + 요일 + 시간 (있는 경우)
3) 돈:
   - 금액 + 납부/출금 방식 + 날짜
   - 선택에 따라 달라지면 “아래 표 참고”로 처리
4) 준비물 / 복장 / 주의사항
5) 링크 / QR:
   - 보이면 URL 형식으로
   - 선명하지 않으면 “링크 확인 필요”

[출력 형식]
📌 부모 액션 요약
- 해야 할 일:
- 기한:
- 돈:
- 준비물/주의:
- 링크/QR:

👉 체크 포인트
- 신청 ⬜ / 확인 ⬜

[체크 표시 규칙]
- 실제로 학부모가 ‘신청’ 또는 ‘확인’을 해야 하는 경우에만 ⬜를 ☑️로 변경
- 둘 다 해당되면 둘 다 ☑️
- 해당되지 않으면 ⬜ 유지

[추가 지침]
- 설명체 문장, 교육적 배경, 학교 인사말은 모두 제거
- 최대한 짧고 명확하게 작성
- 부모에게 말하듯 자연스럽고 친절한 톤 유지
- 사용 가능한 이모지는 ⬜, ☑️ 두 가지만 허용

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
