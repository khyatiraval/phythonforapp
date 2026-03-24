from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
import io
import re
from typing import List, Optional, Tuple, Dict, Any

import pdfplumber

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Core regex
# =========================

AMOUNT_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{2,3})*|\d+)\.\d{2}(?!\d)")
DATE_ANY_RE = re.compile(
    r"(?<!\d)("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"
    r")(?!\d)",
    re.I,
)

DATE_START_RE = re.compile(
    r"^\s*(?:\d+\s+)?("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"
    r")\b",
    re.I,
)

CRDR_RE = re.compile(r"\b(CR|DR|Cr|Dr|CREDIT|DEBIT)\b")
REF_RE = re.compile(
    r"\b(?:UTR|RRN|REF(?:ERENCE)?|CHEQUE(?:\s*NO)?|CHQ(?:\s*NO)?|TXN(?:\s*ID)?|IMPS|UPI|NEFT|RTGS)[\s:/-]*([A-Za-z0-9\-_/]+)\b",
    re.I,
)

BANK_KEYWORDS = [
    ("Yes Bank", ["yes bank"]),
    ("ICICI Bank", ["icici bank"]),
    ("Bank of Baroda", ["bank of baroda", "baroda bank"]),
    ("Bank of India", ["bank of india"]),
    ("HDFC Bank", ["hdfc bank"]),
    ("State Bank of India", ["state bank of india", "sbi"]),
    ("Axis Bank", ["axis bank"]),
    ("Kotak Mahindra Bank", ["kotak", "kotak mahindra"]),
    ("Punjab National Bank", ["punjab national bank", "pnb"]),
    ("Canara Bank", ["canara bank"]),
    ("Union Bank", ["union bank"]),
    ("IDFC First Bank", ["idfc first", "idfc bank"]),
    ("AU Small Finance Bank", ["au small finance", "au bank"]),
    ("IndusInd Bank", ["indusind"]),
    ("Federal Bank", ["federal bank"]),
]

SKIP_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^page\s+\d+", re.I),
    re.compile(r"^statement\b", re.I),
    re.compile(r"^account\s+statement\b", re.I),
    re.compile(r"^customer\s+id\b", re.I),
    re.compile(r"^customer\s+name\b", re.I),
    re.compile(r"^account\s+number\b", re.I),
    re.compile(r"^account\s+no\b", re.I),
    re.compile(r"^branch\b", re.I),
    re.compile(r"^ifsc\b", re.I),
    re.compile(r"^micr\b", re.I),
    re.compile(r"^currency\b", re.I),
    re.compile(r"^nominee\b", re.I),
    re.compile(r"^primary\s+holder\b", re.I),
    re.compile(r"^opening\s+balance\b", re.I),
    re.compile(r"^closing\s+balance\b", re.I),
    re.compile(r"^total\s+withdraw", re.I),
    re.compile(r"^total\s+deposit", re.I),
    re.compile(r"^total\s+credit", re.I),
    re.compile(r"^total\s+debit", re.I),
    re.compile(r"^generated\s+on\b", re.I),
    re.compile(r"^this\s+is\s+a\s+computer\s+generated", re.I),
    re.compile(r"^date\s+particular", re.I),
    re.compile(r"^date\s+description", re.I),
    re.compile(r"^txn\s+date", re.I),
    re.compile(r"^tran(?:saction)?\s+date", re.I),
    re.compile(r"^value\s+date", re.I),
    re.compile(r"^serial\s+no", re.I),
    re.compile(r"^sr\.\s*no", re.I),
    re.compile(r"^balance\s*$", re.I),
    re.compile(r"^debit\s+credit", re.I),
    re.compile(r"^withdrawal\s+deposit", re.I),
]

MONTHS = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}


# =========================
# Utility
# =========================

def clean_line(line: str) -> str:
    if line is None:
        return ""
    line = line.replace("\u00a0", " ")
    line = line.replace("\t", " ")
    line = re.sub(r"[|]+", " ", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line


def normalize_text(value: str = "") -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9\s/._:&@-]", " ", (value or "").lower())
    ).strip()


def contains_any(text: str, keywords) -> bool:
    d = normalize_text(text)
    return any(normalize_text(k) in d for k in keywords)


def has_strict_token(text: str, token: str) -> bool:
    d = normalize_text(text)
    t = normalize_text(token)
    if not t:
        return False
    return re.search(rf"(^|[^a-z0-9]){re.escape(t)}([^a-z0-9]|$)", d, re.I) is not None


def amount_to_float(s: str) -> float:
    return float(str(s).replace(",", "").strip())


def safe_round(v: float) -> float:
    return round(float(v or 0), 2)


def month_key(iso_date: str) -> str:
    return iso_date[:7] if iso_date and len(iso_date) >= 7 else ""


def month_label(key: str) -> str:
    if not key or "-" not in key:
        return ""
    y, m = key.split("-")
    names = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
    }
    return f"{names.get(m, m)} {y}"


def parse_date_to_iso(s: str) -> Optional[str]:
    s = clean_line(s)
    if not s:
        return None

    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", s)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})$", s, re.I)
    if m:
        d = int(m.group(1))
        mon = MONTHS.get(m.group(2).lower())
        y = int(m.group(3))
        if y < 100:
            y += 2000
        if mon and 1 <= d <= 31:
            return f"{y:04d}-{mon}-{d:02d}"

    return None


def should_skip_line(line: str) -> bool:
    line = clean_line(line)
    if not line:
        return True
    return any(p.search(line) for p in SKIP_PATTERNS)


def detect_bank_name(lines: List[str], bank_hint: str = "") -> str:
    hint = normalize_text(bank_hint)
    if hint == "yesbank":
        return "Yes Bank"
    if hint == "icici":
        return "ICICI Bank"
    if hint == "bob":
        return "Bank of Baroda"
    if hint == "boi":
        return "Bank of India"
    if hint == "universal":
        pass

    text = normalize_text(" ".join(lines[:120]))
    for bank_name, keys in BANK_KEYWORDS:
        if any(normalize_text(k) in text for k in keys):
            return bank_name
    return "Universal Parser"


def extract_account_last4(lines: List[str]) -> str:
    joined = " ".join(lines[:120])

    patterns = [
        r"account\s+(?:number|no\.?)\s*[:\-]?\s*(?:x+|\*+)?(\d{4})\b",
        r"a/c\s*(?:number|no\.?)?\s*[:\-]?\s*(?:x+|\*+)?(\d{4})\b",
        r"\bxx+(\d{4})\b",
        r"\*{2,}(\d{4})\b",
    ]

    for pat in patterns:
        m = re.search(pat, joined, re.I)
        if m:
            return m.group(1)

    return ""


def extract_lines_from_pdf(content: bytes) -> List[str]:
    lines: List[str] = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(
                x_tolerance=2,
                y_tolerance=2,
                layout=False,
            ) or ""

            for raw in page_text.splitlines():
                line = clean_line(raw)
                if line:
                    lines.append(line)

            # also pull tables if any parser exposes them better
            try:
                tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        if not row:
                            continue
                        row_text = clean_line(" ".join([clean_line(str(c or "")) for c in row if str(c or "").strip()]))
                        if row_text:
                            lines.append(row_text)
            except Exception:
                pass

    # de-duplicate adjacent duplicates
    deduped = []
    prev = None
    for line in lines:
        if line != prev:
            deduped.append(line)
        prev = line

    return deduped


# =========================
# Category + merchant logic
# =========================

def category_label(category: str) -> str:
    mapping = {
        "investment": "Investment",
        "cash_withdrawal": "Cash Withdrawal",
        "electricity_bill": "Electricity Bill",
        "mobile_bill": "Mobile Bill",
        "gas_bill": "Gas Bill",
        "water_bill": "Water Bill",
        "broadband_bill": "Broadband Bill",
        "credit_card_bill": "Credit Card Bill",
        "travel": "Travel",
        "movie": "Movie",
        "food": "Food",
        "shopping": "Shopping",
        "medical": "Medical",
        "salary": "Salary",
        "interest": "Interest",
        "upi": "UPI",
        "imps": "IMPS",
        "neft": "NEFT",
        "rtgs": "RTGS",
        "received": "Received",
        "other": "Other",
    }
    return mapping.get(category, category.replace("_", " ").title())


def classify_channel(description: str) -> str:
    d = normalize_text(description)
    if contains_any(d, ["upi", "gpay", "google pay", "phonepe", "paytm upi", "bhim", "tez"]):
        return "UPI"
    if "imps" in d:
        return "IMPS"
    if "neft" in d:
        return "NEFT"
    if "rtgs" in d:
        return "RTGS"
    if contains_any(d, ["ecs", "ach", "nach", "mandate"]):
        return "ACH"
    if contains_any(d, ["atm", "cash withdrawal", "cash wd", "cash wdl"]):
        return "ATM"
    return "BANK"


def is_investment_transaction(description: str) -> bool:
    d = normalize_text(description)

    investment_platforms = [
        "zerodha", "groww", "upstox", "kuvera", "coin", "cams", "kfin",
        "mf central", "mutual fund", "nps", "ppfas", "hdfcmf", "sbimf",
        "icicipru", "nippon", "axis mf", "kotak mf", "uti mf",
        "aditya birla", "tata mf", "icici prudential", "sbi mutual fund",
        "hdfc mutual fund", "nippon india mutual fund"
    ]

    if contains_any(d, investment_platforms):
        return True

    strict_tokens = ["sip", "ecs", "ach", "nach", "mandate"]
    finance_context = [
        "mutual fund", "auto debit", "systematic investment",
        "installment", "brokerage", "fund", "scheme",
        "zerodha", "groww", "upstox", "kuvera", "coin", "cams", "kfin"
    ]

    return any(has_strict_token(d, t) for t in strict_tokens) and contains_any(d, finance_context)


def detect_category(description: str, txn_type: str, payment_mode: str) -> str:
    d = normalize_text(description)

    if contains_any(d, ["cash withdrawal", "atm withdrawal", "atm cash", "cash wd", "cash wdl", "self cash"]) or has_strict_token(d, "atm"):
        return "cash_withdrawal"

    if is_investment_transaction(d):
        return "investment"

    if contains_any(d, ["electricity", "torrent power", "dakshin gujarat vij", "pgvcl", "mgvcl", "dgvcl", "ugvcl", "bescom", "tneb", "brihanmumbai electric", "adani electricity", "mahadiscom"]):
        return "electricity_bill"

    if contains_any(d, ["jio recharge", "airtel recharge", "vi recharge", "vodafone idea", "mobile recharge", "prepaid recharge", "postpaid bill"]) and contains_any(d, ["airtel", "jio", "vi", "vodafone", "idea", "bsnl"]):
        return "mobile_bill"

    if contains_any(d, ["bharat gas", "indane", "hp gas", "gas bill", "lpg"]):
        return "gas_bill"

    if contains_any(d, ["water bill", "municipal water", "water charges"]):
        return "water_bill"

    if contains_any(d, ["broadband", "fiber", "wifi bill", "internet bill", "airtel xstream", "jiofiber", "bsnl broadband", "act fibernet", "hathway"]):
        return "broadband_bill"

    if contains_any(d, ["credit card payment", "cc payment", "card payment to", "credit card bill", "cred"]) and not contains_any(d, ["credit interest capitalised", "credit interest", "interest capitalised"]):
        return "credit_card_bill"

    if contains_any(d, ["irctc", "railway", "train ticket", "flight booking", "air india", "indigo", "vistara", "spicejet", "akasa", "makemytrip", "mmt", "goibibo", "yatra", "cleartrip", "ixigo", "easemytrip", "booking.com", "agoda", "oyo", "hotel booking", "resort booking", "redbus", "abhibus", "bus booking"]):
        return "travel"

    if contains_any(d, ["bookmyshow", "bms", "movie ticket", "cinema ticket", "inox", "pvr", "cinepolis", "ticketnew", "show ticket"]):
        return "movie"

    if contains_any(d, ["swiggy", "zomato", "zepto", "blinkit", "instamart", "bigbasket", "dominos", "pizza hut", "mcdonald", "kfc", "burger king", "subway", "starbucks", "faasos", "eatsure", "freshmenu", "restaurant", "cafe", "bakery", "eatclub"]):
        return "food"

    if contains_any(d, ["amazon", "flipkart", "myntra", "ajio", "meesho", "nykaa", "zudio", "reliance", "reliance retail", "trends", "westside", "shopperstop", "tata cliq", "firstcry", "croma", "vijay sales", "ikea", "jiomart", "zara", "h&m", "decathlon", "dmart", "dmart ready"]):
        return "shopping"

    if contains_any(d, ["apollo", "hospital", "medical", "clinic", "pharmacy", "medicines", "medplus", "netmeds", "1mg"]):
        return "medical"

    if contains_any(d, ["salary", "salaries", "sallary"]):
        return "salary"

    if contains_any(d, ["interest capitalised", "credit interest capitalised", "credit interest", "monthly interest credit", "quarterly interest credit", "interest"]):
        return "interest"

    if payment_mode == "UPI":
        return "upi"
    if payment_mode == "IMPS":
        return "imps"
    if payment_mode == "NEFT":
        return "neft"
    if payment_mode == "RTGS":
        return "rtgs"

    return "received" if txn_type == "credit" else "other"


def detect_merchant(description: str, category: str, payment_mode: str) -> str:
    d = normalize_text(description)

    merchant_map = {
        "swiggy": "Swiggy",
        "zomato": "Zomato",
        "zepto": "Zepto",
        "blinkit": "Blinkit",
        "bookmyshow": "BookMyShow",
        "pvr": "PVR",
        "inox": "INOX",
        "cinepolis": "Cinepolis",
        "amazon": "Amazon",
        "flipkart": "Flipkart",
        "myntra": "Myntra",
        "ajio": "Ajio",
        "meesho": "Meesho",
        "nykaa": "Nykaa",
        "zudio": "Zudio",
        "reliance": "Reliance",
        "jiomart": "JioMart",
        "dmart": "DMart",
        "zerodha": "Zerodha",
        "groww": "Groww",
        "upstox": "Upstox",
        "irctc": "IRCTC",
        "makemytrip": "MakeMyTrip",
        "mmt": "MakeMyTrip",
        "goibibo": "Goibibo",
        "airtel": "Airtel",
        "jio": "Jio",
        "bharat gas": "Bharat Gas",
        "indane": "Indane",
        "hp gas": "HP Gas",
        "cred": "CRED",
    }

    for keyword, merchant in merchant_map.items():
        if normalize_text(keyword) in d:
            return merchant

    if category == "upi":
        return "UPI"
    if category == "cash_withdrawal":
        return "ATM"
    if category == "credit_card_bill":
        return "Credit Card Bill"
    if payment_mode in ("IMPS", "NEFT", "RTGS"):
        return payment_mode

    return "Other"


def infer_type_from_description(description: str) -> str:
    d = normalize_text(description)

    if contains_any(d, [
        "salary", "interest", "refund", "cash deposit", "deposit", "credited",
        "credit", "by transfer", "neft in", "imps in", "upi/cr", "received"
    ]):
        return "credit"

    if contains_any(d, [
        "debited", "debit", "withdrawal", "withdraw", "purchase", "dr",
        "upi", "imps", "neft", "rtgs", "ach", "ecs", "emi", "bill", "payment"
    ]):
        return "debit"

    return "debit"


# =========================
# Row grouping
# =========================

def line_looks_like_new_txn(line: str) -> bool:
    line = clean_line(line)
    if not line:
        return False
    return DATE_START_RE.search(line) is not None


def split_rows(lines: List[str]) -> List[str]:
    rows: List[str] = []
    current = ""

    for raw in lines:
        line = clean_line(raw)
        if should_skip_line(line):
            continue

        if line_looks_like_new_txn(line):
            if current:
                rows.append(clean_line(current))
            current = line
        else:
            # attach continuation only if a transaction has already started
            if current:
                current += " " + line

    if current:
        rows.append(clean_line(current))

    return rows


# =========================
# Parsing helpers
# =========================

def extract_leading_dates(row: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    Returns:
      txn_date_iso, value_date_iso, remaining_text
    """
    row = clean_line(row)
    matches = list(DATE_ANY_RE.finditer(row[:60]))

    if not matches:
        return None, None, row

    first = matches[0]
    txn_date_raw = first.group(1)
    txn_date_iso = parse_date_to_iso(txn_date_raw)
    if not txn_date_iso:
        return None, None, row

    rest_start = first.end()
    value_date_iso = txn_date_iso

    if len(matches) >= 2:
        second = matches[1]
        # only consider second date if it is close to the beginning
        if second.start() < 35:
            value_raw = second.group(1)
            value_iso = parse_date_to_iso(value_raw)
            if value_iso:
                value_date_iso = value_iso
                rest_start = second.end()

    rest = clean_line(row[rest_start:])
    return txn_date_iso, value_date_iso, rest


def extract_reference(text: str) -> str:
    m = REF_RE.search(text or "")
    return clean_line(m.group(1)) if m else ""


def remove_reference_noise(text: str) -> str:
    text = clean_line(text)
    text = re.sub(r"\b(?:CR|DR|Cr|Dr|CREDIT|DEBIT)\b", " ", text)
    text = re.sub(r"\b(?:INR|Rs\.?|₹)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def pick_amount_type_balance(rest: str) -> Tuple[Optional[float], Optional[str], Optional[float], str]:
    """
    Heuristic parser:
    returns amount, type, balance, description
    """
    original = clean_line(rest)
    if not original:
        return None, None, None, ""

    number_matches = list(AMOUNT_RE.finditer(original))
    if not number_matches:
        return None, None, None, original

    crdr_matches = list(CRDR_RE.finditer(original))
    amount = None
    tx_type = None
    balance = None

    # --- Case 1: explicit DR/CR markers exist
    if crdr_matches:
        last_marker = crdr_matches[-1]
        marker_text = last_marker.group(1).lower()

        # find nearest amount before the last DR/CR marker
        nums_before = [m for m in number_matches if m.end() <= last_marker.start()]
        nums_after = [m for m in number_matches if m.start() >= last_marker.end()]

        if nums_before:
            amount = amount_to_float(nums_before[-1].group())
            tx_type = "credit" if marker_text in ("cr", "credit") else "debit"

            # last numeric after marker may be balance
            if nums_after:
                balance = amount_to_float(nums_after[-1].group())
            elif len(number_matches) >= 2:
                # fallback: if there are many numbers, take the final one as balance if different
                last_num = amount_to_float(number_matches[-1].group())
                if abs(last_num - amount) > 0.0001:
                    balance = last_num

            desc_end = nums_before[-1].start()
            description = clean_line(original[:desc_end])

            # if something meaningful remains after balance, keep it too
            tail = ""
            if nums_after:
                tail = clean_line(original[nums_after[-1].end():])
            if tail:
                description = clean_line(f"{description} {tail}")

            return amount, tx_type, balance, remove_reference_noise(description)

    # --- Case 2: withdrawal + deposit + balance style
    # Usually description ... debit credit balance
    nums = [amount_to_float(m.group()) for m in number_matches]

    if len(nums) >= 3:
        n1, n2, n3 = nums[-3], nums[-2], nums[-1]
        # choose balance as final number
        balance = n3

        if n1 == 0 and n2 > 0:
            amount = n2
            tx_type = "credit"
        elif n2 == 0 and n1 > 0:
            amount = n1
            tx_type = "debit"
        elif n1 > 0 and n2 > 0:
            # ambiguous; choose nearer to end as amount? safer to infer by text
            inferred = infer_type_from_description(original)
            amount = n2 if inferred == "credit" else n1
            tx_type = inferred

        if amount is not None and tx_type is not None:
            desc_end = number_matches[-3].start()
            description = clean_line(original[:desc_end])
            return amount, tx_type, balance, remove_reference_noise(description)

    # --- Case 3: amount + balance style
    if len(nums) >= 2:
        amount = nums[-2]
        balance = nums[-1]
        tx_type = infer_type_from_description(original)
        description = clean_line(original[:number_matches[-2].start()])
        return amount, tx_type, balance, remove_reference_noise(description)

    # --- Case 4: only one amount visible
    if len(nums) == 1:
        amount = nums[0]
        tx_type = infer_type_from_description(original)
        description = clean_line(original[:number_matches[-1].start()])
        return amount, tx_type, None, remove_reference_noise(description)

    return None, None, None, original


def parse_row(row: str, bank_name: str = "") -> Optional[Dict[str, Any]]:
    row = clean_line(row)
    if not row:
        return None

    txn_date, value_date, rest = extract_leading_dates(row)
    if not txn_date:
        return None

    amount, tx_type, balance, description = pick_amount_type_balance(rest)
    if amount is None or amount <= 0:
        return None

    description = description or rest
    description = clean_line(description)

    # remove accidental headers captured as rows
    if not description:
        return None
    if re.fullmatch(r"(opening|closing)\s+balance", normalize_text(description), re.I):
        return None

    reference = extract_reference(rest)
    payment_mode = classify_channel(description)
    category = detect_category(description, tx_type or "debit", payment_mode)
    merchant = detect_merchant(description, category, payment_mode)
    key = month_key(txn_date)

    return {
        "date": txn_date,
        "valueDate": value_date or txn_date,
        "description": description,
        "amount": safe_round(amount),
        "balance": safe_round(balance) if balance is not None else None,
        "type": tx_type or "debit",
        "channel": payment_mode,
        "merchant": merchant,
        "category": category,
        "displayCategory": category_label(category),
        "reference": reference,
        "currencySymbol": "₹",
        "monthKey": key,
        "monthLabel": month_label(key),
        "bankName": bank_name or "Universal Parser",
        "raw": row,
    }


def dedupe_transactions(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []

    for tx in items:
        key = (
            tx.get("date", ""),
            tx.get("amount", 0),
            tx.get("type", ""),
            normalize_text(tx.get("description", "")),
            tx.get("reference", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(tx)

    return out


# =========================
# Routes
# =========================

@app.get("/")
def home():
    return {"status": "API running"}


@app.post("/parse")
async def parse_pdf(
    file: UploadFile = File(...),
    bankHint: str = Form(default="")
):
    content = await file.read()

    lines = extract_lines_from_pdf(content)
    detected_bank = detect_bank_name(lines, bankHint)
    account_last4 = extract_account_last4(lines)

    rows = split_rows(lines)

    parsed: List[Dict[str, Any]] = []
    unparsed: List[str] = []

    for row in rows:
        tx = parse_row(row, detected_bank)
        if tx:
            parsed.append(tx)
        else:
            unparsed.append(row)

    parsed = dedupe_transactions(parsed)

    total_debit = safe_round(sum(tx["amount"] for tx in parsed if tx["type"] == "debit"))
    total_credit = safe_round(sum(tx["amount"] for tx in parsed if tx["type"] == "credit"))

    # basic confidence
    row_count = len(rows)
    tx_count = len(parsed)
    ratio = (tx_count / row_count) if row_count else 0
    confidence = round(min(0.95, max(0.35, ratio)), 2)

    return {
        "success": True,
        "statement": {
            "bankName": detected_bank,
            "accountLast4": account_last4,
            "statementPeriodStart": parsed[-1]["date"] if parsed else None,
            "statementPeriodEnd": parsed[0]["date"] if parsed else None,
            "currencyCode": "INR",
            "currencySymbol": "₹",
        },
        "transactions": parsed,
        "summary": {
            "transactionCount": len(parsed),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
        },
        "warnings": unparsed[:25],
        "parser": {
            "name": "python_universal_v1",
            "confidence": confidence,
        },
        "meta": {
            "bankHint": bankHint or "",
            "detectedBank": detected_bank,
            "totalRowsSeen": row_count,
            "parsedRows": len(parsed),
            "unparsedRows": len(unparsed),
            "originalFileName": file.filename or "",
        },
    }
