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
AGRI_TERMS = ("земеделска земя", "нива", "лозе", "трайни насаждения", "землището", "категория на земята", "дка")
YARD_TERMS = ("дворно място", "урегулиран", "упи", "за жилищно", "ниско застрояване", "жилищно строителство")


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
    document_count: int = 0
    document_text_chars: int = 0
    extraction_source: str = ""

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
        d["expired"] = deadline_is_expired(self.deadline)
        d["deal_candidate"] = deal_candidate(self, 200000)
        d["deal_reasons"] = deal_reasons(self, 200000)
        return d


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_balchik_url(index_url: str, href: str) -> str:
    """Normalize Balchik.bg article links, including malformed nested ``bg/...`` paths.

    The municipality index frequently emits an href like ``bg/section/article``
    while the browser is already inside ``/bg/section/2026-...``. A normal
    urljoin therefore creates ``/.../2026-.../bg/section/article`` and returns
    404. The real article starts at the LAST ``/bg/`` segment.
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
    path = parsed.path or "/"
    starts = [m.start() for m in re.finditer(r"/bg/", path)]
    if len(starts) >= 2:
        path = path[starts[-1]:]
    path = re.sub(r"/{2,}", "/", path)
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
    # Check explicit EUR first so a generic "начална цена" pattern cannot
    # accidentally interpret euros as leva.
    euro_patterns = (
        r"(?:начална|стартова|тръжна|продажна)?\s*цена[^0-9€]{0,60}([0-9][0-9\s.,]{2,})\s*(?:€|евро|eur)",
        r"([0-9][0-9\s.,]{2,})\s*(?:€|евро|eur)",
    )
    for p in euro_patterns:
        m = re.search(p, text, re.I)
        if m:
            eur = normalize_number(m.group(1))
            if eur and 50 <= eur <= 50_000_000:
                return round(eur * 1.95583, 2)

    patterns = (
        r"(?:начална|първоначална|стартова|тръжна)\s+(?:тръжна\s+)?цена[^0-9]{0,80}([0-9][0-9\s.,]{2,})\s*(?:лв\.?|лева)",
        r"(?:оценка|цена)[^0-9]{0,35}([0-9][0-9\s.,]{2,})\s*(?:лв\.?|лева)",
        r"([0-9][0-9\s.,]{3,})\s*(?:лв\.?|лева)\s*(?:без|с)?\s*ддс",
        # Some municipality notices omit the currency immediately after the number.
        # Keep this last and only when no EUR marker is present nearby.
        r"(?:начална\s+цена\s+на\s+имота|начална\s+цена)[^0-9]{0,80}([0-9][0-9\s.,]{2,})(?!\s*(?:€|евро|eur))",
    )
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            n = normalize_number(m.group(1))
            if n and 100 <= n <= 100_000_000:
                return n
    return None

def normalize_decare_number(raw: str) -> float | None:
    """Parse Bulgarian decare notation.

    In property notices values such as 11,250 dka conventionally mean 11.250 dka,
    not eleven thousand two hundred and fifty decares.
    """
    if not raw:
        return None
    s = re.sub(r"[^0-9,.]", "", raw.replace(" ", ""))
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "." in s and "," not in s:
        # A single separator in dka context is treated as decimal separator.
        pass
    elif "," in s and "." in s:
        # Use the right-most separator as decimal, discard the other as grouping.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_areas(text: str) -> tuple[float | None, float | None]:
    building = None
    building_patterns = (
        r"(?:застроена\s+площ|рзп|разгъната\s+застроена\s+площ|площ\s+на\s+(?:сграда|жилище|апартамент))[^0-9]{0,40}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
        r"(?:жилищна|еднофамилна|двуфамилна|масивна)\s+сграда[^0-9]{0,120}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
    )
    for p in building_patterns:
        m = re.search(p, text, re.I)
        if m:
            n = normalize_number(m.group(1))
            if n and 5 <= n <= 10000:
                building = n
                break

    land = None
    # Prefer explicit parcel/yard area in square metres.
    sqm_patterns = (
        r"(?:поземлен\s+имот|дворно\s+място|парцел|упи)[^0-9]{0,120}(?:с\s+площ|площ(?:\s+от)?\s*)[^0-9]{0,20}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
        r"(?:площ\s+на\s+(?:поземления\s+имот|имота|двора|парцела))[^0-9]{0,30}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)",
    )
    for p in sqm_patterns:
        m = re.search(p, text, re.I)
        if m:
            n = normalize_number(m.group(1))
            if n and 20 <= n <= 5_000_000:
                land = n
                break

    if land is None:
        # Bulgarian notices use decimal comma for decares: 11,250 dka = 11 250 sqm.
        m = re.search(r"([0-9][0-9\s.,]*)\s*(?:дка|декар(?:а|и)?)", text, re.I)
        if m:
            n = normalize_decare_number(m.group(1))
            if n and 0.01 <= n <= 100000:
                land = n * 1000

    # Generic area fallback only for clearly residential objects.
    if building is None and any(k in text.lower() for k in HOUSE_TERMS + APARTMENT_TERMS):
        m = re.search(r"(?:площ)[^0-9]{0,20}([0-9][0-9\s.,]*)\s*(?:кв\.?\s*м|м2|m2)", text, re.I)
        if m:
            n = normalize_number(m.group(1))
            if n and 5 <= n <= 10000 and (land is None or abs(n-land) > 0.01):
                building = n
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


def categorize(text: str, area_sqm: float | None = None, land_area_sqm: float | None = None) -> str:
    """Classify conservatively using both wording and extracted geometry.

    A generic word such as "сграда" is not enough to call something a house.
    When a building is detected but its residential use is unclear, keep it in
    the explicit "Сграда + парцел" / "Друг недвижим имот" buckets.
    """
    tl = text.lower()
    has_house = any(k in tl for k in HOUSE_TERMS)
    has_apartment = any(k in tl for k in APARTMENT_TERMS)
    has_yard = any(k in tl for k in YARD_TERMS)
    has_land = any(k in tl for k in LAND_TERMS)
    has_agri = any(k in tl for k in AGRI_TERMS)
    generic_building = bool(re.search(r"\bсград[аи]\b|застроена\s+площ|\bрзп\b", tl, re.I)) or area_sqm is not None

    if has_house and (has_yard or has_land or land_area_sqm is not None):
        return "Къща + двор/парцел"
    if has_house:
        return "Къща/вила"
    if has_apartment:
        return "Апартамент"
    if generic_building and (has_yard or has_land or land_area_sqm is not None):
        return "Сграда + парцел"
    if has_yard:
        return "УПИ/дворно място"
    if has_agri:
        return "Земеделска земя"
    if has_land:
        return "Парцел/земя"
    if generic_building:
        return "Друг недвижим имот"
    return "Друг недвижим имот"


def classification_evidence(text: str, area_sqm: float | None, land_area_sqm: float | None) -> list[str]:
    tl = text.lower()
    out: list[str] = []
    if any(k in tl for k in HOUSE_TERMS): out.append("жилищни ключови думи")
    if any(k in tl for k in APARTMENT_TERMS): out.append("самостоятелен жилищен обект")
    if area_sqm is not None: out.append("извлечена застроена площ")
    if land_area_sqm is not None: out.append("извлечена площ на парцел")
    if any(k in tl for k in YARD_TERMS): out.append("двор/УПИ в документа")
    if any(k in tl for k in AGRI_TERMS): out.append("земеделско предназначение")
    return out

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




def extract_article_text(soup: BeautifulSoup, title_hint: str = "") -> str:
    """Return the most likely article body instead of the entire site chrome/navigation."""
    selectors = (
        "article", "main", ".article", ".article-content", ".post", ".post-content",
        ".news", ".news-content", ".content", "#content", ".page-content", ".entry-content"
    )
    candidates: list[str] = []
    for sel in selectors:
        for node in soup.select(sel):
            txt = clean(node.get_text(" ", strip=True))
            if len(txt) >= 80:
                candidates.append(txt)
    # Also inspect parent containers around a heading that resembles the listing title.
    hint_words = [w.lower() for w in re.findall(r"[А-Яа-яA-Za-z0-9]{5,}", title_hint)[:6]]
    for h in soup.find_all(["h1", "h2", "h3"]):
        ht = clean(h.get_text(" ", strip=True)).lower()
        if hint_words and sum(1 for w in hint_words if w in ht) >= min(2, len(hint_words)):
            node = h.parent
            for _ in range(3):
                if node is None:
                    break
                txt = clean(node.get_text(" ", strip=True))
                if len(txt) >= 80:
                    candidates.append(txt)
                node = node.parent
    if not candidates:
        return clean(soup.get_text(" ", strip=True))[:20000]

    def score(txt: str) -> tuple[int, int]:
        tl = txt.lower()
        signals = sum(1 for k in (SALE_TERMS + HOUSE_TERMS + APARTMENT_TERMS + LAND_TERMS + ("недвижим имот", "начална цена", "идентификатор")) if k in tl)
        nav_penalty = sum(1 for k in ("начало", "контакти", "карта на сайта", "обществени поръчки", "административни услуги") if k in tl)
        return (signals * 100 - nav_penalty * 20, min(len(txt), 20000))
    candidates.sort(key=score, reverse=True)
    return candidates[0][:30000]


def read_pdf_document(url: str, referer: str) -> tuple[str, int]:
    """Extract text from a PDF. Returns (text, page_count). Scanned/image-only PDFs return empty text."""
    try:
        r = fetch(url, tries=2, referer=referer)
        if len(r.content) > 15_000_000:
            return "", 0
        reader = PdfReader(io.BytesIO(r.content))
        chunks = []
        pages = min(len(reader.pages), 30)
        for page in reader.pages[:pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return clean(" ".join(chunks))[:70000], pages
    except Exception as exc:
        print(f"[warn] PDF skipped {url}: {exc}")
        return "", 0

def normalize_balchik_asset_url(page_url: str, href: str) -> str:
    """Normalize municipality attachment links.

    Balchik.bg frequently emits attachment hrefs such as `uploads/posts/...`
    without a leading slash. urljoin() would resolve those relative to the
    current article path, producing a non-existent /bg/.../uploads/... URL.
    Attachments live at the site root, so root-relative treatment is needed.
    """
    href = clean(href)
    if not href:
        return page_url
    if href.startswith("//"):
        return "https:" + href
    if re.match(r"^https?://", href, re.I):
        return href
    stripped = href.lstrip("./")
    if stripped.startswith(("uploads/", "files/", "userfiles/")):
        return "https://www.balchik.bg/" + stripped
    return normalize_balchik_url(page_url, href)


def read_pdf_text(url: str, referer: str) -> str:
    text, _ = read_pdf_document(url, referer)
    return text




def probe_url(url: str, referer: str | None = None) -> dict:
    """Fetch a URL once without raising, for diagnostics only."""
    headers = {"Referer": referer} if referer else None
    try:
        r = SESSION.get(url, headers=headers, timeout=25, allow_redirects=True)
        info = {
            "status": r.status_code,
            "final_url": r.url,
            "content_type": r.headers.get("Content-Type", ""),
            "length": len(r.content),
        }
        if "html" in info["content_type"].lower() or not info["content_type"]:
            try:
                soup = BeautifulSoup(r.text, "lxml")
                pdfs = []
                for a in soup.find_all("a", href=True):
                    h = normalize_balchik_asset_url(r.url, a["href"])
                    if urlparse(h).path.lower().endswith(".pdf") and h not in pdfs:
                        pdfs.append(h)
                info["pdfs"] = pdfs[:10]
                info["page_text_sample"] = clean(soup.get_text(" ", strip=True))[:500]
            except Exception as exc:
                info["parse_error"] = str(exc)
        return info
    except Exception as exc:
        return {"status": None, "final_url": url, "error": str(exc), "pdfs": []}


def scrape_balchik_detail(url: str, source: str, title_hint: str, locations: list[str]) -> Listing | None:
    soup = BeautifulSoup(fetch(url, referer="https://www.balchik.bg/").text, "lxml")
    page_text = clean(soup.get_text(" ", strip=True))
    article_text = extract_article_text(soup, title_hint)

    pdf_urls: list[str] = []
    pdf_texts: list[str] = []
    pdf_pages = 0
    for a in soup.find_all("a", href=True):
        href = normalize_balchik_asset_url(url, a["href"])
        if urlparse(href).path.lower().endswith(".pdf") and href not in pdf_urls:
            pdf_urls.append(href)
    for href in pdf_urls[:8]:
        txt, pages = read_pdf_document(href, url)
        pdf_pages += pages
        if txt:
            pdf_texts.append(txt)

    # PDF text is placed first because the official notice usually contains the
    # exact starting price, cadastral description, building area and ideal parts.
    structured_text = clean(" ".join(pdf_texts + [article_text, title_hint]))
    relevance_text = clean(f"{title_hint} {article_text[:8000]} {' '.join(pdf_texts)[:12000]}")
    # Avoid rejecting a valid property because unrelated navigation/footer text
    # happens to contain a reject term such as 'кандидати'.
    if not (is_relevant_sale(title_hint) or is_relevant_sale(relevance_text)):
        return None

    h = soup.find(["h1", "h2"])
    title = clean(h.get_text(" ", strip=True)) if h else clean(title_hint)
    if len(title) < 10:
        title = clean(title_hint)
    location = detect_location(structured_text, locations) or detect_location(title_hint, locations)
    price = extract_price(structured_text)
    area, land_area = extract_areas(structured_text)
    deadline = extract_deadline(structured_text) or extract_deadline(title_hint)
    published = first_match(page_text, (r"публикувано\s+на[:\s]*([0-3]?\d[.\-/][01]?\d[.\-/](?:20)?\d{2})",))
    category = categorize(structured_text, area, land_area)
    desc = structured_text[:9000]
    source_label = "PDF + страница" if pdf_texts else ("страница + PDF без извлечен текст" if pdf_urls else "страница")
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
        ideal_parts=is_ideal_parts(structured_text),
        published=published,
        document_count=len(pdf_urls),
        document_text_chars=sum(len(x) for x in pdf_texts),
        extraction_source=source_label,
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
        category=categorize(combined, extract_areas(combined)[0], extract_areas(combined)[1]),
        ideal_parts=is_ideal_parts(combined),
        extraction_source="индекс",
    )


def scrape_balchik_index(url: str, source: str, locations: list[str]) -> tuple[list[Listing], dict]:
    soup = BeautifulSoup(fetch(url, referer="https://www.balchik.bg/").text, "lxml")
    candidates: list[tuple[str, str, str, str]] = []
    seen_urls: set[str] = set()
    anchors_scanned = 0
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 15:
            continue
        anchors_scanned += 1
        raw_href = clean(a["href"])
        href = normalize_balchik_url(url, raw_href)
        if "balchik.bg" not in href or href in seen_urls:
            continue
        # Keep a compact piece of surrounding text; it often contains the publication date.
        parent_text = clean(a.parent.get_text(" ", strip=True)) if a.parent else title
        if not is_relevant_sale(title):
            tl = title.lower()
            if not ("продан" in tl or "продаж" in tl):
                continue
        seen_urls.add(href)
        candidates.append((raw_href, href, title, parent_text[:1200]))

    out: list[Listing] = []
    detail_ok = 0
    fallback_count = 0
    rejected_after_detail = 0
    trace: list[dict] = []
    for idx, (raw_href, href, title, nearby) in enumerate(candidates[:100], start=1):
        probe = probe_url(href, referer=url)
        pdfs = probe.get("pdfs", []) or []
        pdf_probes = []
        for pdf_url in pdfs[:3]:
            pinfo = probe_url(pdf_url, referer=probe.get("final_url") or href)
            pdf_probes.append({
                "url": pdf_url,
                "status": pinfo.get("status"),
                "final_url": pinfo.get("final_url"),
                "content_type": pinfo.get("content_type", ""),
            })
        row = {
            "n": idx,
            "title": title[:180],
            "raw_href": raw_href,
            "normalized_url": href,
            "status": probe.get("status"),
            "final_url": probe.get("final_url"),
            "content_type": probe.get("content_type", ""),
            "pdf_count": len(pdfs),
            "pdfs": pdf_probes,
            "probe_error": probe.get("error", ""),
        }
        trace.append(row)
        print(
            f"[trace] {source} #{idx} status={row['status']} pdfs={row['pdf_count']} "
            f"raw={raw_href} normalized={href} final={row['final_url']}"
        )
        for j, pp in enumerate(pdf_probes, start=1):
            print(f"[trace-pdf] {source} #{idx}.{j} status={pp['status']} url={pp['url']} final={pp['final_url']}")
        try:
            item = scrape_balchik_detail(href, source, title, locations)
            if item:
                out.append(item)
                detail_ok += 1
            else:
                # Detail pages can be thin shells whose useful content is in a
                # broken/moved attachment. Keep a relevant index-level listing
                # instead of silently throwing the property away.
                fallback = listing_from_index(title, href, source, locations, nearby)
                if fallback:
                    out.append(fallback)
                    fallback_count += 1
                else:
                    rejected_after_detail += 1
        except Exception as exc:
            # Do not lose a potentially useful property merely because the detail page is broken.
            fallback = listing_from_index(title, href, source, locations, nearby)
            if fallback:
                out.append(fallback)
                fallback_count += 1
            else:
                rejected_after_detail += 1
            print(f"[warn] Balchik detail fallback {href}: {exc}")

    stats = {
        "anchors_scanned": anchors_scanned,
        "index_candidates": len(candidates),
        "detail_ok": detail_ok,
        "fallback_from_index": fallback_count,
        "rejected_after_detail": rejected_after_detail,
        "returned": len(out),
        "pdf_documents": sum(x.document_count for x in out),
        "pdf_text_items": sum(1 for x in out if x.document_text_chars > 0),
        "prices_extracted": sum(1 for x in out if x.price_bgn is not None),
        "buildings_detected": sum(1 for x in out if x.area_sqm is not None or x.category.startswith("Къща")),
        "trace": trace,
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
        source="Камара на ЧСИ", title=title, location=location, price_bgn=price,
        area_sqm=area, land_area_sqm=land_area, deadline=deadline, url=url,
        description=description[:5000], category=categorize(combined, area, land_area),
        ideal_parts=is_ideal_parts(combined), extraction_source="страница на ЧСИ"
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


def web_matches(item: Listing, cfg: dict) -> bool:
    """Website inclusion must be permissive after source-level validation.

    Municipality listings have already passed sale/property checks in
    ``scrape_balchik_index`` / ``scrape_balchik_detail``. Re-running the global
    reject-keyword filter on the full PDF/article text can falsely hide valid
    sales because official documents or page chrome may mention words such as
    "наем", "кандидати" or other unrelated procedures.

    Therefore municipality items are retained once collected. The BCPEA source
    can cover a wider court area, so it still gets a lightweight location check.
    Price, category and minimum-area preferences belong to browser filters and
    email alerts, not to the website dataset.
    """
    if item.source.startswith("Община Балчик"):
        return True

    hay = f"{item.title} {item.location} {item.description}".lower()
    locations = [str(x).lower() for x in cfg.get("locations", [])]
    if locations and not any(x in hay for x in locations):
        return False
    return True


def alert_matches(item: Listing, cfg: dict) -> bool:
    """Strict email filter: notify only on plausible house/yard opportunities."""
    if not web_matches(item, cfg):
        return False
    max_price = float(cfg.get("max_price_bgn", 200000) or 200000)
    min_area = float(cfg.get("min_area_sqm", 0) or 0)
    if deadline_is_expired(item.deadline):
        return False
    if item.ideal_parts and bool(cfg.get("alert_exclude_ideal_parts", True)):
        return False
    allowed = set(cfg.get("alert_categories") or [
        "Къща + двор/парцел", "Къща/вила", "Сграда + парцел", "УПИ/дворно място"
    ])
    if item.category not in allowed:
        return False
    if item.price_bgn is not None and item.price_bgn > max_price:
        return False
    if item.price_bgn is None:
        if not bool(cfg.get("alert_allow_unknown_price", True)):
            return False
        if opportunity_score(item) < float(cfg.get("alert_unknown_price_min_score", 68) or 68):
            return False
    if item.area_sqm is not None and item.area_sqm < min_area and item.category in ("Къща + двор/парцел", "Къща/вила"):
        return False
    return True

def build_signals(item: Listing) -> list[str]:
    out: list[str] = []
    tl = f"{item.title} {item.description}".lower()
    has_building = bool(re.search(r"\bсград[аи]\b|застроена\s+площ|рзп|еднофамил|жилищна\s+сграда|къща|вила", tl, re.I))
    has_yard = any(k in tl for k in YARD_TERMS) or item.land_area_sqm is not None
    if has_building:
        out.append("Сграда открита")
    if has_yard and item.category != "Земеделска земя":
        out.append("Двор/УПИ засечен")
    if item.ideal_parts:
        out.append("Идеални части")
    if item.document_text_chars > 0:
        out.append("PDF прочетен")
    elif item.document_count > 0:
        out.append("PDF без извлечен текст")
    if item.price_bgn is None:
        out.append("Цена за проверка")
    else:
        out.append("Начална цена извлечена")
    if item.category.startswith("Къща") and item.land_area_sqm is None:
        out.append("Дворът не е извлечен")
    if item.category == "Земеделска земя":
        out.append("Земеделска земя")
    if deadline_is_expired(item.deadline):
        out.append("Изтекъл срок")
    elif not item.deadline:
        out.append("Срокът не е извлечен")
    if item.price_bgn is None or (not has_building and item.category == "Друг недвижим имот"):
        out.append("Документ за проверка")
    return out


def opportunity_score(item: Listing) -> int:
    # Heuristic prioritization, not a property valuation.
    score = 50
    if item.category == "Къща + двор/парцел": score += 24
    elif item.category == "Къща/вила": score += 18
    elif item.category == "Апартамент": score += 6
    elif item.category == "Сграда + парцел": score += 14
    elif item.category == "УПИ/дворно място": score += 10
    elif item.category == "Парцел/земя": score += 3
    elif item.category == "Земеделска земя": score -= 12
    if item.price_bgn is not None: score += 10
    if item.document_text_chars > 0: score += 5
    if item.land_area_sqm: score += 7
    if item.area_sqm: score += 4
    if item.deadline and not deadline_is_expired(item.deadline): score += 3
    if deadline_is_expired(item.deadline): score -= 35
    if item.ideal_parts: score -= 22
    return max(0, min(100, score))


def parse_deadline_date(value: str):
    value = clean(value)
    if not value:
        return None
    for pattern in (r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})",):
        m = re.search(pattern, value)
        if m:
            try:
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
            except ValueError:
                return None
    return None


def deadline_is_expired(value: str) -> bool:
    d = parse_deadline_date(value)
    if d is None:
        return False
    return d < datetime.now().date()


def deal_reasons(item: Listing, max_price: float = 200000) -> list[str]:
    reasons: list[str] = []
    if item.category in ("Къща + двор/парцел", "Къща/вила"):
        reasons.append("Жилищен имот")
    elif item.category in ("Сграда + парцел", "УПИ/дворно място"):
        reasons.append("Сграда/двор/УПИ")
    if item.price_bgn is not None and item.price_bgn <= max_price:
        reasons.append(f"До {max_price:,.0f} лв.".replace(",", " "))
    elif item.price_bgn is None and item.category in ("Къща + двор/парцел", "Къща/вила", "Сграда + парцел", "УПИ/дворно място"):
        reasons.append("Цена за бърза проверка")
    if not item.ideal_parts:
        reasons.append("Без засечени идеални части")
    if item.deadline and not deadline_is_expired(item.deadline):
        reasons.append("Срокът не е изтекъл")
    if item.document_text_chars > 0:
        reasons.append("Документът е прочетен")
    return reasons


def deal_candidate(item: Listing, max_price: float = 200000) -> bool:
    if deadline_is_expired(item.deadline):
        return False
    if item.ideal_parts:
        return False
    if item.category in ("Земеделска земя", "Апартамент", "Друг недвижим имот"):
        return False
    preferred = item.category in ("Къща + двор/парцел", "Къща/вила", "Сграда + парцел", "УПИ/дворно място")
    if not preferred:
        return False
    if item.price_bgn is not None:
        return item.price_bgn <= max_price
    # Unknown price is retained only for the strongest property types.
    return item.category in ("Къща + двор/парцел", "Къща/вила", "Сграда + парцел", "УПИ/дворно място") and opportunity_score(item) >= 68


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
    print("[version] Balchik Property Hunter V3.7 Deal Finder")
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
        errors.append("Камара на ЧСИ временно недостъпна")
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
                f"fallback={stats['fallback_from_index']} върнати={stats['returned']} "
                f"pdf={stats.get('pdf_documents',0)} pdf_text={stats.get('pdf_text_items',0)} "
                f"цени={stats.get('prices_extracted',0)} сгради={stats.get('buildings_detected',0)}"
            )
            all_items.extend(got)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            diagnostics[name] = {"error": str(exc)}
            print(f"[warn] {name}: {exc}")

    uniq = {x.uid: x for x in all_items}
    before_filters = len(uniq)
    current = [x for x in uniq.values() if web_matches(x, cfg)]
    current.sort(key=lambda x: (-opportunity_score(x), x.price_bgn is None, x.price_bgn or 10**18, x.location, x.title))
    alert_pool = [x for x in current if alert_matches(x, cfg)]
    diagnostics["summary"] = {
        "unique_before_filters": before_filters,
        "shown_after_filters": len(current),
        "alert_candidates": len(alert_pool),
        "deal_candidates": sum(1 for x in current if deal_candidate(x, float(cfg.get("max_price_bgn", 200000) or 200000))),
        "expired": sum(1 for x in current if deadline_is_expired(x.deadline)),
        "prices": sum(1 for x in current if x.price_bgn is not None),
        "houses": sum(1 for x in current if x.category.startswith("Къща")),
        "apartments": sum(1 for x in current if x.category == "Апартамент"),
        "buildings": sum(1 for x in current if x.category == "Сграда + парцел"),
        "yards": sum(1 for x in current if x.category == "УПИ/дворно място"),
        "land": sum(1 for x in current if x.category == "Парцел/земя"),
        "agri": sum(1 for x in current if x.category == "Земеделска земя"),
        "other": sum(1 for x in current if x.category == "Друг недвижим имот"),
    }
    save_public(current, errors, diagnostics)

    seen = load_seen()
    first_run = not SEEN_FILE.exists() or not seen
    new_items = [x for x in alert_pool if x.uid not in seen]
    should_send_existing = bool(cfg.get("send_existing_on_first_run", False))
    to_alert = new_items if (not first_run or should_send_existing) else []

    if to_alert:
        send_email(to_alert)
        print(f"[ok] notification candidates: {len(to_alert)}")
    elif first_run and new_items:
        print("[info] First run: current items stored as baseline; no backlog email sent.")

    save_seen(seen | {x.uid for x in alert_pool})
    print(f"[diag] unique={before_filters} shown={len(current)} deals={diagnostics['summary']['deal_candidates']} expired={diagnostics['summary']['expired']} alerts={len(alert_pool)} prices={diagnostics['summary']['prices']} houses={diagnostics['summary']['houses']} apartments={diagnostics['summary']['apartments']} buildings={diagnostics['summary']['buildings']} yards={diagnostics['summary']['yards']} land={diagnostics['summary']['land']} agri={diagnostics['summary']['agri']} other={diagnostics['summary']['other']}")
    print(f"[done] website={len(current)} alert_pool={len(alert_pool)} new_alerts={len(new_items)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
