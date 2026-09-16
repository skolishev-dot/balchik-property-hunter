from __future__ import annotations

import hashlib
import html
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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
DATA_FILE = BASE / "data" / "listings.json"
SEEN_FILE = BASE / "state" / "seen.json"
UA = "BalchikPropertyHunterWeb/1.0 (+personal public-property monitoring)"

BCPEA_LIST = "https://sales.bcpea.org/properties?court=8&perpage=100"
BALCHIK_CSI = "https://www.balchik.bg/bg/obyavleniya-chsi-i-sinditsi/2026-godina/"
BALCHIK_AUCTIONS = "https://www.balchik.bg/bg/targove-i-konkursi/2026-g"


@dataclass(frozen=True)
class Listing:
    source: str
    title: str
    location: str
    price_bgn: float | None
    area_sqm: float | None
    deadline: str
    url: str
    description: str = ""

    @property
    def uid(self) -> str:
        raw = f"{self.source}|{self.url}|{self.title}|{self.location}|{self.price_bgn}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def public_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        if self.price_bgn and self.area_sqm:
            d["price_per_sqm"] = round(self.price_bgn / self.area_sqm, 2)
        else:
            d["price_per_sqm"] = None
        return d


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def num_bg(text: str) -> float | None:
    if not text:
        return None
    s = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def fetch(url: str, tries: int = 3) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
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


def parse_bcpea_detail(url: str) -> Listing | None:
    soup = BeautifulSoup(fetch(url).text, "lxml")
    text = clean(soup.get_text(" ", strip=True))
    title = ""
    h = soup.find(["h1", "h2"])
    if h:
        title = clean(h.get_text(" ", strip=True))
    if not title or title.lower() == "имоти":
        m = re.search(r"(?:Имоти\s+)?(.{2,80}?)\s+Публикувано на", text, re.I)
        title = clean(m.group(1)) if m else "Имот от ЧСИ"

    location = text_after_label(text, "НАСЕЛЕНО МЯСТО", ["Адрес", "ОКРЪЖЕН СЪД", "ЧАСТЕН СЪДЕБЕН ИЗПЪЛНИТЕЛ"])
    price_bgn = None
    m_bgn = re.search(r"Начална цена.*?([\d\s.,]+)\s*лв", text, re.I)
    if m_bgn:
        price_bgn = num_bg(m_bgn.group(1))

    area = None
    m_area = re.search(r"ПЛОЩ\s*([\d\s.,]+)\s*кв\.?(?:м|м\.)", text, re.I)
    if m_area:
        area = num_bg(m_area.group(1))

    deadline = ""
    m_dead = re.search(r"СРОК\s*от\s*([0-9.]+)\s*до\s*([0-9.]+)", text, re.I)
    if m_dead:
        deadline = f"{m_dead.group(1)} – {m_dead.group(2)}"

    description = text_after_label(text, "ОПИСАНИЕ", ["РЕГ. № ЧСИ", "Адрес Окръжен съд"])
    return Listing("Камара на ЧСИ", title, location, price_bgn, area, deadline, url, description[:1800])


def scrape_bcpea() -> list[Listing]:
    soup = BeautifulSoup(fetch(BCPEA_LIST).text, "lxml")
    links: list[str] = []
    for a in soup.select('a[href*="/properties/"]'):
        href = a.get("href", "")
        if re.search(r"/properties/\d+", href):
            u = urljoin(BCPEA_LIST, href)
            if u not in links:
                links.append(u)
    out: list[Listing] = []
    for u in links:
        try:
            item = parse_bcpea_detail(u)
            if item:
                out.append(item)
        except Exception as exc:
            print(f"[warn] BCPEA detail skipped {u}: {exc}")
    return out


def scrape_balchik_index(url: str, source: str, locations: list[str]) -> list[Listing]:
    soup = BeautifulSoup(fetch(url).text, "lxml")
    out: list[Listing] = []
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 12:
            continue
        tl = title.lower()
        relevant = any(k in tl for k in ("продан", "продаж", "търг", "имот", "чси", "ликвидатор", "синдик"))
        if not relevant:
            continue
        href = urljoin(url, a["href"])
        if "balchik.bg" not in href or href in seen_urls:
            continue
        seen_urls.add(href)
        loc = next((name for name in locations if name.lower() in tl), "")
        out.append(Listing(source, title, loc, None, None, "", href, ""))
    return out


def matches(item: Listing, cfg: dict) -> bool:
    max_price = float(cfg.get("max_price_bgn", 200000) or 200000)
    min_area = float(cfg.get("min_area_sqm", 0) or 0)
    if item.price_bgn is not None and item.price_bgn > max_price:
        return False
    if item.area_sqm is not None and item.area_sqm < min_area:
        return False

    hay = f"{item.title} {item.location} {item.description}".lower()
    locations = [str(x).lower() for x in cfg.get("locations", [])]
    if locations and not any(x in hay for x in locations):
        return False

    if item.source.startswith("Община Балчик"):
        return True

    kws = [str(x).lower() for x in cfg.get("property_keywords", [])]
    return not kws or any(k in hay for k in kws)


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


def save_public(items: list[Listing]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": [x.public_dict() for x in items],
    }
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def send_email(items: list[Listing]) -> None:
    recipient = os.getenv("ALERT_EMAIL_TO", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    if not recipient or not user or not password:
        print("[info] Email secrets are not configured; skipping email.")
        return

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = f"Балчик: {len(items)} нови имотни обяви"

    lines = []
    rows = []
    for x in items:
        price = f"{x.price_bgn:,.0f} лв.".replace(",", " ") if x.price_bgn else "—"
        area = f"{x.area_sqm:,.0f} кв.м".replace(",", " ") if x.area_sqm else "—"
        lines.append(f"[{x.source}] {x.title}\nМясто: {x.location or '—'}\nЦена: {price}\nПлощ: {area}\n{x.url}")
        rows.append(
            "<tr>"
            f"<td>{html.escape(x.source)}</td><td>{html.escape(x.title)}</td>"
            f"<td>{html.escape(x.location or '—')}</td><td>{price}</td><td>{area}</td>"
            f"<td><a href=\"{html.escape(x.url)}\">Отвори</a></td></tr>"
        )
    msg.set_content("\n\n---\n\n".join(lines))
    msg.add_alternative(
        "<html><body><h2>Нови попадения за Балчик</h2>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th>Източник</th><th>Имот</th><th>Място</th><th>Цена</th><th>Площ</th><th>Линк</th></tr>"
        + "".join(rows)
        + "</table><p><small>Автоматично известие. Проверявай тежести, идеални части, владение и документите по делото преди участие.</small></p></body></html>",
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

    sources = [
        ("Камара на ЧСИ", scrape_bcpea),
        ("Община Балчик / ЧСИ", lambda: scrape_balchik_index(BALCHIK_CSI, "Община Балчик / ЧСИ и синдици", locations)),
        ("Община Балчик / търгове", lambda: scrape_balchik_index(BALCHIK_AUCTIONS, "Община Балчик / търгове", locations)),
    ]
    for name, fn in sources:
        try:
            got = fn()
            print(f"[ok] {name}: {len(got)} прочетени")
            all_items.extend(got)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"[warn] {name}: {exc}")

    uniq = {x.uid: x for x in all_items}
    current = [x for x in uniq.values() if matches(x, cfg)]
    current.sort(key=lambda x: (x.price_bgn is None, x.price_bgn or 10**18, x.location, x.title))
    save_public(current)

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
    print(f"[done] matches={len(current)} new={len(new_items)} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
