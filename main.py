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
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ====== 사용자가 요구한 프롬프트(축약 안정형) ======
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

# ====== 레이트리밋 보호 설정 (추가 지출 방지 목적) ======
RATE_LIMIT_MIN_INTERVAL_SEC = 25  # RPM 3이면 최소 20초 이상 필요. 여유로 25초.
_openai_lock = asyncio.Lock()      # 동시에 1개만 처리
_last_openai_call_time = 0.0       # 마지막 OpenAI 호출 시간
_cooldown_until = 0.0              # TPM/RPM으로 장시간 락 걸렸을 때(초 단위 epoch)

# ====== Kakao helpers ======
def kakao_simple_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def kakao_use_callback() -> dict:
    # 콜백 모드 진입 (5초 SLA 회피)
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
        print("📮 callback status:", r.status_code)
        if r.status_code >= 400:
            print("📮 callback body:", r.text[:500])

# ====== Image helpers ======
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

# ====== RateLimitError wait time parsing ======
def _parse_wait_seconds_from_error(err_text: str) -> int | None:
    """
    OpenAI 429 메시지에 'Please try again in 20s' 또는 'in 6h42m37.44s' 같이 나올 수 있음.
    대략적인 대기 시간을 초로 변환해준다.
    """
    m = re.search(r"try again in ([0-9]+)s", err_text)
    if m:
        return int(m.group(1))

    # 6h42m37.44s 같은 형태
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)(?:\.\d+)?)s", err_text)
    if m:
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s

    return None

# ====== OpenAI call ======
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

async def run_and_callback(image_url: str, callback_url: str, callback_token: str | None) -> None:
    """
    - 콜백 URL은 1분/1회라서 최대 55초 내 처리 시도
    - OpenAI는 레이트리밋 보호(추가 지출 방지)를 위해:
      * 동시에 1건
      * 최소 25초 간격
      * 429면 재시도 안 하고 안내만
      * 장시간 락(시간 단위)이면 cooldown 걸고 그동안 호출 금지
    """
    global _last_openai_call_time, _cooldown_until

    try:
        # 1) 이미지 다운로드 (OpenAI 호출 전에)
        img = await download_image_bytes(image_url)
        print("🖼️ downloaded bytes:", len(img))

        # 이미지가 너무 크면(요청 크기↑/실패↑/비용↑) => OpenAI 호출 자체를 피해서 비용 방지
        if len(img) > 2_500_000:
            await post_callback(
                callback_url,
                callback_token,
                "사진 용량이 조금 커서 요약이 실패할 수 있어요.\n"
                "카톡에서 ‘일반 화질’로 다시 보내주시면 더 잘 돼요."
            )
            return

        # 2) OpenAI 레이트리밋 보호 구간
        async with _openai_lock:
            now = time.time()

            # 장시간 쿨다운 중이면 호출 자체를 하지 않음 (추가 지출/실패 방지)
            if now < _cooldown_until:
                remain = int(_cooldown_until - now)
                await post_callback(
                    callback_url,
                    callback_token,
                    f"지금은 요청이 많아 잠시 대기 중이에요.\n"
                    f"{remain//3600}시간 { (remain%3600)//60 }분 후 다시 시도해주세요."
                )
                return

            # RPM 보호(최소 간격)
            wait = RATE_LIMIT_MIN_INTERVAL_SEC - (now - _last_openai_call_time)
            if wait > 0:
                await post_callback(
                    callback_url,
                    callback_token,
                    f"요청이 몰려서 잠깐 대기 중이에요.\n"
                    f"{int(wait)}초 후에 다시 사진을 보내주세요."
                )
                return

            _last_openai_call_time = time.time()

            # 3) OpenAI 호출 (55초 안에 끝내야 callback 만료 전 전송 가능)
            try:
                summary = await asyncio.wait_for(
                    asyncio.to_thread(lambda: _openai_summarize_with_base64(img)),
                    timeout=55.0
                )
                await post_callback(callback_url, callback_token, summary)
                return

            except Exception as e:
                err = repr(e)
                print("❌ openai error:", err)

                # 429(레이트리밋) 처리: 재시도하지 않고 안내만 + 필요한 경우 cooldown 설정
                if "rate_limit" in err.lower() or "429" in err:
                    wait_sec = _parse_wait_seconds_from_error(err) or 30

                    # 아주 길게(예: 시간 단위) 막혔으면 cooldown 걸어서 추가 지출/실패 방지
                    if wait_sec >= 3600:
                        _cooldown_until = time.time() + wait_sec

                    # 사용자에게는 짧고 명확하게
                    if wait_sec >= 3600:
                        msg = "요청 한도에 걸렸어요.\n조금 뒤에 다시 시도해주세요."
                    else:
                        msg = f"요청이 많아 잠시 대기 중이에요.\n{wait_sec}초 후 다시 보내주세요."

                    await post_callback(callback_url, callback_token, msg)
                    return

                # 그 외 오류
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
        print("❌ final error:", repr(e))
        await post_callback(
            callback_url,
            callback_token,
            "요약 중 오류가 발생했어요. 사진을 다시 보내주시거나 잠시 후 다시 시도해주세요."
        )

@app.get("/")
async def health():
    return {"status": "alive", "version": "v10.1-rate-limit-guard"}

@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()
    print("🔥 KAKAO REQUEST RECEIVED (v10.1)")

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
            "callbackUrl이 요청에 포함되지 않았어요.\n오픈빌더에서 콜백 설정이 해당 블록에 적용됐는지 확인 후 운영 배포해주세요."
        ))

    # 콜백 모드 진입(즉시 반환)
    asyncio.create_task(run_and_callback(image_url, callback_url, callback_token))
    return JSONResponse(kakao_use_callback())
