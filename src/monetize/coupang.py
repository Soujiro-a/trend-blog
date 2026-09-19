"""쿠팡파트너스 상품 링크.

검수관이 '구매 의도가 있는 주제'로 판단한 글에만 관련 상품 2~3개를 넣습니다.
쿠팡파트너스 오픈 API 의 상품 검색은 결과 URL 이 이미 제휴 링크라 별도 변환이 없습니다.

필요한 환경변수 (쿠팡파트너스 → 링크 생성 → API 키 발급):
    COUPANG_ACCESS_KEY
    COUPANG_SECRET_KEY

법적으로 반드시 붙여야 하는 문구가 있어 블록 끝에 고정으로 넣습니다.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
import urllib.parse
from datetime import datetime, timezone

from .. import net
from ..config import env

log = logging.getLogger(__name__)

HOST = "https://api-gateway.coupang.com"
SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"

# 쿠팡파트너스 운영 정책상 제휴 링크가 있는 글에 반드시 표시해야 하는 문구입니다.
DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."


def configured() -> bool:
    return bool(env("COUPANG_ACCESS_KEY") and env("COUPANG_SECRET_KEY"))


def _authorization(method: str, path: str, query: str) -> str:
    """쿠팡 HMAC 서명 헤더. 서명 시각은 GMT 기준 yyMMddTHHmmssZ 형식입니다."""
    access = env("COUPANG_ACCESS_KEY", required=True)
    secret = env("COUPANG_SECRET_KEY", required=True)
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = signed_date + method + path + query
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return (
        f"CEA algorithm=HmacSHA256, access-key={access}, "
        f"signed-date={signed_date}, signature={signature}"
    )


def search(keyword: str, limit: int = 3) -> list[dict]:
    """상품 검색. (상품명, 가격, 이미지, 제휴 URL, 로켓배송 여부) 목록."""
    query = urllib.parse.urlencode({"keyword": keyword, "limit": limit})
    resp = net.session().get(
        HOST + SEARCH_PATH + "?" + query,
        headers={
            "Authorization": _authorization("GET", SEARCH_PATH, query),
            "Content-Type": "application/json;charset=UTF-8",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"쿠팡 API 오류 ({resp.status_code}): {resp.text[:300]}")

    payload = resp.json()
    if str(payload.get("rCode", "0")) != "0":
        raise RuntimeError(f"쿠팡 API 응답 오류: {payload.get('rMessage', payload)}")

    products = (payload.get("data") or {}).get("productData") or []
    out = []
    for p in products[:limit]:
        if not p.get("productUrl") or not p.get("productName"):
            continue
        out.append(
            {
                "name": p["productName"],
                "price": int(p.get("productPrice") or 0),
                "image": p.get("productImage", ""),
                "url": p["productUrl"],
                "rocket": bool(p.get("isRocket")),
            }
        )
    return out


def render(products: list[dict], heading: str) -> str:
    """상품 카드 HTML. Blogger 템플릿을 타지 않도록 인라인 스타일만 씁니다."""
    if not products:
        return ""
    cards = []
    for p in products:
        price = f"{p['price']:,}원" if p["price"] else "가격 확인"
        rocket = " · 로켓배송" if p["rocket"] else ""
        img = (
            f'<img src="{html.escape(p["image"])}" alt="" loading="lazy" '
            'style="width:88px;height:88px;object-fit:cover;border-radius:8px;flex:none">'
            if p["image"]
            else ""
        )
        cards.append(
            '<a href="{url}" target="_blank" rel="nofollow sponsored noopener" '
            'style="display:flex;gap:12px;align-items:center;padding:10px 12px;'
            'border:1px solid #e5e5e5;border-radius:10px;text-decoration:none;color:inherit;margin:8px 0">'
            "{img}<span><strong style=\"display:block;font-size:.95em;line-height:1.4\">{name}</strong>"
            '<span style="color:#666;font-size:.9em">{price}{rocket}</span></span></a>'.format(
                url=html.escape(p["url"]),
                img=img,
                name=html.escape(p["name"][:70]),
                price=price,
                rocket=rocket,
            )
        )
    return (
        f"<h2>{html.escape(heading)}</h2>\n"
        + "\n".join(cards)
        + f'\n<p style="color:#888;font-size:.85em">{DISCLOSURE}</p>'
    )


def product_block(cfg: dict, product_query: str) -> str:
    cp = cfg["monetize"]["coupang"]
    products = search(product_query, limit=cp.get("max_products", 3))
    if not products:
        log.info("쿠팡 검색 결과 없음: %s", product_query)
        return ""
    return render(products, cp.get("heading", "관련 상품"))


_FAQ_HEADING = re.compile(r"<h2>\s*자주 묻는 질문", re.IGNORECASE)
_REFS_HEADING = re.compile(r"<h2>\s*참고한 자료", re.IGNORECASE)


def insert(body_html: str, block: str) -> str:
    """상품 블록을 FAQ 앞(없으면 참고 자료 앞, 그것도 없으면 맨 끝)에 넣습니다.

    본문 중간에 끼우면 읽는 흐름을 깨고, 맨 끝에 두면 아무도 안 봅니다.
    """
    if not block:
        return body_html
    for pattern in (_FAQ_HEADING, _REFS_HEADING):
        m = pattern.search(body_html)
        if m:
            return body_html[: m.start()] + block + "\n" + body_html[m.start() :]
    return body_html + "\n" + block
