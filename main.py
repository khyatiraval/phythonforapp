from fastapi import FastAPI, File, UploadFile
import io
import re
import pdfplumber

app = FastAPI()


DATE_START_RE = re.compile(
    r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\S+"
)

ROW_PARSE_RE = re.compile(
    r"^"
    r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"      # txn date
    r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"      # value date
    r"(\S+)\s+"                              # reference
    r"(.+?)\s+"                              # description
    r"(\d[\d,]*\.\d{2})\s+"                  # amount
    r"(\d[\d,]*\.\d{2})$"                    # balance
)

SKIP_PATTERNS = [
    re.compile(r"^Customer Id:", re.I),
    re.compile(r"^Primary Account Holder Name:", re.I),
    re.compile(r"^Transaction details for your account number", re.I),
    re.compile(r"^Primary Holder:", re.I),
    re.compile(r"^FOR WOMEN$", re.I),
    re.compile(r"^Nominee Details:", re.I),
    re.compile(r"^Transaction$", re.I),
    re.compile(r"^Date$", re.I),
    re.compile(r"^Value Date Cheque No/Reference No Description Withdrawals Deposits Running Balance$", re.I),
    re.compile(r"^Opening Balance:", re.I),
    re.compile(r"^Total Withdrawals:", re.I),
    re.compile(r"^Total Deposits:", re.I),
    re.compile(r"^Closing Balance:", re.I),
    re.compile(r"^Page\s+\d+", re.I),
]


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def should_skip_line(line: str) -> bool:
    line = clean_line(line)
    if not line:
        return True
    return any(p.search(line) for p in SKIP_PATTERNS)


def date_to_iso(s: str) -> str:
    parts = s.split()
    day = parts[0]
    mon = parts[1].lower()
    year = parts[2]

    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    return f"{year}-{months[mon]}-{day}"


def amount_to_float(s: str) -> float:
    return float(s.replace(",", "").strip())


def month_key(iso_date: str) -> str:
    return iso_date[:7]


def month_label(key: str) -> str:
    year, month = key.split("-")
    names = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
    }
    return f"{names[month]} {year}"


def classify_channel(description: str) -> str:
    d = description.lower()
    if "upi/" in d:
        return "UPI"
    if "imps" in d:
        return "IMPS"
    if "neft" in d:
        return "NEFT"
    if "interest" in d:
        return "BANK"
    if "ach" in d:
        return "ACH"
    return "BANK"


def classify_type(description: str, reference: str) -> str:
    d = description.lower()
    r = reference.lower()

    # explicit credits
    if (
        "zerodha payout" in d
        or "credit interest" in d
        or "monthly interest credit" in d
        or "quarterly interest credit" in d
        or "funds trf from" in d
        or "cr-funds recevied" in d
        or "cr-funds received" in d
        or "neft cr-" in d
        or "ach cr " in d
        or r.startswith("yesf")
        or r.startswith("yesob")
        or r.startswith("yes0n")
        or r.startswith("yesi1")
        or r.startswith("chbatch")
        or "refund" in d
        or "payout" in d
    ):
        return "credit"

    # explicit debits
    if (
        " to:" in d
        or "payment to" in d
        or "paymentto" in d
        or "neft o/w" in d
        or "funds trf to" in d
        or "ach dr " in d
        or "autopay" in d
        or "rent" in d
        or r.startswith("ybp")
        or r.startswith("ybs")
        or r.startswith("yesib")
        or r.startswith("yesi3")
    ):
        return "debit"

    return "unknown"


def extract_lines_from_pdf(content: bytes):
    lines = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = clean_line(line)
                if line:
                    lines.append({"page": page_no, "line": line})
    return lines


def split_rows(lines):
    rows = []
    current = None

    for item in lines:
        line = item["line"]

        if should_skip_line(line):
            continue

        if DATE_START_RE.match(line):
            if current:
                rows.append(clean_line(current))
            current = line
        else:
            if current:
                current += " " + line

    if current:
        rows.append(clean_line(current))

    return rows


def parse_row(row: str):
    m = ROW_PARSE_RE.match(row)
    if not m:
        return None

    txn_date_raw = m.group(1)
    value_date_raw = m.group(2)
    reference = m.group(3)
    description = clean_line(m.group(4))
    amount = amount_to_float(m.group(5))
    balance = amount_to_float(m.group(6))

    txn_date = date_to_iso(txn_date_raw)
    value_date = date_to_iso(value_date_raw)
    tx_type = classify_type(description, reference)
    key = month_key(txn_date)

    return {
        "date": txn_date,
        "valueDate": value_date,
        "description": description,
        "amount": amount,
        "balance": balance,
        "type": tx_type,
        "channel": classify_channel(description),
        "merchant": "",
        "category": "other",
        "displayCategory": "Other",
        "reference": reference,
        "currencySymbol": "₹",
        "monthKey": key,
        "monthLabel": month_label(key),
        "raw": row,
    }


@app.get("/")
def home():
    return {"status": "API running"}


@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    content = await file.read()

    line_items = extract_lines_from_pdf(content)
    rows = split_rows(line_items)

    parsed = []
    unparsed = []

    for row in rows:
        tx = parse_row(row)
        if tx:
            parsed.append(tx)
        else:
            unparsed.append(row)

    total_debit = round(sum(tx["amount"] for tx in parsed if tx["type"] == "debit"), 2)
    total_credit = round(sum(tx["amount"] for tx in parsed if tx["type"] == "credit"), 2)

    return {
        "success": True,
        "statement": {
            "bankName": "Yes Bank",
            "accountLast4": "",
            "statementPeriodStart": None,
            "statementPeriodEnd": None,
            "currencyCode": "INR",
            "currencySymbol": "₹",
        },
        "transactions": parsed,
        "summary": {
            "transactionCount": len(parsed),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
        },
        "warnings": unparsed[:20],
        "parser": {
            "name": "python_yesbank_v1",
            "confidence": 0.90,
        },
        "debug": {
            "pages": len(set(x["page"] for x in line_items)),
            "lineCount": len(line_items),
            "rowCount": len(rows),
        },
    }
