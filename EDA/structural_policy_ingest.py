"""Structural RAG ingestion for company policy PDFs.

This pipeline deliberately separates document metadata extraction from body
chunking so policy metadata tables are not shredded into semantic chunks.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
from dateutil import parser as date_parser
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models


LOGGER = logging.getLogger("structural_policy_ingest")

DEFAULT_PDF_DIR = "policies"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "company_policies_structural"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_BATCH_SIZE = 64
EXPECTED_MIN_CHUNKS = 200
EXPECTED_MAX_CHUNKS = 500


@dataclass(frozen=True)
class PolicyMetadata:
    department: str
    version: str
    effective_date: str
    owner: str | None = None
    policy_title: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "Department": self.department,
            "Version": self.version,
            "Effective Date": self.effective_date,
            "effective_date": f"{self.effective_date}T00:00:00Z",
        }
        if self.owner:
            payload["Owner"] = self.owner
        if self.policy_title:
            payload["Policy Title"] = self.policy_title
        return payload


@dataclass(frozen=True)
class IngestedPolicy:
    pdf_path: Path
    page_count: int
    body_pages: int
    chunks: list[Document]
    metadata: PolicyMetadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest structured company policy PDFs into local Qdrant."
    )
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR, help="Directory of policy PDFs.")
    parser.add_argument(
        "--qdrant-url",
        default=DEFAULT_QDRANT_URL,
        help="Qdrant URL, for example http://localhost:6333.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help="Qdrant collection name.",
    )
    parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="Sentence Transformers model used through LangChain embeddings.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Number of chunks to embed and upsert per batch.",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate the Qdrant collection before ingestion.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity.",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def normalize_status(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def parse_policy_date(value: str) -> str:
    """Parse policy dates like 'Dec 17th, 2025' as Qdrant DATETIME strings."""
    normalized = re.sub(r"(\d+)\s*(st|nd|rd|th)", r"\1", value, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+,", ",", normalized)
    parsed = date_parser.parse(normalized, fuzzy=True)
    return parsed.replace(tzinfo=timezone.utc).date().isoformat()


def extract_policy_title(rows: list[list[str]]) -> str | None:
    for row in rows:
        for cell in row:
            if cell.lower().startswith("title:"):
                return cell.split(":", 1)[1].strip() or None
    return None


def extract_change_history_metadata(tables: list[list[list[Any]]]) -> PolicyMetadata | None:
    """Return latest approved Change History metadata from Page 2 tables."""
    for table in tables:
        rows = [[clean_cell(cell) for cell in row if clean_cell(cell)] for row in table]
        rows = [row for row in rows if row]
        policy_title = extract_policy_title(rows)

        for row_index, row in enumerate(rows):
            normalized_headers = [normalize_header(cell) for cell in row]
            required_headers = {"date", "version", "department", "type"}
            if not required_headers.issubset(set(normalized_headers)):
                continue

            header_positions = {
                normalize_header(header): index for index, header in enumerate(row)
            }
            approved_rows: list[tuple[str, dict[str, str]]] = []

            for data_row in rows[row_index + 1 :]:
                if len(data_row) < len(row):
                    continue

                row_by_header = {
                    header: data_row[index]
                    for header, index in header_positions.items()
                    if index < len(data_row)
                }
                status = normalize_status(row_by_header.get("type", ""))
                if status != "approved":
                    continue

                try:
                    effective_date = parse_policy_date(row_by_header["date"])
                except (KeyError, ValueError, OverflowError) as exc:
                    LOGGER.warning("Skipping change-history row with invalid date: %s", exc)
                    continue

                approved_rows.append((effective_date, row_by_header))

            if not approved_rows:
                return None

            effective_date, latest = max(approved_rows, key=lambda item: item[0])
            return PolicyMetadata(
                department=latest.get("department", "").strip(),
                version=latest.get("version", "").strip(),
                effective_date=effective_date,
                owner=latest.get("owner", "").strip() or None,
                policy_title=policy_title,
            )

    return None


def extract_body_documents(pdf_path: Path, pdf: pdfplumber.PDF) -> list[Document]:
    body_documents: list[Document] = []

    # pdfplumber uses zero-based page indexes. Page 3+ means index 2 onward.
    for page_number, page in enumerate(pdf.pages[2:], start=3):
        text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
        text = text.strip()
        if not text:
            LOGGER.warning("No extractable body text in %s page %s", pdf_path.name, page_number)
            continue

        body_documents.append(
            Document(
                page_content=text,
                metadata={
                    "source": str(pdf_path),
                    "file_name": pdf_path.name,
                    "page": page_number,
                },
            )
        )

    return body_documents


def process_policy_pdf(
    pdf_path: Path,
    splitter: RecursiveCharacterTextSplitter,
) -> IngestedPolicy | None:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            if page_count < 3:
                LOGGER.warning("Skipping %s: expected at least 3 pages, found %s", pdf_path, page_count)
                return None

            tables = pdf.pages[1].extract_tables() or []
            if not tables:
                LOGGER.warning("Skipping %s: Page 2 has no extractable tables", pdf_path)
                return None

            policy_metadata = extract_change_history_metadata(tables)
            if not policy_metadata:
                LOGGER.warning("Skipping %s: missing required Page 2 metadata", pdf_path)
                return None

            required_values = policy_metadata.to_payload()
            missing = [key for key in ("Department", "Version", "Effective Date") if not required_values[key]]
            if missing:
                LOGGER.warning("Skipping %s: missing required metadata fields %s", pdf_path, missing)
                return None

            body_documents = extract_body_documents(pdf_path, pdf)

    except Exception:
        LOGGER.exception("Skipping %s: failed to parse PDF", pdf_path)
        return None

    chunks = splitter.split_documents(body_documents)
    inherited_payload = policy_metadata.to_payload()

    for chunk_index, chunk in enumerate(chunks):
        chunk.metadata.update(inherited_payload)
        chunk.metadata["chunk_index"] = chunk_index
        chunk.metadata["policy_name"] = pdf_path.stem

    LOGGER.info(
        "Processed %s: pages=%s body_pages=%s chunks=%s metadata=%s",
        pdf_path.name,
        page_count,
        len(body_documents),
        len(chunks),
        inherited_payload,
    )

    return IngestedPolicy(
        pdf_path=pdf_path,
        page_count=page_count,
        body_pages=len(body_documents),
        chunks=chunks,
        metadata=policy_metadata,
    )


def iter_batches(items: list[Document], batch_size: int) -> Iterable[list[Document]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def stable_point_id(document: Document) -> str:
    metadata = document.metadata
    source = metadata.get("source", "")
    page = metadata.get("page", "")
    chunk_index = metadata.get("chunk_index", "")
    start_index = metadata.get("start_index", "")
    digest = hashlib.sha256(document.page_content.encode("utf-8")).hexdigest()[:16]
    key = f"{source}|{page}|{chunk_index}|{start_index}|{digest}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def ensure_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
    recreate: bool,
) -> None:
    if recreate and client.collection_exists(collection_name):
        LOGGER.info("Deleting existing Qdrant collection %s", collection_name)
        client.delete_collection(collection_name)

    if not client.collection_exists(collection_name):
        LOGGER.info("Creating Qdrant collection %s with vector size %s", collection_name, vector_size)
        client.create_collection(
            collection_name=collection_name,
            vectors_config=models.VectorParams(
                size=vector_size,
                distance=models.Distance.COSINE,
            ),
        )
    else:
        LOGGER.info("Using existing Qdrant collection %s", collection_name)


def ensure_payload_indexes(client: QdrantClient, collection_name: str) -> None:
    index_specs = {
        "Department": models.PayloadSchemaType.KEYWORD,
        "Version": models.PayloadSchemaType.KEYWORD,
        "effective_date": models.PayloadSchemaType.DATETIME,
        "policy_name": models.PayloadSchemaType.KEYWORD,
        "source": models.PayloadSchemaType.KEYWORD,
        "file_name": models.PayloadSchemaType.KEYWORD,
    }

    for field_name, schema in index_specs.items():
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema,
                wait=True,
            )
            LOGGER.info("Created Qdrant payload index for %s", field_name)
        except Exception as exc:
            message = str(exc).lower()
            if "already exists" in message or "same params" in message:
                LOGGER.debug("Qdrant payload index already exists for %s", field_name)
                continue
            raise


def build_points(
    documents: list[Document],
    embeddings: list[list[float]],
) -> list[models.PointStruct]:
    points: list[models.PointStruct] = []
    for document, vector in zip(documents, embeddings, strict=True):
        payload = dict(document.metadata)
        payload["text"] = document.page_content
        points.append(
            models.PointStruct(
                id=stable_point_id(document),
                vector=vector,
                payload=payload,
            )
        )
    return points


def upsert_documents(
    client: QdrantClient,
    collection_name: str,
    documents: list[Document],
    embedding_model: HuggingFaceEmbeddings,
    batch_size: int,
) -> None:
    if not documents:
        LOGGER.warning("No chunks to upsert.")
        return

    for batch_number, batch in enumerate(iter_batches(documents, batch_size), start=1):
        texts = [document.page_content for document in batch]
        embeddings = embedding_model.embed_documents(texts)
        points = build_points(batch, embeddings)
        client.upsert(collection_name=collection_name, points=points, wait=True)
        LOGGER.info("Upserted batch %s: %s chunks", batch_number, len(batch))


def load_embedding_model(model_name: str) -> HuggingFaceEmbeddings:
    LOGGER.info("Loading embedding model %s", model_name)
    return HuggingFaceEmbeddings(
        model_name=model_name,
        encode_kwargs={"normalize_embeddings": True},
    )


def collect_chunks(pdf_dir: Path) -> tuple[list[Document], int, int]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        add_start_index=True,
    )

    pdf_paths = sorted(pdf_dir.glob("*.pdf"))
    all_chunks: list[Document] = []
    ingested_count = 0
    skipped_count = 0

    LOGGER.info("Discovered %s PDF files in %s", len(pdf_paths), pdf_dir)

    for pdf_path in pdf_paths:
        result = process_policy_pdf(pdf_path, splitter)
        if result is None:
            skipped_count += 1
            continue

        ingested_count += 1
        all_chunks.extend(result.chunks)

    return all_chunks, ingested_count, skipped_count


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    pdf_dir = Path(args.pdf_dir)
    if not pdf_dir.exists():
        LOGGER.error("PDF directory does not exist: %s", pdf_dir)
        return 1

    try:
        client = QdrantClient(url=args.qdrant_url)
        client.get_collections()
    except Exception:
        LOGGER.exception("Could not reach Qdrant at %s", args.qdrant_url)
        return 1

    chunks, ingested_count, skipped_count = collect_chunks(pdf_dir)
    total_chunks = len(chunks)

    LOGGER.info(
        "Ingestion summary before vectorization: ingested_pdfs=%s skipped_pdfs=%s total_chunks=%s",
        ingested_count,
        skipped_count,
        total_chunks,
    )

    if total_chunks < EXPECTED_MIN_CHUNKS or total_chunks > EXPECTED_MAX_CHUNKS:
        LOGGER.warning(
            "Total chunk count %s is outside expected range %s-%s",
            total_chunks,
            EXPECTED_MIN_CHUNKS,
            EXPECTED_MAX_CHUNKS,
        )

    if total_chunks == 0:
        LOGGER.error("No chunks were produced; aborting before collection creation.")
        return 1

    embedding_model = load_embedding_model(args.embedding_model)
    sample_vector = embedding_model.embed_query("vector size probe")

    ensure_collection(
        client=client,
        collection_name=args.collection,
        vector_size=len(sample_vector),
        recreate=args.recreate,
    )
    ensure_payload_indexes(client, args.collection)
    upsert_documents(
        client=client,
        collection_name=args.collection,
        documents=chunks,
        embedding_model=embedding_model,
        batch_size=args.batch_size,
    )

    LOGGER.info(
        "Done: pdfs_discovered=%s pdfs_ingested=%s pdfs_skipped=%s total_chunks=%s collection=%s",
        ingested_count + skipped_count,
        ingested_count,
        skipped_count,
        total_chunks,
        args.collection,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
