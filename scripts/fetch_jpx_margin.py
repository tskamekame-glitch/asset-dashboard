import io
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import pdfplumber
import requests
from supabase import create_client


JPX_PAGE = (
    "https://www.jpx.co.jp/markets/"
    "statistics-equities/margin/01.html"
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ[
    "SUPABASE_SERVICE_ROLE_KEY"
]


def get_latest_pdf_url():
    r = requests.get(
        JPX_PAGE,
        timeout=30,
        headers={
            "User-Agent":
                "Mozilla/5.0 JPX-Margin-Updater/2.0"
        },
    )
    r.raise_for_status()

    links = re.findall(
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r.text,
        flags=re.I,
    )

    urls = [
        urljoin(
            JPX_PAGE,
            x.replace("&amp;", "&")
        )
        for x in links
    ]

    # 現行の日次全銘柄ファイルを優先
    candidates = [
        u for u in urls
        if re.search(
            r"/\d{8}_mtall\.pdf(?:\?|$)",
            u,
            flags=re.I,
        )
    ]

    if not candidates:
        candidates = [
            u for u in urls
            if "/statistics-equities/margin/" in u
            and "-att/" in u
        ]

    if not candidates:
        raise RuntimeError(
            "JPXページから銘柄別信用取引残高PDFを"
            "検出できませんでした"
        )

    return candidates[0]


def normalize_cell(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def parse_number(value):
    value = normalize_cell(value)

    if not value:
        return None

    negative = (
        "▲" in value
        or value.startswith("-")
    )

    digits = re.sub(r"[^\d]", "", value)

    if not digits:
        return None

    number = int(digits)

    return -number if negative else number


def find_date_from_pdf(pdf, pdf_url):
    # ファイル名を最優先
    m = re.search(
        r"(\d{4})(\d{2})(\d{2})_mtall\.pdf",
        pdf_url,
        flags=re.I,
    )

    if m:
        return (
            f"{m.group(1)}-"
            f"{m.group(2)}-"
            f"{m.group(3)}"
        )

    # 念のため1ページ目からも取得
    text = pdf.pages[0].extract_text() or ""

    patterns = [
        r"(\d{4})/(\d{1,2})/(\d{1,2})",
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
    ]

    for pattern in patterns:
        m = re.search(pattern, text)

        if m:
            return (
                f"{m.group(1)}-"
                f"{int(m.group(2)):02d}-"
                f"{int(m.group(3)):02d}"
            )

    raise RuntimeError(
        "JPX PDFの日付を取得できませんでした"
    )


def is_code(value):
    value = normalize_cell(value).upper()

    return bool(
        re.fullmatch(
            r"(?:\d{4}|\d{3}[A-Z])",
            value
        )
    )


def find_code_index(row):
    """
    現行JPX表ではコード列は概ね5列目前後。
    表抽出時の列ズレにも対応するため、
    先頭側から証券コード候補を探す。
    """
    for i, cell in enumerate(row[:8]):
        value = normalize_cell(cell).upper()

        if is_code(value):
            return i

    return None


def extract_tables(pdf):
    """
    JPX PDFは罫線付き表。
    まず lines 方式で表を抽出し、
    失敗ページだけ text 方式を試す。
    """
    for page in pdf.pages:

        tables = page.extract_tables(
            {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "intersection_tolerance": 5,
                "snap_tolerance": 3,
                "join_tolerance": 3,
                "edge_min_length": 3,
                "text_tolerance": 3,
            }
        )

        if not tables:
            tables = page.extract_tables(
                {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "text_tolerance": 3,
                }
            )

        for table in tables or []:
            yield table


def parse_table_row(row, date, pdf_url):
    if not row:
        return None

    cells = [
        normalize_cell(x)
        for x in row
    ]

    code_index = find_code_index(cells)

    if code_index is None:
        return None

    code = cells[code_index].upper()

    # 「小計」の2724銘柄等を誤ってコード扱いしない
    joined = " ".join(cells[:code_index + 2])

    if (
        "小計" in joined
        or "sub-total" in joined.lower()
        or "銘柄" == normalize_cell(
            cells[code_index + 1]
            if code_index + 1 < len(cells)
            else ""
        )
    ):
        return None

    #
    # 現行JPX PDF:
    #
    # code
    # new security code
    # total outstanding sales
    # daily change
    # ratio
    # total outstanding purchases
    # daily change
    # ratio
    # ...
    #
    # したがってコード列より後方の
    # 「売残高」「買残高」を位置で取得する。
    #
    after = cells[code_index + 1:]

    # ISIN/New Sec. Codeの次から数値列を調べる
    numeric = []

    for index, cell in enumerate(after):
        n = parse_number(cell)

        if n is not None:
            numeric.append(
                (index, cell, n)
            )

    if len(numeric) < 4:
        return None

    #
    # 上場比(Ratio)は小数値なので、
    # 「整数の株数列」を抽出する。
    #
    share_values = []

    for index, raw, number in numeric:
        # 小数値は上場比なので除外
        if "." in raw:
            continue

        # ISIN等を数値化したものを除外
        clean = re.sub(
            r"[,\s▲△+-]",
            "",
            raw
        )

        if len(clean) > 12:
            continue

        share_values.append(
            (index, number)
        )

    if len(share_values) < 4:
        return None

    #
    # コード直後にはNew Sec. Codeがあり、
    # その後の株数列は
    #   売残 → 売前日比 → 買残 → 買前日比
    # の順。
    #
    sell = share_values[0][1]
    sell_change = share_values[1][1]
    buy = share_values[2][1]
    buy_change = share_values[3][1]

    if sell < 0 or buy < 0:
        return None

    return {
        "code": code,
        "date": date,
        "buy": buy,
        "sell": sell,
        "source_url": pdf_url,
        "fetched_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "_sell_change": sell_change,
        "_buy_change": buy_change,
    }


def parse_pdf(pdf_bytes, pdf_url):
    rows = []

    with pdfplumber.open(
        io.BytesIO(pdf_bytes)
    ) as pdf:

        date = find_date_from_pdf(
            pdf,
            pdf_url
        )

        print("公表日:", date)
        print("PDFページ数:", len(pdf.pages))

        table_count = 0

        for table in extract_tables(pdf):
            table_count += 1

                    # 診断用：最初の3テーブルの先頭10行をログへ表示
        if table_count <= 3:
            print(f"=== DEBUG TABLE {table_count} ===")
            for debug_row in table[:10]:
                print("DEBUG ROW:", repr(debug_row))
            for row in table:
                parsed = parse_table_row(
                    row,
                    date,
                    pdf_url
                )

                if parsed:
                    rows.append(parsed)

        print("抽出テーブル数:", table_count)

    # コード単位で重複除去
    unique = {}

    for row in rows:
        unique[row["code"]] = row

    result = list(unique.values())

    # デバッグ確認
    for code in [
        "7974",
        "285A",
        "9984",
        "8035",
        "6857",
    ]:
        hit = unique.get(code)

        if hit:
            print(
                f"確認 {code}: "
                f"売残={hit['sell']:,} "
                f"買残={hit['buy']:,}"
            )

    return result


def main():
    print("JPX信用残データ取得開始")

    pdf_url = get_latest_pdf_url()

    print("PDF:", pdf_url)

    r = requests.get(
        pdf_url,
        timeout=90,
        headers={
            "User-Agent":
                "Mozilla/5.0 JPX-Margin-Updater/2.0",
            "Referer": JPX_PAGE,
        },
    )

    r.raise_for_status()

    print(
        "PDFサイズ:",
        f"{len(r.content):,} bytes"
    )

    rows = parse_pdf(
        r.content,
        pdf_url
    )

    print("解析銘柄数:", len(rows))

    #
    # 誤解析をSupabaseへ入れない安全装置
    #
    if len(rows) < 1000:
        raise RuntimeError(
            f"解析銘柄数が少なすぎます "
            f"({len(rows)}件)。"
            "JPX PDF形式変更または解析失敗の"
            "可能性があるため更新を中止しました。"
        )

    # 内部確認用項目はDBへ送らない
    db_rows = []

    for row in rows:
        db_rows.append(
            {
                "code": row["code"],
                "date": row["date"],
                "buy": row["buy"],
                "sell": row["sell"],
                "source_url":
                    row["source_url"],
                "fetched_at":
                    row["fetched_at"],
            }
        )

    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY
    )

    batch_size = 500

    for i in range(
        0,
        len(db_rows),
        batch_size
    ):
        batch = db_rows[
            i:i + batch_size
        ]

        supabase.table(
            "jpx_margin_data"
        ).upsert(
            batch,
            on_conflict="code,date"
        ).execute()

        print(
            "Supabase保存:",
            f"{i + 1}〜"
            f"{min(i + batch_size, len(db_rows))}"
        )

    print(
        "JPX信用残更新完了:",
        len(db_rows),
        "銘柄"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as e:
        print(
            "ERROR:",
            str(e),
            file=sys.stderr
        )
        raise
