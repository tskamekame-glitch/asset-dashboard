import io
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import pdfplumber
import requests
from supabase import create_client


JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


def get_latest_pdf_url():
    r = requests.get(
        JPX_PAGE,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 JPX-Margin-Updater/1.0"
        },
    )
    r.raise_for_status()

    links = re.findall(
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r.text,
        flags=re.I,
    )

    urls = [
        urljoin(JPX_PAGE, x.replace("&amp;", "&"))
        for x in links
    ]

    candidates = [
        u for u in urls
        if "/statistics-equities/margin/" in u
        and "-att/" in u
    ]

    if not candidates:
        raise RuntimeError(
            "JPXページから信用取引残高PDFを検出できませんでした"
        )

    return candidates[0]


def extract_text(pdf_bytes):
    pages = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(
                x_tolerance=2,
                y_tolerance=2
            )
            if text:
                pages.append(text)

    return "\n".join(pages)


def find_date(text):
    patterns = [
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
        r"(\d{4})/(\d{1,2})/(\d{1,2})",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return (
                f"{m.group(1)}-"
                f"{int(m.group(2)):02d}-"
                f"{int(m.group(3)):02d}"
            )

    raise RuntimeError("JPX PDFの日付を取得できませんでした")


def parse_number(value):
    if value is None:
        return None

    value = str(value).strip()

    negative = "▲" in value or value.startswith("-")

    value = re.sub(r"[^\d]", "", value)

    if not value:
        return None

    number = int(value)

    return -number if negative else number


def parse_rows(text, date, pdf_url):
    rows = []

    for line in text.splitlines():
        line = " ".join(line.split())

        m = re.match(
            r"^(\d{4}|[0-9]{3}[A-Z])\s+",
            line
        )

        if not m:
            continue

        code = m.group(1)

        numbers = re.findall(
            r"(?:▲|-)?\d{1,3}(?:,\d{3})+",
            line
        )

        if len(numbers) < 3:
            continue

        sell = parse_number(numbers[0])

        # JPX表では売残・前日比・買残・前日比…
        buy = parse_number(numbers[2])

        if sell is None or buy is None:
            continue

        if sell < 0 or buy < 0:
            continue

        rows.append(
            {
                "code": code,
                "date": date,
                "buy": buy,
                "sell": sell,
                "source_url": pdf_url,
                "fetched_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )

    # 同一コードを重複登録しない
    unique = {}

    for row in rows:
        unique[row["code"]] = row

    return list(unique.values())


def main():
    print("JPX信用残データ取得開始")

    pdf_url = get_latest_pdf_url()
    print("PDF:", pdf_url)

    r = requests.get(
        pdf_url,
        timeout=60,
        headers={
            "User-Agent": "Mozilla/5.0 JPX-Margin-Updater/1.0",
            "Referer": JPX_PAGE,
        },
    )
    r.raise_for_status()

    text = extract_text(r.content)

    if not text:
        raise RuntimeError(
            "JPX PDFからテキストを抽出できませんでした"
        )

    date = find_date(text)
    print("公表日:", date)

    rows = parse_rows(text, date, pdf_url)

    print("解析銘柄数:", len(rows))

    # JPXのPDF形式変更・誤解析時にDBを壊さないための安全装置
    if len(rows) < 1000:
        raise RuntimeError(
            f"解析銘柄数が少なすぎます ({len(rows)}件)。"
            "JPX PDF形式変更の可能性があるため更新を中止しました。"
        )

    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY
    )

    batch_size = 500

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]

        supabase.table(
            "jpx_margin_data"
        ).upsert(
            batch,
            on_conflict="code,date"
        ).execute()

        print(
            f"Supabase保存: "
            f"{i + 1}〜{min(i + batch_size, len(rows))}"
        )

    print("JPX信用残更新完了")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", str(e), file=sys.stderr)
        raise
