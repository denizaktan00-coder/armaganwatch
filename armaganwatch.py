import os
import sys
import json
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ============== ENV & SABİTLER ==============

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, ".env")

print("DEBUG[ARM]: .env yolu:", env_path, "Var mı?", os.path.exists(env_path))
if os.path.exists(env_path):
    load_dotenv(env_path, override=True)

BOT_TOKEN = os.getenv("ARMAGAN_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("ARMAGAN_TELEGRAM_CHAT_ID")

print("DEBUG[ARM]: BOT_TOKEN boş mu?", BOT_TOKEN is None or BOT_TOKEN == "")
print("DEBUG[ARM]: CHAT_ID:", repr(CHAT_ID))

if not BOT_TOKEN or not CHAT_ID:
    print("Hata: ARMAGAN_TELEGRAM_BOT_TOKEN veya ARMAGAN_TELEGRAM_CHAT_ID eksik.", file=sys.stderr)
    sys.exit(1)

SEEN_FILE = os.path.join(BASE_DIR, "seen_armagan.json")
REQUEST_TIMEOUT = 15
BASE_URL = "https://www.armaganoyuncak.com.tr"

# Takip edilecek kategori URL'leri (.env'den)
SOURCES = [
    ("Hot Wheels", os.getenv("ARMAGAN_URL_HOTWHEELS")),
    ("Matchbox", os.getenv("ARMAGAN_URL_MATCHBOX")),
    ("LEGO", os.getenv("ARMAGAN_URL_LEGO")),
    ("Majorette", os.getenv("ARMAGAN_URL_MAJORETTE")),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ArmaganWatchBot/1.0)",
}

# ============== GENEL YARDIMCI ==============

def load_seen():
    if not os.path.exists(SEEN_FILE) or os.path.getsize(SEEN_FILE) == 0:
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_seen(data):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(text: str, image_url: str | None = None):
    """
    image_url varsa sendPhoto, yoksa uzun metni 4000'lik parçalara bölüp sendMessage.
    """
    if image_url:
        img = image_url.strip()
        if not img.startswith("http"):
            img = BASE_URL + "/" + img.lstrip("/")
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": CHAT_ID,
            "photo": img,
            "caption": text,
            "parse_mode": "HTML",
        }
        try:
            r = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
            print("DEBUG[ARM]: sendPhoto status:", r.status_code)
            if r.status_code != 200:
                print("DEBUG[ARM]: sendPhoto response:", r.text, file=sys.stderr)
        except Exception as e:
            print("DEBUG[ARM]: sendPhoto hata:", e, file=sys.stderr)
        return

    MAX_LEN = 4000
    remaining = text

    while remaining:
        chunk = remaining[:MAX_LEN]
        if len(remaining) > MAX_LEN:
            nl = chunk.rfind("\n")
            if nl > 0:
                chunk = chunk[:nl]

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        try:
            r = requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
            print("DEBUG[ARM]: sendMessage status:", r.status_code)
            if r.status_code != 200:
                print("DEBUG[ARM]: sendMessage response:", r.text, file=sys.stderr)
        except Exception as e:
            print("DEBUG[ARM]: sendMessage hata:", e, file=sys.stderr)

        remaining = remaining[len(chunk):].lstrip()

def to_float(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = "".join(ch for ch in str(val) if ch.isdigit() or ch in ",.")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def detect_stock_state(text: str):
    t = text.lower()
    if any(x in t for x in ["tükendi", "stokta yok", "stokta bulunmamaktadır", "tukendi"]):
        return {"in_stock": False, "qty": None}

    m = re.search(r"son\s+(\d+)\s*adet", t)
    if m:
        try:
            return {"in_stock": True, "qty": int(m.group(1))}
        except Exception:
            pass

    if any(x in t for x in ["sepete ekle", "hemen al", "stokta"]):
        return {"in_stock": True, "qty": None}

    return {"in_stock": True, "qty": None}

def is_stock_drop(prev, now):
    if not prev:
        return False

    o_in, o_q = prev.get("in_stock"), prev.get("qty")
    n_in, n_q = now.get("in_stock"), now.get("qty")

    if o_in == n_in and (o_q == n_q or (o_q is None and n_q is None)):
        return False

    if o_in and not n_in:
        return True

    if o_in and n_in and o_q is not None and n_q is not None and n_q < o_q:
        return True

    return False

def extract_image_url(card) -> str | None:
    img = card.select_one("img")
    if not img:
        return None
    src = img.get("data-src") or img.get("src") or ""
    if not src:
        return None
    if not src.startswith("http"):
        src = BASE_URL + "/" + src.lstrip("/")
    return src

# ============== HTML SCRAPER ==============

def scrape_category(brand_name: str, url: str, max_pages: int = 10):
    if not url:
        print(f"[Armagan] {brand_name}: URL yok, atlanıyor.")
        return []

    all_products = []
    seen_links = set()
    page_url = url
    page = 1

    while page_url and page <= max_pages:
        print(f"[Armagan] {brand_name}: Taranıyor: {page_url} (sayfa {page})")
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"[Armagan] {brand_name}: istek hatası: {e}", file=sys.stderr)
            break

        soup = BeautifulSoup(r.text, "html.parser")

        cards = soup.select(
            "div.product, div.product-item, div.product-box, "
            "li.product, li.product-item"
        )
        print(f"[Armagan] {brand_name}: {len(cards)} ürün kartı bulundu.")

        for card in cards:
            a = card.select_one("a[href]")
            if not a:
                continue

            href = (a.get("href") or "").strip()
            if not href:
                continue

            if not href.startswith("http"):
                href = BASE_URL + "/" + href.lstrip("/")
            link = href

            if link in seen_links:
                continue
            seen_links.add(link)

            name_el = card.select_one(".product-name, .name, h2, h3") or a
            name = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue

            image_url = extract_image_url(card)

            new_el = card.select_one(".new-price, .price, .current, .urunFiyat, .discounted")
            old_el = card.select_one(".old-price, .line-through, .eskiFiyat, .strike")

            price = to_float(new_el.get_text()) if new_el else None
            old_price = to_float(old_el.get_text()) if old_el else None

            stock_state = detect_stock_state(card.get_text(" ", strip=True))

            pid = f"{brand_name}:{link}"

            all_products.append(
                {
                    "id": pid,
                    "brand": brand_name,
                    "name": name,
                    "link": link,
                    "in_stock": stock_state["in_stock"],
                    "qty": stock_state["qty"],
                    "price": price,
                    "old_price": old_price,
                    "image_url": image_url,
                }
            )

        # Pagination: sonraki sayfayı bul
        next_link = (
            soup.select_one("ul.pagination a[rel='next'], .pagination a[rel='next']")
            or soup.select_one("ul.pagination a.next, .pagination a.next")
            or soup.select_one("a[aria-label='Sonraki'], a[aria-label='Next']")
        )

        if not next_link:
            active = soup.select_one(".pagination li.active, .pagination .current")
            if active:
                sib = active.find_next_sibling("li")
                if sib:
                    a_sib = sib.find("a", href=True)
                    if a_sib:
                        next_link = a_sib

        if next_link and next_link.get("href"):
            href = next_link.get("href").strip()
            if not href.startswith("http"):
                href = BASE_URL + "/" + href.lstrip("/")
            page_url = href
            page += 1
        else:
            break

    print(f"[Armagan] {brand_name}: TOPLAM {len(all_products)} ürün toplandı.")
    return all_products

def fetch_all_products():
    all_products = []
    for brand_name, url in SOURCES:
        if url:
            all_products.extend(scrape_category(brand_name, url))
    print(f"[Armagan] GENEL TOPLAM {len(all_products)} ürün.")
    return all_products

# ============== MAIN ==============

def main():
    print(">>> ArmaganWatch başladı.")
    seen = load_seen()
    products = fetch_all_products()

    current_ids = set()
    changed = False
    new_items = []
    drops = []

    for p in products:
        pid = p["id"]
        current_ids.add(pid)

        now_state = {
            "in_stock": p["in_stock"],
            "qty": p["qty"],
        }

        prev = seen.get(pid)

        # Yeni ürün
        if prev is None:
            seen[pid] = {
                "brand": p["brand"],
                "name": p["name"],
                "link": p["link"],
                "in_stock": p["in_stock"],
                "qty": p["qty"],
                "price": p["price"],
                "old_price": p["old_price"],
                "image_url": p.get("image_url"),
            }
            new_items.append(p)
            changed = True
            continue

        prev_state = {
            "in_stock": prev.get("in_stock"),
            "qty": prev.get("qty"),
        }

        if is_stock_drop(prev_state, now_state):
            drops.append((p, prev_state, now_state))

        # Güncelle
        updated = False
        for key in ["brand", "name", "link", "in_stock", "qty", "price", "old_price", "image_url"]:
            new_val = p.get(key)
            if key in ["in_stock", "qty"]:
                new_val = now_state[key]
            if prev.get(key) != new_val:
                prev[key] = new_val
                updated = True

        if updated:
            seen[pid] = prev
            changed = True

    # Artık olmayan ürünleri sil
    for pid in list(seen.keys()):
        if pid not in current_ids:
            seen.pop(pid, None)
            changed = True

    # ========= Bildirimler =========

    # Yeni ürünler
    for p in new_items[:80]:
        stok = ""
        if not p["in_stock"]:
            stok = " (STOK YOK)"
        elif p["qty"] is not None:
            stok = f" (Son {p['qty']} adet)"
        fiyat = f"\nFiyat: {p['price']} TL" if p["price"] else ""
        text = (
            f"🆕 Armağan Yeni Ürün\n"
            f"{p['brand']} - {p['name']}{stok}{fiyat}\n"
            f"{p['link']}"
        )
        send_telegram(text, p.get("image_url"))
    if new_items:
        print(f"[Armagan] {len(new_items)} yeni ürün bildirildi.")

    # Stok düşüşü / tükenme
    for p, old_s, new_s in drops[:120]:
        if old_s["in_stock"] and not new_s["in_stock"]:
            durum = "❌ Stok bitti"
        elif (
            old_s["in_stock"]
            and new_s["in_stock"]
            and old_s["qty"] is not None
            and new_s["qty"] is not None
            and new_s["qty"] < old_s["qty"]
        ):
            durum = f"⚠️ Stok azaldı: {old_s['qty']} → {new_s['qty']} adet"
        else:
            continue

        fiyat = f"\nFiyat: {p['price']} TL" if p["price"] else ""
        text = (
            f"📉 Armağan Stok Değişimi\n"
            f"{p['brand']} - {p['name']}\n"
            f"{durum}{fiyat}\n"
            f"{p['link']}"
        )
        send_telegram(text, p.get("image_url"))
    if drops:
        print(f"[Armagan] {len(drops)} stok düşüşü bildirildi.")

    if changed:
        save_seen(seen)
        print("[Armagan] seen_armagan.json güncellendi.")
    else:
        print("[Armagan] Değişiklik yok, veri aynı.")

    print(">>> ArmaganWatch bitti.")

# ============== RUN ==============

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Hata:", e, file=sys.stderr)
        sys.exit(1)