from fastapi import FastAPI, File, UploadFile
import io
import re
import pdfplumber

app = FastAPI()


@app.get("/")
def home():
    return {"status": "API running"}


@app.post("/parse")
async def parse_pdf(file: UploadFile = File(...)):
    content = await file.read()

    all_text = []
    lines = []

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            all_text.append(text)

            for line in text.split("\n"):
                line = re.sub(r"\s+", " ", line).strip()
                if line:
                    lines.append({
                        "page": page_no,
                        "line": line
                    })

    return {
        "success": True,
        "pages": len(all_text),
        "lineCount": len(lines),
        "lines": lines[:400],
        "fullTextPreview": "\n".join(all_text)[:12000]
    }
