#!/usr/bin/env python3
"""Vinted Pokémon card monitor with Telegram alerts."""

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

if not TELEGRAM_TOKEN or not CHAT_ID:
    sys.exit("Erreur : TELEGRAM_TOKEN et CHAT_ID doivent être définis dans le fichier .env")
CHECK_INTERVAL = 180  # 3 minutes

SEEN_IDS_FILE = Path("seen_ids.json")
KEYWORDS_FILE = Path("keywords.txt")

VINTED_BASE_URL = "https://www.vinted.fr"
VINTED_API_URL = f"{VINTED_BASE_URL}/api/v2/catalog/items"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.vinted.fr/",
    "Origin": "https://www.vinted.fr",
    "DNT": "1",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Persistence ───────────────────────────────────────────────────────────────
def load_seen_ids() -> set:
    if SEEN_IDS_FILE.exists():
        return set(json.loads(SEEN_IDS_FILE.read_text()))
    return set()


def save_seen_ids(ids: set) -> None:
    SEEN_IDS_FILE.write_text(json.dumps(list(ids)))


def load_keywords() -> list[str]:
    if not KEYWORDS_FILE.exists():
        log.error(f"Fichier {KEYWORDS_FILE} introuvable.")
        return []
    return [
        line.strip()
        for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ── Vinted scraping ───────────────────────────────────────────────────────────
class VintedScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._refresh_session()

    def _refresh_session(self):
        """Visit Vinted search page to obtain fresh session cookies."""
        try:
            # Homepage first, then a search page to mimic real browser navigation
            self.session.get(VINTED_BASE_URL, timeout=10)
            time.sleep(random.uniform(1.0, 2.5))
            self.session.get(
                f"{VINTED_BASE_URL}/catalog",
                params={"search_text": "pokemon"},
                timeout=10,
            )
            log.info("Session Vinted initialisée.")
        except Exception as e:
            log.warning(f"Impossible d'initialiser la session : {e}")

    def search(self, keyword: str) -> list[dict]:
        # Random delay to mimic human browsing rhythm
        time.sleep(random.uniform(2.0, 5.0))

        params = {
            "search_text": keyword,
            "per_page": 20,
            "page": 1,
            "order": "newest_first",
        }

        for attempt in range(2):
            try:
                resp = self.session.get(VINTED_API_URL, params=params, timeout=15)
                resp.raise_for_status()

                if not resp.content:
                    log.warning(f"Réponse vide pour « {keyword} » (tentative {attempt + 1}/2)")
                    self._refresh_session()
                    time.sleep(random.uniform(4.0, 8.0))
                    continue

                content_type = resp.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    log.warning(
                        f"Vinted a renvoyé du HTML pour « {keyword} » "
                        f"(bot détecté ?), renouvellement de session…"
                    )
                    self._refresh_session()
                    time.sleep(random.uniform(8.0, 15.0))
                    continue

                return resp.json().get("items", [])

            except json.JSONDecodeError:
                preview = resp.text[:300] if resp.text else "(vide)"
                log.warning(
                    f"JSON invalide pour « {keyword} » (tentative {attempt + 1}/2). "
                    f"Début de la réponse : {preview!r}"
                )
                self._refresh_session()
                time.sleep(random.uniform(6.0, 12.0))

            except requests.HTTPError as e:
                status = e.response.status_code if e.response else "?"
                if status in (401, 403):
                    log.warning(f"Session rejetée (HTTP {status}), renouvellement…")
                    self._refresh_session()
                    time.sleep(random.uniform(4.0, 8.0))
                else:
                    log.error(f"Erreur HTTP {status} pour « {keyword} »")
                    break

            except Exception as e:
                log.error(f"Erreur inattendue pour « {keyword} » : {e}")
                break

        return []


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")
        return False


def parse_price(item: dict) -> str:
    """Handle both string and object price formats from Vinted API."""
    price = item.get("price")
    if isinstance(price, dict):
        amount = price.get("amount", "?")
        currency = price.get("currency_code", "€")
    else:
        amount = price or "?"
        currency = item.get("currency", "€")
    return f"{amount} {currency}"


def parse_url(item: dict) -> str:
    url = item.get("url", "")
    if url and not url.startswith("http"):
        return f"{VINTED_BASE_URL}{url}"
    return url or f"{VINTED_BASE_URL}/items/{item.get('id', '')}"


def format_message(item: dict, keyword: str) -> str:
    title = item.get("title", "—")
    price_str = parse_price(item)
    status = item.get("status", "—")
    url = parse_url(item)

    return (
        f"🃏 <b>{title}</b>\n"
        f"💰 Prix : <b>{price_str}</b>\n"
        f"✨ État : {status}\n"
        f"🔍 Recherche : <i>{keyword}</i>\n"
        f'🔗 <a href="{url}">Voir l\'annonce</a>'
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=== Pokémon Vinted Alert Bot démarré ===")
    scraper = VintedScraper()
    seen_ids = load_seen_ids()

    send_telegram("✅ <b>Bot Pokémon Vinted démarré !</b>\nSurveillance en cours…")

    while True:
        keywords = load_keywords()
        if not keywords:
            log.warning("Aucun mot-clé trouvé dans keywords.txt, nouvelle tentative dans 60s.")
            time.sleep(60)
            continue

        log.info(f"Vérification de {len(keywords)} mot(s)-clé(s)…")
        new_count = 0

        for keyword in keywords:
            items = scraper.search(keyword)
            for item in items:
                item_id = str(item.get("id", ""))
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                msg = format_message(item, keyword)
                if send_telegram(msg):
                    log.info(f"  → Alerte : {item.get('title', item_id)}")
                    new_count += 1
                time.sleep(0.5)

        save_seen_ids(seen_ids)
        log.info(
            f"{new_count} nouvelle(s) annonce(s). "
            f"Prochaine vérif dans {CHECK_INTERVAL // 60} min."
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
