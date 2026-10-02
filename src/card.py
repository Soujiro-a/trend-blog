"""글 첫머리에 넣는 '한눈에 정리' 요약 카드 이미지(1200×675 PNG).

AI 그림이 아니라 글에 쓴 핵심 값 3~4줄을 코드로 그린 카드입니다. 비용이 들지 않고 글자가 깨지지 않으며,
글마다 내용이 달라 검색 썸네일·공유 미리보기·이미지 검색에서 그 글의 정보를 그대로 보여 줍니다.

흐름: 작성 모델이 <<<CARD>>> 블록으로 카드 글을 쓰고(src/writer.py) → 검수관이 본문과 맞는지 보고
(card_ok, src/reviewer.py) → 여기서 그림을 그려 GitHub 저장소 cards/ 에 올린 뒤(Contents API) →
커밋 고정 주소(raw.githubusercontent.com/<저장소>/<커밋>/...)로 본문 맨 앞에 넣습니다.

Blogger API 는 이미지를 올릴 수 없어 저장소를 이미지 호스팅으로 씁니다. **저장소가 공개여야** 보입니다.
어느 단계에서 실패해도 글은 카드 없이 그대로 발행됩니다(카드 때문에 글이 막히지 않게).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

W, H = 1200, 675
ROOT = Path(__file__).resolve().parent.parent

# Noto Sans KR 가변 글꼴. GitHub Actions 는 워크플로가 assets/fonts/ 에 내려받습니다(깃에는 올리지 않음).
FONT_PATHS = [ROOT / "assets" / "fonts" / "NotoSansKR-VF.ttf", Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")]
WEIGHTS = {"regular": 400, "medium": 500, "bold": 700}

_CARD_IMG = re.compile(r'<img[^>]+src="https://raw\.githubusercontent\.com/[^"]+/cards/[^"]+\.png"')


def has_card(content: str) -> bool:
    """본문에 이미 요약 카드가 있는지 (기존 글 정리가 씁니다)."""
    return bool(_CARD_IMG.search(content or ""))


@dataclass
class Card:
    headline: str
    rows: list[tuple[str, str]]
    blog_name: str = ""
    site: str = ""
    accent: str = "#1F5FBF"

    @property
    def alt(self) -> str:
        """이미지 대체 텍스트. 카드 글자를 그대로 담아 검색엔진·화면낭독기도 내용을 읽게 합니다."""
        return f"{self.headline} — " + "; ".join(f"{k}: {v}" for k, v in self.rows)


def parse(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """작성 모델의 카드 글('제목: …' 한 줄 + '항목 | 값' 3~4줄)을 읽습니다. 형식이 안 맞으면 None."""
    headline, rows = "", []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line:
            continue
        m = re.match(r"^제목\s*[:：]\s*(.+)$", line)
        if m:
            headline = m.group(1).strip()
        elif "|" in line:
            k, v = (s.strip() for s in line.split("|", 1))
            if k and v and (k, v) != ("항목", "값"):   # 형식 설명 줄을 그대로 베낀 경우
                rows.append((k, v))
    if not headline or len(rows) < 3:
        return None
    return headline, rows[:4]


# ---------------------------------------------------------------------------
# 그리기
# ---------------------------------------------------------------------------

def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if path.exists():
            f = ImageFont.truetype(str(path), size)
            f.set_variation_by_axes([WEIGHTS[weight]])
            return f
    raise FileNotFoundError("한글 글꼴이 없습니다: assets/fonts/NotoSansKR-VF.ttf")


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _tint(c: str, t: float) -> tuple[int, int, int]:
    """c 를 흰색 쪽으로 t(0~1)만큼 옅게."""
    return tuple(round(v + (255 - v) * t) for v in _hex(c))


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """띄어쓰기 단위로 줄바꿈하고, 한 단어가 폭보다 길면 글자 단위로 자릅니다."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if font.getlength(trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for ch in word:
            if font.getlength(cur + ch) > width and cur:
                lines.append(cur)
                cur = ""
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _layout(card: Card, scale: float, label_w: int, value_w: int) -> dict:
    """글자 크기(scale)에 맞춰 줄바꿈과 높이를 계산합니다."""
    hf = _font(round(62 * scale), "bold")
    lf = _font(round(30 * scale), "bold")
    vf = _font(round(33 * scale), "medium")
    head = wrap(card.headline, hf, 1000)[:2]
    rows = [(wrap(k, lf, label_w)[:2], wrap(v, vf, value_w)[:3]) for k, v in card.rows[:4]]
    row_h = [max(len(k) * round(40 * scale), len(v) * round(46 * scale)) + round(46 * scale) for k, v in rows]
    return {"hf": hf, "lf": lf, "vf": vf, "head": head, "rows": rows, "row_h": row_h,
            "head_h": len(head) * round(78 * scale), "scale": scale}


def render(card: Card) -> bytes:
    """카드 PNG 바이트. 밝은 배경 + 블로그별 강조색(왼쪽 띠·배지·항목 이름)."""
    accent = _hex(card.accent)
    ink, sub = (26, 26, 26), (120, 120, 120)
    img = Image.new("RGB", (W, H), (250, 249, 246))
    d = ImageDraw.Draw(img)
    x0, x1 = 88, W - 72
    label_w = 200
    value_w = (x1 - x0) - label_w - 2 * 28 - 20

    # 내용이 넘치면 글자를 조금씩 줄입니다.
    avail = H - 64 - 72 - 60 - 34 - 70   # 위 여백·배지·제목 아래 간격·바닥글
    for scale in (1.0, 0.94, 0.88, 0.82, 0.76, 0.7):
        lay = _layout(card, scale, label_w, value_w)
        need = lay["head_h"] + sum(lay["row_h"]) + 14 * (len(lay["rows"]) - 1)
        if need <= avail:
            break
    # 남는 세로 공간은 행 사이 간격으로 나눠 아래쪽이 비어 보이지 않게 합니다.
    gap = 14 + max(0, min(16, (avail - need) // max(1, len(lay["rows"]))))

    d.rectangle([0, 0, 18, H], fill=accent)

    y = 60
    if card.blog_name:   # 블로그 이름 배지
        bf = _font(24, "bold")
        d.rounded_rectangle([x0, y, x0 + bf.getlength(card.blog_name) + 36, y + 46], radius=23, fill=_tint(card.accent, 0.86))
        d.text((x0 + 18, y + 23), card.blog_name, font=bf, fill=accent, anchor="lm")
    y += 46 + 30

    for line in lay["head"]:
        d.text((x0, y), line, font=lay["hf"], fill=ink)
        y += round(78 * lay["scale"])
    y += 30

    s = lay["scale"]
    llh, vlh = round(40 * s), round(46 * s)
    for (label, value), rh in zip(lay["rows"], lay["row_h"]):
        d.rounded_rectangle([x0, y, x1, y + rh], radius=14, fill=(255, 255, 255), outline=(230, 228, 222), width=2)
        ly = y + (rh - len(label) * llh) / 2
        for i, t in enumerate(label):
            d.text((x0 + 28, ly + llh * (i + 0.5)), t, font=lay["lf"], fill=accent, anchor="lm")
        vy = y + (rh - len(value) * vlh) / 2
        for i, t in enumerate(value):
            d.text((x0 + 28 + label_w + 20, vy + vlh * (i + 0.5)), t, font=lay["vf"], fill=ink, anchor="lm")
        y += rh + gap

    ff = _font(22, "medium")
    if card.site:
        d.text((x0, H - 44), card.site, font=ff, fill=sub, anchor="ls")
    d.text((x1, H - 44), "한눈에 정리", font=ff, fill=sub, anchor="rs")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 올리기 · 본문에 넣기
# ---------------------------------------------------------------------------

def upload(png: bytes, path: str) -> str:
    """GitHub 저장소에 파일을 커밋하고, 커밋에 고정된 이미지 주소를 돌려줍니다.

    로컬 checkout 을 건드리지 않고 API 로 바로 커밋하므로 실행 중인 워크플로의 data/ 커밋과 섞이지 않습니다
    (워크플로 끝의 git pull --rebase 가 이 커밋을 받아 옵니다). 커밋 고정 주소라 나중에 파일이 바뀌어도
    이미 발행한 글의 그림은 그대로입니다.
    """
    token, repo = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        raise RuntimeError("GITHUB_TOKEN/GITHUB_REPOSITORY 없음 (GitHub Actions 에서만 카드를 올립니다)")
    body = {"message": f"chore: 요약 카드 {path}", "content": base64.b64encode(png).decode()}
    if os.environ.get("GITHUB_REF_NAME"):
        body["branch"] = os.environ["GITHUB_REF_NAME"]
    resp = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json=body, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"카드 업로드 실패 ({resp.status_code}): {resp.text[:200]}")
    sha = resp.json()["commit"]["sha"]
    return f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def html(url: str, card: Card) -> str:
    alt = card.alt.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<div class="separator" style="clear: both; text-align: center;">'
        f'<img src="{url}" alt="{alt}" width="1200" height="675" style="max-width: 100%; height: auto;" /></div>\n'
    )


def make(cfg: dict, blog_id: str, blog_name: str, card_text: str, key: str) -> str:
    """카드 글 → 그림 → 업로드 → 본문 맨 앞에 넣을 HTML. 쓸 수 없으면 "" (이유는 로그로)."""
    c = cfg.get("card") or {}
    if not c.get("enabled", False):
        return ""
    parsed = parse(card_text)
    if not parsed:
        log.info("요약 카드 형식이 맞지 않아 넣지 않습니다: %r", (card_text or "")[:120])
        return ""
    headline, rows = parsed
    card = Card(
        headline=headline, rows=rows, blog_name=blog_name, site=f"{blog_id}.blogspot.com",
        accent=(c.get("accents") or {}).get(blog_id, c.get("default_accent", "#1F5FBF")),
    )
    try:
        png = render(card)
        name = f"{datetime.now():%Y%m%d-%H%M}-{hashlib.sha1(key.encode()).hexdigest()[:8]}.png"
        url = upload(png, f"cards/{blog_id}/{name}")
    except Exception as exc:  # 카드 때문에 발행이 막히면 안 됩니다
        log.warning("요약 카드를 넣지 못했습니다 (글은 그대로 발행): %s", exc)
        return ""
    log.info("요약 카드: %s", url)
    return html(url, card)


# ---------------------------------------------------------------------------
# 기존 글: 본문에서 카드 글 뽑기 (scripts/retrofit_posts.py)
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """이미 공개된 블로그 글의 본문에서, 글 맨 앞 '한눈에 정리' 이미지에 넣을 핵심 3~4가지를 뽑습니다.

아래 형식만 출력합니다. 다른 말은 덧붙이지 마세요.

제목: 카드 제목 (20자 이내. 글 제목을 반복하지 말고 핵심만. 예: 해외직구 면세 기준)
항목 | 값
항목 | 값
항목 | 값

- 줄은 3~4개. 항목은 8자 이내, 값은 28자 이내로 짧게. `항목 | 값` 이라는 머리줄은 쓰지 않습니다.
- 독자가 이 글에서 찾던 답을 고릅니다: 기한·금액·신청처·조건·순서처럼 구체적인 것.
- **본문에 적힌 내용만** 씁니다. 본문에 없는 숫자나 사실을 더하지 말고, 본문이 단정하지 않은 값은 단정하지 마세요.
- 본문에 카드로 만들 만한 구체적인 값이 3개도 없으면 `없음` 한 단어만 출력합니다."""


def _plain(html_text: str) -> str:
    t = re.sub(r"<(h2|h3|li|p|tr)[^>]*>", "\n", html_text)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def extract(cfg: dict, title: str, body_html: str, client=None) -> tuple[str, float]:
    """(카드 글, 비용 USD). 뽑을 게 없으면 ("", 비용)."""
    from . import llm
    from .writer import PRICES

    model = (cfg.get("card") or {}).get("retrofit_model", "claude-sonnet-5-5")
    client = client or llm.client()
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=EXTRACT_SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": f"제목: {title}\n\n본문:\n{_plain(body_html)[:12000]}"}],
    )
    price_in, price_out = PRICES.get(model, (3.00, 15.00))
    cost = (response.usage.input_tokens * price_in + response.usage.output_tokens * price_out) / 1_000_000
    text = llm.text_of(response, f"[{title[:20]}] 카드 글 뽑기").strip()
    return ("" if text.startswith("없음") else text), cost
