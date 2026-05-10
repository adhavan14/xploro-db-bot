import os
from typing import Optional

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

# ─────────────────────────────────────────
# ANTHROPIC CLIENT
# ─────────────────────────────────────────

def get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in environment.")
    return anthropic.Anthropic(api_key=api_key)


# ─────────────────────────────────────────
# APP
# ─────────────────────────────────────────

app = FastAPI(
    title="Claude Files API",
    description="Upload DB exports and query them via Claude",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────
# SCHEMAS
# ─────────────────────────────────────────

class UploadResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    created_at: str
    message: str


class FileMetadata(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    created_at: str


class FilesListResponse(BaseModel):
    files: list[FileMetadata]
    total: int


class DeleteResponse(BaseModel):
    file_id: str
    deleted: bool
    message: str


class AskRequest(BaseModel):
    file_id: str
    question: str
    business_id: Optional[str] = None   # optional context hint
    model: Optional[str] = "claude-sonnet-4-20250514"


class AskResponse(BaseModel):
    file_id: str
    question: str
    answer: str
    input_tokens: int
    output_tokens: int


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────

@app.post("/upload", response_model=UploadResponse, tags=["Files"])
async def upload_file(file: UploadFile = File(...)):
    """
    Upload a markdown/text export file to Claude Files API.
    Accepts: .md, .txt files (plain text MIME type)
    """
    # Validate file type
    allowed_types = ["text/plain", "text/markdown", "application/octet-stream"]
    if file.content_type not in allowed_types:
        # Also allow by extension
        if not (file.filename.endswith(".md") or file.filename.endswith(".txt")):
            raise HTTPException(
                status_code=400,
                detail=f"Only .md or .txt files are supported. Got: {file.content_type}",
            )

    content = await file.read()

    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    if len(content) > 500 * 1024 * 1024:  # 500 MB
        raise HTTPException(status_code=400, detail="File exceeds 500 MB limit.")

    client = get_client()

    try:
        response = client.beta.files.upload(
            file=(file.filename, content, "text/plain"),
        )
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {str(e)}")

    return UploadResponse(
        file_id=response.id,
        filename=file.filename,
        size_bytes=len(content),
        created_at=str(response.created_at),
        message="File uploaded successfully to Claude Files API.",
    )


@app.get("/files", response_model=FilesListResponse, tags=["Files"])
def list_files():
    """List all files uploaded to Claude Files API."""
    client = get_client()

    try:
        response = client.beta.files.list()
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {str(e)}")

    files = [
        FileMetadata(
            file_id=f.id,
            filename=f.filename,
            size_bytes=getattr(f, "size", 0),
            created_at=str(f.created_at),
        )
        for f in response.data
    ]

    return FilesListResponse(files=files, total=len(files))


@app.get("/files/{file_id}", response_model=FileMetadata, tags=["Files"])
def get_file_metadata(file_id: str):
    """Get metadata for a specific uploaded file."""
    client = get_client()

    try:
        f = client.beta.files.retrieve_metadata(file_id)
    except anthropic.NotFoundError:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found.")
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {str(e)}")

    return FileMetadata(
        file_id=f.id,
        filename=f.filename,
        size_bytes=getattr(f, "size", 0),
        created_at=str(f.created_at),
    )


@app.delete("/files/{file_id}", response_model=DeleteResponse, tags=["Files"])
def delete_file(file_id: str):
    """Delete a file from Claude Files API."""
    client = get_client()

    try:
        client.beta.files.delete(file_id)
    except anthropic.NotFoundError:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found.")
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {str(e)}")

    return DeleteResponse(
        file_id=file_id,
        deleted=True,
        message="File deleted successfully.",
    )


@app.post("/ask", response_model=AskResponse, tags=["Query"])
def ask_question(body: AskRequest):
    """
    Ask a question against an uploaded file.
    Claude reads the entire document and answers based on its content.
    """
    client = get_client()

    # Build system prompt
    system_prompt = (
        "You are a data analyst assistant. "
        "The user will provide a database export in Markdown format. "
        "Answer questions accurately based only on the data in the document. "
        "If the answer is not in the document, say so clearly. "
        "For numerical questions, compute totals or aggregates where needed. "
        "Be concise and structured in your answers."
    )

    # Build user message
    user_text = body.question
    if body.business_id:
        user_text = (
            f"Context: This export is for business ID {body.business_id}.\n\n"
            f"Question: {body.question}"
        )

    try:
        response = client.beta.messages.create(
            model=body.model,
            max_tokens=2048,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "file",
                                "file_id": body.file_id,
                            },
                            "title": "Database Export",
                            "context": "Exported relational database in Markdown format.",
                        },
                        {
                            "type": "text",
                            "text": user_text,
                        },
                    ],
                }
            ],
            betas=["files-api-2025-04-14"],
        )
    except anthropic.NotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"File '{body.file_id}' not found. Upload it first via POST /upload.",
        )
    except anthropic.BadRequestError as e:
        raise HTTPException(status_code=400, detail=f"Bad request: {str(e)}")
    except anthropic.APIError as e:
        raise HTTPException(status_code=502, detail=f"Claude API error: {str(e)}")

    answer_text = response.content[0].text if response.content else "No response from Claude."

    return AskResponse(
        file_id=body.file_id,
        question=body.question,
        answer=answer_text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────

@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "Claude Files API"}