from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


def kakao_simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }


@app.post("/kakao-skill")
async def kakao_skill(req: Request):
    body = await req.json()

    # 오픈빌더에서 파라미터명을 secureimage 로 만들었다는 가정
    detail = body.get("action", {}).get("detailParams", {})
    secureimage_value = detail.get("secureimage", {}).get("value", {})

    secure_urls = secureimage_value.get("secureUrls", [])

    if not secure_urls:
        return JSONResponse(kakao_simple_text(
            "사진이 안 들어왔어요.\n가정통신문 사진을 1장 보내주세요 🙂"
        ))

    # 여기까지 왔다는 건 사진 URL을 정상 수신한 것
    image_url = secure_urls[0]
    return JSONResponse(kakao_simple_text(
        "✅ 사진 수신 완료!\n(다음 단계에서 요약을 붙일게요)\n\n"
        f"- image_url: {image_url}"
    ))
