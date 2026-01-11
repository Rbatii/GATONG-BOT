from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import re

app = FastAPI()


def kakao_simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def extract_first_url(value) -> str | None:
    """
    secureimage 값이 아래처럼 다양한 형태로 올 수 있어서
    무조건 '첫 번째 URL 문자열'만 뽑아낸다.

    - dict: {"secureUrls": ["http://..."], ...}
    - list: ["http://..."]
    - str : "List(http://...)" 또는 "http://..."
    """
    if value is None:
        return None

    # dict 형태
    if isinstance(value, dict):
        if "secureUrls" in value:
            return extract_first_url(value.get("secureUrls"))
        # 혹시 다른 키에 들어왔을 때도 대비
        for v in value.values():
            url = extract_first_url(v)
            if url:
                return url
        return None

    # list/tuple 형태
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        return extract_first_url(value[0])

    # 문자열 형태 (List(...) 포함)
    s = value if isinstance(value, str) else str(value)
    m = re.search(r"https?://[^\s)]+", s)
    return m.group(0) if m else None


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()

    # Render 로그에서 호출 여부 확인용
    print("🔥 KAKAO REQUEST RECEIVED (v3)")
    # print(body)  # 필요하면 주석 해제

    detail = body.get("action", {}).get("detailParams", {})

    # 오픈빌더 파라미터명: secureimage
    secureimage_raw = detail.get("secureimage", {}).get("value", {})

    # ✅ 어떤 형태로 오든 URL만 뽑아내기
    image_url = extract_first_url(secureimage_raw)

    if not image_url:
        text = "v3) 사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂"
    else:
        text = (
            "v3) ✅ 사진 수신 완료!\n"
            "(URL 파싱 완료)\n\n"
            f"- image_url: {image_url}"
        )

    return JSONResponse(kakao_simple_text(text))
