from fastapi import FastAPI, File, UploadFile
import pdfplumber

app = FastAPI()

@app.get("/")
def home():
    return {"status": "API running"}

@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    content = await file.read()

    transactions = []

    with pdfplumber.open(file.file) as pdf:
        for page in pdf.pages:
            text = page.extract_text()

            if not text:
                continue

            lines = text.split("\n")

            for line in lines:
                if "UPI" in line or "IMPS" in line:
                    transactions.append({
                        "raw": line
                    })

    return {
        "success": True,
        "transactions": transactions
    }
