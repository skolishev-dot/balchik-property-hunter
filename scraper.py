from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import smtplib
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
DATA_FILE = BASE / "data" / "listings.json"
SEEN_FILE = BASE / "state" / "seen.json"

BCPEA_LIST = "https://sales.bcpea.org/properties?court=8&perpage=100"
BALCHIK_CSI = "https://www.balchik.bg/bg/obyavleniya-chsi-i-sinditsi/2026-godina/"
BALCHIK_AUCTIONS = "https://www.balchik.bg/bg/targove-i-konkursi/2026-g"

# Browser-like headers improve compatibility with sites that reject obvious bot user agents.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SALE_TERMS = (
    "продажба", "продан", "публична продан", "публична продажба",
    "търг", "продава", "ликвидатор", "синдик",
)
REJECT_TERMS = (
    "отдаване под наем", "под наем", "наем на", "наемане",
    "рекламно-информацион", "рекламно информацион", "рие",
    "павилион", "павилиони", "тротоарн", "контейнер", "контейнери",
    "пасища", "мери", "поземлен фонд", "земеделски земи под наем",
    "кандидати", "одобрени кандидати", "процедура за отдаване",
)
HOUSE_TERMS = ("къща", "жилищна сграда", "вила", "еднофамил", "двуфамил", "жилище")
APARTMENT_TERMS = ("апартамент", "самостоятелен обект", "ателие")
LAND_TERMS = ("поземлен имот", "пи ", "парцел", "дворно място", "урегулиран", "упи", "земя", "лозе")


@dataclass(frozen=True)
class Listing:
    source: str
    title: str
    location: str
    price_bgn: float | None
    area_sqm: float | None
    land_area_sqm: float | None
    deadline: str
    url: str
    description: str = ""
    category: str = "Имот"
    ideal_parts: bool = False
    active: bool = True
    published: str = ""

    @property
    def uid(self) -> str:
        raw = f"{self.source}|{self.url}|{self.title}|{self.location}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def public_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        basis = self.area_sqm or self.land_area_sqm
        d["price_per_sqm"] = round(self.price_bgn / basis, 2) if self.price_bgn and basis else None
        d["signals"] = build_signals(self)
        d["score"] = opportunity_score(self)
        return d


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_balchik_url(index_url: str, href: str) -> str:
    """Repair Balchik.bg links that are emitted as path-relative `bg/...` URLs.

    The municipality index currently contains links such as
    `bg/targove-i-konkursi/...`. A normal urljoin against the index page
    duplicates the section path (`.../bg/targove-i-konkursi/bg/...`).
    """
    href = clean(href)
    if not href:
        return index_url
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("bg/"):
        href = "https://www.balchik.bg/" + href
    else:
        href = urljoin(index_url, href)

    parsed = urlparse(href)
    path = re.sub(r"/(bg/(?:targove-i-konkursi|obyavleniya-chsi-i-sinditsi))/(?:bg/\1/)?", r"/\1/", parsed.path)
    # Explicit collapse for the malformed paths observed on the municipality site.
    path = path.replace("/bg/targove-i-konkursi/bg/targove-i-konkursi/", "/bg/targove-i-konkursi/")
    path = path.replace("/bg/obyavleniya-chsi-i-sinditsi/bg/obyavleniya-chsi-i-sinditsi/", "/bg/obyavleniya-chsi-i-sinditsi/")
    return urlunparse((parsed.scheme or "https", parsed.netloc or "www.balchik.bg", path, "", parsed.query, ""))


def normalize_number(raw: str) -> float | None:
    if not raw:
        return None
    s = clean(raw).replace("\xa0", " ")
    # Keep the right-most separator as decimal only when followed by 1-2 digits.
    s = re.sub(r"[^0-9,\. ]", "", s).strip()
    s = s.replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts[-1]) <= 2:
            s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s = "".join(parts)
    elif s.count(".") > 1:
        parts = s.split(".")
        if len(parts[-1]) <= 2:
            s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            s = "".join(parts)
    try:
        return float(s)
    except ValueError:
        return None


def fetch(url: str, tries: int = 3, referer: str | None = None) -> requests.Response:
    last = None
    headers = {"Referer": referer} if referer else None
    for i in range(tries):
        try:
            r = SESSION.get(url, headers=headers, timeout=35, allow_redirects=True)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(1.4 * (i + 1))
    raise RuntimeError(f"Неуспешно изтегляне: {url}: {last}")


def text_after_label(text: str, label: str, stop_labels: Iterable[str]) -> str:
    upper = text.upper()
    idx = upper.find(label.upper())
    if idx < 0:
        return ""
    rest = text[idx + len(label):]
    end = len(rest)
    rest_upper = rest.upper()
    for stop in stop_labels:
        j = rest_upper.find(stop.upper())
        if j >= 0:
            end = min(end, j)
    return clean(rest[:end])


def first_match(text: str, patterns: Iterable[str]) -> str:
    for pattern in patterns:
        m = re.search(pattern, text, re.I | re.S)
        if m:
            return clean(m.group(1))
    return ""


def extract_price(text: str) -> float | None:
    patterns = (
        r"\(([0-9][0-9\s.,]{2,})\s*(?:лв|лева)\)",
        r"(?:начална|първоначална|стартова)\s+цена[^0-9]{0,50}([0-9][0-9\s.,]{2,})\s*(?:лв|лева)",
        r"(?:цена|оценка)[^0-9]{0,30}([0-9][0-9\s.,]{2,})\s*(?:лв|лева)",
        r"([0-9][0-9\s.,]{3,})\s*(?:лв|лева)\s*(?:без|с)?\s*ддс",
    )
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            n = normalize_number(m.group(1))
            if n and n >= 100:
                return n
    # EUR fallback, converted at fixed Bulgarian lev rate.
    m = re.search(r"(?:начална|стартова|продажна)?\s*цена[^0-9€]{0,40}([0-9][0-9\s.,]{2,})\s*(?:€|евро|eur)", text, re.I)
    if m:
        eur = normalize_number(m.group(1))
        if eur and eur >= 50:
            return round(eur * 1.95583, 2)
    return None


def extract_areas(text: str) -> tuple[float | None, float | None]:
    candidates: list[tuple[float, int]] = []
    patterns = (
        r"(?:застроена\s+площ|рзп|площ\s+на\s+(?:сграда|жилище|апартамент))[^0-9]{0,30}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
        r"(?:площ)[^0-9]{0,20}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
    )
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            n = normalize_number(m.group(1))
            if n and 5 <= n <= 200000:
                candidates.append((n, m.start()))
    building = candidates[0][0] if candidates else None

    land = None
    land_patterns = (
        r"(?:поземлен имот|дворно място|парцел|упи)[^0-9]{0,80}(?:площ(?:\s+от)?\s*)?([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
        r"([0-9][0-9\s.,]*)\s*(?:дка|декар)",
    )
    for idx, p in enumerate(land_patterns):
        m = re.search(p, text, re.I)
        if m:
            n = normalize_number(m.group(1))
            if n:
                land = n * 1000 if idx == 1 else n
                break
    if land and building and land == building and "двор" not in text.lower() and "парцел" not in text.lower():
        land = None
    return building, land


def extract_deadline(text: str) -> str:
    patterns = (
        r"(?:срок|проданта)[^\n]{0,80}?от\s*([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})\s*(?:г\.?\s*)?(?:до|–|-)\s*([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})",
        r"(?:краен\s+срок|до)[^0-9]{0,30}([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})",
        r"(?:публична\s+продан|търг)[^0-9]{0,30}(?:на\s*)?([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})",
    )
    m = re.search(patterns[0], text, re.I)
    if m:
        return f"{m.group(1)} – {m.group(2)}"
    for p in patterns[1:]:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return ""


def categorize(text: str) -> str:
    tl = text.lower()
    if any(k in tl for k in HOUSE_TERMS) and any(k in tl for k in LAND_TERMS):
        return "Къща + двор/парцел"
    if any(k in tl for k in HOUSE_TERMS):
        return "Къща/вила"
    if any(k in tl for k in APARTMENT_TERMS):
        return "Апартамент"
    if any(k in tl for k in LAND_TERMS):
        return "Парцел/земя"
    return "Друг недвижим имот"


def detect_location(text: str, locations: list[str]) -> str:
    tl = text.lower()
    # Prefer the longest name, avoiding accidental partial matches.
    for loc in sorted(locations, key=len, reverse=True):
        if loc.lower() in tl:
            return loc
    return ""


def is_ideal_parts(text: str) -> bool:
    return bool(re.search(r"идеалн(?:а|и|ите)?\s+част|\b\d+\s*/\s*\d+\s*(?:ид\.?\s*ч|идеал)", text, re.I))


def is_relevant_sale(text: str) -> bool:
    tl = text.lower()
    if any(term in tl for term in REJECT_TERMS):
        return False
    if not any(term in tl for term in SALE_TERMS):
        return False
    # Must look like real estate, not a generic procurement/event.
    property_terms = HOUSE_TERMS + APARTMENT_TERMS + LAND_TERMS + ("недвижим имот", "имоти", "сграда")
    return any(term in tl for term in property_terms)


def read_pdf_text(url: str, referer: str) -> str:
    try:
        r = fetch(url, tries=2, referer=referer)
        if len(r.content) > 15_000_000:
            return ""
        reader = PdfReader(io.BytesIO(r.content))
        chunks = []
        for page in reader.pages[:25]:
            chunks.append(page.extract_text() or "")
        return clean(" ".join(chunks))[:50000]
    except Exception as exc:
        print(f"[warn] PDF skipped {url}: {exc}")
        return ""


def scrape_balchik_detail(url: str, source: str, title_hint: str, locations: list[str]) -> Listing | None:
    soup = BeautifulSoup(fetch(url, referer="https://www.balchik.bg/").text, "lxml")
    page_text = clean(soup.get_text(" ", strip=True))
    extra = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if urlparse(href).path.lower().endswith(".pdf"):
            txt = read_pdf_text(href, url)
            if txt:
                extra.append(txt)
    full_text = clean(" ".join([title_hint, page_text] + extra))
    if not is_relevant_sale(full_text):
        return None

    h = soup.find(["h1", "h2"])
    title = clean(h.get_text(" ", strip=True)) if h else clean(title_hint)
    if len(title) < 10:
        title = clean(title_hint)
    location = detect_location(full_text, locations)
    price = extract_price(full_text)
    area, land_area = extract_areas(full_text)
    deadline = extract_deadline(full_text)
    published = first_match(page_text, (r"публикувано\s+на[:\s]*([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})",))
    category = categorize(full_text)
    desc = full_text[:5000]
    return Listing(
        source=source,
        title=title,
        location=location,
        price_bgn=price,
        area_sqm=area,
        land_area_sqm=land_area,
        deadline=deadline,
        url=url,
        description=desc,
        category=category,
        ideal_parts=is_ideal_parts(full_text),
        published=published,
    )


def listing_from_index(title: str, href: str, source: str, locations: list[str], nearby_text: str = "") -> Listing | None:
    combined = clean(f"{title} {nearby_text}")
    if not is_relevant_sale(combined):
        return None
    return Listing(
        source=source,
        title=clean(title),
        location=detect_location(combined, locations),
        price_bgn=extract_price(combined),
        area_sqm=extract_areas(combined)[0],
        land_area_sqm=extract_areas(combined)[1],
        deadline=extract_deadline(combined),
        url=href,
        description=combined[:5000],
        category=categorize(combined),
        ideal_parts=is_ideal_parts(combined),
    )


def scrape_balchik_index(url: str, source: str, locations: list[str]) -> tuple[list[Listing], dict]:
    soup = BeautifulSoup(fetch(url, referer="https://www.balchik.bg/").text, "lxml")
    candidates: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    anchors_scanned = 0
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 15:
            continue
        anchors_scanned += 1
        href = normalize_balchik_url(url, a["href"])
        if "balchik.bg" not in href or href in seen_urls:
            continue
        # Keep a compact piece of surrounding text; it often contains the publication date.
        parent_text = clean(a.parent.get_text(" ", strip=True)) if a.parent else title
        if not is_relevant_sale(title):
            tl = title.lower()
            if not ("продан" in tl or "продаж" in tl):
                continue
        seen_urls.add(href)
        candidates.append((href, title, parent_text[:1200]))

    out: list[Listing] = []
    detail_ok = 0
    fallback_count = 0
    rejected_after_detail = 0
    for href, title, nearby in candidates[:100]:
        try:
            item = scrape_balchik_detail(href, source, title, locations)
            if item:
                out.append(item)
                detail_ok += 1
            else:
                rejected_after_detail += 1
        except Exception as exc:
            # Do not lose a potentially useful property merely because the detail page is broken.
            fallback = listing_from_index(title, href, source, locations, nearby)
            if fallback:
                out.append(fallback)
                fallback_count += 1
            print(f"[warn] Balchik detail fallback {href}: {exc}")

    stats = {
        "anchors_scanned": anchors_scanned,
        "index_candidates": len(candidates),
        "detail_ok": detail_ok,
        "fallback_from_index": fallback_count,
        "rejected_after_detail": rejected_after_detail,
        "returned": len(out),
    }
    return out, stats


def parse_bcpea_detail(url: str, locations: list[str]) -> Listing | None:
    soup = BeautifulSoup(fetch(url, referer=BCPEA_LIST).text, "lxml")
    text = clean(soup.get_text(" ", strip=True))
    h = soup.find(["h1", "h2"])
    title = clean(h.get_text(" ", strip=True)) if h else "Имот от ЧСИ"
    location = text_after_label(text, "НАСЕЛЕНО МЯСТО", ["Адрес", "ОКРЪЖЕН СЪД", "ЧАСТЕН СЪДЕБЕН ИЗПЪЛНИТЕЛ"])
    location = location or detect_location(text, locations)
    price = extract_price(text)
    area, land_area = extract_areas(text)
    deadline = extract_deadline(text)
    description = text_after_label(text, "ОПИСАНИЕ", ["РЕГ. № ЧСИ", "Адрес Окръжен съд"]) or text[:5000]
    combined = clean(f"{title} {location} {description}")
    return Listing(
        "Камара на ЧСИ", title, location, price, area, land_area, deadline, url,
        description[:5000], categorize(combined), is_ideal_parts(combined)
    )


def scrape_bcpea(locations: list[str]) -> list[Listing]:
    # This source may occasionally return 403 from cloud-hosted runners. The rest of the app continues if so.
    r = fetch(BCPEA_LIST, referer="https://sales.bcpea.org/")
    soup = BeautifulSoup(r.text, "lxml")
    links: list[str] = []
    for a in soup.select('a[href*="/properties/"]'):
        href = a.get("href", "")
        if re.search(r"/properties/\d+", href):
            u = urljoin(BCPEA_LIST, href)
            if u not in links:
                links.append(u)
    out: list[Listing] = []
    for u in links[:120]:
        try:
            item = parse_bcpea_detail(u, locations)
            if item:
                out.append(item)
        except Exception as exc:
            print(f"[warn] BCPEA detail skipped {u}: {exc}")
    return out


def matches(item: Listing, cfg: dict) -> bool:
    max_price = float(cfg.get("max_price_bgn", 200000) or 200000)
    min_area = float(cfg.get("min_area_sqm", 0) or 0)
    if item.price_bgn is not None and item.price_bgn > max_price:
        return False
    if item.area_sqm is not None and item.area_sqm < min_area and item.category not in ("Парцел/земя",):
        return False

    hay = f"{item.title} {item.location} {item.description}".lower()
    locations = [str(x).lower() for x in cfg.get("locations", [])]
    if locations and not any(x in hay for x in locations):
        return False
    if not is_relevant_sale(hay):
        return False

    allowed = cfg.get("categories", [])
    if allowed and item.category not in allowed:
        return False
    return True


def build_signals(item: Listing) -> list[str]:
    out: list[str] = []
    if item.ideal_parts:
        out.append("Идеални части")
    if item.price_bgn is None:
        out.append("Цена за проверка")
    if item.category.startswith("Къща") and item.land_area_sqm is None:
        out.append("Дворът не е извлечен")
    if not item.deadline:
        out.append("Срокът не е извлечен")
    return out


def opportunity_score(item: Listing) -> int:
    # Heuristic prioritization, not a property valuation.
    score = 50
    if item.category == "Къща + двор/парцел": score += 24
    elif item.category == "Къща/вила": score += 18
    elif item.category == "Апартамент": score += 6
    elif item.category == "Парцел/земя": score += 3
    if item.price_bgn is not None: score += 8
    if item.land_area_sqm: score += 7
    if item.area_sqm: score += 4
    if item.deadline: score += 3
    if item.ideal_parts: score -= 22
    return max(0, min(100, score))


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")).get("seen", []))
    except Exception:
        return set()


def save_seen(ids: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps({"seen": sorted(ids)}, ensure_ascii=False, indent=2), encoding="utf-8")


def save_public(items: list[Listing], errors: list[str], diagnostics: dict | None = None) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "source_errors": errors,
        "diagnostics": diagnostics or {},
        "items": [x.public_dict() for x in items],
    }
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def send_email(items: list[Listing]) -> None:
    recipient = (os.getenv("EMAIL_TO") or os.getenv("ALERT_EMAIL_TO") or "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip().replace(" ", "")
    if not recipient or not user or not password:
        print("[info] Email secrets are not configured; skipping email.")
        return

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = f"Балчик Property Hunter: {len(items)} нови попадения"

    lines, rows = [], []
    for x in items:
        price = f"{x.price_bgn:,.0f} лв.".replace(",", " ") if x.price_bgn else "за проверка"
        area = f"{x.area_sqm:,.0f} кв.м".replace(",", " ") if x.area_sqm else "—"
        land = f"{x.land_area_sqm:,.0f} кв.м".replace(",", " ") if x.land_area_sqm else "—"
        warning = "⚠ ИДЕАЛНИ ЧАСТИ" if x.ideal_parts else ""
        lines.append(
            f"[{x.category}] {x.title}\nМясто: {x.location or '—'}\nЦена: {price}\n"
            f"Площ: {area} | Двор/парцел: {land}\nСрок: {x.deadline or 'за проверка'}\n{warning}\n{x.url}"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(x.category)}</td><td>{html.escape(x.title)}</td>"
            f"<td>{html.escape(x.location or '—')}</td><td>{price}</td><td>{area}</td><td>{land}</td>"
            f"<td>{'⚠ Да' if x.ideal_parts else 'Не е засечено'}</td>"
            f"<td><a href=\"{html.escape(x.url)}\">Отвори</a></td></tr>"
        )
    msg.set_content("\n\n---\n\n".join(lines))
    msg.add_alternative(
        "<html><body><h2>Нови имотни попадения около Балчик</h2>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>Тип</th><th>Имот</th><th>Място</th><th>Цена</th><th>Застр. площ</th><th>Двор/парцел</th><th>Идеални части</th><th>Линк</th></tr>"
        + "".join(rows)
        + "</table><p><small>Автоматичен филтър, не правна или пазарна оценка. Проверявай тежести, собственост, владение и документите по делото.</small></p></body></html>",
        subtype="html",
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def main() -> int:
    cfg = load_config()
    locations = [str(x) for x in cfg.get("locations", [])]
    all_items: list[Listing] = []
    errors: list[str] = []

    diagnostics: dict[str, dict] = {}

    try:
        got = scrape_bcpea(locations)
        diagnostics["Камара на ЧСИ"] = {"returned": len(got)}
        print(f"[ok] Камара на ЧСИ: {len(got)} релевантни обяви")
        all_items.extend(got)
    except Exception as exc:
        errors.append(f"Камара на ЧСИ: {exc}")
        diagnostics["Камара на ЧСИ"] = {"error": str(exc)}
        print(f"[warn] Камара на ЧСИ: {exc}")

    for name, index_url, source_label in [
        ("Община Балчик / ЧСИ", BALCHIK_CSI, "Община Балчик / ЧСИ и синдици"),
        ("Община Балчик / търгове", BALCHIK_AUCTIONS, "Община Балчик / търгове"),
    ]:
        try:
            got, stats = scrape_balchik_index(index_url, source_label, locations)
            diagnostics[name] = stats
            print(
                f"[ok] {name}: кандидати={stats['index_candidates']} detail={stats['detail_ok']} "
                f"fallback={stats['fallback_from_index']} върнати={stats['returned']}"
            )
            all_items.extend(got)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            diagnostics[name] = {"error": str(exc)}
            print(f"[warn] {name}: {exc}")

    uniq = {x.uid: x for x in all_items}
    before_filters = len(uniq)
    current = [x for x in uniq.values() if matches(x, cfg)]
    current.sort(key=lambda x: (-opportunity_score(x), x.price_bgn is None, x.price_bgn or 10**18, x.location, x.title))
    diagnostics["summary"] = {
        "unique_before_filters": before_filters,
        "shown_after_filters": len(current),
        "houses": sum(1 for x in current if x.category.startswith("Къща")),
        "apartments": sum(1 for x in current if x.category == "Апартамент"),
        "land": sum(1 for x in current if x.category == "Парцел/земя"),
    }
    save_public(current, errors, diagnostics)

    seen = load_seen()
    first_run = not SEEN_FILE.exists() or not seen
    new_items = [x for x in current if x.uid not in seen]
    should_send_existing = bool(cfg.get("send_existing_on_first_run", False))
    to_alert = new_items if (not first_run or should_send_existing) else []

    if to_alert:
        send_email(to_alert)
        print(f"[ok] notification candidates: {len(to_alert)}")
    elif first_run and new_items:
        print("[info] First run: current items stored as baseline; no backlog email sent.")

    save_seen(seen | {x.uid for x in current})
    print(f"[diag] unique={before_filters} houses={diagnostics['summary']['houses']} apartments={diagnostics['summary']['apartments']} land={diagnostics['summary']['land']}")
    print(f"[done] matches={len(current)} new={len(new_items)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
