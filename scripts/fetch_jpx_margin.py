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
    """
    JPXの銘柄別信用取引残高ページから、
    最新の日次・全銘柄PDF（YYYYMMDD_mtall.pdf）を取得する。
    """
    response = requests.get(
        JPX_PAGE,
        timeout=30,
        headers={
            "User-Agent":
                "Mozilla/5.0 JPX-Margin-Updater/3.0"
        },
    )
    response.raise_for_status()

    links = re.findall(
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        response.text,
        flags=re.I,
    )

    urls = [
        urljoin(
            JPX_PAGE,
            link.replace("&amp;", "&")
        )
        for link in links
    ]

    candidates = []

    for url in urls:
        match = re.search(
            r"/(\d{8})_mtall\.pdf(?:\?|$)",
            url,
            flags=re.I,
        )

        if match:
            candidates.append(
                (match.group(1), url)
            )

    if not candidates:
        raise RuntimeError(
            "JPXページから日次全銘柄信用残PDF"
            "（YYYYMMDD_mtall.pdf）を検出できませんでした"
        )

    # HTML上の並び順に依存せず、
    # ファイル名の日付が最も新しいPDFを採用
    candidates.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidates[0][1]


def normalize_cell(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def parse_nonnegative_integer(value):
    """
    売残・買残などの残高を整数化する。

    例:
      9,400     -> 9400
      160,100   -> 160100
      0         -> 0

    ▲やマイナス値は残高列として不正なのでNone。
    """
    value = normalize_cell(value)

    if not value:
        return None

    if (
        "▲" in value
        or value.startswith("-")
    ):
        return None

    digits = re.sub(
        r"[^\d]",
        "",
        value
    )

    if not digits:
        return None

    return int(digits)


def valid_security_code(value):
    """
    JPXの証券コードを判定。
    通常の4桁コードに加え、285A等にも対応。
    """
    value = normalize_cell(value).upper()

    return bool(
        re.fullmatch(
            r"(?:\d{4}|\d{3}[A-Z])",
            value
        )
    )


def find_date_from_url(pdf_url):
    match = re.search(
        r"/(\d{4})(\d{2})(\d{2})_mtall\.pdf",
        pdf_url,
        flags=re.I,
    )

    if not match:
        raise RuntimeError(
            "JPX PDF URLから公表日を取得できませんでした"
        )

    return (
        f"{match.group(1)}-"
        f"{match.group(2)}-"
        f"{match.group(3)}"
    )


def extract_tables(pdf):
    """
    JPX PDFは罫線付きの表なので、
    pdfplumberでページごとに表として取得する。
    """
    for page_number, page in enumerate(
        pdf.pages,
        start=1
    ):
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
            print(
                f"WARNING: "
                f"{page_number}ページ目で"
                "表を取得できませんでした",
                file=sys.stderr,
                flush=True,
            )
            continue

        for table in tables:
            yield table


def parse_table_row(
    row,
    date,
    pdf_url
):
    """
    2026-10-02時点で確認したJPX PDFの実列構造:

      0 銘柄 Issue
      1 市場 Section
      2 銘柄種別 Loan/Margin
      3 コード Code
      4 新証券コード New Sec. Code
      5 単位（株数 Shs. / 金額 Val.）
      6 売残高 Outstanding Sales
      7 売残高 前日比
      8 売残高 上場比
      9 買残高 Outstanding Purchases
     10 買残高 前日比
     11 買残高 上場比
     ...

    同一コードについて「株数 Shs.」と「金額 Val.」の
    2行が存在するため、株数行のみ採用する。
    """

    if not row:
        return None

    cells = [
        normalize_cell(cell)
        for cell in row
    ]

    # 必要列まで存在しない行はヘッダー等なので除外
    if len(cells) < 10:
        return None

    code = cells[3].upper()

    if not valid_security_code(code):
        return None

    unit = cells[5].lower()

    # 「株数 Shs.」行のみ採用。
    # 金額 Val. 行は絶対に登録しない。
    if (
        "shs" not in unit
        and "株数" not in unit
    ):
        return None

    sell = parse_nonnegative_integer(
        cells[6]
    )

    buy = parse_nonnegative_integer(
        cells[9]
    )

    if sell is None or buy is None:
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
    }


def parse_pdf(
    pdf_bytes,
    pdf_url
):
    date = find_date_from_url(
        pdf_url
    )

    rows = []
    table_count = 0

    with pdfplumber.open(
        io.BytesIO(pdf_bytes)
    ) as pdf:

        print(
            "公表日:",
            date,
            flush=True
        )

        print(
            "PDFページ数:",
            len(pdf.pages),
            flush=True
        )

        for table in extract_tables(pdf):
            table_count += 1

            for row in table:
                parsed = parse_table_row(
                    row,
                    date,
                    pdf_url
                )

                if parsed is not None:
                    rows.append(parsed)

    print(
        "抽出テーブル数:",
        table_count,
        flush=True
    )

    #
    # 同一コードが複数市場等で現れた場合に備える。
    #
    # 原則1コード1株数行の想定だが、
    # 重複した場合は残高を合算せず更新を停止する。
    #
    unique = {}
    duplicates = set()

    for row in rows:
        code = row["code"]

        if code in unique:
            duplicates.add(code)
        else:
            unique[code] = row

    if duplicates:
        sample = ", ".join(
            sorted(duplicates)[:20]
        )

        raise RuntimeError(
            "同一証券コードの株数行が複数検出されました。"
            "誤集計防止のため更新を中止します。"
            f" 重複例: {sample}"
        )

    result = list(
        unique.values()
    )

    print(
        "解析銘柄数:",
        len(result),
        flush=True
    )

    #
    # 代表銘柄をログへ出して検証しやすくする
    #
    for code in (
        "7974",
        "285A",
        "9984",
        "8035",
        "6857",
    ):
        row = unique.get(code)

        if row:
            ratio = (
                row["buy"] / row["sell"]
                if row["sell"] > 0
                else None
            )

            if ratio is None:
                ratio_text = "算出不可"
            else:
                ratio_text = (
                    f"{ratio:.2f}倍"
                )

            print(
                f"確認 {code}: "
                f"売残={row['sell']:,} "
                f"買残={row['buy']:,} "
                f"信用倍率={ratio_text}",
                flush=True
            )
        else:
            print(
                f"確認 {code}: 未検出",
                flush=True
            )

    return result


def validate_rows(rows):
    """
    誤解析データをSupabaseへ保存しないための安全確認。
    """

    if len(rows) < 1000:
        raise RuntimeError(
            f"解析銘柄数が少なすぎます "
            f"({len(rows)}件)。"
            "JPX PDF形式変更または解析失敗の"
            "可能性があるため更新を中止しました。"
        )

    by_code = {
        row["code"]: row
        for row in rows
    }

    #
    # 7974 任天堂を構造確認用の基準銘柄にする。
    # 数値そのものは固定しない。
    #
    if "7974" not in by_code:
        raise RuntimeError(
            "検証銘柄7974を取得できませんでした。"
            "JPX PDF解析に問題がある可能性があるため"
            "更新を中止しました。"
        )

    for row in rows:
        if (
            row["buy"] < 0
            or row["sell"] < 0
        ):
            raise RuntimeError(
                f"{row['code']}で負の残高を検出しました。"
                "更新を中止しました。"
            )


def save_to_supabase(rows):
    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY
    )

    batch_size = 500

    for start in range(
        0,
        len(rows),
        batch_size
    ):
        batch = rows[
            start:start + batch_size
        ]

        supabase.table(
            "jpx_margin_data"
        ).upsert(
            batch,
            on_conflict="code,date"
        ).execute()

        print(
            "Supabase保存:",
            f"{start + 1}〜"
            f"{min(start + batch_size, len(rows))}",
            flush=True
        )


def main():
    print(
        "JPX信用残データ取得開始",
        flush=True
    )

    pdf_url = get_latest_pdf_url()

    print(
        "PDF:",
        pdf_url,
        flush=True
    )

    response = requests.get(
        pdf_url,
        timeout=90,
        headers={
            "User-Agent":
                "Mozilla/5.0 JPX-Margin-Updater/3.0",
            "Referer": JPX_PAGE,
        },
    )

    response.raise_for_status()

    if not response.content.startswith(
        b"%PDF"
    ):
        raise RuntimeError(
            "取得ファイルがPDFではありません"
        )

    print(
        "PDFサイズ:",
        f"{len(response.content):,} bytes",
        flush=True
    )

    rows = parse_pdf(
        response.content,
        pdf_url
    )

    #
    # DB書き込みより前に必ず検証
    #
    validate_rows(rows)

    save_to_supabase(rows)

    print(
        "JPX信用残更新完了:",
        len(rows),
        "銘柄",
        flush=True
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            "ERROR:",
            str(error),
            file=sys.stderr,
            flush=True
        )
        raise
