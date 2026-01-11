from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


def kakao_simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }


def extract_first_url_from_anything(obj):
    """
    카카오 secureimage 값이 환경/설정에 따라
    - dict({"secureUrls":[...]}),
    - list([...]),
    - string("List(http://...)") 형태로 올 때가 있어
    최대한 안전하게 첫 URL을 뽑아낸다.
    """
    import re

    if obj is None:
        return None

    # dict인 경우
    if isinstance(obj, dict):
        if "secureUrls" in obj:
            return extract_first_url_from_anything(obj.get("secureUrls"))
        # 혹시 다른 키에 들어오는 경우까지 대비
        for v in obj.values():
            url = extract_first_url_from_anything(v)
            if url:
                return url
        return None

    # list/tuple인 경우
    if isinstance(obj, (list, tuple)):
        if not obj:
            return None
        return extract_first_url_from_anything(obj[0])

    # 문자열인 경우: "List(http://...)" 같은 것도 여기서 처리
    if isinstance(obj, str):
        m = re.search(r"https?://\S+", obj)
        return m.group(0) if m else None

    # 그 외 타입은 문자열로 바꿔서 URL 추출 시도
    s = str(obj)
    import re
    m = re.search(r"https?://\S+", s)
    return m.group(0) if m else None


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()

    # ✅ Render 로그에서 실제로 스킬이 호출됐는지 확인용
    print("🔥 KAKAO REQUEST RECEIVED (v2)")
    print(body)

    detail = body.get("action", {}).get("detailParams", {})
    secureimage_raw = detail.get("secureimage", {}).get("value", {})

    image_url = extract_first_url_from_anything(secureimage_raw)

    if not image_url:
        text = "v2) 사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂"
    else:
        text = (
            "v2) ✅ 사진 수신 완료!\n"
            "(지금은 URL 확인 단계)\n\n"
            f"- image_url: {image_url}"
        )

    return JSONResponse(kakao_simple_text(text))
