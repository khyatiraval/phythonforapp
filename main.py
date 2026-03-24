from fastapi import FastAPI, File, UploadFile
import io
import re
import pdfplumber

app = FastAPI()

DATE_START_RE = re.compile(
    r"^\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}\s+[A-Za-z]{3}\s+\d{4}\s+\S+"
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

AMOUNT_RE = re.compile(r"\d[\d,]*\.\d{2}")


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def normalize_text(value: str = "") -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s/._:&@-]", " ", (value or "").lower())).strip()


def contains_any(text: str, keywords) -> bool:
    d = normalize_text(text)
    return any(normalize_text(k) in d for k in keywords)


def has_strict_token(text: str, token: str) -> bool:
    d = normalize_text(text)
    t = normalize_text(token)
    if not t:
        return False
    return re.search(rf"(^|[^a-z0-9]){re.escape(t)}([^a-z0-9]|$)", d, re.I) is not None


def should_skip_line(line: str) -> bool:
    line = clean_line(line)
    if not line:
        return True
    return any(p.search(line) for p in SKIP_PATTERNS)


def date_to_iso(s: str) -> str:
    d, mon, y = s.split()
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    return f"{y}-{months[mon.lower()]}-{d}"


def amount_to_float(s: str) -> float:
    return float(s.replace(",", "").strip())


def month_key(iso_date: str) -> str:
    return iso_date[:7]


def month_label(key: str) -> str:
    y, m = key.split("-")
    names = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
    }
    return f"{names[m]} {y}"


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

    if contains_any(d, ["electricity bill", "electric bill", "power bill", "bijli bill", "torrent power", "pgvcl", "ugvcl", "dgvcl", "mseb", "bescom", "electricity"]):
        return "electricity_bill"

    if contains_any(d, ["mobile recharge", "mobile bill", "prepaid recharge", "postpaid bill", "airtel recharge", "jio recharge", "bsnl recharge", "vodafone recharge", "vi recharge"]):
        return "mobile_bill"

    if contains_any(d, ["gas bill", "gas booking", "bharat gas", "indane", "hp gas", "igl", "adani gas", "png bill", "gas cylinder"]):
        return "gas_bill"

    if contains_any(d, ["water bill", "water charges", "municipal water"]):
        return "water_bill"

    if contains_any(d, ["broadband bill", "internet bill", "wifi bill", "fiber bill", "jiofiber", "airtel xstream", "excitel", "act fibernet", "hathway"]):
        return "broadband_bill"

    if contains_any(d, ["credit card payment", "credit card bill", "cc payment", "cc bill", "card payment", "card bill", "cred", "sbi card", "hdfc card", "axis card", "icici card", "amex"]):
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

    if contains_any(d, ["interest capitalised","credit interest capitalised", "credit interest", "monthly interest credit", "quarterly interest credit", "interest"]):
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
        "kuvera": "Kuvera",
        "irctc": "IRCTC",
        "makemytrip": "MakeMyTrip",
        "mmt": "MakeMyTrip",
        "goibibo": "Goibibo",
        "yatra": "Yatra",
        "cleartrip": "Cleartrip",
        "ixigo": "Ixigo",
        "easemytrip": "EaseMyTrip",
        "redbus": "RedBus",
        "abhibus": "AbhiBus",
        "indigo": "IndiGo",
        "air india": "Air India",
        "apollo": "Apollo",
        "airtel": "Airtel",
        "jio": "Jio",
        "bsnl": "BSNL",
        "bharat gas": "Bharat Gas",
        "indane": "Indane",
        "hp gas": "HP Gas",
        "adani gas": "Adani Gas",
        "torrent power": "Torrent Power",
        "pgvcl": "PGVCL",
        "ugvcl": "UGVCL",
        "dgvcl": "DGVCL",
        "cred": "CRED",
    }

    for keyword, merchant in merchant_map.items():
        if normalize_text(keyword) in d:
            return merchant

    m = re.search(r"to:([^/\s]+)", description, re.I)
    if m:
        value = m.group(1).strip()
        if value:
            return value.upper()

    if category == "upi":
        return "UPI"
    if category == "cash_withdrawal":
        return "ATM"
    if category != "other":
        return category_label(category)
    return payment_mode if payment_mode else "Other"


def classify_type(description: str, reference: str) -> str:
    d = normalize_text(description)
    r = reference.lower()

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
        or "refund" in d
        or "payout" in d
        or r.startswith("yesf")
        or r.startswith("yesob")
        or r.startswith("yes0n")
        or r.startswith("yesi1")
        or r.startswith("chbatch")
        or r.startswith("ybp")
    ):
        return "credit"

    if (
        " to:" in d
        or "payment to" in d
        or "paymentto" in d
        or "neft o/w" in d
        or "funds trf to" in d
        or "ach dr " in d
        or "autopay" in d
        or "rent" in d
        or r.startswith("ybs")
        or r.startswith("yesib")
        or r.startswith("yesi3")
    ):
        return "debit"

    return "unknown"


def extract_lines_from_pdf(content: bytes):
    lines = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = clean_line(line)
                if line:
                    lines.append(line)
    return lines


def split_rows(lines):
    rows = []
    current = None

    for line in lines:
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
    row = clean_line(row)

    m = re.match(
        r"^(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"
        r"(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+"
        r"(\S+)\s+(.*)$",
        row
    )
    if not m:
        return None

    txn_date_raw = m.group(1)
    value_date_raw = m.group(2)
    reference = m.group(3)
    rest = m.group(4)

    nums = list(AMOUNT_RE.finditer(rest))
    if len(nums) < 2:
        return None

    amt_match = nums[0]
    bal_match = nums[1]

    amount = amount_to_float(amt_match.group())
    balance = amount_to_float(bal_match.group())

    before = rest[:amt_match.start()].strip()
    after = rest[bal_match.end():].strip()
    description = clean_line((before + " " + after).strip())

    if not description:
        return None

    txn_date = date_to_iso(txn_date_raw)
    value_date = date_to_iso(value_date_raw)
    tx_type = classify_type(description, reference)
    payment_mode = classify_channel(description)
    category = detect_category(description, tx_type, payment_mode)
    merchant = detect_merchant(description, category, payment_mode)
    key = month_key(txn_date)

    return {
        "date": txn_date,
        "valueDate": value_date,
        "description": description,
        "amount": amount,
        "balance": balance,
        "type": tx_type,
        "channel": payment_mode,
        "merchant": merchant,
        "category": category,
        "displayCategory": category_label(category),
        "reference": reference,
        "currencySymbol": "?",
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

    lines = extract_lines_from_pdf(content)
    rows = split_rows(lines)

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
            "currencySymbol": "?",
        },
        "transactions": parsed,
        "summary": {
            "transactionCount": len(parsed),
            "totalDebit": total_debit,
            "totalCredit": total_credit,
        },
        "warnings": unparsed[:10],
        "parser": {
            "name": "python_yesbank_v3",
            "confidence": 0.95,
        },
    }
