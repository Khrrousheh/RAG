import pdfplumber
from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter

def process_structured_policy(pdf_path):
    metadata = {}
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf) > 1:
            table_page = pdf.pages[1]
            table = table_page.extract_text()

            if table:
                metadata = {
                    row[0]: row[1] for row in table if row[0]
                }
    metadata["source"] = pdf_path

    loader = PyPDFLoader(pdf_path)
    full_doc = loader.load()
    body_docs = full_doc[2:]

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        add_start_index=True
    )

    chunks = text_splitter.split_documents(body_docs)

    for chunk in chunks:
        chunk.metadata.update(metadata)

    return chunks